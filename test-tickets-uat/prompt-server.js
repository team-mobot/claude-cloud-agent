const http = require('http');
const { spawn } = require('child_process');

const PORT = process.env.PROMPT_PORT || 8080;
const WORK_DIR = process.env.WORK_DIR || '/app/repo';

// Queue for prompts
const promptQueue = [];
let isProcessing = false;

// Process next prompt in queue
async function processQueue() {
  if (isProcessing || promptQueue.length === 0) return;

  isProcessing = true;
  const { prompt, resolve, reject } = promptQueue.shift();

  console.log(`[Prompt Server] Processing prompt: ${prompt.substring(0, 100)}...`);

  try {
    const result = await runClaude(prompt);
    resolve(result);
  } catch (error) {
    reject(error);
  }

  isProcessing = false;
  processQueue(); // Process next
}

// Run Claude Code with a prompt
function runClaude(prompt) {
  return new Promise((resolve, reject) => {
    const args = ['-p', prompt];

    console.log(`[Prompt Server] Running: claude ${args.join(' ')}`);
    console.log(`[Prompt Server] CWD: ${WORK_DIR}`);
    console.log(`[Prompt Server] AWS_REGION: ${process.env.AWS_REGION}`);
    console.log(`[Prompt Server] CLAUDE_CODE_USE_BEDROCK will be set to: 1`);

    const proc = spawn('claude', args, {
      cwd: WORK_DIR,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: {
        ...process.env,
        CLAUDE_CODE_USE_BEDROCK: '1',
        // Ensure AWS credentials are passed through
        AWS_ACCESS_KEY_ID: process.env.AWS_ACCESS_KEY_ID,
        AWS_SECRET_ACCESS_KEY: process.env.AWS_SECRET_ACCESS_KEY,
        AWS_SESSION_TOKEN: process.env.AWS_SESSION_TOKEN,
        AWS_REGION: process.env.AWS_REGION || 'us-east-1'
      }
    });

    // Close stdin immediately
    proc.stdin.end();

    let stdout = '';
    let stderr = '';

    proc.stdout.on('data', (data) => {
      stdout += data.toString();
    });

    proc.stderr.on('data', (data) => {
      stderr += data.toString();
    });

    proc.on('close', (code) => {
      if (code === 0) {
        console.log(`[Prompt Server] Claude completed successfully`);
        resolve({ success: true, output: stdout, stderr });
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
      isProcessing
    }));
    return;
  }

  // Queue status
  if (req.method === 'GET' && req.url === '/status') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      queueLength: promptQueue.length,
      isProcessing,
      workDir: WORK_DIR
    }));
    return;
  }

  // Submit prompt
  if (req.method === 'POST' && req.url === '/prompt') {
    let body = '';

    req.on('data', chunk => {
      body += chunk.toString();
    });

    req.on('end', async () => {
      try {
        const { prompt } = JSON.parse(body);

        if (!prompt) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Missing prompt field' }));
          return;
        }

        console.log(`[Prompt Server] Received prompt: ${prompt.substring(0, 100)}...`);

        // Add to queue and wait for result
        const result = await new Promise((resolve, reject) => {
          promptQueue.push({ prompt, resolve, reject });
          processQueue();
        });

        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(result));

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
});
