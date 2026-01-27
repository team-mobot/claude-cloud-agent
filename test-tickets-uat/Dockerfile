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

# Use existing node user (uid 1000) and create workspace
RUN mkdir -p /app && chown -R node:node /app

WORKDIR /app

# Copy entrypoint script
COPY --chown=root:root entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER node

EXPOSE 3001

ENTRYPOINT ["/entrypoint.sh"]
