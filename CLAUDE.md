# Claude Cloud Agent

Infrastructure for running Claude Code as an autonomous development agent, triggered by GitHub issues/PRs.

## Repository Structure

```
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

The webhook is triggered by a GitHub App. The webhook URL must point to the API Gateway endpoint:

| Setting | Value |
|---------|-------|
| Webhook URL | `https://emolxuoaf7.execute-api.us-east-1.amazonaws.com/webhook` |
| Content type | `application/json` |
| Secret | Stored in `claude-dev/github-app` Secrets Manager |
| Events | Issues (labeled), Pull requests (labeled) |

**To update:** GitHub → Settings → Developer settings → GitHub Apps → claude-dev → Webhook

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

The Lambda **configuration** (env vars, IAM, timeout, etc.) is managed by Terraform. Only deploy **code changes** manually:

```bash
# Copy updated files to deployment directory
cp webhook/*.py /path/to/version-two/webhook-deploy/

# Create zip and deploy
cd /path/to/version-two/webhook-deploy
zip -r ../webhook-lambda.zip .
aws lambda update-function-code \
  --function-name claude-cloud-agent-webhook \
  --zip-file fileb://../webhook-lambda.zip
```

The deployment directory must contain dependencies (requests, jwt, cryptography, etc.) - don't create a fresh zip from just the .py files.

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
| Lambda | `claude-cloud-agent-webhook` |
| ECS Cluster | `claude-cloud-agent` |
| Agent Task Def | `claude-agent` |
| UAT Task Def | `test-tickets-uat` |
| Sessions Table | `claude-cloud-agent-sessions` |
| GitHub Secret | `claude-dev/github-app` |
| UAT Secret | `test-tickets/uat/agnts-0` |
| ECR (agent) | `claude-agent` |
| ECR (UAT) | `test-tickets-uat` |

## Environment Variables (Lambda)

| Variable | Purpose |
|----------|---------|
| `ECS_CLUSTER` | ECS cluster name |
| `AGENT_TASK_DEFINITION` | Task def for claude-agent |
| `TEST_TICKETS_TASK_DEFINITION` | Task def for test-tickets-uat |
| `SESSIONS_TABLE` | DynamoDB table |
| `GITHUB_APP_SECRET_ARN` | GitHub App credentials |
| `ALB_LISTENER_ARN` | For UAT routing rules |
| `UAT_DOMAIN_SUFFIX` | `uat.teammobot.dev` |

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
| Lambda Function | `claude-cloud-agent-webhook` |
| API Gateway HTTP API | `claude-cloud-agent-webhook` (ID: `emolxuoaf7`) |
| ECS Cluster | `claude-cloud-agent` |
| ECS Service | `claude-cloud-agent-uat-proxy` |
| ECS Task Definitions | `claude-cloud-agent`, `claude-cloud-agent-uat-proxy` |
| DynamoDB Table | `claude-cloud-agent-sessions` |
| Target Group | `claude-cloud-agent-uat-proxy` |
| Listener Rule | Priority 10 on `test-tickets-uat-alb` |
| Security Groups | `claude-cloud-agent-uat-proxy`, `claude-cloud-agent-agent` |
| IAM Roles | `claude-cloud-agent-AgentExecutionRole`, `claude-cloud-agent-AgentTaskRole`, `claude-cloud-agent-ProxyExecutionRole`, `claude-cloud-agent-UatProxyTaskRole`, `claude-cloud-agent-WebhookLambdaRole` |
| CloudWatch Log Groups | `/ecs/claude-cloud-agent`, `/ecs/claude-cloud-agent-uat-proxy`, `/aws/lambda/claude-cloud-agent-webhook` |

**Webhook API Endpoint:** `https://emolxuoaf7.execute-api.us-east-1.amazonaws.com`

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

---

## Known Issues

### claude-agent Container Source

The `claude-agent` ECS container uses Python modules (`session_reporter.py`, `github_reporter.py`, `claude_runner.py`) but the source location is unknown. The `/Users/dave/git/cca/agent/` directory has a bash-based entrypoint that doesn't match the deployed container.

### Lambda Deployment

The Lambda requires Linux binaries for cryptography. Don't rebuild the zip on macOS - update the existing deployment directory that has pre-built Linux dependencies.

### Infrastructure Drift (Fully Resolved 2026-02-02)

The original CloudFormation ALB was deleted outside of IaC, causing UAT proxy failures. This has been fully resolved:
1. Migrated all infrastructure to Terraform Cloud
2. Tore down all manually-created resources
3. Terraform now manages everything from scratch
4. Uses existing `test-tickets-uat-alb` (shared with test-tickets project)

All infrastructure is now fully managed by Terraform Cloud workspace `aws-projects__claude-cloud-agent`.

### Orphaned AWS Resources (Not Terraform-Managed)

These resources exist in AWS but are NOT managed by Terraform. Consider cleanup:

| Resource | Name | Notes |
|----------|------|-------|
| Lambda Function | `claude-cloud-agent-idle-timeout` | Old function, likely unused |
| CloudWatch Log Group | `/aws/lambda/claude-cloud-agent-idle-timeout` | No retention set |
| Task Definition | `claude-agent-task-dev` | Old dev task definition |
| Task Definition | `claude-dev-agent` | Old dev task definition |

To clean up, verify these are not in use, then delete manually via AWS console or CLI.
