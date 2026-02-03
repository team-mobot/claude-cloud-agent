# UAT Subdomain Pattern

This document describes how to create new services that automatically get a subdomain under `*.uat.teammobot.dev` without requiring Terraform changes.

## Overview

Any container can self-register with the existing ALB infrastructure to get a subdomain like `https://my-service.uat.teammobot.dev`. This is useful for:

- Development/prototyping new services
- Ephemeral environments (per-PR, per-session)
- Quick iteration without Terraform deployment cycles

## What's Already In Place

| Resource | Value | Notes |
|----------|-------|-------|
| Wildcard DNS | `*.uat.teammobot.dev` → ALB | Route 53 |
| Wildcard SSL | `*.uat.teammobot.dev` | ACM certificate |
| ALB | `test-tickets-uat-alb` | Shared across services |
| ALB Listener ARN | `arn:aws:elasticloadbalancing:us-east-1:678954237808:listener/app/test-tickets-uat-alb/7e47b6b368ee29e1/9b650b437fde8e6f` | HTTPS :443 |
| VPC | `vpc-0fde49947ce39aec4` | test-tickets VPC |
| Subnets | `subnet-016037c717cc89fb2`, `subnet-0ede91cdf265af677` | us-east-1a, us-east-1b |
| Security Group | `sg-0c6486526730186cb` | claude-cloud-agent-sg |
| ECS Cluster | `claude-cloud-agent` | Shared cluster |
| Execution Role | `arn:aws:iam::678954237808:role/claude-cloud-agent-AgentExecutionRole` | For ECR pull, logs |
| Task Role | `arn:aws:iam::678954237808:role/claude-cloud-agent-AgentTaskRole` | For ALB, DynamoDB |
| Sessions Table | `claude-cloud-agent-sessions` | DynamoDB |
| Idle Timeout | 60 minutes | Lambda auto-stops idle tasks |

## How It Works

1. Container starts with `SESSION_ID` env var (e.g., `my-service-dev`)
2. Entrypoint script creates a target group: `my-service-dev-tg`
3. Registers container's private IP with target group
4. Creates ALB listener rule: `my-service-dev.uat.teammobot.dev` → target group
5. Service is immediately available at `https://my-service-dev.uat.teammobot.dev`

When the container stops, the idle timeout Lambda (or explicit cleanup) removes the target group and listener rule.

## Creating a New Service

### Step 1: Create Container Image

Your Dockerfile needs:
- Your application
- AWS CLI (for self-registration)
- The registration script in entrypoint

```dockerfile
FROM python:3.11-slim
# or FROM node:20-slim, etc.

# Install AWS CLI for self-registration
RUN apt-get update && apt-get install -y curl unzip \
    && curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" \
    && unzip awscliv2.zip && ./aws/install && rm -rf aws awscliv2.zip \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy your app
COPY . /app
WORKDIR /app

# Copy registration script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 3000

ENTRYPOINT ["/entrypoint.sh"]
```

### Step 2: Add Self-Registration Script

Create `entrypoint.sh` with the registration logic:

```bash
#!/bin/bash
set -e

echo "=== Starting Service ==="
echo "  Session ID: ${SESSION_ID:-unknown}"

# Start your application in background
# Adjust command and port as needed
npm start &
# or: python -m uvicorn main:app --host 0.0.0.0 --port 3000 &
APP_PID=$!

# Wait for app to be ready
echo "Waiting for app to start..."
for i in $(seq 1 30); do
    if curl -s http://localhost:3000/health > /dev/null 2>&1; then
        echo "App is ready"
        break
    fi
    sleep 1
done

# === SELF-REGISTRATION ===
if [ -n "$SESSION_ID" ] && [ -n "$ALB_LISTENER_ARN" ] && [ -n "$VPC_ID" ]; then
    echo "Registering with ALB..."

    # Get container's private IP from ECS metadata
    TASK_METADATA=$(curl -s "${ECS_CONTAINER_METADATA_URI_V4}/task" 2>/dev/null || echo "{}")
    PRIVATE_IP=$(echo "$TASK_METADATA" | grep -o '"PrivateIPv4Address":"[^"]*"' | head -1 | cut -d'"' -f4)
    if [ -z "$PRIVATE_IP" ]; then
        PRIVATE_IP=$(hostname -i 2>/dev/null || echo "localhost")
    fi
    echo "  Private IP: $PRIVATE_IP"

    # Create session-specific target group
    TG_NAME="${SESSION_ID}-tg"
    TG_NAME="${TG_NAME:0:32}"  # AWS limit: 32 chars

    echo "  Creating target group: $TG_NAME"
    TG_ARN=$(aws elbv2 create-target-group \
        --name "$TG_NAME" \
        --protocol HTTP \
        --port ${APP_PORT:-3000} \
        --vpc-id "$VPC_ID" \
        --target-type ip \
        --health-check-path "${HEALTH_CHECK_PATH:-/health}" \
        --health-check-interval-seconds 30 \
        --healthy-threshold-count 2 \
        --query 'TargetGroups[0].TargetGroupArn' \
        --output text 2>/dev/null) || true

    # Handle existing target group
    if [ -z "$TG_ARN" ] || [ "$TG_ARN" = "None" ]; then
        TG_ARN=$(aws elbv2 describe-target-groups \
            --names "$TG_NAME" \
            --query 'TargetGroups[0].TargetGroupArn' \
            --output text 2>/dev/null)
    fi

    if [ -n "$TG_ARN" ] && [ "$TG_ARN" != "None" ]; then
        echo "  Target group ARN: $TG_ARN"

        # Register container with target group
        aws elbv2 register-targets \
            --target-group-arn "$TG_ARN" \
            --targets "Id=$PRIVATE_IP,Port=${APP_PORT:-3000}" \
            2>/dev/null && echo "  Registered with target group"

        # Create ALB listener rule for subdomain
        SUBDOMAIN="${SESSION_ID}.${UAT_DOMAIN_SUFFIX:-uat.teammobot.dev}"

        # Find next available priority
        EXISTING=$(aws elbv2 describe-rules \
            --listener-arn "$ALB_LISTENER_ARN" \
            --query 'Rules[*].Priority' \
            --output text 2>/dev/null | tr '\t' '\n' | grep -v default | sort -n)

        PRIORITY=10
        while echo "$EXISTING" | grep -q "^${PRIORITY}$"; do
            PRIORITY=$((PRIORITY + 1))
        done

        echo "  Creating ALB rule for $SUBDOMAIN (priority: $PRIORITY)"
        RULE_ARN=$(aws elbv2 create-rule \
            --listener-arn "$ALB_LISTENER_ARN" \
            --priority "$PRIORITY" \
            --conditions "[{\"Field\":\"host-header\",\"Values\":[\"$SUBDOMAIN\"]}]" \
            --actions "[{\"Type\":\"forward\",\"TargetGroupArn\":\"$TG_ARN\"}]" \
            --query 'Rules[0].RuleArn' \
            --output text 2>/dev/null) || true

        if [ -n "$RULE_ARN" ] && [ "$RULE_ARN" != "None" ]; then
            echo "  ALB rule created: $RULE_ARN"
            echo "  Service available at: https://$SUBDOMAIN"
        fi

        # Update DynamoDB session if configured
        if [ -n "$SESSIONS_TABLE" ]; then
            aws dynamodb update-item \
                --table-name "$SESSIONS_TABLE" \
                --key "{\"session_id\": {\"S\": \"$SESSION_ID\"}}" \
                --update-expression "SET container_ip = :ip, #st = :status, target_group_arn = :tg" \
                --expression-attribute-names '{"#st": "status"}' \
                --expression-attribute-values "{\":ip\": {\"S\": \"$PRIVATE_IP\"}, \":status\": {\"S\": \"RUNNING\"}, \":tg\": {\"S\": \"$TG_ARN\"}}" \
                2>/dev/null && echo "  Updated DynamoDB session"
        fi
    else
        echo "  Warning: Could not create target group"
    fi
else
    echo "  Skipping ALB registration (missing SESSION_ID, ALB_LISTENER_ARN, or VPC_ID)"
fi

# Wait for app process
wait $APP_PID
```

### Step 3: Create ECR Repository

```bash
aws ecr create-repository \
    --repository-name my-service \
    --region us-east-1
```

### Step 4: Build and Push Image

```bash
# Build for linux/amd64 (required for ECS Fargate)
docker build --platform linux/amd64 -t my-service .

# Login to ECR
aws ecr get-login-password --region us-east-1 | \
    docker login --username AWS --password-stdin 678954237808.dkr.ecr.us-east-1.amazonaws.com

# Push
docker tag my-service:latest 678954237808.dkr.ecr.us-east-1.amazonaws.com/my-service:latest
docker push 678954237808.dkr.ecr.us-east-1.amazonaws.com/my-service:latest
```

### Step 5: Create Task Definition

Create `task-definition.json`:

```json
{
    "family": "my-service",
    "networkMode": "awsvpc",
    "requiresCompatibilities": ["FARGATE"],
    "cpu": "512",
    "memory": "1024",
    "executionRoleArn": "arn:aws:iam::678954237808:role/claude-cloud-agent-AgentExecutionRole",
    "taskRoleArn": "arn:aws:iam::678954237808:role/claude-cloud-agent-AgentTaskRole",
    "containerDefinitions": [
        {
            "name": "my-service",
            "image": "678954237808.dkr.ecr.us-east-1.amazonaws.com/my-service:latest",
            "portMappings": [
                {"containerPort": 3000, "protocol": "tcp"}
            ],
            "environment": [
                {"name": "ALB_LISTENER_ARN", "value": "arn:aws:elasticloadbalancing:us-east-1:678954237808:listener/app/test-tickets-uat-alb/7e47b6b368ee29e1/9b650b437fde8e6f"},
                {"name": "VPC_ID", "value": "vpc-0fde49947ce39aec4"},
                {"name": "UAT_DOMAIN_SUFFIX", "value": "uat.teammobot.dev"},
                {"name": "SESSIONS_TABLE", "value": "claude-cloud-agent-sessions"},
                {"name": "APP_PORT", "value": "3000"},
                {"name": "HEALTH_CHECK_PATH", "value": "/health"}
            ],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": "/ecs/my-service",
                    "awslogs-region": "us-east-1",
                    "awslogs-stream-prefix": "ecs"
                }
            }
        }
    ]
}
```

Register the task definition:

```bash
aws ecs register-task-definition --cli-input-json file://task-definition.json
```

### Step 6: Launch the Service

**Manual launch for development:**

```bash
aws ecs run-task \
    --cluster claude-cloud-agent \
    --task-definition my-service \
    --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[subnet-016037c717cc89fb2,subnet-0ede91cdf265af677],securityGroups=[sg-0c6486526730186cb],assignPublicIp=ENABLED}" \
    --overrides '{"containerOverrides":[{"name":"my-service","environment":[{"name":"SESSION_ID","value":"my-service-dev"}]}]}'
```

Your service will be available at `https://my-service-dev.uat.teammobot.dev`

## Required IAM Permissions

The task role needs these permissions for self-registration:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "elasticloadbalancing:CreateTargetGroup",
                "elasticloadbalancing:DeleteTargetGroup",
                "elasticloadbalancing:DescribeTargetGroups",
                "elasticloadbalancing:RegisterTargets",
                "elasticloadbalancing:DeregisterTargets",
                "elasticloadbalancing:CreateRule",
                "elasticloadbalancing:DeleteRule",
                "elasticloadbalancing:DescribeRules"
            ],
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "dynamodb:UpdateItem",
                "dynamodb:GetItem"
            ],
            "Resource": "arn:aws:dynamodb:us-east-1:678954237808:table/claude-cloud-agent-sessions"
        }
    ]
}
```

**Note:** The `claude-cloud-agent-AgentTaskRole` now has an `ELBAccess` policy with these permissions (added 2026-02-03).

## Required Security Group Configuration

The container security group must allow inbound traffic from the ALB on the service port.

**ALB Security Group:** `sg-01e33c097eb569074`

If using the `claude-cloud-agent-sg` (`sg-0c6486526730186cb`), ensure it has a rule allowing TCP traffic from the ALB SG:

```bash
# Add rule if missing
aws ec2 authorize-security-group-ingress \
  --group-id sg-0c6486526730186cb \
  --protocol tcp \
  --port 3000 \
  --source-group sg-01e33c097eb569074 \
  --region us-east-1
```

**Note:** This rule was added 2026-02-03. Without it, ALB health checks will timeout.

## Cleanup

### Automatic (Idle Timeout)

The `claude-cloud-agent-idle-timeout` Lambda runs every 10 minutes and stops tasks that have been idle for 60+ minutes. It also cleans up the target group and listener rule.

### Manual

```bash
# Stop the task
aws ecs stop-task --cluster claude-cloud-agent --task <task-arn>

# Delete listener rule (find it first)
aws elbv2 describe-rules --listener-arn <listener-arn> \
    --query 'Rules[?Conditions[?Values[?contains(@, `my-service-dev`)]]]'

aws elbv2 delete-rule --rule-arn <rule-arn>

# Delete target group
aws elbv2 delete-target-group --target-group-arn <tg-arn>
```

## Migrating to Static Terraform

When your service is stable and you want a permanent deployment, migrate to Terraform:

### 1. Add to Terraform Configuration

Add to `/Users/dave/git/mobot/terraform-cloud/infrastructure/aws-projects/claude-cloud-agent/`:

**ecs.tf** (add new task definition and service):

```hcl
# Task definition for my-service
resource "aws_ecs_task_definition" "my_service" {
  family                   = "my-service"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.agent_execution_role.arn
  task_role_arn            = aws_iam_role.agent_task_role.arn

  container_definitions = jsonencode([
    {
      name  = "my-service"
      image = "678954237808.dkr.ecr.us-east-1.amazonaws.com/my-service:latest"
      portMappings = [
        { containerPort = 3000, protocol = "tcp" }
      ]
      environment = [
        # No need for ALB_LISTENER_ARN, VPC_ID - static routing handles this
        { name = "NODE_ENV", value = "production" }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.my_service.name
          awslogs-region        = "us-east-1"
          awslogs-stream-prefix = "ecs"
        }
      }
    }
  ])
}

# ECS Service (always-on)
resource "aws_ecs_service" "my_service" {
  name            = "my-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.my_service.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnets
    security_groups  = [aws_security_group.agent.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.my_service.arn
    container_name   = "my-service"
    container_port   = 3000
  }
}

# CloudWatch log group
resource "aws_cloudwatch_log_group" "my_service" {
  name              = "/ecs/my-service"
  retention_in_days = 7
}
```

**alb.tf** (add target group and listener rule):

```hcl
# Target group for my-service
resource "aws_lb_target_group" "my_service" {
  name        = "my-service-tg"
  port        = 3000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    enabled             = true
    healthy_threshold   = 2
    interval            = 30
    matcher             = "200"
    path                = "/health"
    port                = "traffic-port"
    timeout             = 5
    unhealthy_threshold = 3
  }
}

# Listener rule for my-service subdomain
resource "aws_lb_listener_rule" "my_service" {
  listener_arn = data.aws_lb_listener.uat_https.arn
  priority     = 20  # Pick a unique priority

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.my_service.arn
  }

  condition {
    host_header {
      values = ["my-service.uat.teammobot.dev"]
    }
  }
}
```

### 2. Update Container Entrypoint

Remove or skip the self-registration logic when running as a static service:

```bash
# In entrypoint.sh, add this check at the start of registration section:
if [ "$STATIC_DEPLOYMENT" = "true" ]; then
    echo "Static deployment - skipping self-registration"
else
    # ... existing registration code ...
fi
```

### 3. Deploy via Terraform

```bash
cd /Users/dave/git/mobot/terraform-cloud
git add infrastructure/aws-projects/claude-cloud-agent/
git commit -m "Add my-service static deployment"
git push origin terraform-cloud
# Review and apply in Terraform Cloud UI
```

### 4. Clean Up Dynamic Resources

After Terraform apply succeeds, manually clean up any remaining dynamic resources from development.

## Example Projects Using This Pattern

| Project | ECR Repo | Subdomain Pattern | Trigger |
|---------|----------|-------------------|---------|
| test-tickets-uat | `test-tickets-uat` | `tt-{branch}.uat.teammobot.dev` | `uat` label on PR |
| claude-agent | `claude-agent` | `{session-id}.uat.teammobot.dev` | `claude-dev` label on issue |

## Troubleshooting

### Container can't create target group

Check IAM permissions on task role. The role needs `elasticloadbalancing:CreateTargetGroup`.

### ALB rule not created

- Check `ALB_LISTENER_ARN` is correct
- Check IAM permissions include `elasticloadbalancing:CreateRule`
- Check for priority conflicts (script should auto-increment)

### Service not accessible

- Wait 30-60 seconds for target group health check to pass
- Check security group allows inbound from ALB
- Check container is listening on the expected port

### Cleanup not happening

- Idle timeout Lambda runs every 10 minutes
- Check Lambda logs: `/aws/lambda/claude-cloud-agent-idle-timeout`
- Manual cleanup commands in "Cleanup" section above
