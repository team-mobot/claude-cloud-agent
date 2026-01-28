/**
 * Development proxy server for UAT environment.
 *
 * Routes traffic between Vite dev server and Express API:
 * - /api/*, /auth/* -> Express (port 3002)
 * - Everything else -> Vite (port 5173)
 *
 * Supports WebSocket proxying for Vite HMR.
 */

const http = require('http');
const httpProxy = require('http-proxy');

const PROXY_PORT = process.env.PROXY_PORT || 3001;
const VITE_PORT = process.env.VITE_PORT || 5173;
const EXPRESS_PORT = process.env.EXPRESS_PORT || 3002;

// Create proxy instances
const proxy = httpProxy.createProxyServer({});

// Handle proxy errors
proxy.on('error', (err, req, res) => {
  console.error(`[Proxy] Error: ${err.message}`);
  if (res.writeHead) {
    res.writeHead(502, { 'Content-Type': 'text/plain' });
    res.end('Proxy error');
  }
});

// Determine target based on URL path
function getTarget(url) {
  if (url.startsWith('/api/') || url.startsWith('/auth/')) {
    return `http://localhost:${EXPRESS_PORT}`;
  }
  return `http://localhost:${VITE_PORT}`;
}

// Create HTTP server
const server = http.createServer((req, res) => {
  const target = getTarget(req.url);
  proxy.web(req, res, { target });
});

// Handle WebSocket upgrades (for Vite HMR)
server.on('upgrade', (req, socket, head) => {
  const target = getTarget(req.url);
  proxy.ws(req, socket, head, { target });
});

server.listen(PROXY_PORT, () => {
  console.log(`[Dev Proxy] Listening on port ${PROXY_PORT}`);
  console.log(`[Dev Proxy] API routes -> http://localhost:${EXPRESS_PORT}`);
  console.log(`[Dev Proxy] Frontend  -> http://localhost:${VITE_PORT}`);
});
