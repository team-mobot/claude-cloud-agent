# Session Handoff: Claude Cloud Agent Infrastructure

**Date:** 2026-02-02
**Purpose:** Enable new Claude session to continue Terraform Cloud setup after restart

---

## Current Status: READY FOR TERRAFORM CLOUD WORKSPACE SETUP

The Terraform code has been written and pushed. Next step is creating the workspace in Terraform Cloud via the newly installed MCP server.

---

## What Was Completed This Session

### 1. Infrastructure Audit
- Diagnosed UAT proxy failure (504 Gateway Timeout)
- Root causes: deleted ALB, wrong security group rules, wrong env var name
- Applied temporary CLI fixes to restore functionality
- Documented in `docs/INFRASTRUCTURE-AUDIT-2026-02-02.md`

### 2. Terraform Created
- Location: `/Users/dave/git/mobot/claude-cloude-agent-infra/infrastructure/aws-projects/claude-cloud-agent/`
- Files: main.tf, variables.tf, data.tf, dynamodb.tf, iam.tf, security_groups.tf, ecs.tf, alb.tf, outputs.tf, terraform.tfvars, import.sh, README.md

### 3. Terraform Pushed to Trigger Terraform Cloud
```bash
# Already done:
cd /Users/dave/git/mobot/claude-cloude-agent-infra
git commit  # committed on claude-cloude-agent-infra branch

cd /Users/dave/git/mobot/terraform-cloud
git merge claude-cloude-agent-infra
git push origin terraform-cloud  # PUSHED - waiting for workspace
```

### 4. MCP Server Installed
- Docker image built: `terraform-cloud-mcp:latest`
- Settings updated: `~/.claude/settings.json`
- Token configured
- **Restart required to load MCP server**

---

## Next Steps (After Restart)

### 1. Verify MCP Server Loaded
Search for terraform tools to confirm MCP server is working:
```
Use ToolSearch with query "terraform"
```

### 2. Create Terraform Cloud Workspace
Using MCP tools, create workspace with:
- **Organization:** `mobot`
- **Workspace name:** `aws-projects__claude-cloud-agent`
- **VCS:** `team-mobot/mobot` repository
- **Branch:** `terraform-cloud`
- **Working directory:** `infrastructure/aws-projects/claude-cloud-agent`

### 3. Set Workspace Environment Variables
Add these as **sensitive environment variables**:
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`

(Values need to come from user or existing workspace)

### 4. Trigger Initial Plan
Either:
- Push a commit to trigger
- Or manually queue a run via MCP

### 5. Import Existing Resources
Before applying, import existing resources so Terraform doesn't recreate them:
- DynamoDB table: `claude-cloud-agent-sessions`
- ECS cluster: `claude-cloud-agent`
- Target group: `claude-cloud-agent-uat-proxy`
- Listener rule (priority 10 on test-tickets-uat-alb)
- Security groups
- IAM roles

Import script available: `infrastructure/aws-projects/claude-cloud-agent/import.sh`

### 6. Review and Apply Plan
Once imports are done, the plan should show minimal changes.

---

## Key Configuration Values

### Terraform Cloud
- Organization: `mobot`
- Workspace: `aws-projects__claude-cloud-agent`
- URL: https://app.terraform.io/app/mobot/workspaces

### AWS Resources (Already Deployed)
| Resource | ID/ARN |
|----------|--------|
| VPC | `vpc-0fde49947ce39aec4` (test-tickets VPC) |
| Subnets | `subnet-0ede91cdf265af677`, `subnet-016037c717cc89fb2` |
| ECS Cluster | `claude-cloud-agent` |
| DynamoDB Table | `claude-cloud-agent-sessions` |
| Target Group | `arn:aws:elasticloadbalancing:us-east-1:678954237808:targetgroup/claude-cloud-agent-uat-proxy/1948de628b6d1ae1` |
| ALB Listener | `arn:aws:elasticloadbalancing:us-east-1:678954237808:listener/app/test-tickets-uat-alb/7e47b6b368ee29e1/9b650b437fde8e6f` |
| Listener Rule | `arn:aws:elasticloadbalancing:us-east-1:678954237808:listener-rule/app/test-tickets-uat-alb/7e47b6b368ee29e1/9b650b437fde8e6f/d8e00e24bf7a5ab0` |
| Proxy Security Group | `sg-04918a5bd8c94e634` |
| ALB Security Group | `sg-01e33c097eb569074` |
| GitHub Secret | `arn:aws:secretsmanager:us-east-1:678954237808:secret:claude-dev/github-app-704JPc` |
| ACM Certificate | `arn:aws:acm:us-east-1:678954237808:certificate/9480cbca-4a44-42c4-a29b-6154c93cd815` |

### Repository Locations
| Purpose | Path |
|---------|------|
| Claude agent code | `/Users/dave/git/claude-cloud-agent/main` |
| Terraform (feature branch) | `/Users/dave/git/mobot/claude-cloude-agent-infra` |
| Terraform (deploy branch) | `/Users/dave/git/mobot/terraform-cloud` |
| Repo agent works on | `/Users/dave/git/spencer-test-tickets/cli-pr-agent` |

### Git Workflow for Terraform Changes
```bash
# 1. Edit on feature branch
cd /Users/dave/git/mobot/claude-cloude-agent-infra
# make changes
git add infrastructure/aws-projects/claude-cloud-agent/
git commit -m "Description"

# 2. Merge to terraform-cloud
cd /Users/dave/git/mobot/terraform-cloud
git merge claude-cloude-agent-infra -m "Merge description"

# 3. Push to trigger Terraform Cloud
git push origin terraform-cloud
```

---

## Related Documentation

- Infrastructure Audit: `docs/INFRASTRUCTURE-AUDIT-2026-02-02.md`
- Terraform README: `/Users/dave/git/mobot/claude-cloude-agent-infra/infrastructure/aws-projects/claude-cloud-agent/README.md`
- CLAUDE.md: `/Users/dave/git/claude-cloud-agent/main/CLAUDE.md`
