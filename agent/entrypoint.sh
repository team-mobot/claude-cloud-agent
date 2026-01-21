#!/bin/bash
set -e

# Claude Cloud Agent Entrypoint
# Runs as an ECS Fargate task to execute Claude Code on a GitHub PR

echo "=== Claude Cloud Agent Starting ==="
echo "Session ID: $SESSION_ID"
echo "PR Number: $PR_NUMBER"
echo "Branch: $BRANCH"
echo "Repo: $REPO"

# Validate required environment variables
if [ -z "$SESSION_ID" ] || [ -z "$PR_NUMBER" ] || [ -z "$BRANCH" ] || [ -z "$REPO" ]; then
    echo "ERROR: Missing required environment variables"
    exit 1
fi

# Get GitHub credentials from Secrets Manager
echo "Fetching GitHub credentials..."
GITHUB_CREDS=$(aws secretsmanager get-secret-value --secret-id "$GITHUB_SECRET_ARN" --query 'SecretString' --output text)
APP_ID=$(echo "$GITHUB_CREDS" | python3 -c "import sys, json; print(json.load(sys.stdin)['app_id'])")
PRIVATE_KEY=$(echo "$GITHUB_CREDS" | python3 -c "import sys, json; print(json.load(sys.stdin)['private_key'])")

# Generate installation token
echo "Generating GitHub installation token..."
INSTALLATION_TOKEN=$(python3 << EOF
import jwt
import time
import requests

app_id = "$APP_ID"
private_key = """$PRIVATE_KEY"""

# Create JWT
payload = {
    'iat': int(time.time()),
    'exp': int(time.time()) + 600,
    'iss': app_id
}
token = jwt.encode(payload, private_key, algorithm='RS256')

# Get installation ID
headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}
resp = requests.get('https://api.github.com/app/installations', headers=headers)
installation_id = resp.json()[0]['id']

# Get installation token
resp = requests.post(f'https://api.github.com/app/installations/{installation_id}/access_tokens', headers=headers)
print(resp.json()['token'])
EOF
)

# Export for use in scripts
export INSTALLATION_TOKEN
export REPO
export PR_NUMBER

# Configure git
git config --global user.name "Claude Cloud Agent[bot]"
git config --global user.email "claude-cloud-agent[bot]@users.noreply.github.com"
git config --global credential.helper store
echo "https://x-access-token:${INSTALLATION_TOKEN}@github.com" > ~/.git-credentials

# Configure gh CLI for GitHub API access
export GH_TOKEN="$INSTALLATION_TOKEN"

# Clone the repository
echo "Cloning repository..."
mkdir -p /app/workspace
cd /app/workspace
git clone "https://x-access-token:${INSTALLATION_TOKEN}@github.com/${REPO}.git" repo
cd repo

# Checkout the branch
echo "Checking out branch: $BRANCH"
git checkout "$BRANCH"

# Post starting comment
echo "Posting start comment..."
python3 << PYEOF
import requests
import os

token = os.environ['INSTALLATION_TOKEN']
repo = os.environ['REPO']
pr_number = os.environ['PR_NUMBER']
is_resume = os.environ.get('RESUME', 'false') == 'true'

headers = {
    'Authorization': f'token {token}',
    'Accept': 'application/vnd.github+json'
}

if is_resume:
    body = '''**Claude Cloud Agent Resuming** 🔄

Processing your feedback...
'''
else:
    body = '''**Claude Cloud Agent Started** 🤖

Starting Claude Code session...
'''

requests.post(
    f'https://api.github.com/repos/{repo}/issues/{pr_number}/comments',
    headers=headers,
    json={'body': body}
)
PYEOF

# Create the prompt file
echo "Creating prompt..."
CLAUDE_OUTPUT_FILE="/tmp/claude_output.txt"
touch "$CLAUDE_OUTPUT_FILE"

# Fetch PR context for resumed sessions
PR_CONTEXT=""
if [ "$RESUME" = "true" ]; then
    echo "Fetching PR context for resumed session..."
    PR_CONTEXT=$(python3 << 'PYEOF'
import requests
import os

token = os.environ['INSTALLATION_TOKEN']
repo = os.environ['REPO']
pr_number = os.environ['PR_NUMBER']

headers = {
    'Authorization': f'token {token}',
    'Accept': 'application/vnd.github+json'
}

# Get recent comments (last 10)
resp = requests.get(
    f'https://api.github.com/repos/{repo}/issues/{pr_number}/comments?per_page=10&direction=desc',
    headers=headers
)
comments = resp.json()[::-1]  # Reverse to chronological order

context = "## Recent PR Comments (for context)\n\n"
for c in comments[-5:]:  # Last 5 comments
    user = c['user']['login']
    body = c['body'][:500]  # Truncate long comments
    context += f"**{user}:**\n{body}\n\n---\n\n"

print(context)
PYEOF
)
fi

echo "$PROMPT" > /tmp/prompt.txt
cat >> /tmp/prompt.txt << PROMPT_EOF

Important instructions:
1. You are working on branch: $BRANCH
2. Repository: $REPO
3. PR Number: $PR_NUMBER
4. Make commits as you work - commit early and often
5. Push your changes to the remote branch
6. You can read PR comments with: gh pr view $PR_NUMBER --repo $REPO --comments
7. When done, summarize what you accomplished
PROMPT_EOF

# Add PR context for resumed sessions
if [ -n "$PR_CONTEXT" ]; then
    echo "" >> /tmp/prompt.txt
    echo "$PR_CONTEXT" >> /tmp/prompt.txt
fi

# Start the streaming commenter that parses JSON and posts readable comments
echo "Starting output streamer..."
python3 << 'PYEOF' &
import requests
import os
import json
import time

token = os.environ['INSTALLATION_TOKEN']
repo = os.environ['REPO']
pr_number = os.environ['PR_NUMBER']
output_file = '/tmp/claude_output.txt'

headers = {
    'Authorization': f'token {token}',
    'Accept': 'application/vnd.github+json'
}

def post_comment(body):
    """Post a comment to the PR."""
    try:
        requests.post(
            f'https://api.github.com/repos/{repo}/issues/{pr_number}/comments',
            headers=headers,
            json={'body': body}
        )
    except Exception as e:
        print(f"Error posting comment: {e}")

def format_tool_use(tool_name, tool_input):
    """Format a tool use for display."""
    if tool_name == "Read":
        return f"📖 **Reading file:** `{tool_input.get('file_path', 'unknown')}`"
    elif tool_name == "Write":
        path = tool_input.get('file_path', 'unknown')
        content = tool_input.get('content', '')
        preview = content[:500] + "..." if len(content) > 500 else content
        return f"✏️ **Writing file:** `{path}`\n```\n{preview}\n```"
    elif tool_name == "Edit":
        path = tool_input.get('file_path', 'unknown')
        old = tool_input.get('old_string', '')[:200]
        new = tool_input.get('new_string', '')[:200]
        return f"✏️ **Editing file:** `{path}`\n\nReplacing:\n```\n{old}\n```\nWith:\n```\n{new}\n```"
    elif tool_name == "Bash":
        cmd = tool_input.get('command', '')
        desc = tool_input.get('description', '')
        return f"💻 **Running:** `{cmd}`" + (f"\n_{desc}_" if desc else "")
    elif tool_name == "Glob":
        return f"🔍 **Searching for files:** `{tool_input.get('pattern', '')}`"
    elif tool_name == "Grep":
        return f"🔎 **Searching content:** `{tool_input.get('pattern', '')}`"
    elif tool_name == "Task":
        return f"🤖 **Spawning agent:** {tool_input.get('description', '')}"
    else:
        return f"🔧 **{tool_name}**\n```json\n{json.dumps(tool_input, indent=2)[:500]}\n```"

def format_tool_result(result, is_error=False):
    """Format a tool result for display."""
    if is_error:
        return f"❌ **Error:**\n```\n{str(result)[:1000]}\n```"
    else:
        result_str = str(result)
        if len(result_str) > 1500:
            result_str = result_str[:1500] + "\n...(truncated)"
        return f"✅ **Result:**\n```\n{result_str}\n```"

last_pos = 0
pending_tool_uses = {}

while True:
    time.sleep(2)

    try:
        with open('/tmp/claude_running', 'r') as f:
            running = f.read().strip() == '1'
    except:
        running = True

    try:
        with open(output_file, 'r') as f:
            f.seek(last_pos)
            new_content = f.read()
            last_pos = f.tell()
    except:
        new_content = ""

    if new_content.strip():
        for line in new_content.strip().split('\n'):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                msg_type = data.get('type')

                if msg_type == 'assistant':
                    message = data.get('message', {})
                    content = message.get('content', [])

                    for item in content:
                        if item.get('type') == 'text':
                            text = item.get('text', '').strip()
                            if text:
                                post_comment(f"💬 **Claude:**\n\n{text}")

                        elif item.get('type') == 'tool_use':
                            tool_name = item.get('name', 'unknown')
                            tool_input = item.get('input', {})
                            tool_id = item.get('id', '')
                            pending_tool_uses[tool_id] = (tool_name, tool_input)
                            formatted = format_tool_use(tool_name, tool_input)
                            post_comment(formatted)

                elif msg_type == 'user':
                    message = data.get('message', {})
                    content = message.get('content', [])

                    for item in content:
                        if item.get('type') == 'tool_result':
                            tool_id = item.get('tool_use_id', '')
                            result = item.get('content', '')
                            is_error = item.get('is_error', False)

                            tool_result = data.get('tool_use_result')
                            if isinstance(tool_result, dict):
                                if tool_result.get('type') == 'create':
                                    result = f"Created file: {tool_result.get('filePath', 'unknown')}"
                                elif 'stdout' in tool_result:
                                    result = tool_result.get('stdout', '') or tool_result.get('stderr', '')
                            elif isinstance(tool_result, str):
                                result = tool_result

                            formatted = format_tool_result(result, is_error)
                            post_comment(formatted)

            except json.JSONDecodeError:
                if line.strip() and not line.startswith('{'):
                    post_comment(f"```\n{line}\n```")
            except Exception as e:
                print(f"Error processing line: {e}")

    if not running:
        break

PYEOF
STREAMER_PID=$!

# Mark as running
echo "1" > /tmp/claude_running

# Run Claude Code
echo "Running Claude Code..."
claude --print --verbose --output-format stream-json --dangerously-skip-permissions -p "$(cat /tmp/prompt.txt)" 2>&1 | tee "$CLAUDE_OUTPUT_FILE" || true

# Mark as complete
echo "0" > /tmp/claude_running

# Wait for streamer to finish
sleep 10
kill $STREAMER_PID 2>/dev/null || true

# Push any remaining changes
echo "Pushing final changes..."
git push origin "$BRANCH" || true

# Post completion comment
echo "Posting completion comment..."
python3 << 'PYEOF'
import requests
import os

token = os.environ['INSTALLATION_TOKEN']
repo = os.environ['REPO']
pr_number = os.environ['PR_NUMBER']

headers = {
    'Authorization': f'token {token}',
    'Accept': 'application/vnd.github+json'
}

body = '''**Claude Cloud Agent Complete** ✅

I've finished working on this task. Please review the changes and let me know if you need any modifications.'''

requests.post(
    f'https://api.github.com/repos/{repo}/issues/{pr_number}/comments',
    headers=headers,
    json={'body': body}
)
PYEOF

# Update DynamoDB session status
echo "Updating session status..."
aws dynamodb update-item \
    --table-name "$SESSION_TABLE" \
    --key "{\"session_id\": {\"S\": \"$SESSION_ID\"}}" \
    --update-expression "SET #status = :status" \
    --expression-attribute-names '{"#status": "status"}' \
    --expression-attribute-values '{":status": {"S": "completed"}}'

echo "=== Claude Cloud Agent Complete ==="
