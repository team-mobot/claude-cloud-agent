#!/bin/bash
set -e

# Claude Cloud Agent Entrypoint
# Runs as an ECS Fargate task to execute Claude Code on a GitHub PR
# Supports both GitHub and JIRA as work sources

echo "=== Claude Cloud Agent Starting ==="
echo "Session ID: $SESSION_ID"
echo "PR Number: $PR_NUMBER"
echo "Branch: $BRANCH"
echo "Repo: $REPO"
echo "Source: ${SOURCE:-github}"
echo "Dev Server Port: ${DEV_SERVER_PORT:-3000}"
if [ -n "$JIRA_ISSUE_KEY" ]; then
    echo "JIRA Issue: $JIRA_ISSUE_KEY"
fi

# Get the container's public IP for UAT access
echo "Discovering public IP..."
PUBLIC_IP=$(curl -s --max-time 5 http://checkip.amazonaws.com 2>/dev/null || curl -s --max-time 5 http://ifconfig.me 2>/dev/null || echo "")
if [ -n "$PUBLIC_IP" ]; then
    export PUBLIC_IP
    export DEV_SERVER_PORT="${DEV_SERVER_PORT:-3000}"
    echo "Public IP: $PUBLIC_IP"
else
    echo "Warning: Could not determine public IP"
fi

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
export SOURCE="${SOURCE:-github}"
export JIRA_ISSUE_KEY="${JIRA_ISSUE_KEY:-}"
export JIRA_SITE="${JIRA_SITE:-}"
export JIRA_SECRET_ARN="${JIRA_SECRET_ARN:-}"

# Get JIRA credentials if this is a JIRA-sourced session
JIRA_EMAIL=""
JIRA_API_TOKEN=""
if [ "$SOURCE" = "jira" ] && [ -n "$JIRA_SECRET_ARN" ]; then
    echo "Fetching JIRA credentials..."
    JIRA_CREDS=$(aws secretsmanager get-secret-value --secret-id "$JIRA_SECRET_ARN" --query 'SecretString' --output text)
    JIRA_EMAIL=$(echo "$JIRA_CREDS" | python3 -c "import sys, json; print(json.load(sys.stdin)['email'])")
    JIRA_API_TOKEN=$(echo "$JIRA_CREDS" | python3 -c "import sys, json; print(json.load(sys.stdin)['api_token'])")
    export JIRA_EMAIL
    export JIRA_API_TOKEN
fi

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

# Post starting comment to GitHub (and JIRA if applicable)
echo "Posting start comment..."
python3 << 'PYEOF'
import requests
import os

# GitHub posting
token = os.environ['INSTALLATION_TOKEN']
repo = os.environ['REPO']
pr_number = os.environ['PR_NUMBER']
is_resume = os.environ.get('RESUME', 'false') == 'true'
source = os.environ.get('SOURCE', 'github')
jira_issue_key = os.environ.get('JIRA_ISSUE_KEY', '')
jira_site = os.environ.get('JIRA_SITE', '')
jira_email = os.environ.get('JIRA_EMAIL', '')
jira_api_token = os.environ.get('JIRA_API_TOKEN', '')
public_ip = os.environ.get('PUBLIC_IP', '')
dev_server_port = os.environ.get('DEV_SERVER_PORT', '3000')

gh_headers = {
    'Authorization': f'token {token}',
    'Accept': 'application/vnd.github+json'
}

# Build UAT access info
uat_info = ""
if public_ip:
    uat_info = f"""

**UAT Access**
- Dev Server URL: http://{public_ip}:{dev_server_port}
- The server will be available once Claude starts it
"""

if is_resume:
    body = f'''**Claude Cloud Agent Resuming** 🔄

Processing your feedback...{uat_info}'''
else:
    body = f'''**Claude Cloud Agent Started** 🤖

Starting Claude Code session...{uat_info}'''

# Post to GitHub PR
requests.post(
    f'https://api.github.com/repos/{repo}/issues/{pr_number}/comments',
    headers=gh_headers,
    json={'body': body}
)

# Post to JIRA if this is a JIRA-sourced session
if source == 'jira' and jira_issue_key and jira_site and jira_email and jira_api_token:
    jira_body = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": body.replace('*', '')}
                    ]
                }
            ]
        }
    }
    try:
        requests.post(
            f'https://{jira_site}/rest/api/3/issue/{jira_issue_key}/comment',
            auth=(jira_email, jira_api_token),
            headers={
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            },
            json=jira_body
        )
    except Exception as e:
        print(f"Error posting to JIRA: {e}")
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
data = resp.json()

# Handle error responses (dict) vs success (list)
if isinstance(data, list):
    comments = data[::-1]  # Reverse to chronological order
    context = "## Recent PR Comments (for context)\n\n"
    for c in comments[-5:]:  # Last 5 comments
        user = c.get('user', {}).get('login', 'unknown')
        body = c.get('body', '')[:500]  # Truncate long comments
        context += f"**{user}:**\n{body}\n\n---\n\n"
    print(context)
else:
    # API error - just print empty context
    print("")
PYEOF
)
fi

echo "$PROMPT" > /tmp/prompt.txt
cat >> /tmp/prompt.txt << PROMPT_EOF

Important instructions:
1. You are working on branch: $BRANCH
2. Repository: $REPO
3. PR Number: $PR_NUMBER
4. Before starting work, check README.md and CLAUDE.md for development setup instructions. You have sudo access - if prerequisites like PostgreSQL, Redis, or other services are required but not installed, install and start them (e.g., sudo apt-get update && sudo apt-get install -y postgresql && sudo service postgresql start). Set up any required databases, run migrations, etc.
5. **UAT SERVER REQUIRED**: After setup, start the dev server in the background on port ${DEV_SERVER_PORT:-3000} and keep it running throughout your session. Use the project's dev server command (npm run dev, python manage.py runserver, etc.) or a simple HTTP server. Run it with nohup or & to keep it running. The UAT URL http://${PUBLIC_IP:-localhost}:${DEV_SERVER_PORT:-3000} has been shared for testing.
6. Make commits as you work - commit early and often
7. Push your changes to the remote branch
8. You can read PR comments with: gh pr view $PR_NUMBER --repo $REPO --comments
9. When done, summarize what you accomplished
PROMPT_EOF

# Add JIRA context if applicable
if [ "$SOURCE" = "jira" ] && [ -n "$JIRA_ISSUE_KEY" ]; then
    echo "" >> /tmp/prompt.txt
    echo "10. This task originated from JIRA issue $JIRA_ISSUE_KEY" >> /tmp/prompt.txt
fi

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

# GitHub config
token = os.environ['INSTALLATION_TOKEN']
repo = os.environ['REPO']
pr_number = os.environ['PR_NUMBER']
output_file = '/tmp/claude_output.txt'

# JIRA config
source = os.environ.get('SOURCE', 'github')
jira_issue_key = os.environ.get('JIRA_ISSUE_KEY', '')
jira_site = os.environ.get('JIRA_SITE', '')
jira_email = os.environ.get('JIRA_EMAIL', '')
jira_api_token = os.environ.get('JIRA_API_TOKEN', '')

gh_headers = {
    'Authorization': f'token {token}',
    'Accept': 'application/vnd.github+json'
}

def post_github_comment(body):
    """Post a comment to the GitHub PR."""
    try:
        requests.post(
            f'https://api.github.com/repos/{repo}/issues/{pr_number}/comments',
            headers=gh_headers,
            json={'body': body}
        )
    except Exception as e:
        print(f"Error posting GitHub comment: {e}")

def post_jira_comment(body):
    """Post a comment to the JIRA issue."""
    if not (source == 'jira' and jira_issue_key and jira_site and jira_email and jira_api_token):
        return

    # Strip markdown formatting for JIRA (basic cleanup)
    plain_body = body.replace('**', '').replace('`', "'")

    jira_body = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": plain_body[:30000]}  # JIRA comment limit
                    ]
                }
            ]
        }
    }
    try:
        requests.post(
            f'https://{jira_site}/rest/api/3/issue/{jira_issue_key}/comment',
            auth=(jira_email, jira_api_token),
            headers={
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            },
            json=jira_body
        )
    except Exception as e:
        print(f"Error posting JIRA comment: {e}")

def post_comment(body, to_jira=False):
    """Post a comment to GitHub (always) and JIRA (only if to_jira=True)."""
    post_github_comment(body)
    if to_jira:
        post_jira_comment(body)

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
                                # Claude's text responses are summaries - send to JIRA
                                post_comment(f"💬 **Claude:**\n\n{text}", to_jira=True)

                        elif item.get('type') == 'tool_use':
                            tool_name = item.get('name', 'unknown')
                            tool_input = item.get('input', {})
                            tool_id = item.get('id', '')
                            pending_tool_uses[tool_id] = (tool_name, tool_input)
                            formatted = format_tool_use(tool_name, tool_input)
                            # Only send AskUserQuestion to JIRA (questions need user response)
                            to_jira = tool_name == 'AskUserQuestion'
                            post_comment(formatted, to_jira=to_jira)

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
                            # Tool results only go to GitHub, not JIRA
                            post_comment(formatted, to_jira=False)

            except json.JSONDecodeError:
                if line.strip() and not line.startswith('{'):
                    # Raw output only goes to GitHub
                    post_comment(f"```\n{line}\n```", to_jira=False)
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

# Post completion comment to GitHub (and JIRA if applicable)
echo "Posting completion comment..."
python3 << 'PYEOF'
import requests
import os

# GitHub config
token = os.environ['INSTALLATION_TOKEN']
repo = os.environ['REPO']
pr_number = os.environ['PR_NUMBER']

# JIRA config
source = os.environ.get('SOURCE', 'github')
jira_issue_key = os.environ.get('JIRA_ISSUE_KEY', '')
jira_site = os.environ.get('JIRA_SITE', '')
jira_email = os.environ.get('JIRA_EMAIL', '')
jira_api_token = os.environ.get('JIRA_API_TOKEN', '')

gh_headers = {
    'Authorization': f'token {token}',
    'Accept': 'application/vnd.github+json'
}

body = '''**Claude Cloud Agent Complete** ✅

I've finished working on this task. Please review the changes and let me know if you need any modifications.'''

# Post to GitHub PR
requests.post(
    f'https://api.github.com/repos/{repo}/issues/{pr_number}/comments',
    headers=gh_headers,
    json={'body': body}
)

# Post to JIRA if this is a JIRA-sourced session
if source == 'jira' and jira_issue_key and jira_site and jira_email and jira_api_token:
    jira_body = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": f"Claude Cloud Agent Complete\n\nI've finished working on this task. Please review the changes in the PR:\nhttps://github.com/{repo}/pull/{pr_number}"}
                    ]
                }
            ]
        }
    }
    try:
        requests.post(
            f'https://{jira_site}/rest/api/3/issue/{jira_issue_key}/comment',
            auth=(jira_email, jira_api_token),
            headers={
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            },
            json=jira_body
        )
    except Exception as e:
        print(f"Error posting to JIRA: {e}")
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

# Keep container alive for UAT testing until PR is closed
echo "=== UAT Server Running ==="
echo "Dev server URL: http://${PUBLIC_IP:-localhost}:${DEV_SERVER_PORT:-3000}"
echo "Container will run until the PR is closed or merged."
echo ""

# Post UAT notification and poll for PR closure
python3 << 'PYEOF'
import requests
import os
import time

token = os.environ['INSTALLATION_TOKEN']
repo = os.environ['REPO']
pr_number = os.environ['PR_NUMBER']
public_ip = os.environ.get('PUBLIC_IP', 'localhost')
dev_port = os.environ.get('DEV_SERVER_PORT', '3000')

gh_headers = {
    'Authorization': f'token {token}',
    'Accept': 'application/vnd.github+json'
}

# Post UAT notification
body = f'''**UAT Server Running** 🧪

The dev server is now available for testing:
- **URL:** http://{public_ip}:{dev_port}

The server will remain running until this PR is closed or merged.'''

requests.post(
    f'https://api.github.com/repos/{repo}/issues/{pr_number}/comments',
    headers=gh_headers,
    json={'body': body}
)

# Poll for PR closure every 60 seconds
print(f"Polling PR #{pr_number} status...")
while True:
    time.sleep(60)
    try:
        resp = requests.get(
            f'https://api.github.com/repos/{repo}/pulls/{pr_number}',
            headers=gh_headers
        )
        if resp.status_code == 200:
            pr_data = resp.json()
            state = pr_data.get('state', 'open')
            merged = pr_data.get('merged', False)

            if state == 'closed' or merged:
                print(f"PR #{pr_number} is {'merged' if merged else 'closed'}. Shutting down.")
                break
        else:
            print(f"Error checking PR status: {resp.status_code}")
    except Exception as e:
        print(f"Error polling PR: {e}")

print("UAT server stopping.")
PYEOF

echo "PR closed. Container stopping."
