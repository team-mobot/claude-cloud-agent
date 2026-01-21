FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ENV UV_SYSTEM_PYTHON=1 \
    PYTHONUNBUFFERED=1 \
    CLAUDE_CODE_USE_BEDROCK=1 \
    ANTHROPIC_MODEL=us.anthropic.claude-opus-4-5-20251101-v1:0 \
    AWS_DEFAULT_REGION=us-east-1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    sudo \
    awscli \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y gh \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user with sudo access
RUN useradd -m -u 1000 -s /bin/bash agent \
    && echo "agent ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Install Python dependencies for the agent scripts
RUN pip install PyJWT requests boto3 PyGithub

# Copy agent code
COPY --chown=agent:agent entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Create workspace directory
RUN mkdir -p /app/workspace && chown -R agent:agent /app

USER agent

CMD ["/app/entrypoint.sh"]
