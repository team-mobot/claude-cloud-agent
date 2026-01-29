#!/bin/bash
set -e

echo "=== test_tickets UAT Container Starting ==="

# Initialize and start PostgreSQL
echo "[1/6] Starting PostgreSQL..."
if [ ! -f "$PGDATA/PG_VERSION" ]; then
    su postgres -c "initdb -D $PGDATA"
fi

# Configure PostgreSQL for local connections
echo "host all all 127.0.0.1/32 trust" >> "$PGDATA/pg_hba.conf"
echo "local all all trust" >> "$PGDATA/pg_hba.conf"

su postgres -c "pg_ctl start -D $PGDATA -l /var/log/postgresql.log -w -t 60"

# Wait for PostgreSQL to be ready
echo "[2/6] Waiting for PostgreSQL to be ready..."
for i in {1..30}; do
    if su postgres -c "pg_isready" > /dev/null 2>&1; then
        echo "PostgreSQL is ready"
        break
    fi
    sleep 1
done

# Create database
echo "[3/6] Creating database..."
su postgres -c "createdb test_tickets" 2>/dev/null || echo "Database already exists"

# Clone data from staging (if STAGING_DATABASE_URL set)
if [ -n "$STAGING_DATABASE_URL" ]; then
    echo "[4/6] Cloning staging data..."
    PGPASSWORD="" pg_dump "$STAGING_DATABASE_URL" \
        --no-owner --no-privileges \
        --exclude-table='*_migrations' \
        --exclude-table='schema_migrations' \
        2>/dev/null | \
        su postgres -c "psql -d test_tickets" > /dev/null 2>&1 || \
        echo "Warning: Could not clone staging data, starting with empty database"
else
    echo "[4/6] No STAGING_DATABASE_URL set, starting with empty database"
fi

# Set local DATABASE_URL for the application
export DATABASE_URL="postgresql://postgres@localhost/test_tickets"

# Run migrations
echo "[5/6] Running migrations..."
if [ -d "server/migrations" ]; then
    npm run migrate 2>/dev/null || echo "Migration command not found or failed"
fi

# Register with DynamoDB
echo "[6/6] Registering container with DynamoDB..."
if [ -n "$SESSIONS_TABLE" ] && [ -n "$SESSION_ID" ]; then
    # Get container's task metadata for IP
    TASK_METADATA=$(curl -s "${ECS_CONTAINER_METADATA_URI_V4}/task" 2>/dev/null || echo "{}")

    # Try to extract private IP from task metadata
    CONTAINER_IP=$(echo "$TASK_METADATA" | grep -o '"PrivateIPv4Address":"[^"]*"' | head -1 | cut -d'"' -f4)

    # Fallback to hostname
    if [ -z "$CONTAINER_IP" ]; then
        CONTAINER_IP=$(hostname -i 2>/dev/null || echo "localhost")
    fi

    TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    aws dynamodb put-item \
        --table-name "$SESSIONS_TABLE" \
        --item "{
            \"session_id\": {\"S\": \"$SESSION_ID\"},
            \"container_ip\": {\"S\": \"$CONTAINER_IP\"},
            \"container_port\": {\"N\": \"3001\"},
            \"branch\": {\"S\": \"${BRANCH:-unknown}\"},
            \"repo_full_name\": {\"S\": \"${REPO:-team-mobot/test_tickets}\"},
            \"pr_number\": {\"N\": \"${PR_NUMBER:-0}\"},
            \"status\": {\"S\": \"RUNNING\"},
            \"app_type\": {\"S\": \"test-tickets\"},
            \"created_at\": {\"S\": \"$TIMESTAMP\"},
            \"uat_url\": {\"S\": \"https://${SESSION_ID}.uat.teammobot.dev\"}
        }" 2>/dev/null && echo "Registered with DynamoDB" || echo "Warning: Could not register with DynamoDB"
else
    echo "Warning: SESSIONS_TABLE or SESSION_ID not set, skipping DynamoDB registration"
fi

# Set frontend URL for CORS
export FRONTEND_URL="https://${SESSION_ID:-localhost}.uat.teammobot.dev"

# Set authentication endpoints to staging
export MOBOT_BASE_URL="${MOBOT_BASE_URL:-https://app.teammobot.dev}"

echo "=== Starting test_tickets server ==="
echo "  DATABASE_URL: $DATABASE_URL"
echo "  FRONTEND_URL: $FRONTEND_URL"
echo "  MOBOT_BASE_URL: $MOBOT_BASE_URL"
echo "  Listening on port 3001"

# Switch to app user and start the server
cd /app
exec su app -c "node dist/index.js"
