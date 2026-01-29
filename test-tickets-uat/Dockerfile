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

# Use existing node user (uid 1000) and create workspace
RUN mkdir -p /app && chown -R node:node /app

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
