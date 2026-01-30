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

## Staging/Production Image Workflow

The UAT system supports testing container changes before deploying to production using separate Docker image tags.

### Image Tags

| Tag | Purpose | Used By |
|-----|---------|---------|
| `:latest` | Production image | `uat` label |
| `:staging` | Testing image | `uat-staging` label |
| `:{git-sha}` | Immutable reference | Rollback/auditing |

### Workflow

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  1. Build &     │     │  2. Test with   │     │  3. Promote to  │
│  Push :staging  │ ──▶ │  uat-staging    │ ──▶ │  :latest        │
│                 │     │  label          │     │                 │
└─────────────────┘     └─────────────────┘     └─────────────────┘
     deploy.sh              GitHub PR            deploy.sh promote
      staging
```

### Step-by-Step

#### 1. Build and Push Staging Image

```bash
cd test-tickets-uat
./deploy.sh staging
```

This builds the Docker image and pushes it with two tags:
- `:staging` - For testing
- `:{git-sha}` - Immutable reference (e.g., `:16b7bc7`)

#### 2. Test with Staging Image

Add the `uat-staging` label to a PR. This:
- Creates a new ECS task definition revision with the `:staging` image
- Launches the container with the staging image
- Posts a "UAT Environment Started" comment

Verify your changes work correctly in the UAT environment.

#### 3. Promote to Production

Once testing passes:

```bash
./deploy.sh promote
```

This copies the `:staging` image manifest to `:latest` in ECR (no rebuild needed).

#### 4. Production Deployment

New UATs with the regular `uat` label will now use the updated `:latest` image.

### Deploy Script Reference

```bash
./deploy.sh staging   # Build and push :staging (default)
./deploy.sh promote   # Copy :staging to :latest
./deploy.sh latest    # Build and push directly to :latest (emergency hotfix)
```

### How It Works (Technical Details)

The `ecs_launcher.py` handles image tag selection:

1. **For `uat` label (`image_tag="latest"`):**
   - Uses the base task definition (`test-tickets-uat`) which has `:latest` image
   - No new task definition revision created

2. **For `uat-staging` label (`image_tag="staging"`):**
   - Calls `_register_task_definition_with_image()` to create a new task definition revision
   - The new revision has the `:staging` image baked in
   - Launches task with the new revision

This approach is required because ECS `run-task` API does not support overriding the container image via `containerOverrides`. The image must be set at the task definition level.

### Labels Reference

| Label | Image Tag | Task Definition | Use Case |
|-------|-----------|-----------------|----------|
| `uat` | `:latest` | Base revision | Production testing |
| `uat-staging` | `:staging` | New revision | Container changes testing |
| `claude-dev` | `:latest` | Base revision | AI-assisted development |

### Rollback

If a promoted image has issues:

```bash
# Find the previous working image SHA
aws ecr describe-images --repository-name test-tickets-uat \
  --query 'imageDetails[*].{tags:imageTags,pushed:imagePushedAt}' \
  --output table

# Re-tag a known good SHA as :latest
aws ecr batch-get-image --repository-name test-tickets-uat \
  --image-ids imageTag={known-good-sha} \
  --query 'images[0].imageManifest' --output text > /tmp/manifest.json

aws ecr put-image --repository-name test-tickets-uat \
  --image-tag latest --image-manifest file:///tmp/manifest.json
```

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

### Update Docker Image (Recommended: Staging Workflow)

Always use the staging workflow for container changes:

```bash
cd test-tickets-uat

# 1. Build and push to :staging
./deploy.sh staging

# 2. Test with uat-staging label on a PR

# 3. After testing passes, promote to production
./deploy.sh promote
```

### Update Docker Image (Emergency Hotfix)

For urgent fixes that can't wait for staging testing:

```bash
cd test-tickets-uat
./deploy.sh latest   # Builds and pushes directly to :latest
```

### Update Task Definition

Only needed when changing CPU, memory, IAM roles, or secrets:

```bash
aws ecs register-task-definition --cli-input-json file://task-def-v5.json
```

Note: Image changes don't require updating the base task definition. The staging workflow creates new revisions automatically.

### Update Lambda

```bash
# Create deployment package with dependencies
mkdir -p /tmp/webhook-deploy
unzip -q webhook-lambda.zip -d /tmp/webhook-deploy
cp webhook/*.py /tmp/webhook-deploy/
cd /tmp/webhook-deploy && zip -q -r /tmp/webhook-updated.zip .

# Deploy
aws lambda update-function-code \
  --function-name claude-cloud-agent-webhook \
  --zip-file fileb:///tmp/webhook-updated.zip
```

### IAM Permissions for Staging Workflow

The Lambda role needs these additional ECS permissions for the staging workflow:

```json
{
  "Effect": "Allow",
  "Action": [
    "ecs:DescribeTaskDefinition",
    "ecs:RegisterTaskDefinition"
  ],
  "Resource": "*"
}
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

### Staging Image Not Used

If `uat-staging` label doesn't use the staging image:

1. Check Lambda has `ecs:DescribeTaskDefinition` and `ecs:RegisterTaskDefinition` permissions
2. Check CloudWatch logs for Lambda errors
3. Verify `:staging` tag exists in ECR:
   ```bash
   aws ecr describe-images --repository-name test-tickets-uat \
     --image-ids imageTag=staging
   ```

### "Unknown parameter: image" Error

This error means the Lambda code is outdated. The old code tried to override the image via `containerOverrides` which ECS doesn't support. Deploy the latest Lambda code:

```bash
# The fix registers a new task definition revision instead
cd /tmp && mkdir webhook-deploy && cd webhook-deploy
unzip -q /path/to/webhook-lambda.zip
cp /path/to/webhook/*.py .
zip -q -r ../webhook-updated.zip .
aws lambda update-function-code \
  --function-name claude-cloud-agent-webhook \
  --zip-file fileb:///tmp/webhook-updated.zip
```

### GitHub Comment Not Posted

If the "UAT Environment Started" comment doesn't appear:

1. Check container logs for HTTP status code:
   ```bash
   aws logs filter-log-events \
     --log-group-name /ecs/test-tickets-uat \
     --log-stream-name "uat/test-tickets/<task-id>" \
     --filter-pattern "GitHub"
   ```

2. Common issues:
   - `401`: GITHUB_TOKEN expired or invalid
   - `400`: JSON body malformed (check escaping)
   - `404`: Wrong repo or PR number

3. The container uses Python's `json.dumps()` for proper JSON escaping of the comment body

## Container Endpoints

The prompt server (port 8080) exposes these endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check (status, queue length, processing state) |
| `/status` | GET | Queue status and work directory |
| `/version` | GET | Git commit info of running code |
| `/prompt` | POST | Submit a prompt for Claude processing |

### Version Endpoint

Use `/version` to verify which code is running:

```bash
curl https://tt-{branch}.uat.teammobot.dev:8080/version
```

Response:
```json
{
  "commit": "f2a93bf142841cec723122b4f3b0428bd65aa607",
  "shortCommit": "f2a93bf",
  "branch": "add-toast-notifications",
  "commitDate": "2026-01-30 17:56:20 -0500",
  "commitMessage": "Add toast notification system",
  "workDir": "/app/repo",
  "nodeVersion": "v20.20.0",
  "uptime": 103.19
}
```

Note: This shows the git commit of the **cloned branch**, not the container image version. The container image version is visible in the ECS task definition.

## Files Reference

| File | Purpose |
|------|---------|
| `webhook/handler.py` | GitHub webhook routing and UAT lifecycle |
| `webhook/ecs_launcher.py` | ECS task launching and task definition management |
| `webhook/session_manager.py` | DynamoDB session CRUD |
| `webhook/github_client.py` | GitHub API client |
| `test-tickets-uat/Dockerfile` | Container image definition |
| `test-tickets-uat/entrypoint.sh` | Container startup script (cloning, ALB setup, GitHub comments) |
| `test-tickets-uat/prompt-server.js` | HTTP API for health, status, version, and prompts |
| `test-tickets-uat/deploy.sh` | Build and deployment script (staging/promote/latest) |
| `task-def-v5.json` | ECS task definition |

## Limitations

- **Build Required:** The branch must build successfully (no TypeScript errors)
- **Single Container:** One container per branch (no horizontal scaling)
- **No Database Isolation:** All UAT environments share the staging database
- **Session ID Length:** Limited to 50 characters (subdomain constraints)
- **Target Group Name:** Limited to 32 characters (AWS constraint)
