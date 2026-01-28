const http = require('http');
const https = require('https');
const { spawn, execSync } = require('child_process');

const PORT = process.env.PROMPT_PORT || 8080;
const WORK_DIR = process.env.WORK_DIR || '/app/repo';
const SESSION_ID = process.env.SESSION_ID;
const SESSIONS_TABLE = process.env.SESSIONS_TABLE;
const AWS_REGION = process.env.AWS_REGION || 'us-east-1';

// Claude session ID for --resume (loaded from DynamoDB)
let claudeSessionId = null;

// GitHub comment update batching
const COMMENT_UPDATE_INTERVAL_MS = 3000;

// Update last activity timestamp in DynamoDB
function updateLastActivity() {
  if (!SESSION_ID || !SESSIONS_TABLE) {
    console.log('[Activity] No session info, skipping activity update');
    return;
  }

  const now = Math.floor(Date.now() / 1000);
  const cmd = `aws dynamodb update-item --table-name "${SESSIONS_TABLE}" --key '{"session_id":{"S":"${SESSION_ID}"}}' --update-expression "SET last_activity = :ts" --expression-attribute-values '{":ts":{"N":"${now}"}}' --region ${AWS_REGION}`;

  try {
    execSync(cmd, { stdio: 'pipe' });
    console.log(`[Activity] Updated last_activity for ${SESSION_ID}`);
  } catch (error) {
    console.error(`[Activity] Failed to update: ${error.message}`);
  }
}

// Save Claude session ID to DynamoDB for --resume
function saveClaudeSessionId(newSessionId) {
  if (!SESSION_ID || !SESSIONS_TABLE || !newSessionId) {
    return;
  }

  claudeSessionId = newSessionId;
  const cmd = `aws dynamodb update-item --table-name "${SESSIONS_TABLE}" --key '{"session_id":{"S":"${SESSION_ID}"}}' --update-expression "SET claude_session_id = :sid" --expression-attribute-values '{":sid":{"S":"${newSessionId}"}}' --region ${AWS_REGION}`;

  try {
    execSync(cmd, { stdio: 'pipe' });
    console.log(`[Session] Saved claude_session_id: ${newSessionId}`);
  } catch (error) {
    console.error(`[Session] Failed to save claude_session_id: ${error.message}`);
  }
}

// Get session from DynamoDB
function getSession() {
  if (!SESSION_ID || !SESSIONS_TABLE) {
    return null;
  }

  const cmd = `aws dynamodb get-item --table-name "${SESSIONS_TABLE}" --key '{"session_id":{"S":"${SESSION_ID}"}}' --region ${AWS_REGION}`;

  try {
    const result = execSync(cmd, { stdio: 'pipe' }).toString();
    const parsed = JSON.parse(result);
    if (parsed.Item) {
      return {
        session_id: parsed.Item.session_id?.S,
        initial_prompt: parsed.Item.initial_prompt?.S,
        claude_session_id: parsed.Item.claude_session_id?.S,
        repo_full_name: parsed.Item.repo_full_name?.S,
        pr_number: parseInt(parsed.Item.pr_number?.N || '0', 10)
      };
    }
  } catch (error) {
    console.error(`[Session] Failed to get session: ${error.message}`);
  }
  return null;
}

// Clear initial_prompt after processing
function clearInitialPrompt() {
  if (!SESSION_ID || !SESSIONS_TABLE) {
    return;
  }

  const cmd = `aws dynamodb update-item --table-name "${SESSIONS_TABLE}" --key '{"session_id":{"S":"${SESSION_ID}"}}' --update-expression "REMOVE initial_prompt" --region ${AWS_REGION}`;

  try {
    execSync(cmd, { stdio: 'pipe' });
    console.log(`[Session] Cleared initial_prompt`);
  } catch (error) {
    console.error(`[Session] Failed to clear initial_prompt: ${error.message}`);
  }
}

// Post a comment to a GitHub PR
function postGitHubComment(owner, repo, prNumber, body, token) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify({ body });

    const options = {
      hostname: 'api.github.com',
      port: 443,
      path: `/repos/${owner}/${repo}/issues/${prNumber}/comments`,
      method: 'POST',
      headers: {
        'Authorization': `token ${token}`,
        'User-Agent': 'claude-agent',
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(data),
        'Accept': 'application/vnd.github.v3+json'
      }
    };

    console.log(`[GitHub] Posting comment to ${owner}/${repo}#${prNumber}`);

    const req = https.request(options, (res) => {
      let responseBody = '';
      res.on('data', chunk => responseBody += chunk);
      res.on('end', () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          console.log(`[GitHub] Comment posted successfully`);
          resolve(JSON.parse(responseBody));
        } else {
          console.error(`[GitHub] Failed to post comment: ${res.statusCode} ${responseBody}`);
          reject(new Error(`GitHub API error: ${res.statusCode} ${responseBody}`));
        }
      });
    });

    req.on('error', (error) => {
      console.error(`[GitHub] Request error: ${error.message}`);
      reject(error);
    });

    req.write(data);
    req.end();
  });
}

// Update an existing GitHub comment
function updateGitHubComment(owner, repo, commentId, body, token) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify({ body });

    const options = {
      hostname: 'api.github.com',
      port: 443,
      path: `/repos/${owner}/${repo}/issues/comments/${commentId}`,
      method: 'PATCH',
      headers: {
        'Authorization': `token ${token}`,
        'User-Agent': 'claude-agent',
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(data),
        'Accept': 'application/vnd.github.v3+json'
      }
    };

    const req = https.request(options, (res) => {
      let responseBody = '';
      res.on('data', chunk => responseBody += chunk);
      res.on('end', () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(JSON.parse(responseBody));
        } else {
          console.error(`[GitHub] Failed to update comment: ${res.statusCode} ${responseBody}`);
          reject(new Error(`GitHub API error: ${res.statusCode} ${responseBody}`));
        }
      });
    });

    req.on('error', (error) => {
      console.error(`[GitHub] Update request error: ${error.message}`);
      reject(error);
    });

    req.write(data);
    req.end();
  });
}

// GitHub comment updater with debouncing
class GitHubCommentUpdater {
  constructor(owner, repo, prNumber, token) {
    this.owner = owner;
    this.repo = repo;
    this.prNumber = prNumber;
    this.token = token;
    this.commentId = null;
    this.pendingUpdate = null;
    this.updateTimer = null;
    this.thinkingContent = '';
    this.toolUses = [];          // Array of {id, name, input, result}
    this.toolUseById = {};       // Map id -> tool object for matching results
    this.textBlocks = [];        // Collect text blocks, only show final summary
    this.isComplete = false;
  }

  async ensureComment() {
    if (this.commentId) return;

    const body = this.formatComment();
    const result = await postGitHubComment(
      this.owner, this.repo, this.prNumber, body, this.token
    );
    this.commentId = result.id;
    console.log(`[GitHub] Created comment ${this.commentId}`);
  }

  formatComment() {
    let parts = ['## Claude Agent\n'];

    // Add thinking section if present (collapsed)
    if (this.thinkingContent) {
      parts.push('<details>');
      parts.push('<summary>Thinking...</summary>\n');
      // Truncate thinking content
      const truncatedThinking = this.thinkingContent.length > 3000
        ? this.thinkingContent.substring(0, 3000) + '\n...(truncated)'
        : this.thinkingContent;
      parts.push(truncatedThinking);
      parts.push('\n</details>\n');
    }

    // Show tool usage summary (collapsed)
    if (this.toolUses.length > 0) {
      parts.push('<details>');
      parts.push(`<summary>Tool Activity (${this.toolUses.length} operations)</summary>\n`);

      for (const tool of this.toolUses) {
        parts.push(`**${tool.name}**`);
        if (tool.input) {
          const inputStr = typeof tool.input === 'string'
            ? tool.input
            : JSON.stringify(tool.input, null, 2);
          // Truncate long inputs
          const truncatedInput = inputStr.length > 300
            ? inputStr.substring(0, 300) + '...'
            : inputStr;
          parts.push('```json');
          parts.push(truncatedInput);
          parts.push('```');
        }
        if (tool.result) {
          // Truncate long results
          const truncatedResult = tool.result.length > 500
            ? tool.result.substring(0, 500) + '...(truncated)'
            : tool.result;
          parts.push('<details>');
          parts.push('<summary>Result</summary>\n');
          parts.push('```');
          parts.push(truncatedResult);
          parts.push('```');
          parts.push('</details>');
        }
        parts.push('');
      }
      parts.push('</details>\n');
    }

    // Show only the final text block (the summary), not intermediate messages
    const finalText = this.getFinalSummary();
    if (finalText) {
      parts.push(finalText);
    }

    // Add status indicator
    if (!this.isComplete) {
      parts.push('\n---\n*Processing...*');
    }

    parts.push('\n<!-- claude-agent -->');
    return parts.join('\n');
  }

  // Extract final summary from text blocks
  // Claude typically ends with a summary after "Here's a summary" or similar
  getFinalSummary() {
    if (this.textBlocks.length === 0) return '';

    // Combine all text and look for the final summary section
    const fullText = this.textBlocks.join('');

    // Try to find a summary section (usually starts with ## or "Here's" or "I have")
    const summaryPatterns = [
      /## Implementation Summary[\s\S]*/,
      /## Summary[\s\S]*/,
      /Here's a summary[\s\S]*/i,
      /I have successfully[\s\S]*/i,
      /The implementation[\s\S]*/i
    ];

    for (const pattern of summaryPatterns) {
      const match = fullText.match(pattern);
      if (match && match[0].length > 100) {
        return match[0];
      }
    }

    // Fallback: if text is short enough, show all; otherwise show last part
    if (fullText.length < 1000) {
      return fullText;
    }

    // Show last 1500 chars as likely contains summary
    return '...\n\n' + fullText.slice(-1500);
  }

  scheduleUpdate() {
    if (this.updateTimer) return;

    this.updateTimer = setTimeout(async () => {
      this.updateTimer = null;
      await this.flushUpdate();
    }, COMMENT_UPDATE_INTERVAL_MS);
  }

  async flushUpdate() {
    if (!this.commentId) {
      await this.ensureComment();
      return;
    }

    try {
      const body = this.formatComment();
      await updateGitHubComment(
        this.owner, this.repo, this.commentId, body, this.token
      );
    } catch (error) {
      console.error(`[GitHub] Failed to update comment: ${error.message}`);
    }
  }

  addThinking(content) {
    this.thinkingContent += content;
    this.scheduleUpdate();
  }

  addToolUse(id, name, input) {
    const tool = { id, name, input, result: null };
    this.toolUses.push(tool);
    if (id) {
      this.toolUseById[id] = tool;
    }
    this.scheduleUpdate();
  }

  addToolResult(toolUseId, result) {
    // Match result to tool by ID
    const tool = this.toolUseById[toolUseId];
    if (tool) {
      tool.result = result;
    } else if (this.toolUses.length > 0) {
      // Fallback: assign to most recent tool without result
      for (let i = this.toolUses.length - 1; i >= 0; i--) {
        if (!this.toolUses[i].result) {
          this.toolUses[i].result = result;
          break;
        }
      }
    }
    this.scheduleUpdate();
  }

  addText(content) {
    this.textBlocks.push(content);
    this.scheduleUpdate();
  }

  async complete() {
    this.isComplete = true;
    if (this.updateTimer) {
      clearTimeout(this.updateTimer);
      this.updateTimer = null;
    }
    await this.flushUpdate();
  }
}

// Queue for prompts
const promptQueue = [];
let isProcessing = false;

// Process next prompt in queue
async function processQueue() {
  if (isProcessing || promptQueue.length === 0) return;

  isProcessing = true;
  const { prompt, github } = promptQueue.shift();

  console.log(`[Prompt Server] Processing prompt: ${prompt.substring(0, 100)}...`);

  try {
    await runClaudeStreaming(prompt, github);
  } catch (error) {
    console.error(`[Prompt Server] Claude error: ${error.message}`);
    // Post error to GitHub if context provided
    if (github && github.token && github.owner && github.repo && github.prNumber) {
      try {
        const errorComment = `## Claude Agent Error\n\n\`\`\`\n${error.message}\n\`\`\`\n\n<!-- claude-agent -->`;
        await postGitHubComment(github.owner, github.repo, github.prNumber, errorComment, github.token);
      } catch (ghError) {
        console.error(`[Prompt Server] Failed to post error to GitHub: ${ghError.message}`);
      }
    }
  }

  isProcessing = false;
  processQueue(); // Process next
}

// Run Claude Code with streaming output
function runClaudeStreaming(prompt, github) {
  return new Promise((resolve, reject) => {
    const args = [
      '-p', prompt,
      '--output-format', 'stream-json',
      '--verbose',
      '--allowedTools', 'Read,Edit,Bash,Glob,Grep,Write'
    ];

    // Add --resume if we have a previous session
    if (claudeSessionId) {
      args.push('--resume', claudeSessionId);
      console.log(`[Prompt Server] Resuming session: ${claudeSessionId}`);
    }

    console.log(`[Prompt Server] Running: claude ${args.join(' ')}`);
    console.log(`[Prompt Server] CWD: ${WORK_DIR}`);

    const proc = spawn('claude', args, {
      cwd: WORK_DIR,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: {
        ...process.env,
        CLAUDE_CODE_USE_BEDROCK: '1',
        AWS_REGION: AWS_REGION
      }
    });

    // Close stdin immediately
    proc.stdin.end();

    // Create GitHub comment updater if we have context
    let updater = null;
    if (github && github.token && github.owner && github.repo && github.prNumber) {
      updater = new GitHubCommentUpdater(
        github.owner, github.repo, github.prNumber, github.token
      );
    }

    let buffer = '';
    let stderr = '';
    let newSessionId = null;

    proc.stdout.on('data', async (data) => {
      buffer += data.toString();

      // Process complete lines (NDJSON)
      const lines = buffer.split('\n');
      buffer = lines.pop(); // Keep incomplete line in buffer

      for (const line of lines) {
        if (!line.trim()) continue;

        try {
          const event = JSON.parse(line);

          // Extract session_id from result message
          if (event.type === 'result' && event.session_id) {
            newSessionId = event.session_id;
            console.log(`[Prompt Server] Got session_id: ${newSessionId}`);
          }

          // Process assistant messages (tool_use, text, thinking)
          if (event.type === 'assistant' && event.message?.content) {
            for (const block of event.message.content) {
              if (block.type === 'thinking' && updater) {
                updater.addThinking(block.thinking || '');
              } else if (block.type === 'tool_use' && updater) {
                updater.addToolUse(block.id, block.name, block.input);
              } else if (block.type === 'text' && updater) {
                updater.addText(block.text || '');
              }
            }
          }

          // Process user messages (tool_result)
          if (event.type === 'user' && event.message?.content) {
            for (const block of event.message.content) {
              if (block.type === 'tool_result' && updater) {
                const resultText = typeof block.content === 'string'
                  ? block.content
                  : (Array.isArray(block.content)
                    ? block.content.map(c => c.text || JSON.stringify(c)).join('\n')
                    : JSON.stringify(block.content));
                updater.addToolResult(block.tool_use_id, resultText);
              }
            }
          }

          // Handle content_block_delta for streaming partial content
          if (event.type === 'content_block_delta') {
            const delta = event.delta;
            if (delta?.type === 'thinking_delta' && updater) {
              updater.addThinking(delta.thinking || '');
            } else if (delta?.type === 'text_delta' && updater) {
              updater.addText(delta.text || '');
            }
          }

        } catch (parseError) {
          console.error(`[Prompt Server] Failed to parse JSON: ${parseError.message}`);
        }
      }
    });

    proc.stderr.on('data', (data) => {
      stderr += data.toString();
    });

    proc.on('close', async (code) => {
      // Process any remaining buffer
      if (buffer.trim()) {
        try {
          const event = JSON.parse(buffer);
          if (event.type === 'result' && event.session_id) {
            newSessionId = event.session_id;
          }
        } catch (e) {
          // Ignore parse errors on final buffer
        }
      }

      // Save new session ID for future --resume
      if (newSessionId) {
        saveClaudeSessionId(newSessionId);
      }

      // Complete the GitHub comment
      if (updater) {
        await updater.complete();
      }

      if (code === 0) {
        console.log(`[Prompt Server] Claude completed successfully`);
        resolve({ success: true });
      } else {
        console.error(`[Prompt Server] Claude failed with code ${code}`);
        reject(new Error(`Claude exited with code ${code}: ${stderr}`));
      }
    });

    proc.on('error', (error) => {
      console.error(`[Prompt Server] Failed to start Claude: ${error.message}`);
      reject(error);
    });
  });
}

// Check for initial_prompt on startup
async function checkInitialPrompt() {
  console.log('[Prompt Server] Checking for initial_prompt...');

  const session = getSession();
  if (!session) {
    console.log('[Prompt Server] No session found');
    return;
  }

  // Load existing claude_session_id for --resume
  if (session.claude_session_id) {
    claudeSessionId = session.claude_session_id;
    console.log(`[Prompt Server] Loaded existing claude_session_id: ${claudeSessionId}`);
  }

  // Check for initial_prompt
  if (session.initial_prompt) {
    console.log('[Prompt Server] Found initial_prompt, starting Claude...');

    // Build GitHub context from session
    let github = null;
    if (session.repo_full_name && session.pr_number) {
      const [owner, repo] = session.repo_full_name.split('/');
      // Get token from environment (set by entrypoint)
      const token = process.env.GITHUB_TOKEN;
      if (token) {
        github = { owner, repo, prNumber: session.pr_number, token };
      }
    }

    // Clear initial_prompt so we don't process it again
    clearInitialPrompt();

    // Queue the initial prompt
    promptQueue.push({ prompt: session.initial_prompt, github });
    processQueue();
  } else {
    console.log('[Prompt Server] No initial_prompt found');
  }
}

// HTTP server
const server = http.createServer(async (req, res) => {
  // CORS headers
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') {
    res.writeHead(200);
    res.end();
    return;
  }

  // Health check
  if (req.method === 'GET' && req.url === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      status: 'ok',
      queueLength: promptQueue.length,
      isProcessing,
      claudeSessionId
    }));
    return;
  }

  // Queue status
  if (req.method === 'GET' && req.url === '/status') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      queueLength: promptQueue.length,
      isProcessing,
      workDir: WORK_DIR,
      claudeSessionId
    }));
    return;
  }

  // Submit prompt
  // Body: { prompt: string, github?: { owner: string, repo: string, prNumber: number, token: string } }
  if (req.method === 'POST' && req.url === '/prompt') {
    let body = '';

    req.on('data', chunk => {
      body += chunk.toString();
    });

    req.on('end', async () => {
      try {
        const { prompt, github } = JSON.parse(body);

        if (!prompt) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Missing prompt field' }));
          return;
        }

        console.log(`[Prompt Server] Received prompt: ${prompt.substring(0, 100)}...`);
        if (github) {
          console.log(`[Prompt Server] GitHub context: ${github.owner}/${github.repo}#${github.prNumber}`);
        }

        // Update activity timestamp
        updateLastActivity();

        // Add to queue (fire and forget - response posted to GitHub async)
        promptQueue.push({ prompt, github });
        processQueue();

        // Return immediately - processing happens async
        res.writeHead(202, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          message: 'Prompt accepted',
          queueLength: promptQueue.length,
          isProcessing,
          claudeSessionId
        }));

      } catch (error) {
        console.error(`[Prompt Server] Error: ${error.message}`);
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: error.message }));
      }
    });
    return;
  }

  // 404 for everything else
  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ error: 'Not found' }));
});

server.listen(PORT, () => {
  console.log(`[Prompt Server] Listening on port ${PORT}`);
  console.log(`[Prompt Server] Work directory: ${WORK_DIR}`);
  console.log(`[Prompt Server] Session ID: ${SESSION_ID}`);

  // Check for initial_prompt after a brief delay (allow container to fully start)
  setTimeout(checkInitialPrompt, 2000);
});
