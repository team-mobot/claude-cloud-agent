#!/bin/bash
# Clean up unused UAT resources
# Usage: ./cleanup-uat-resources.sh [--dry-run]
#
# Removes:
# - STOPPED sessions from DynamoDB
# - Orphaned ALB listener rules
# - Orphaned target groups
# - Stopped ECS tasks

set -e

DRY_RUN=false
if [ "$1" = "--dry-run" ]; then
    DRY_RUN=true
    echo "=== DRY RUN MODE - No changes will be made ==="
fi

CLUSTER="claude-cloud-agent"
SESSIONS_TABLE="claude-cloud-agent-sessions"
ALB_NAME="test-tickets-uat-alb"
REGION="${AWS_REGION:-us-east-1}"

# Define aws function to use docker if aws CLI not in PATH
if ! command -v aws &> /dev/null; then
    aws() {
        docker run --rm -v ~/.aws:/root/.aws -v "$(pwd):/aws" amazon/aws-cli "$@"
    }
fi

echo "=========================================="
echo "UAT Resource Cleanup"
echo "Generated: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "=========================================="

# 1. Get list of STOPPED sessions
echo ""
echo "=== Finding STOPPED sessions ==="
STOPPED_SESSIONS=$(aws dynamodb scan --table-name "$SESSIONS_TABLE" \
    --filter-expression "#st = :stopped" \
    --expression-attribute-names '{"#st": "status"}' \
    --expression-attribute-values '{":stopped": {"S": "STOPPED"}}' \
    --projection-expression "session_id, target_group_arn, task_arn" \
    --output json 2>/dev/null)

SESSION_IDS=$(echo "$STOPPED_SESSIONS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for item in data.get('Items', []):
    print(item.get('session_id', {}).get('S', ''))
" 2>/dev/null)

if [ -z "$SESSION_IDS" ]; then
    echo "  No STOPPED sessions found"
else
    echo "  Found STOPPED sessions:"
    echo "$SESSION_IDS" | while read -r sid; do
        [ -n "$sid" ] && echo "    - $sid"
    done
fi

# 2. Get ALB listener ARN
echo ""
echo "=== Getting ALB configuration ==="
ALB_ARN=$(aws elbv2 describe-load-balancers --names "$ALB_NAME" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null)

if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ]; then
    echo "  ALB not found: $ALB_NAME"
    LISTENER_ARN=""
else
    echo "  ALB: $ALB_NAME"
    LISTENER_ARN=$(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" \
        --query 'Listeners[?Port==`443`].ListenerArn | [0]' --output text 2>/dev/null)
    echo "  Listener ARN: ${LISTENER_ARN:-not found}"
fi

# 3. Delete orphaned listener rules
echo ""
echo "=== Cleaning up ALB listener rules ==="
if [ -n "$LISTENER_ARN" ] && [ "$LISTENER_ARN" != "None" ]; then
    # Get all non-default rules
    RULES=$(aws elbv2 describe-rules --listener-arn "$LISTENER_ARN" --output json 2>/dev/null)

    echo "$RULES" | python3 -c "
import sys, json

data = json.load(sys.stdin)
stopped_sessions = '''$SESSION_IDS'''.strip().split('\n')
stopped_sessions = [s.strip() for s in stopped_sessions if s.strip()]

for rule in data.get('Rules', []):
    if rule.get('IsDefault'):
        continue

    rule_arn = rule.get('RuleArn', '')
    priority = rule.get('Priority', 'N/A')

    # Get host from conditions
    host = ''
    for c in rule.get('Conditions', []):
        if c.get('Field') == 'host-header':
            hosts = c.get('Values', []) or c.get('HostHeaderConfig', {}).get('Values', [])
            if hosts:
                host = hosts[0]

    # Check if this rule belongs to a stopped session
    for sid in stopped_sessions:
        if sid and sid in host:
            print(f'DELETE_RULE|{rule_arn}|{priority}|{host}')
            break
" 2>/dev/null | while IFS='|' read -r action rule_arn priority host; do
        if [ "$action" = "DELETE_RULE" ]; then
            echo "  Deleting rule: $host (priority $priority)"
            if [ "$DRY_RUN" = "false" ]; then
                aws elbv2 delete-rule --rule-arn "$rule_arn" 2>/dev/null && echo "    ✓ Deleted" || echo "    ✗ Failed"
            else
                echo "    [dry-run] Would delete"
            fi
        fi
    done
else
    echo "  No listener found, skipping rule cleanup"
fi

# 4. Delete orphaned target groups
echo ""
echo "=== Cleaning up target groups ==="
echo "$SESSION_IDS" | while read -r sid; do
    [ -z "$sid" ] && continue

    # Find target groups matching this session
    TG_ARNS=$(aws elbv2 describe-target-groups \
        --query "TargetGroups[?contains(TargetGroupName, '${sid}')].TargetGroupArn" \
        --output text 2>/dev/null)

    for tg_arn in $TG_ARNS; do
        [ -z "$tg_arn" ] || [ "$tg_arn" = "None" ] && continue
        TG_NAME=$(echo "$tg_arn" | rev | cut -d'/' -f2 | rev)
        echo "  Deleting target group: $TG_NAME"
        if [ "$DRY_RUN" = "false" ]; then
            aws elbv2 delete-target-group --target-group-arn "$tg_arn" 2>/dev/null && echo "    ✓ Deleted" || echo "    ✗ Failed (may have active rules)"
        else
            echo "    [dry-run] Would delete"
        fi
    done
done

# 5. Delete STOPPED sessions from DynamoDB
echo ""
echo "=== Cleaning up DynamoDB sessions ==="
echo "$SESSION_IDS" | while read -r sid; do
    [ -z "$sid" ] && continue
    echo "  Deleting session: $sid"
    if [ "$DRY_RUN" = "false" ]; then
        aws dynamodb delete-item \
            --table-name "$SESSIONS_TABLE" \
            --key "{\"session_id\": {\"S\": \"$sid\"}}" \
            2>/dev/null && echo "    ✓ Deleted" || echo "    ✗ Failed"
    else
        echo "    [dry-run] Would delete"
    fi
done

# 6. Stop any ECS tasks for stopped sessions
echo ""
echo "=== Checking for orphaned ECS tasks ==="
echo "$STOPPED_SESSIONS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for item in data.get('Items', []):
    task_arn = item.get('task_arn', {}).get('S', '')
    sid = item.get('session_id', {}).get('S', '')
    if task_arn:
        print(f'{sid}|{task_arn}')
" 2>/dev/null | while IFS='|' read -r sid task_arn; do
    [ -z "$task_arn" ] && continue

    # Check if task is still running
    TASK_STATUS=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task_arn" \
        --query 'tasks[0].lastStatus' --output text 2>/dev/null)

    if [ "$TASK_STATUS" = "RUNNING" ]; then
        echo "  Stopping task for $sid: ${task_arn##*/}"
        if [ "$DRY_RUN" = "false" ]; then
            aws ecs stop-task --cluster "$CLUSTER" --task "$task_arn" \
                --reason "Cleanup: session stopped" >/dev/null 2>&1 && echo "    ✓ Stopped" || echo "    ✗ Failed"
        else
            echo "    [dry-run] Would stop"
        fi
    fi
done

echo ""
echo "=========================================="
echo "Cleanup complete!"
echo "=========================================="
