#!/bin/bash
set -e

echo "=== test_tickets UAT Container Starting ==="
echo "  Branch: ${BRANCH:-main}"
echo "  Session: ${SESSION_ID:-unknown}"

# Clone repository
echo "[1/5] Cloning repository..."
REPO_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO:-team-mobot/test_tickets}.git"
git clone --depth 1 --branch "${BRANCH:-main}" "$REPO_URL" /app/repo 2>&1 || {
    echo "Failed to clone branch ${BRANCH}, trying main..."
    git clone --depth 1 --branch main "$REPO_URL" /app/repo
}
cd /app/repo

echo "[2/5] Installing frontend dependencies..."
npm ci --include=dev 2>&1

echo "[3/5] Building frontend..."
# Vite needs VITE_* env vars at build time
export VITE_GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID}"
export VITE_API_URL=""
echo "  VITE_GOOGLE_CLIENT_ID: ${VITE_GOOGLE_CLIENT_ID:0:20}..."

npm run build 2>&1 || {
    echo "Build failed, trying with explicit paths..."
    ./node_modules/.bin/tsc -b && ./node_modules/.bin/vite build
}

# Build server from its own directory (separate package.json)
if [ -f "server/package.json" ]; then
    echo "[3.5/5] Building server (separate package)..."
    cd server
    npm ci --include=dev 2>&1
    npm run build 2>&1

    # Copy built frontend to server's public directory
    echo "  Copying frontend build to server/public..."
    mkdir -p public
    cp -r ../dist/* public/
    cd ..
fi

# Register with DynamoDB and ALB target group
echo "[4/5] Registering container..."
if [ -n "$SESSIONS_TABLE" ] && [ -n "$SESSION_ID" ]; then
    # Get container's private IP from ECS metadata
    TASK_METADATA=$(curl -s "${ECS_CONTAINER_METADATA_URI_V4}/task" 2>/dev/null || echo "{}")
    CONTAINER_IP=$(echo "$TASK_METADATA" | grep -o '"PrivateIPv4Address":"[^"]*"' | head -1 | cut -d'"' -f4)

    if [ -z "$CONTAINER_IP" ]; then
        CONTAINER_IP=$(hostname -i 2>/dev/null || echo "localhost")
    fi

    echo "  Container IP: $CONTAINER_IP"

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

        # Register container with session-specific target group
        aws elbv2 register-targets \
            --target-group-arn "$SESSION_TG_ARN" \
            --targets "Id=$CONTAINER_IP,Port=3001" \
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
        # Fallback to shared target group
        if [ -n "$TARGET_GROUP_ARN" ]; then
            aws elbv2 register-targets \
                --target-group-arn "$TARGET_GROUP_ARN" \
                --targets "Id=$CONTAINER_IP,Port=3001" \
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

# Set environment for the app
export FRONTEND_URL="https://${SESSION_ID:-localhost}.uat.teammobot.dev"
export MOBOT_BASE_URL="${MOBOT_BASE_URL:-https://app.teammobot.dev}"

echo "[5/5] Starting server..."
echo "  FRONTEND_URL: $FRONTEND_URL"
echo "  MOBOT_BASE_URL: $MOBOT_BASE_URL"
echo "  Listening on port 3001"

# Try to run the server
# Check for compiled server from server/ directory build
if [ -f "server/dist/index.js" ]; then
    echo "  Running compiled server from server/dist/index.js"
    cd server
    exec node dist/index.js
elif [ -f "dist/index.js" ]; then
    echo "  Running compiled server from dist/index.js"
    exec node dist/index.js
else
    echo "ERROR: Cannot find compiled server"
    echo "  Checked: server/dist/index.js, dist/index.js"
    ls -la server/dist/ 2>/dev/null || echo "  server/dist/ directory does not exist"
    ls -la dist/ 2>/dev/null || echo "  dist/ directory contents shown above"
    exit 1
fi
