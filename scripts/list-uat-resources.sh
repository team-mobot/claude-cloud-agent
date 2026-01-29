#!/bin/bash
# List all UAT resources currently in use
# Usage: ./list-uat-resources.sh

CLUSTER="claude-cloud-agent"
SESSIONS_TABLE="claude-cloud-agent-sessions"
ALB_NAME="test-tickets-uat-alb"
ECR_REPO="claude-dev-uat"
REGION="${AWS_REGION:-us-east-1}"

# Define aws function to use docker if aws CLI not in PATH
if ! command -v aws &> /dev/null; then
    aws() {
        docker run --rm -v ~/.aws:/root/.aws -v "$(pwd):/aws" amazon/aws-cli "$@"
    }
fi

echo "========================================"
echo "UAT Resources Report"
echo "Generated: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "========================================"

# 1. ECS Tasks
echo ""
echo "=== ECS Tasks (Cluster: $CLUSTER) ==="
TASK_ARNS=$(aws ecs list-tasks --cluster "$CLUSTER" --query 'taskArns[]' --output text 2>/dev/null)
if [ -n "$TASK_ARNS" ]; then
    for task_arn in $TASK_ARNS; do
        aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task_arn" --output json 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
for task in data.get('tasks', []):
    task_def = task.get('taskDefinitionArn', '').split('/')[-1]
    status = task.get('lastStatus', 'N/A')
    started = task.get('startedAt', '')[:19] if task.get('startedAt') else 'N/A'
    ip = 'N/A'
    for att in task.get('attachments', []):
        for d in att.get('details', []):
            if d.get('name') == 'privateIPv4Address':
                ip = d.get('value', 'N/A')
    task_id = task.get('taskArn', '').split('/')[-1]
    print(f'  - {task_def}')
    print(f'    Task ID: {task_id[:12]}...')
    print(f'    Status: {status} | IP: {ip} | Started: {started}')
"
    done
else
    echo "  No tasks running"
fi

# 2. DynamoDB Sessions
echo ""
echo "=== DynamoDB Sessions (Table: $SESSIONS_TABLE) ==="
aws dynamodb scan --table-name "$SESSIONS_TABLE" \
    --projection-expression "session_id, #st, created_at, pr_number, repo_full_name, uat_url, container_ip" \
    --expression-attribute-names '{"#st": "status"}' \
    --output json 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    items = data.get('Items', [])
    if not items:
        print('  No sessions found')
    else:
        items.sort(key=lambda x: x.get('created_at', {}).get('S', ''), reverse=True)
        for item in items:
            sid = item.get('session_id', {}).get('S', 'N/A')
            status = item.get('status', {}).get('S', 'N/A')
            created = item.get('created_at', {}).get('S', 'N/A')[:19] if item.get('created_at', {}).get('S') else 'N/A'
            pr = item.get('pr_number', {}).get('N', 'N/A')
            repo = item.get('repo_full_name', {}).get('S', 'N/A')
            ip = item.get('container_ip', {}).get('S', 'N/A')
            url = item.get('uat_url', {}).get('S', '')

            print(f'  - {sid}')
            print(f'    Status: {status} | PR: {repo}#{pr} | IP: {ip}')
            print(f'    Created: {created}')
            if url:
                print(f'    URL: {url}')
except json.JSONDecodeError:
    print('  No sessions found or table does not exist')
except Exception as e:
    print(f'  Error reading sessions: {e}')
"

# 3. ALB Target Groups (UAT-related)
echo ""
echo "=== ALB Target Groups (UAT) ==="
aws elbv2 describe-target-groups \
    --query "TargetGroups[?contains(TargetGroupName, 'tt-') || contains(TargetGroupName, 'uat')]" \
    --output json 2>/dev/null | python3 -c "
import sys, json
try:
    tgs = json.load(sys.stdin)
    if not tgs:
        print('  No UAT target groups found')
    else:
        for tg in sorted(tgs, key=lambda x: x.get('TargetGroupName', '')):
            name = tg.get('TargetGroupName', 'N/A')
            port = tg.get('Port', 'N/A')
            print(f'  - {name} (port {port})')
except json.JSONDecodeError:
    print('  No target groups found')
except Exception as e:
    print(f'  Error listing target groups: {e}')
"

# 4. ALB Listener Rules (for UAT subdomains)
echo ""
echo "=== ALB Listener Rules (UAT subdomains) ==="
ALB_ARN=$(aws elbv2 describe-load-balancers --names "$ALB_NAME" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null)

if [ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ]; then
    LISTENER_ARN=$(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" \
        --query 'Listeners[?Port==`443`].ListenerArn | [0]' --output text 2>/dev/null)

    if [ -n "$LISTENER_ARN" ] && [ "$LISTENER_ARN" != "None" ]; then
        aws elbv2 describe-rules --listener-arn "$LISTENER_ARN" --output json 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    rules = [r for r in data.get('Rules', []) if not r.get('IsDefault')]
    if not rules:
        print('  No custom rules found')
    else:
        rules.sort(key=lambda x: int(x.get('Priority', '99999')) if x.get('Priority', 'default') != 'default' else 99999)
        for rule in rules:
            priority = rule.get('Priority', 'N/A')
            conditions = rule.get('Conditions', [])
            host = 'N/A'
            for c in conditions:
                if c.get('Field') == 'host-header':
                    hosts = c.get('Values', []) or c.get('HostHeaderConfig', {}).get('Values', [])
                    if hosts:
                        host = hosts[0]
            actions = rule.get('Actions', [])
            tg_name = 'N/A'
            for a in actions:
                if a.get('Type') == 'forward':
                    tg_arn = a.get('TargetGroupArn', '')
                    if tg_arn:
                        tg_name = tg_arn.split('/')[-2]
            print(f'  - Priority {priority}: {host}')
            print(f'    -> {tg_name}')
except json.JSONDecodeError:
    print('  No rules found')
except Exception as e:
    print(f'  Error listing rules: {e}')
"
    else
        echo "  No HTTPS listener found"
    fi
else
    echo "  ALB not found: $ALB_NAME"
fi

# 5. ECR Images (last 5)
echo ""
echo "=== ECR Images ($ECR_REPO, last 5) ==="
aws ecr describe-images --repository-name "$ECR_REPO" \
    --query 'sort_by(imageDetails, &imagePushedAt)[-5:]' \
    --output json 2>/dev/null | python3 -c "
import sys, json
try:
    images = json.load(sys.stdin)
    if not images:
        print('  No images found')
    else:
        for img in reversed(images):
            tags = img.get('imageTags') or ['untagged']
            pushed = str(img.get('imagePushedAt', 'N/A'))[:19]
            size_mb = img.get('imageSizeInBytes', 0) / 1024 / 1024
            print(f'  - {chr(44).join(tags)}')
            print(f'    Pushed: {pushed} | Size: {size_mb:.1f} MB')
except json.JSONDecodeError:
    print('  No images found or repository does not exist')
except Exception as e:
    print(f'  Error listing images: {e}')
"

# 6. Summary
echo ""
echo "=== Summary ==="
TASK_COUNT=$(aws ecs list-tasks --cluster "$CLUSTER" --query 'length(taskArns)' --output text 2>/dev/null || echo "0")
SESSION_COUNT=$(aws dynamodb scan --table-name "$SESSIONS_TABLE" --select COUNT --query 'Count' --output text 2>/dev/null || echo "0")
TG_COUNT=$(aws elbv2 describe-target-groups --query "length(TargetGroups[?contains(TargetGroupName, 'tt-') || contains(TargetGroupName, 'uat')])" --output text 2>/dev/null || echo "0")

echo "  ECS Tasks:       ${TASK_COUNT:-0}"
echo "  DB Sessions:     ${SESSION_COUNT:-0}"
echo "  Target Groups:   ${TG_COUNT:-0}"
echo ""
echo "========================================"
