#!/bin/bash
set -e

echo "=== test_tickets UAT Container Starting ==="
echo "  Branch: ${BRANCH:-main}"
echo "  Session: ${SESSION_ID:-unknown}"

# Clone repository
echo "[1/8] Cloning repository..."
REPO_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO:-team-mobot/test_tickets}.git"
git clone --depth 1 --branch "${BRANCH:-main}" "$REPO_URL" /app/repo 2>&1 || {
    echo "Failed to clone branch ${BRANCH}, trying main..."
    git clone --depth 1 --branch main "$REPO_URL" /app/repo
}
cd /app/repo

# Configure git identity for commits
git config user.email "claude-dev@teammobot.dev"
git config user.name "Claude Dev Agent"

# Patch Vite config to allow UAT subdomain hosts
echo "  Patching Vite config for UAT subdomains..."
node -e "
const fs = require('fs');
const configs = ['vite.config.ts', 'vite.config.js', 'vite.config.mts', 'vite.config.mjs'];
const config = configs.find(f => fs.existsSync(f));
if (config) {
    let content = fs.readFileSync(config, 'utf8');
    // Check if allowedHosts already configured
    if (!content.includes('allowedHosts')) {
        // Add allowedHosts to existing server config or create server config
        if (content.includes('server:') || content.includes('server :')) {
            // Insert after 'server: {' or 'server:{'
            content = content.replace(/(server\s*:\s*\{)/, '\$1 allowedHosts: true,');
        } else {
            // Add server config before final closing brace of defineConfig
            content = content.replace(/(\}\s*\)\s*;?\s*)$/, ', server: { allowedHosts: true } \$1');
        }
        fs.writeFileSync(config, content);
        console.log('  Patched ' + config + ' with allowedHosts: true');
    } else {
        console.log('  ' + config + ' already has allowedHosts configured');
    }
} else {
    console.log('  No vite config found, skipping patch');
}
"

echo "[2/8] Installing dependencies..."
npm ci --include=dev 2>&1

# Install server dependencies if separate package
if [ -f "server/package.json" ]; then
    echo "  Installing server dependencies..."
    cd server
    npm ci --include=dev 2>&1
    cd ..
fi

# Set Vite env vars
export VITE_GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID}"
export VITE_API_URL=""

echo "[3/8] Starting Vite dev server..."
# Re-apply Vite config patch (in case it was reverted during development)
node -e "
const fs = require('fs');
const configs = ['vite.config.ts', 'vite.config.js', 'vite.config.mts', 'vite.config.mjs'];
const config = configs.find(f => fs.existsSync(f));
if (config) {
    let content = fs.readFileSync(config, 'utf8');
    if (!content.includes('allowedHosts')) {
        if (content.includes('server:') || content.includes('server :')) {
            content = content.replace(/(server\s*:\s*\{)/, '\$1 allowedHosts: true,');
        } else {
            content = content.replace(/(\}\s*\)\s*;?\s*)$/, ', server: { allowedHosts: true } \$1');
        }
        fs.writeFileSync(config, content);
        console.log('  Re-applied Vite allowedHosts patch to ' + config);
    }
}
"
# Start Vite in background with network access enabled
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
echo "[4/8] Registering container..."
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
        --output text 2>/dev/null)

    if [ -z "$SESSION_TG_ARN" ] || [ "$SESSION_TG_ARN" = "None" ]; then
        # Target group might already exist, try to get it
        SESSION_TG_ARN=$(aws elbv2 describe-target-groups \
            --names "$SESSION_TG_NAME" \
            --query 'TargetGroups[0].TargetGroupArn' \
            --output text 2>/dev/null)
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
                --output text 2>/dev/null | tr '\t' '\n' | grep -v default | sort -n)

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
                --output text 2>/dev/null)

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
else
    echo "  Warning: SESSIONS_TABLE or SESSION_ID not set, skipping registration"
fi

echo "[5/8] Setting environment..."
export NODE_ENV=development
# Accept RDS SSL certificates (Amazon's CA)
export NODE_TLS_REJECT_UNAUTHORIZED=0
export FRONTEND_URL="https://${SESSION_ID:-localhost}.uat.teammobot.dev"
export MOBOT_BASE_URL="${MOBOT_BASE_URL:-https://app.teammobot.dev}"
export WORK_DIR="/app/repo"
export VITE_DEV_SERVER="http://localhost:5173"
echo "  NODE_ENV: $NODE_ENV"

echo "[6/8] Starting prompt server..."
echo "  Prompt API on port 8080"
node /app/prompt-server.js &
PROMPT_SERVER_PID=$!

echo "[7/8] Starting dev proxy..."
echo "  Proxy on port 3001 -> API (3002) + Vite (5173)"
PROXY_PORT=3001 VITE_PORT=5173 EXPRESS_PORT=3002 node /app/dev-proxy.js &
PROXY_PID=$!

echo "[8/8] Starting Express server..."
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
