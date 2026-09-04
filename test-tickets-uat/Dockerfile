FROM node:20-alpine

# Install git, build dependencies, and AWS CLI
RUN apk add --no-cache \
    git \
    python3 \
    make \
    g++ \
    curl \
    bash \
    aws-cli

# Install tsx globally for running TypeScript directly
RUN npm install -g tsx

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Use existing node user (uid 1000) and create workspace/native git repo roots
# Runtime `npm ci` uses /opt/uat-npm-cache when a branch lockfile does not
# match the prebuilt dependency layer. The container runs as the node user,
# so that cache directory must exist and be writable before startup.
RUN mkdir -p /app /data/repos /data/customer-docs-media /opt/uat-npm-cache && \
    chown -R node:node /app /data /opt/uat-npm-cache

WORKDIR /app

# Copy entrypoint, prompt server, and dev proxy
COPY --chown=root:root entrypoint.sh /entrypoint.sh
COPY --chown=node:node prompt-server.js /app/prompt-server.js
COPY --chown=node:node dev-proxy.js /app/dev-proxy.js
RUN chmod +x /entrypoint.sh

USER node

# Install http-proxy for dev server routing (as node user in /app)
RUN cd /app && npm init -y && npm install http-proxy

EXPOSE 3001 5173 8080

ENTRYPOINT ["/entrypoint.sh"]
