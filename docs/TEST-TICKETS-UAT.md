# test_tickets UAT Implementation

Ephemeral UAT environments for `team-mobot/test_tickets`, deployed automatically when PRs are labeled.

## Overview

When a PR in the `test_tickets` repository is labeled with `uat`, the system automatically:

1. Clones the branch
2. Builds the frontend and backend
3. Spins up an ECS container
4. Creates dedicated ALB routing
5. Posts the UAT URL to the PR

The environment is accessible at `https://tt-{branch-name}.uat.teammobot.dev` and authenticates against staging (`app.teammobot.dev`).

## Quick Start

1. Open a PR in `team-mobot/test_tickets`
2. Add the `uat` label
3. Wait for the bot comment with the UAT URL
4. Access `https://tt-{branch-name}.uat.teammobot.dev`

To stop: Remove the `uat` label or close/merge the PR.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              GitHub                                         │
│  PR labeled "uat" ──webhook──→ Lambda (webhook handler)                    │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              AWS                                            │
│                                                                             │
│   Lambda                    ECS Fargate                    DynamoDB         │
│   ┌──────────┐             ┌──────────────┐              ┌──────────┐      │
│   │ webhook  │──run task──▶│ test-tickets │──register───▶│ sessions │      │
│   │ handler  │             │ container    │              │  table   │      │
│   └──────────┘             └──────┬───────┘              └──────────┘      │
│                                   │                                         │
│                                   │ creates target group & ALB rule         │
│                                   ▼                                         │
│                            ┌──────────────┐                                 │
│                            │     ALB      │                                 │
│                            │ (host-based  │                                 │
│                            │  routing)    │                                 │
│                            └──────────────┘                                 │
│                                   │                                         │
└───────────────────────────────────│─────────────────────────────────────────┘
                                    │
                                    ▼
                    https://tt-{branch}.uat.teammobot.dev
```

## Components

### Lambda Webhook Handler

**File:** `webhook/handler.py`

Handles GitHub webhook events:

| Event | Action | Handler |
|-------|--------|---------|
| `pull_request.labeled` | `uat` label added | `handle_pr_labeled()` |
| `pull_request.unlabeled` | `uat` label removed | `handle_pr_unlabeled_or_closed()` |
| `pull_request.closed` | PR closed/merged | `handle_pr_unlabeled_or_closed()` |
| `issues.labeled` | `uat` label added | `handle_test_tickets_uat()` |
| `issues.unlabeled` | `uat` label removed | `handle_issue_unlabeled_or_closed()` |
| `issues.closed` | Issue closed | `handle_issue_unlabeled_or_closed()` |

### ECS Task Definition

**File:** `task-def-v5.json`

- **Family:** `test-tickets-uat`
- **CPU:** 1024 (1 vCPU)
- **Memory:** 2048 MB
- **Port:** 3001

**Environment Variables:**
| Variable | Purpose |
|----------|---------|
| `SESSION_ID` | Unique identifier (e.g., `tt-feature-branch`) |
| `BRANCH` | Git branch to clone |
| `REPO` | Repository (`team-mobot/test_tickets`) |
| `GITHUB_TOKEN` | For cloning private repo |
| `SESSIONS_TABLE` | DynamoDB table name |
| `ALB_LISTENER_ARN` | For creating routing rules |
| `VPC_ID` | For creating target groups |
| `UAT_DOMAIN_SUFFIX` | `uat.teammobot.dev` |

**Secrets (from Secrets Manager):**
- `DATABASE_URL` - PostgreSQL connection string
- `JWT_SECRET` - For session tokens
- `MOBOT_JWS_SECRET` - For Mobot API auth
- `GOOGLE_CLIENT_ID` - OAuth client ID
- `GCP_PROJECT_ID` - For Vertex AI
- `GEMINI_API_KEY` - For AI features
- `GOOGLE_APPLICATION_CREDENTIALS_JSON` - GCP service account

### Docker Image

**Files:** `test-tickets-uat/Dockerfile`, `test-tickets-uat/entrypoint.sh`

The container:
1. Clones the repository branch
2. Installs frontend dependencies (`npm ci`)
3. Builds frontend with Vite (exports `VITE_GOOGLE_CLIENT_ID`)
4. Installs and builds server
5. Copies frontend build to server's public directory
6. Creates session-specific ALB target group
7. Registers container IP with target group
8. Creates ALB listener rule for subdomain routing
9. Updates DynamoDB session record
10. Starts the Node.js server on port 3001

### ALB Routing

Each UAT session gets:
- **Target Group:** `{session-id}-tg` (truncated to 32 chars)
- **Listener Rule:** Host-header match for `{session-id}.uat.teammobot.dev`

This ensures requests to each subdomain route to the correct container, even when multiple UAT environments are running.

## Session Lifecycle

```
┌─────────┐     ┌──────────┐     ┌─────────┐     ┌─────────┐
│ PENDING │ ──▶ │ STARTING │ ──▶ │ RUNNING │ ──▶ │ STOPPED │
└─────────┘     └──────────┘     └─────────┘     └─────────┘
                     │                               ▲
                     │           ┌────────┐          │
                     └─────────▶ │ FAILED │ ─────────┘
                                 └────────┘
```

1. **PR Labeled:** Lambda creates session record (STARTING), launches ECS task
2. **Container Starts:** Clones repo, builds app, creates ALB routing
3. **Container Ready:** Updates DynamoDB (RUNNING), server listening on 3001
4. **PR Unlabeled/Closed:** Lambda stops task, cleans up ALB resources (STOPPED)
5. **Build Failure:** Container exits, session remains STARTING/FAILED

## Configuration

### AWS Resources

| Resource | ARN/ID |
|----------|--------|
| ECS Cluster | `claude-cloud-agent` |
| Task Definition | `test-tickets-uat` |
| ALB | `test-tickets-uat-alb` |
| ALB Listener | `arn:aws:elasticloadbalancing:us-east-1:678954237808:listener/app/test-tickets-uat-alb/7e47b6b368ee29e1/9b650b437fde8e6f` |
| VPC | `vpc-0fde49947ce39aec4` |
| Sessions Table | `claude-cloud-agent-sessions` |
| Secrets | `test-tickets/uat/agnts-0-aViArH` |
| ECR Repository | `678954237808.dkr.ecr.us-east-1.amazonaws.com/test-tickets-uat` |

### IAM Permissions

**Task Role** (`test-tickets-ecs-task-role`) needs:
```json
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
    "elasticloadbalancing:DescribeRules",
    "elasticloadbalancing:DescribeTargetHealth"
  ],
  "Resource": "*"
}
```

Plus DynamoDB write access to the sessions table.

### Lambda Environment Variables

| Variable | Value |
|----------|-------|
| `TEST_TICKETS_TASK_DEFINITION` | `test-tickets-uat` |
| `TEST_TICKETS_SECRET_ARN` | `arn:aws:secretsmanager:...` |
| `ALB_LISTENER_ARN` | `arn:aws:elasticloadbalancing:...` |
| `UAT_DOMAIN_SUFFIX` | `uat.teammobot.dev` |

## Deployment

### Update Docker Image

```bash
cd test-tickets-uat

# Build for AMD64 (required for ECS Fargate)
docker buildx build --platform linux/amd64 -t test-tickets-uat:latest .

# Tag and push to ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 678954237808.dkr.ecr.us-east-1.amazonaws.com
docker tag test-tickets-uat:latest 678954237808.dkr.ecr.us-east-1.amazonaws.com/test-tickets-uat:latest
docker push 678954237808.dkr.ecr.us-east-1.amazonaws.com/test-tickets-uat:latest
```

### Update Task Definition

```bash
aws ecs register-task-definition --cli-input-json file://task-def-v5.json
```

### Update Lambda

```bash
cd webhook-deploy
unzip -q ../webhook-v3.zip
cp ../webhook/*.py .
zip -q -r ../webhook-v4.zip .
aws lambda update-function-code \
  --function-name claude-cloud-agent-webhook \
  --zip-file fileb://webhook-v4.zip
```

## Troubleshooting

### UAT Shows 502 Bad Gateway

**Check session status:**
```bash
aws dynamodb get-item \
  --table-name claude-cloud-agent-sessions \
  --key '{"session_id": {"S": "tt-your-branch"}}'
```

**Check ECS task:**
```bash
aws ecs describe-tasks \
  --cluster claude-cloud-agent \
  --tasks <task-arn>
```

**Check container logs:**
```bash
aws logs get-log-events \
  --log-group-name /ecs/test-tickets-uat \
  --log-stream-name "uat/test-tickets/<task-id>"
```

### Build Fails

The branch must compile without TypeScript errors. Check logs for:
- `error TS` - TypeScript compilation errors
- `npm ERR!` - Dependency issues
- `cp: can't stat '../dist/*'` - Frontend build failed

### Wrong Content Served (MIME Type Errors)

If JS files return HTML, the ALB rule may be missing:
```bash
aws elbv2 describe-rules \
  --listener-arn <listener-arn> \
  --query 'Rules[*].{Priority:Priority,Host:Conditions[0].Values[0]}'
```

### Cleanup Stuck Resources

Manually delete target group and rule:
```bash
# Find and delete rule
aws elbv2 describe-rules --listener-arn <listener-arn>
aws elbv2 delete-rule --rule-arn <rule-arn>

# Delete target group
aws elbv2 delete-target-group --target-group-arn <tg-arn>
```

## Files Reference

| File | Purpose |
|------|---------|
| `webhook/handler.py` | GitHub webhook routing and UAT lifecycle |
| `webhook/ecs_launcher.py` | ECS task launching |
| `webhook/session_manager.py` | DynamoDB session CRUD |
| `webhook/github_client.py` | GitHub API client |
| `test-tickets-uat/Dockerfile` | Container image definition |
| `test-tickets-uat/entrypoint.sh` | Container startup script |
| `task-def-v5.json` | ECS task definition |

## Limitations

- **Build Required:** The branch must build successfully (no TypeScript errors)
- **Single Container:** One container per branch (no horizontal scaling)
- **No Database Isolation:** All UAT environments share the staging database
- **Session ID Length:** Limited to 50 characters (subdomain constraints)
- **Target Group Name:** Limited to 32 characters (AWS constraint)
