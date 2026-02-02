# Claude Cloud Agent

Infrastructure for running Claude Code as an autonomous development agent, triggered by GitHub issues/PRs.

## Repository Structure

```
agent/                    # Claude agent container (Python)
  main.py                 # Agent orchestrator entry point
  claude_runner.py        # Runs Claude Code CLI with streaming
  github_reporter.py      # Posts status updates to GitHub PRs
  session_reporter.py     # Updates DynamoDB session state
  dev_server.py           # Manages project dev servers
  api_server.py           # HTTP API for receiving prompts
  Dockerfile              # Container build
  entrypoint.sh           # Container startup

webhook/                  # Lambda webhook handler
  handler.py              # Main webhook routing
  ecs_launcher.py         # ECS task launching
  session_manager.py      # DynamoDB session CRUD
  github_client.py        # GitHub API client
  jira_client.py          # JIRA API client

test-tickets-uat/         # UAT container for test_tickets repo
  Dockerfile              # Container image
  entrypoint.sh           # Container startup (clone, build, ALB setup)
  prompt-server.js        # HTTP API for prompts
  deploy.sh               # Build/deploy script

docs/
  DESIGN.md               # Architecture and design decisions
  TEST-TICKETS-UAT.md     # Full UAT documentation
```

## GitHub App Configuration

The webhook is triggered by a GitHub App. Use the **custom domain URL** (stable across infrastructure rebuilds):

| Setting | Value |
|---------|-------|
| Webhook URL | `https://webhook.uat.teammobot.dev/webhook` |
| Content type | `application/json` |
| Secret | Stored in `claude-dev/github-app` Secrets Manager |
| Events | Issues (labeled), Pull requests (labeled) |

**To update:** GitHub → Settings → Developer settings → GitHub Apps → claude-dev → Webhook

> **Note:** The raw API Gateway URL changes when infrastructure is destroyed/recreated. Always use the custom domain `webhook.uat.teammobot.dev`.

## Key Workflows

### 1. GitHub Issue → Claude Agent

When `claude-dev` label is added to a GitHub issue:
1. Lambda creates branch `claude/{issue-number}` and draft PR
2. ECS task launches with `claude-agent` container
3. Agent processes issue body, posts updates to PR
4. Container runs dev server at `{session-id}.uat.teammobot.dev`

### 2. test_tickets UAT Environments

When `uat` or `uat-staging` label is added to a PR in `team-mobot/test_tickets`:
1. Lambda launches `test-tickets-uat` container
2. Container clones branch, builds app, creates ALB routing
3. App available at `tt-{branch}.uat.teammobot.dev`

**Labels:**
- `uat` → Uses `:latest` image (production)
- `uat-staging` → Uses `:staging` image (testing container changes)

## Deploying Changes

### Lambda (webhook handler)

The Lambda **configuration** (env vars, IAM, timeout, etc.) is managed by Terraform. Only deploy **code changes** manually.

**Deployment directory:** `/Users/dave/git/claude-cloud-agent/version-two/webhook-deploy/`

This directory contains pre-built Linux dependencies (requests, jwt, cryptography, etc.). Never rebuild from scratch on macOS.

```bash
# 1. Copy updated Python files to deployment directory
cp /Users/dave/git/claude-cloud-agent/main/webhook/*.py \
   /Users/dave/git/claude-cloud-agent/version-two/webhook-deploy/

# 2. Create deployment zip
cd /Users/dave/git/claude-cloud-agent/version-two/webhook-deploy
zip -r ../webhook-lambda-new.zip . -x "*.pyc" -x "__pycache__/*" -x "*.DS_Store"

# 3. Deploy webhook Lambda
cd /Users/dave/git/claude-cloud-agent/version-two
aws lambda update-function-code \
  --function-name claude-cloud-agent-webhook \
  --zip-file fileb://webhook-lambda-new.zip \
  --region us-east-1

# 4. Deploy idle-timeout Lambda (uses same zip - contains idle_timeout.py)
aws lambda update-function-code \
  --function-name claude-cloud-agent-idle-timeout \
  --zip-file fileb://webhook-lambda-new.zip \
  --region us-east-1
```

**Important:** To change Lambda environment variables or configuration, update Terraform in `lambda.tf`, not the AWS console.

### test-tickets-uat Container

Always use the staging workflow:

```bash
cd test-tickets-uat

# 1. Build and push to :staging
./deploy.sh staging

# 2. Test with 'uat-staging' label on a PR

# 3. Promote to production
./deploy.sh promote
```

See `test-tickets-uat/DEPLOY.md` for details.

## AWS Resources

| Resource | Name/ARN |
|----------|----------|
| Lambda (webhook) | `claude-cloud-agent-webhook` |
| Lambda (idle cleanup) | `claude-cloud-agent-idle-timeout` |
| ECS Cluster | `claude-cloud-agent` |
| Agent Task Def | `claude-agent` |
| UAT Proxy Task Def | `claude-cloud-agent-uat-proxy` |
| Sessions Table | `claude-cloud-agent-sessions` |
| GitHub Secret | `claude-dev/github-app` |
| Webhook Domain | `webhook.uat.teammobot.dev` |
| ECR (agent) | `claude-agent` |
| ECR (UAT proxy) | `claude-cloud-agent-uat-proxy` |

## Environment Variables (Lambda)

### Webhook Lambda (`claude-cloud-agent-webhook`)

| Variable | Purpose |
|----------|---------|
| `ECS_CLUSTER` | ECS cluster name |
| `AGENT_TASK_DEFINITION` | Task def for claude-agent |
| `TEST_TICKETS_TASK_DEFINITION` | Task def for test-tickets-uat |
| `SESSIONS_TABLE` | DynamoDB table |
| `GITHUB_APP_SECRET_ARN` | GitHub App credentials |
| `JIRA_SECRET_ARN` | JIRA API credentials (optional) |
| `ALB_LISTENER_ARN` | For UAT routing rules |
| `UAT_DOMAIN_SUFFIX` | `uat.teammobot.dev` |
| `JIRA_TRIGGER_KEYWORD` | `@claude` |
| `JIRA_TRIGGER_LABEL` | `claude-dev` |

### Idle Timeout Lambda (`claude-cloud-agent-idle-timeout`)

| Variable | Purpose |
|----------|---------|
| `SESSIONS_TABLE` | DynamoDB table to scan for idle sessions |
| `ALB_LISTENER_ARN` | For cleaning up routing rules |
| `IDLE_TIMEOUT_MINUTES` | Minutes before session is considered idle (default: 60) |

## Cleaning Up Test Resources

```bash
# Stop all non-service ECS tasks
aws ecs list-tasks --cluster claude-cloud-agent --desired-status RUNNING

# Delete DynamoDB sessions
aws dynamodb scan --table-name claude-cloud-agent-sessions

# Close test PRs
gh pr list --repo team-mobot/test_tickets --state open
```

## Infrastructure as Code (Terraform)

The infrastructure is fully managed via Terraform Cloud. **All infrastructure changes should go through Terraform** - do not create/modify AWS resources manually.

### Terraform Cloud Workspace

| Setting | Value |
|---------|-------|
| Organization | `mobot` |
| Workspace | `aws-projects__claude-cloud-agent` |
| Workspace ID | `ws-3zmsw77ih3j4YVoT` |
| URL | https://app.terraform.io/app/mobot/workspaces/aws-projects__claude-cloud-agent |
| VCS Repo | `team-mobot/mobot` |
| Branch | `terraform-cloud` |
| Working Directory | `infrastructure/aws-projects/claude-cloud-agent` |
| Terraform Version | `1.12.2` |

### Terraform Source Location

**Feature branch (for editing):** `/Users/dave/git/mobot/claude-cloude-agent-infra/infrastructure/aws-projects/claude-cloud-agent/`

**Deploy branch (worktree):** `/Users/dave/git/mobot/terraform-cloud/infrastructure/aws-projects/claude-cloud-agent/`

### Terraform-Managed Resources

All of these resources are managed by Terraform. Do not modify manually:

| Resource Type | Name |
|---------------|------|
| Lambda Functions | `claude-cloud-agent-webhook`, `claude-cloud-agent-idle-timeout` |
| API Gateway HTTP API | `claude-cloud-agent-webhook` (ID changes on recreate) |
| EventBridge Rule | `claude-agent-idle-timeout-schedule` (runs every 10 min) |
| ECS Cluster | `claude-cloud-agent` |
| ECS Service | `claude-cloud-agent-uat-proxy` |
| ECS Task Definitions | `claude-agent`, `claude-cloud-agent-uat-proxy` |
| DynamoDB Table | `claude-cloud-agent-sessions` |
| Target Group | `claude-cloud-agent-uat-proxy` |
| Listener Rule | Priority 10 on `test-tickets-uat-alb` |
| Security Groups | `claude-cloud-agent-uat-proxy-sg`, `claude-cloud-agent-sg` |
| IAM Roles | `claude-cloud-agent-AgentExecutionRole`, `claude-cloud-agent-AgentTaskRole`, `claude-cloud-agent-ProxyExecutionRole`, `claude-cloud-agent-UatProxyTaskRole`, `claude-cloud-agent-WebhookLambdaRole`, `claude-agent-idle-timeout-role` |
| CloudWatch Log Groups | `/ecs/claude-cloud-agent`, `/ecs/claude-cloud-agent-uat-proxy`, `/aws/lambda/claude-cloud-agent-webhook`, `/aws/lambda/claude-cloud-agent-idle-timeout` |
| API Gateway Custom Domain | `webhook.uat.teammobot.dev` |
| Route 53 Record | `webhook.uat.teammobot.dev` → API Gateway |

**Webhook URL (stable):** `https://webhook.uat.teammobot.dev/webhook`

### Workspace Environment Variables

AWS credentials are configured as **sensitive environment variables** in the Terraform Cloud workspace:
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`

To update credentials: Go to workspace → Variables → Edit

### Key Architecture Facts

1. Infrastructure runs in **test-tickets VPC** (`vpc-0fde49947ce39aec4`), NOT aws0
2. Uses existing `test-tickets-uat-alb` (DNS `*.uat.teammobot.dev` points there)
3. Proxy env var must be `SESSIONS_TABLE` (not `SESSION_TABLE`)
4. Security group must allow traffic from ALB SG `sg-01e33c097eb569074`
5. Agent task definition container must be named `claude-agent` (Lambda code expects this name for overrides)

### Terraform Deployment Workflow

```bash
# 1. Make changes on feature branch
cd /Users/dave/git/mobot/claude-cloude-agent-infra
# edit files in infrastructure/aws-projects/claude-cloud-agent/
git add infrastructure/aws-projects/claude-cloud-agent/
git commit -m "Description of change"

# 2. Merge to terraform-cloud branch (separate worktree)
cd /Users/dave/git/mobot/terraform-cloud
git merge claude-cloude-agent-infra -m "Merge: description"

# 3. Push to trigger Terraform Cloud
git push origin terraform-cloud

# 4. Review plan at Terraform Cloud UI
# 5. Click "Confirm & Apply"
```

### Using Terraform Cloud MCP

Claude Code has the `terraform-cloud` MCP server configured for interacting with Terraform Cloud:

```bash
# Verify MCP server is connected
claude mcp list

# Available tools include:
# - mcp__terraform-cloud__list_workspaces
# - mcp__terraform-cloud__get_workspace_details
# - mcp__terraform-cloud__create_run
# - mcp__terraform-cloud__apply_run
# - mcp__terraform-cloud__get_run_details
# - mcp__terraform-cloud__get_plan_logs
# - mcp__terraform-cloud__get_apply_logs
```

See `docs/INFRASTRUCTURE-AUDIT-2026-02-02.md` for full audit details.

### Testing Infrastructure (Destroy/Recreate)

To fully test that Terraform can recreate infrastructure from scratch:

**Option 1: Using Terraform Cloud MCP (recommended)**

```bash
# Claude Code can use MCP tools directly:

# 1. Create destroy run
mcp__terraform-cloud__create_run
  workspace_id: ws-3zmsw77ih3j4YVoT
  params: {"is-destroy": true, "message": "Full infrastructure destroy for testing"}

# 2. Confirm/apply the destroy (after plan completes)
mcp__terraform-cloud__apply_run
  run_id: <run-id from step 1>

# 3. Create apply run to recreate
mcp__terraform-cloud__create_run
  workspace_id: ws-3zmsw77ih3j4YVoT
  params: {"message": "Recreate all infrastructure"}

# 4. Confirm/apply the recreate
mcp__terraform-cloud__apply_run
  run_id: <run-id from step 3>

# 5. Redeploy Lambda code (see "Deploying Changes" section above)
```

**Option 2: Using Terraform Cloud UI**

1. Go to https://app.terraform.io/app/mobot/workspaces/aws-projects__claude-cloud-agent
2. Actions → Start new run → Destroy all resources
3. Review plan and confirm
4. After destroy completes, create another run to apply
5. Redeploy Lambda code (see "Deploying Changes" section above)

**Post-recreate verification:**

```bash
# Test webhook endpoint
curl -s -X POST https://webhook.uat.teammobot.dev/webhook \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: ping" \
  -d '{"zen": "test"}'
# Should return: {"error": "Invalid signature"} (expected - no valid signature)

# Test end-to-end by creating issue with claude-dev label
gh issue create --repo team-mobot/test_tickets \
  --title "Test: Infrastructure validation" \
  --body "Testing webhook after infrastructure recreate" \
  --label "claude-dev"

# Check Lambda logs
aws logs tail /aws/lambda/claude-cloud-agent-webhook --since 5m --region us-east-1
```

**What survives destroy/recreate:**

| Resource | Survives? | Notes |
|----------|-----------|-------|
| Webhook URL | ✅ Yes | Custom domain `webhook.uat.teammobot.dev` persists |
| ACM Certificate | ✅ Yes | `*.uat.teammobot.dev` not managed by this Terraform |
| ECR Images | ✅ Yes | Container images stored separately |
| Secrets Manager | ✅ Yes | `claude-dev/github-app` not managed here |
| GitHub App config | ✅ Yes | Webhook URL doesn't change |
| Lambda code | ❌ No | Must redeploy after recreate (Terraform creates placeholder) |
| DynamoDB data | ❌ No | Sessions table is recreated empty |
| CloudWatch logs | ❌ No | Log groups recreated (old logs deleted) |

**Last tested:** 2026-02-02 (destroy run-CYtYfcvREsCJbUCk, recreate run-ve3XzHbQmJhrUXai)

### Idle Timeout Lambda

The `claude-cloud-agent-idle-timeout` Lambda runs every 10 minutes to stop ECS tasks that have been idle for 60+ minutes. This prevents runaway costs from forgotten containers.

- **Schedule:** Every 10 minutes (EventBridge rule)
- **Timeout threshold:** 60 minutes of inactivity
- **Actions:** Stops ECS tasks, cleans up ALB rules, updates DynamoDB sessions

---

## Known Issues

### Lambda Deployment Notes

The Lambda requires Linux binaries for cryptography. The deployment directory at `/Users/dave/git/claude-cloud-agent/version-two/webhook-deploy/` contains pre-built Linux dependencies. Never rebuild from scratch on macOS - always copy Python files to this directory and create the zip from there.

### VITE_GOOGLE_CLIENT_ID Configuration (Fixed 2026-02-02)

The `test_tickets` app requires `VITE_GOOGLE_CLIENT_ID` for Google OAuth. Two containers may run this app:

1. **claude-agent** - Generic agent triggered by `claude-dev` label on issues
2. **test-tickets-uat** - Specific UAT container triggered by `uat` label on PRs

**How it works:**
- Both task definitions inject `VITE_GOOGLE_CLIENT_ID` from `mobot-agents/staging` Secrets Manager secret
- The `claude-agent` gets it directly as `VITE_GOOGLE_CLIENT_ID` env var (inherited by npm process)
- The `test-tickets-uat` gets `GOOGLE_CLIENT_ID` and exports it as `VITE_GOOGLE_CLIENT_ID` in entrypoint.sh

**Terraform changes made:**
- `terraform.tfvars`: Set `test_tickets_secret_arn` to `mobot-agents/staging` secret ARN
- `ecs.tf`: Added `VITE_GOOGLE_CLIENT_ID` secret to `claude-agent` task definition
- `ecs.tf`: Added `GOOGLE_CLIENT_ID` secret to `test-tickets-uat` task definition
- `iam.tf`: Added `mobot-agents/staging` secret access to agent execution role

**If you see "Configuration Error - VITE_GOOGLE_CLIENT_ID is not configured":**
1. Verify `test_tickets_secret_arn` is set in `terraform.tfvars`
2. Verify the secret contains the `GOOGLE_CLIENT_ID` key
3. Run `terraform apply` to update task definitions
4. New tasks will get the env var; existing tasks need to be restarted

**Verified working:** 2026-02-02, session `8c6b2de8` loaded https://8c6b2de8.uat.teammobot.dev/ without error

### Infrastructure Drift (Fully Resolved 2026-02-02)

The original CloudFormation ALB was deleted outside of IaC, causing UAT proxy failures. This has been fully resolved:
1. Migrated all infrastructure to Terraform Cloud
2. Tore down all manually-created resources
3. Terraform now manages everything from scratch
4. Uses existing `test-tickets-uat-alb` (shared with test-tickets project)
5. Full destroy/recreate test passed 2026-02-02

All infrastructure is now fully managed by Terraform Cloud workspace `aws-projects__claude-cloud-agent`.
