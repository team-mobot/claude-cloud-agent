# Claude Cloud Agent Container
# IMPORTANT: Must be built with --platform=linux/amd64 for ECS Fargate
FROM --platform=linux/amd64 python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Create non-root user for running Claude Code
# (Claude Code refuses --dangerously-skip-permissions when running as root)
RUN useradd -m -s /bin/bash claude && \
    mkdir -p /app /workspace && \
    chown -R claude:claude /app /workspace

# Create app directory
WORKDIR /app

# Install Python dependencies (as root)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Fix permissions for the app directory
RUN chown -R claude:claude /app

# Copy entrypoint script and set permissions
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && chown claude:claude /entrypoint.sh

# Set working directory for repos
WORKDIR /workspace

# Expose ports
# 8080: Prompt API
# 3000: Dev server (target app)
EXPOSE 8080 3000

# Environment variables (overridden at runtime)
ENV SESSION_ID=""
ENV REPO_CLONE_URL=""
ENV BRANCH_NAME=""
ENV ISSUE_NUMBER=""
ENV PR_NUMBER=""
ENV INITIAL_PROMPT=""
ENV SESSIONS_TABLE=""
ENV GITHUB_APP_ID=""
ENV GITHUB_APP_PRIVATE_KEY=""

# Switch to non-root user
USER claude

ENTRYPOINT ["/entrypoint.sh"]
