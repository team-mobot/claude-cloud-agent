#!/bin/bash
# Check if the latest code is running in UAT containers or locally

# Don't exit on error - we handle exit codes manually

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=== Latest Code Check ==="
echo

# Get local git info
echo "Local Git Info:"
LOCAL_COMMIT=$(git -C "$PROJECT_ROOT" rev-parse HEAD)
LOCAL_SHORT=$(git -C "$PROJECT_ROOT" rev-parse --short HEAD)
LOCAL_BRANCH=$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD)
LOCAL_DATE=$(git -C "$PROJECT_ROOT" log -1 --format=%ci)
LOCAL_MSG=$(git -C "$PROJECT_ROOT" log -1 --format=%s)

echo "  Branch:  $LOCAL_BRANCH"
echo "  Commit:  $LOCAL_SHORT ($LOCAL_COMMIT)"
echo "  Date:    $LOCAL_DATE"
echo "  Message: $LOCAL_MSG"
echo

# Check for running prompt server locally
check_local() {
    echo "Checking local prompt server (port 8080)..."
    if curl -s --connect-timeout 2 http://localhost:8080/version > /tmp/version_response.json 2>/dev/null; then
        REMOTE_COMMIT=$(jq -r '.commit // "unknown"' /tmp/version_response.json)
        REMOTE_SHORT=$(jq -r '.shortCommit // "unknown"' /tmp/version_response.json)
        REMOTE_BRANCH=$(jq -r '.branch // "unknown"' /tmp/version_response.json)

        echo "  Running version:"
        echo "    Branch:  $REMOTE_BRANCH"
        echo "    Commit:  $REMOTE_SHORT"

        if [ "$LOCAL_COMMIT" = "$REMOTE_COMMIT" ]; then
            echo -e "  ${GREEN}✓ Latest code is running locally${NC}"
            return 0
        else
            echo -e "  ${YELLOW}⚠ Different code is running${NC}"
            echo "    Local:  $LOCAL_SHORT"
            echo "    Running: $REMOTE_SHORT"
            return 1
        fi
    else
        echo "  No local prompt server running on port 8080"
        return 2
    fi
}

# Check ECS tasks for running UAT containers
check_ecs() {
    echo "Checking ECS tasks..."

    if ! command -v aws &> /dev/null; then
        echo "  AWS CLI not available, skipping ECS check"
        return 2
    fi

    # List running tasks
    CLUSTER="test-tickets-cluster"
    TASKS=$(aws ecs list-tasks --cluster "$CLUSTER" --desired-status RUNNING --query 'taskArns' --output text 2>/dev/null || echo "")

    if [ -z "$TASKS" ] || [ "$TASKS" = "None" ]; then
        echo "  No running ECS tasks found"
        return 2
    fi

    # Get task details
    for TASK_ARN in $TASKS; do
        TASK_ID=$(echo "$TASK_ARN" | awk -F'/' '{print $NF}')
        echo "  Task: $TASK_ID"

        # Get ENI and public IP
        ENI=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
            --query 'tasks[0].attachments[0].details[?name==`networkInterfaceId`].value' --output text 2>/dev/null || echo "")

        if [ -n "$ENI" ] && [ "$ENI" != "None" ]; then
            IP=$(aws ec2 describe-network-interfaces --network-interface-ids "$ENI" \
                --query 'NetworkInterfaces[0].Association.PublicIp' --output text 2>/dev/null || echo "")

            if [ -n "$IP" ] && [ "$IP" != "None" ]; then
                echo "    IP: $IP"

                # Try to get version info
                if curl -s --connect-timeout 5 "http://$IP:8080/version" > /tmp/ecs_version.json 2>/dev/null; then
                    REMOTE_COMMIT=$(jq -r '.commit // "unknown"' /tmp/ecs_version.json)
                    REMOTE_SHORT=$(jq -r '.shortCommit // "unknown"' /tmp/ecs_version.json)
                    echo "    Running commit: $REMOTE_SHORT"

                    if [ "$LOCAL_COMMIT" = "$REMOTE_COMMIT" ]; then
                        echo -e "    ${GREEN}✓ Latest code${NC}"
                    else
                        echo -e "    ${YELLOW}⚠ Different code (local: $LOCAL_SHORT)${NC}"
                    fi
                else
                    echo "    Could not reach version endpoint"
                fi
            fi
        fi
    done
}

# Check Docker containers
check_docker() {
    echo "Checking Docker containers..."

    if ! command -v docker &> /dev/null; then
        echo "  Docker not available"
        return 2
    fi

    CONTAINERS=$(docker ps --filter "ancestor=test-tickets-uat" --format "{{.ID}}" 2>/dev/null || echo "")

    if [ -z "$CONTAINERS" ]; then
        # Also check for any container with the prompt server
        CONTAINERS=$(docker ps --format "{{.ID}}\t{{.Image}}" 2>/dev/null | grep -i "test-tickets\|uat" | cut -f1 || echo "")
    fi

    if [ -z "$CONTAINERS" ]; then
        echo "  No matching Docker containers running"
        return 2
    fi

    for CONTAINER_ID in $CONTAINERS; do
        echo "  Container: $CONTAINER_ID"

        # Get container IP
        IP=$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$CONTAINER_ID" 2>/dev/null || echo "")

        if [ -n "$IP" ]; then
            if curl -s --connect-timeout 2 "http://$IP:8080/version" > /tmp/docker_version.json 2>/dev/null; then
                REMOTE_COMMIT=$(jq -r '.commit // "unknown"' /tmp/docker_version.json)
                REMOTE_SHORT=$(jq -r '.shortCommit // "unknown"' /tmp/docker_version.json)
                echo "    Running commit: $REMOTE_SHORT"

                if [ "$LOCAL_COMMIT" = "$REMOTE_COMMIT" ]; then
                    echo -e "    ${GREEN}✓ Latest code${NC}"
                else
                    echo -e "    ${YELLOW}⚠ Different code (local: $LOCAL_SHORT)${NC}"
                fi
            fi
        fi
    done
}

# Run checks
echo "---"
check_local
LOCAL_RESULT=$?
echo

echo "---"
check_docker
DOCKER_RESULT=$?
echo

echo "---"
check_ecs
ECS_RESULT=$?
echo

# Summary
echo "=== Summary ==="
if [ $LOCAL_RESULT -eq 0 ] || [ $DOCKER_RESULT -eq 0 ] || [ $ECS_RESULT -eq 0 ]; then
    echo -e "${GREEN}Latest code ($LOCAL_SHORT) is running somewhere${NC}"
    exit 0
elif [ $LOCAL_RESULT -eq 1 ] || [ $DOCKER_RESULT -eq 1 ] || [ $ECS_RESULT -eq 1 ]; then
    echo -e "${YELLOW}Running code differs from local HEAD${NC}"
    exit 1
else
    echo -e "${RED}No running instances found${NC}"
    exit 2
fi
