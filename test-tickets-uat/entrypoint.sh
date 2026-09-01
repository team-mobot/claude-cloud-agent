#!/bin/bash
set -e

echo "=== test_tickets UAT Container Starting ==="
echo "  Branch: ${BRANCH:-main}"
echo "  Session: ${SESSION_ID:-unknown}"

resolve_github_token() {
    if [ -n "$GITHUB_TOKEN" ]; then
        return
    fi

    if [ -z "$GITHUB_APP_ID" ] || [ -z "$GITHUB_APP_PRIVATE_KEY" ]; then
        echo "  Warning: no GitHub token or GitHub App credentials configured"
        return
    fi

    echo "  Minting GitHub App installation token..."
    GITHUB_TOKEN=$(node <<'NODE'
const crypto = require('crypto');

function base64url(value) {
  return Buffer.from(value).toString('base64url');
}

async function main() {
  const appId = process.env.GITHUB_APP_ID;
  const privateKey = process.env.GITHUB_APP_PRIVATE_KEY.replace(/\\n/g, '\n');
  const now = Math.floor(Date.now() / 1000);
  const header = base64url(JSON.stringify({ alg: 'RS256', typ: 'JWT' }));
  const payload = base64url(JSON.stringify({
    iat: now - 60,
    exp: now + 600,
    iss: appId,
  }));
  const signingInput = `${header}.${payload}`;
  const signer = crypto.createSign('RSA-SHA256');
  signer.update(signingInput);
  signer.end();
  const jwt = `${signingInput}.${signer.sign(privateKey).toString('base64url')}`;

  async function github(path, options = {}) {
    const response = await fetch(`https://api.github.com${path}`, {
      method: options.method || 'GET',
      headers: {
        Authorization: `Bearer ${jwt}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
      },
    });
    if (!response.ok) {
      throw new Error(`${response.status} ${await response.text()}`);
    }
    return response.json();
  }

  let installationId = process.env.GITHUB_APP_INSTALLATION_ID || process.env.GITHUB_INSTALLATION_ID;
  if (!installationId) {
    const repo = process.env.REPO || 'team-mobot/test_tickets';
    const installation = await github(`/repos/${repo}/installation`);
    installationId = installation.id;
  }

  const token = await github(`/app/installations/${installationId}/access_tokens`, { method: 'POST' });
  if (!token.token) {
    throw new Error('GitHub did not return an installation token');
  }
  process.stdout.write(token.token);
}

main().catch((error) => {
  console.error(`Failed to mint GitHub App installation token: ${error.message}`);
  process.exit(1);
});
NODE
)
    export GITHUB_TOKEN
}

resolve_github_token

authenticated_github_url() {
    local repo="$1"
    if [ -n "$GITHUB_TOKEN" ]; then
        echo "https://x-access-token:${GITHUB_TOKEN}@github.com/${repo}.git"
    else
        echo "https://github.com/${repo}.git"
    fi
}

disable_git_push() {
    local repo_path="$1"
    mkdir -p "${repo_path}/.git/hooks"
    cat > "${repo_path}/.git/hooks/pre-push" <<'PRE_PUSH_EOF'
#!/bin/sh
echo "Pushes are disabled in the UAT native-git clone." >&2
exit 1
PRE_PUSH_EOF
    chmod +x "${repo_path}/.git/hooks/pre-push"
    git -C "$repo_path" remote set-url --push origin DISABLED
}

clone_native_git_repo() {
    local repo="$1"
    local destination="$2"
    local branch="${3:-main}"
    local public_url="https://github.com/${repo}.git"

    mkdir -p "$(dirname "$destination")"
    if [ -d "${destination}/.git" ]; then
        echo "  Native git repo already exists: ${destination}"
    else
        if [ -e "$destination" ]; then
            rm -rf "$destination"
        fi
        echo "  Cloning ${repo} -> ${destination}"
        git clone --branch "$branch" --single-branch "$(authenticated_github_url "$repo")" "$destination"
    fi

    git -C "$destination" config user.email "claude-dev@teammobot.dev"
    git -C "$destination" config user.name "Claude UAT Agent"
    git -C "$destination" remote set-url origin "$public_url"
    disable_git_push "$destination"
    git config --global --add safe.directory "$destination"
}

clear_github_token_git_rewrites() {
    if [ -n "$GITHUB_AUTH_URL" ]; then
        git config --global --unset-all "url.${GITHUB_AUTH_URL}.insteadOf" 2>/dev/null || true
    fi
    unset GIT_CONFIG_COUNT
    unset GIT_CONFIG_KEY_0
    unset GIT_CONFIG_VALUE_0
    unset GIT_CONFIG_KEY_1
    unset GIT_CONFIG_VALUE_1
    unset GIT_CONFIG_KEY_2
    unset GIT_CONFIG_VALUE_2
    unset GIT_CONFIG_KEY_3
    unset GIT_CONFIG_VALUE_3
}

# Clone repository
echo "[1/9] Cloning repository..."
if [ -n "$GITHUB_TOKEN" ]; then
    REPO_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO:-team-mobot/test_tickets}.git"
else
    REPO_URL="https://github.com/${REPO:-team-mobot/test_tickets}.git"
fi
git clone --depth 1 --branch "${BRANCH:-main}" "$REPO_URL" /app/repo 2>&1 || {
    echo "Failed to clone branch ${BRANCH}, trying main..."
    git clone --depth 1 --branch main "$REPO_URL" /app/repo
}
cd /app/repo

# Configure git identity for commits
git config user.email "claude-dev@teammobot.dev"
git config user.name "Claude Dev Agent"

if [ -n "$GITHUB_TOKEN" ]; then
    echo "  Configuring GitHub token for private Git dependencies..."
    GITHUB_AUTH_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/"
    git config --global url."${GITHUB_AUTH_URL}".insteadOf "ssh://git@github.com/"
    git config --global --add url."${GITHUB_AUTH_URL}".insteadOf "ssh://git@github.com"
    git config --global --add url."${GITHUB_AUTH_URL}".insteadOf "git@github.com:"
    git config --global --add url."${GITHUB_AUTH_URL}".insteadOf "https://github.com/"
    export GIT_CONFIG_COUNT=4
    export GIT_CONFIG_KEY_0="url.${GITHUB_AUTH_URL}.insteadOf"
    export GIT_CONFIG_VALUE_0="ssh://git@github.com/"
    export GIT_CONFIG_KEY_1="url.${GITHUB_AUTH_URL}.insteadOf"
    export GIT_CONFIG_VALUE_1="ssh://git@github.com"
    export GIT_CONFIG_KEY_2="url.${GITHUB_AUTH_URL}.insteadOf"
    export GIT_CONFIG_VALUE_2="git@github.com:"
    export GIT_CONFIG_KEY_3="url.${GITHUB_AUTH_URL}.insteadOf"
    export GIT_CONFIG_VALUE_3="https://github.com/"
fi

# Create a vite wrapper script that patches config before each run
echo "  Creating Vite wrapper script..."
cat > /app/vite-wrapper.js << 'VITE_WRAPPER'
#!/usr/bin/env node
const fs = require('fs');
const { spawn } = require('child_process');

// Patch Vite config to allow UAT subdomain hosts
const configs = ['vite.config.ts', 'vite.config.js', 'vite.config.mts', 'vite.config.mjs'];
const config = configs.find(f => fs.existsSync(f));
if (config) {
    let content = fs.readFileSync(config, 'utf8');
    if (!content.includes('allowedHosts')) {
        if (content.includes('server:') || content.includes('server :')) {
            content = content.replace(/(server\s*:\s*\{)/, '$1 allowedHosts: true,');
        } else {
            content = content.replace(/(\}\s*\)\s*;?\s*)$/, ', server: { allowedHosts: true } $1');
        }
        fs.writeFileSync(config, content);
        console.log('[vite-wrapper] Patched ' + config + ' with allowedHosts: true');
    }
}

// Run vite with all passed arguments
const args = process.argv.slice(2);
const vite = spawn('npx', ['vite', ...args], { stdio: 'inherit' });
vite.on('close', (code) => process.exit(code));
VITE_WRAPPER

chmod +x /app/vite-wrapper.js

# Modify package.json to use our wrapper instead of vite directly
echo "  Updating package.json to use Vite wrapper..."
node -e "
const fs = require('fs');
const pkg = JSON.parse(fs.readFileSync('package.json', 'utf8'));
if (pkg.scripts && pkg.scripts.dev === 'vite') {
    pkg.scripts.dev = 'node /app/vite-wrapper.js';
    fs.writeFileSync('package.json', JSON.stringify(pkg, null, 2));
    console.log('  Updated package.json dev script to use vite-wrapper');
} else {
    console.log('  package.json dev script is not simple vite command, skipping');
}
"

echo "[2/9] Installing dependencies..."
npm ci --include=dev 2>&1

# Install server dependencies if separate package
if [ -f "server/package.json" ]; then
    echo "  Installing server dependencies..."
    cd server
    npm ci --include=dev 2>&1
    cd ..
fi

echo "[3/9] Preparing native Git repositories..."
export USE_NATIVE_GIT="${USE_NATIVE_GIT:-true}"
export TEST_PLANS_GIT_REPO_PATH="${TEST_PLANS_GIT_REPO_PATH:-${GIT_REPO_PATH:-/data/repos/ai_driver_test_plans}}"
export CUSTOMER_DOCS_GIT_REPO_PATH="${CUSTOMER_DOCS_GIT_REPO_PATH:-/data/repos/customer-docs}"
export TEST_PLANS_GIT_REMOTE_SYNC_ENABLED="${TEST_PLANS_GIT_REMOTE_SYNC_ENABLED:-false}"
export CUSTOMER_DOCS_GIT_REMOTE_SYNC_ENABLED="${CUSTOMER_DOCS_GIT_REMOTE_SYNC_ENABLED:-false}"
export GIT_REMOTE_SYNC_ENABLED="${GIT_REMOTE_SYNC_ENABLED:-false}"
export CUSTOMER_DOCS_MEDIA_LOCAL_ROOT_PATH="${CUSTOMER_DOCS_MEDIA_LOCAL_ROOT_PATH:-/data/customer-docs-media}"
clone_native_git_repo "${TEST_PLANS_GIT_REPO:-team-mobot/ai_driver_test_plans}" "$TEST_PLANS_GIT_REPO_PATH" "${TEST_PLANS_GIT_REMOTE_BRANCH:-main}"
clone_native_git_repo "${CUSTOMER_DOCS_GIT_REPO:-team-mobot/customer-docs}" "$CUSTOMER_DOCS_GIT_REPO_PATH" "${CUSTOMER_DOCS_GIT_REMOTE_BRANCH:-main}"
clear_github_token_git_rewrites

# Set Vite env vars
export VITE_GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID}"
export VITE_API_URL=""

echo "[4/9] Starting Vite dev server..."
# Start Vite using our wrapper (which patches allowedHosts before each run)
# This handles both initial start and any restarts by Claude agent
npm run dev -- --host 0.0.0.0 --port 5173 &
VITE_PID=$!
echo "  Vite dev server started (PID: $VITE_PID) on port 5173"

# Wait for Vite to be ready
echo "  Waiting for Vite to start..."
for i in $(seq 1 30); do
    if curl -s http://localhost:5173 > /dev/null 2>&1; then
        echo "  Vite is ready"
        break
    fi
    sleep 1
done

# Register with DynamoDB and ALB target group
echo "[5/9] Registering container..."
if [ -n "$SESSIONS_TABLE" ] && [ -n "$SESSION_ID" ]; then
    # Get container's IPs from ECS metadata
    TASK_METADATA=$(curl -s "${ECS_CONTAINER_METADATA_URI_V4}/task" 2>/dev/null || echo "{}")
    CONTAINER_METADATA=$(curl -s "${ECS_CONTAINER_METADATA_URI_V4}" 2>/dev/null || echo "{}")

    # Private IP for ALB target group registration
    PRIVATE_IP=$(echo "$TASK_METADATA" | grep -o '"PrivateIPv4Address":"[^"]*"' | head -1 | cut -d'"' -f4)
    if [ -z "$PRIVATE_IP" ]; then
        PRIVATE_IP=$(hostname -i 2>/dev/null || echo "localhost")
    fi

    # Public IP for webhook routing (Lambda needs to reach container over internet)
    # Try container metadata Networks array for ENI, fall back to task ARN and ECS API
    ENI_ID=$(echo "$CONTAINER_METADATA" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for network in data.get('Networks', []):
        # For awsvpc mode, look for NetworkInterfaceId
        eni = network.get('NetworkInterfaceId', '')
        if eni:
            print(eni)
            sys.exit(0)
except Exception as e:
    pass
" 2>/dev/null)

    # Fallback: use ECS API to get ENI from task
    if [ -z "$ENI_ID" ]; then
        TASK_ARN=$(echo "$TASK_METADATA" | python3 -c "import sys,json; print(json.load(sys.stdin).get('TaskARN',''))" 2>/dev/null)
        CLUSTER=$(echo "$TASK_METADATA" | python3 -c "import sys,json; print(json.load(sys.stdin).get('Cluster',''))" 2>/dev/null)
        if [ -n "$TASK_ARN" ] && [ -n "$CLUSTER" ]; then
            ENI_ID=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
                --query 'tasks[0].attachments[?type==`ElasticNetworkInterface`].details[] | [?name==`networkInterfaceId`].value | [0]' \
                --output text 2>/dev/null)
        fi
    fi

    echo "  ENI ID: ${ENI_ID:-not found}"

    if [ -n "$ENI_ID" ]; then
        PUBLIC_IP=$(aws ec2 describe-network-interfaces \
            --network-interface-ids "$ENI_ID" \
            --query 'NetworkInterfaces[0].Association.PublicIp' \
            --output text 2>/dev/null)
        echo "  EC2 API returned: ${PUBLIC_IP:-nothing}"
    fi

    # Fallback: try EC2 metadata service (works on some setups)
    if [ -z "$PUBLIC_IP" ] || [ "$PUBLIC_IP" = "None" ]; then
        PUBLIC_IP=$(curl -s --connect-timeout 2 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "")
    fi

    # Final fallback to private IP
    CONTAINER_IP="${PUBLIC_IP:-$PRIVATE_IP}"

    echo "  Private IP: $PRIVATE_IP"
    echo "  Public IP: ${PUBLIC_IP:-not available}"
    echo "  Using for webhook: $CONTAINER_IP"

    # Create session-specific target group for subdomain routing
    SESSION_TG_NAME="${SESSION_ID}-tg"
    # Truncate to 32 chars (AWS limit)
    SESSION_TG_NAME="${SESSION_TG_NAME:0:32}"

    echo "  Creating target group: $SESSION_TG_NAME"
    SESSION_TG_ARN=$(aws elbv2 create-target-group \
        --name "$SESSION_TG_NAME" \
        --protocol HTTP \
        --port 3001 \
        --vpc-id "$VPC_ID" \
        --target-type ip \
        --health-check-path /api/health \
        --health-check-interval-seconds 30 \
        --healthy-threshold-count 2 \
        --query 'TargetGroups[0].TargetGroupArn' \
        --output text 2>/dev/null || true)

    if [ -z "$SESSION_TG_ARN" ] || [ "$SESSION_TG_ARN" = "None" ]; then
        # Target group might already exist, try to get it
        SESSION_TG_ARN=$(aws elbv2 describe-target-groups \
            --names "$SESSION_TG_NAME" \
            --query 'TargetGroups[0].TargetGroupArn' \
            --output text 2>/dev/null || true)
    fi

    if [ -n "$SESSION_TG_ARN" ] && [ "$SESSION_TG_ARN" != "None" ]; then
        echo "  Target group ARN: $SESSION_TG_ARN"

        # Register container with session-specific target group (use private IP for VPC routing)
        aws elbv2 register-targets \
            --target-group-arn "$SESSION_TG_ARN" \
            --targets "Id=$PRIVATE_IP,Port=3001" \
            2>/dev/null && echo "  Registered with session target group"

        # Create ALB listener rule for subdomain routing
        if [ -n "$ALB_LISTENER_ARN" ] && [ -n "$UAT_DOMAIN_SUFFIX" ]; then
            SUBDOMAIN="${SESSION_ID}.${UAT_DOMAIN_SUFFIX}"

            # Find next available priority (start from 10, increment by 1)
            EXISTING_PRIORITIES=$(aws elbv2 describe-rules \
                --listener-arn "$ALB_LISTENER_ARN" \
                --query 'Rules[*].Priority' \
                --output text 2>/dev/null | tr '\t' '\n' | grep -v default | sort -n || true)

            PRIORITY=10
            while echo "$EXISTING_PRIORITIES" | grep -q "^${PRIORITY}$"; do
                PRIORITY=$((PRIORITY + 1))
            done

            echo "  Creating ALB rule for $SUBDOMAIN (priority: $PRIORITY)"
            RULE_ARN=$(aws elbv2 create-rule \
                --listener-arn "$ALB_LISTENER_ARN" \
                --priority "$PRIORITY" \
                --conditions "[{\"Field\":\"host-header\",\"Values\":[\"$SUBDOMAIN\"]}]" \
                --actions "[{\"Type\":\"forward\",\"TargetGroupArn\":\"$SESSION_TG_ARN\"}]" \
                --query 'Rules[0].RuleArn' \
                --output text 2>/dev/null || true)

            if [ -n "$RULE_ARN" ] && [ "$RULE_ARN" != "None" ]; then
                echo "  ALB rule created: $RULE_ARN"
            else
                echo "  Warning: Could not create ALB rule (may already exist)"
            fi
        fi
    else
        echo "  Warning: Could not create/find target group, falling back to shared target group"
        # Fallback to shared target group (use private IP for VPC routing)
        if [ -n "$TARGET_GROUP_ARN" ]; then
            aws elbv2 register-targets \
                --target-group-arn "$TARGET_GROUP_ARN" \
                --targets "Id=$PRIVATE_IP,Port=3001" \
                2>/dev/null && echo "  Registered with shared ALB target group"
        fi
    fi

    # Update DynamoDB session with target group ARN for cleanup
    aws dynamodb update-item \
        --table-name "$SESSIONS_TABLE" \
        --key "{\"session_id\": {\"S\": \"$SESSION_ID\"}}" \
        --update-expression "SET container_ip = :ip, #st = :status, target_group_arn = :tg" \
        --expression-attribute-names '{"#st": "status"}' \
        --expression-attribute-values "{\":ip\": {\"S\": \"$CONTAINER_IP\"}, \":status\": {\"S\": \"RUNNING\"}, \":tg\": {\"S\": \"${SESSION_TG_ARN:-$TARGET_GROUP_ARN}\"}}" \
        2>/dev/null && echo "  Updated DynamoDB session" || echo "  Warning: Could not update DynamoDB"

    # Post "UAT Environment Started" comment to GitHub
    if [ "${SUPPRESS_GITHUB_COMMENTS:-false}" != "true" ] && [ -n "$GITHUB_TOKEN" ] && [ -n "$REPO" ] && [ -n "$PR_NUMBER" ]; then
        UAT_URL="https://${SESSION_ID}.${UAT_DOMAIN_SUFFIX:-uat.teammobot.dev}"

        # Check session details from DynamoDB (initial_prompt, source, jira_issue_key)
        SESSION_DETAILS=$(aws dynamodb get-item \
            --table-name "$SESSIONS_TABLE" \
            --key "{\"session_id\": {\"S\": \"$SESSION_ID\"}}" \
            --projection-expression "initial_prompt, #src, jira_issue_key" \
            --expression-attribute-names '{"#src": "source"}' \
            --output json 2>/dev/null)

        INITIAL_PROMPT=$(echo "$SESSION_DETAILS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Item',{}).get('initial_prompt',{}).get('S',''))" 2>/dev/null)
        SESSION_SOURCE=$(echo "$SESSION_DETAILS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Item',{}).get('source',{}).get('S','github'))" 2>/dev/null)
        JIRA_ISSUE_KEY=$(echo "$SESSION_DETAILS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Item',{}).get('jira_issue_key',{}).get('S',''))" 2>/dev/null)

        if [ -n "$INITIAL_PROMPT" ] && [ "$INITIAL_PROMPT" != "None" ]; then
            COMMENT_BODY=$(cat <<EOF
<!-- claude-agent -->
**UAT + Claude Agent Started**

URL: ${UAT_URL}

Branch: \`${BRANCH:-main}\`
Session: \`${SESSION_ID}\`

The environment is now available. Claude will automatically start implementing the PR description.

Comment on this PR to provide feedback or additional instructions.

To stop, close the PR or remove the \`claude-dev\` label.
EOF
)
        else
            COMMENT_BODY=$(cat <<EOF
<!-- claude-agent -->
**UAT Environment Started**

URL: ${UAT_URL}

Branch: \`${BRANCH:-main}\`
Session: \`${SESSION_ID}\`

The environment is now available. Authentication uses staging (\`app.teammobot.dev\`).

To stop this UAT, close the issue/PR or remove the \`uat\` label.
EOF
)
        fi

        echo "  Posting UAT started comment to GitHub..."
        # Properly escape the comment body for JSON
        ESCAPED_BODY=$(echo "$COMMENT_BODY" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
        HTTP_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
            -H "Authorization: token ${GITHUB_TOKEN}" \
            -H "Accept: application/vnd.github.v3+json" \
            -H "Content-Type: application/json" \
            -d "{\"body\": ${ESCAPED_BODY}}" \
            "https://api.github.com/repos/${REPO}/issues/${PR_NUMBER}/comments" 2>&1)
        HTTP_CODE=$(echo "$HTTP_RESPONSE" | tail -n1)
        if [ "$HTTP_CODE" = "201" ]; then
            echo "  Posted UAT started comment"
        else
            echo "  Warning: GitHub API returned $HTTP_CODE"
            echo "  Response: $(echo "$HTTP_RESPONSE" | head -n1 | cut -c1-200)"
        fi

        # Post to JIRA if this is a JIRA-triggered session
        if [ "$SESSION_SOURCE" = "jira" ] && [ -n "$JIRA_ISSUE_KEY" ]; then
            echo "  Posting UAT ready comment to JIRA issue $JIRA_ISSUE_KEY..."

            # Get JIRA credentials from Secrets Manager
            JIRA_SECRET=$(aws secretsmanager get-secret-value \
                --secret-id "claude-cloud-agent/jira" \
                --query 'SecretString' \
                --output text 2>/dev/null)

            if [ -n "$JIRA_SECRET" ]; then
                JIRA_BASE_URL=$(echo "$JIRA_SECRET" | python3 -c "import sys,json; print(json.load(sys.stdin).get('base_url',''))")
                JIRA_EMAIL=$(echo "$JIRA_SECRET" | python3 -c "import sys,json; print(json.load(sys.stdin).get('email',''))")
                JIRA_TOKEN=$(echo "$JIRA_SECRET" | python3 -c "import sys,json; print(json.load(sys.stdin).get('api_token',''))")

                PR_URL="https://github.com/${REPO}/pull/${PR_NUMBER}"

                # Build ADF comment body
                JIRA_COMMENT=$(cat <<JIRA_EOF
{
    "body": {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "UAT Environment Ready", "marks": [{"type": "strong"}]}
                ]
            },
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "UAT URL: ", "marks": [{"type": "strong"}]}, {"type": "text", "text": "${UAT_URL}"}]}]},
                    {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "GitHub PR: ", "marks": [{"type": "strong"}]}, {"type": "text", "text": "${PR_URL}"}]}]},
                    {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Branch: ", "marks": [{"type": "strong"}]}, {"type": "text", "text": "${BRANCH:-main}"}]}]}
                ]
            },
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "The UAT environment is ready for testing.", "marks": [{"type": "em"}]}
                ]
            }
        ]
    }
}
JIRA_EOF
)

                JIRA_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
                    -u "${JIRA_EMAIL}:${JIRA_TOKEN}" \
                    -H "Content-Type: application/json" \
                    -d "$JIRA_COMMENT" \
                    "${JIRA_BASE_URL}/rest/api/3/issue/${JIRA_ISSUE_KEY}/comment" 2>&1)
                JIRA_HTTP_CODE=$(echo "$JIRA_RESPONSE" | tail -n1)
                if [ "$JIRA_HTTP_CODE" = "201" ]; then
                    echo "  Posted UAT ready comment to JIRA"
                else
                    echo "  Warning: JIRA API returned $JIRA_HTTP_CODE"
                fi
            else
                echo "  Warning: Could not get JIRA secret"
            fi
        fi
    fi
else
    echo "  Warning: SESSIONS_TABLE or SESSION_ID not set, skipping registration"
fi

echo "[6/9] Setting environment..."
export NODE_ENV="${NODE_ENV:-development}"
# Accept RDS SSL certificates (Amazon's CA)
export NODE_TLS_REJECT_UNAUTHORIZED=0
export FRONTEND_URL="https://${SESSION_ID:-localhost}.uat.teammobot.dev"
export MOBOT_BASE_URL="${MOBOT_BASE_URL:-https://app.teammobot.dev}"
export WORK_DIR="/app/repo"
export VITE_DEV_SERVER="http://localhost:5173"
echo "  NODE_ENV: $NODE_ENV"
echo "  USE_NATIVE_GIT: $USE_NATIVE_GIT"
echo "  TEST_PLANS_GIT_REPO_PATH: $TEST_PLANS_GIT_REPO_PATH"
echo "  CUSTOMER_DOCS_GIT_REPO_PATH: $CUSTOMER_DOCS_GIT_REPO_PATH"

echo "[7/9] Starting prompt server..."
echo "  Prompt API on port 8080"
node /app/prompt-server.js &
PROMPT_SERVER_PID=$!

echo "[8/9] Starting dev proxy..."
echo "  Proxy on port 3001 -> API (3002) + Vite (5173)"
PROXY_PORT=3001 VITE_PORT=5173 EXPRESS_PORT=3002 node /app/dev-proxy.js &
PROXY_PID=$!

echo "[9/9] Starting Express server..."
echo "  FRONTEND_URL: $FRONTEND_URL"
echo "  MOBOT_BASE_URL: $MOBOT_BASE_URL"
echo "  Express API on port 3002"

# Run Express server with hot reload on port 3002
export PORT=3002
if [ -f "server/package.json" ]; then
    cd server
    exec npm run dev
else
    exec npm run dev
fi
