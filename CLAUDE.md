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

The infrastructure is managed via Terraform in the mobot repo:

**Location**: `/Users/dave/git/mobot/claude-cloude-agent-infra/infrastructure/aws-projects/claude-cloud-agent/`

**Terraform Cloud**: https://app.terraform.io/app/mobot/workspaces
- Workspace: `aws-projects__claude-cloud-agent`

### Key Findings (2026-02-02 Audit)

1. Infrastructure runs in **test-tickets VPC** (`vpc-0fde49947ce39aec4`), NOT aws0
2. Uses existing `test-tickets-uat-alb` (DNS `*.uat.teammobot.dev` points there)
3. Proxy env var must be `SESSIONS_TABLE` (not `SESSION_TABLE`)
4. Security group must allow traffic from ALB SG `sg-01e33c097eb569074`

### Terraform Deployment Workflow

```bash
# 1. Make changes on feature branch
cd /Users/dave/git/mobot/claude-cloude-agent-infra
git add infrastructure/aws-projects/claude-cloud-agent/
git commit -m "Description"

# 2. Merge to terraform-cloud branch (separate worktree)
cd /Users/dave/git/mobot/terraform-cloud
git merge claude-cloude-agent-infra -m "Merge description"

# 3. Push to trigger Terraform Cloud
git push origin terraform-cloud

# 4. Review plan at Terraform Cloud UI
# 5. Click "Confirm & Apply"
```

See `docs/INFRASTRUCTURE-AUDIT-2026-02-02.md` for full details.

---

## Known Issues

### claude-agent Container Source

The `claude-agent` ECS container uses Python modules (`session_reporter.py`, `github_reporter.py`, `claude_runner.py`) but the source location is unknown. The `/Users/dave/git/cca/agent/` directory has a bash-based entrypoint that doesn't match the deployed container.

### Lambda Deployment

The Lambda requires Linux binaries for cryptography. Don't rebuild the zip on macOS - update the existing deployment directory that has pre-built Linux dependencies.

### Infrastructure Drift (Resolved 2026-02-02)

The original CloudFormation ALB was deleted outside of IaC, causing UAT proxy failures. This has been resolved by migrating to Terraform and using the existing `test-tickets-uat-alb`. See `docs/INFRASTRUCTURE-AUDIT-2026-02-02.md`.
