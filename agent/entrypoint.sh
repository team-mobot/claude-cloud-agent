#!/bin/bash
set -e

echo "=== Claude Cloud Agent Starting ==="
echo "Session ID: ${SESSION_ID}"
echo "Repository: ${REPO_CLONE_URL}"
echo "Branch: ${BRANCH_NAME}"

# Configure git
git config --global user.name "Claude Agent"
git config --global user.email "claude-agent@teammobot.dev"
git config --global init.defaultBranch main

# Clone repository
echo "Cloning repository..."
cd /workspace

# Extract repo name from URL
REPO_NAME=$(basename "${REPO_CLONE_URL}" .git)

# Clone with authentication via GitHub App token
# The token is set up by the main.py script before git operations
if [ -n "${GITHUB_TOKEN}" ]; then
    # Use token authentication
    AUTHENTICATED_URL=$(echo "${REPO_CLONE_URL}" | sed "s|https://|https://x-access-token:${GITHUB_TOKEN}@|")
    git clone "${AUTHENTICATED_URL}" "${REPO_NAME}"
else
    git clone "${REPO_CLONE_URL}" "${REPO_NAME}"
fi

cd "${REPO_NAME}"

# Checkout branch
echo "Checking out branch ${BRANCH_NAME}..."
git checkout "${BRANCH_NAME}" || git checkout -b "${BRANCH_NAME}"

# Configure git for pushing
git config push.default current

echo "Repository ready at /workspace/${REPO_NAME}"

# Start the agent orchestrator
echo "Starting agent orchestrator..."
cd /app
exec python main.py
