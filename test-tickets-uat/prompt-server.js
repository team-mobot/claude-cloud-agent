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

// Chronological comment posting configuration
const FLUSH_INTERVAL_MS = 2000;  // Post every 2 seconds
const MAX_EVENTS_PER_COMMENT = 10;  // Or flush when 10+ events queued

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

// Chronological comment poster - posts events as they happen in order
class ChronologicalCommentPoster {
  constructor(owner, repo, prNumber, token) {
    this.owner = owner;
    this.repo = repo;
    this.prNumber = prNumber;
    this.token = token;
    this.eventQueue = [];  // Chronological list of formatted event strings
    this.pendingToolUses = {};  // Map id -> {name, input} for tools awaiting results
    this.flushTimer = null;
    this.isComplete = false;
  }

  // Add event to queue (maintains chronological order)
  queueEvent(formatted) {
    this.eventQueue.push(formatted);

    // Flush immediately if queue is large
    if (this.eventQueue.length >= MAX_EVENTS_PER_COMMENT) {
      this.flush();
    } else {
      this.scheduleFlush();
    }
  }

  scheduleFlush() {
    if (this.flushTimer) return;
    this.flushTimer = setTimeout(() => {
      this.flushTimer = null;
      this.flush();
    }, FLUSH_INTERVAL_MS);
  }

  async flush() {
    if (this.eventQueue.length === 0) return;

    // Take all queued events
    const events = this.eventQueue.splice(0);

    // Format as single comment with events in order
    const body = events.join('\n\n---\n\n') + '\n\n<!-- claude-agent -->';

    try {
      await postGitHubComment(this.owner, this.repo, this.prNumber, body, this.token);
    } catch (error) {
      console.error(`[GitHub] Failed to post: ${error.message}`);
    }
  }

  addThinking(content) {
    if (!content.trim()) return;
    const truncated = content.length > 2000
      ? content.substring(0, 2000) + '...(truncated)'
      : content;
    this.queueEvent(`**💭 Thinking**\n${truncated}`);
  }

  addToolUse(id, name, input) {
    // Store tool use, wait for result to group them together
    const inputStr = typeof input === 'string' ? input : JSON.stringify(input, null, 2);
    const truncatedInput = inputStr.length > 500
      ? inputStr.substring(0, 500) + '...'
      : inputStr;
    this.pendingToolUses[id] = { name, input: truncatedInput };
  }

  addToolResult(toolUseId, result) {
    const pending = this.pendingToolUses[toolUseId];
    const toolName = pending?.name || 'Tool';
    const toolInput = pending?.input;

    const truncatedResult = result.length > 1000
      ? result.substring(0, 1000) + '...(truncated)'
      : result;

    // Group tool call and result together, with result folded in a <details> tag
    let formatted = `<details>\n<summary><strong>🔧 ${toolName}</strong></summary>\n\n\`\`\`json\n${toolInput}\n\`\`\`\n\n**Result:**\n\`\`\`\n${truncatedResult}\n\`\`\`\n</details>`;

    this.queueEvent(formatted);
    delete this.pendingToolUses[toolUseId];
  }

  addText(content) {
    if (!content.trim()) return;
    this.queueEvent(content);
  }

  async complete() {
    this.isComplete = true;
    if (this.flushTimer) {
      clearTimeout(this.flushTimer);
      this.flushTimer = null;
    }

    // Flush any pending tool uses that never got results
    for (const [id, pending] of Object.entries(this.pendingToolUses)) {
      this.queueEvent(`**🔧 ${pending.name}**\n\`\`\`json\n${pending.input}\n\`\`\`\n\n*Result not captured*`);
    }
    this.pendingToolUses = {};

    // Add completion marker and flush
    this.queueEvent('**✨ Claude agent completed**');
    await this.flush();
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

    // Create chronological comment poster if we have GitHub context
    let updater = null;
    if (github && github.token && github.owner && github.repo && github.prNumber) {
      updater = new ChronologicalCommentPoster(
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
