# Infrastructure Audit: Claude Cloud Agent UAT System

**Date:** 2026-02-02
**Triggered by:** PR 261 on team-mobot/test_tickets - UAT environment unreachable

## Executive Summary

The UAT proxy infrastructure for `*.uat.teammobot.dev` was broken due to infrastructure drift. The CloudFormation-managed ALB was deleted outside of CloudFormation, DNS was reconfigured to point to a different ALB (`test-tickets-uat-alb`), but that ALB was never configured to route traffic to the UAT proxy.

**Temporary fixes were applied via AWS CLI** to restore functionality. Permanent fixes require either redeploying CloudFormation or updating the infrastructure-as-code to match the current DNS configuration.

---

## Architecture Overview

### Intended Design (per DESIGN.md)

```
GitHub Issue (claude-dev label)
        │
        ▼
Lambda Webhook Handler
        │
        ├── Creates branch & draft PR
        └── Launches ECS claude-agent task
                │
                ├── Runs Claude Code (implements feature)
                ├── Runs dev server (port 3000)
                └── Reports IP to DynamoDB
                        │
                        ▼
                UAT Proxy (ECS Service)
                        │
                        ├── Looks up session in DynamoDB
                        └── Forwards requests to agent container
                                │
                                ▼
                        ALB (*.uat.teammobot.dev)
                                │
                                ▼
                        User accesses https://{session-id}.uat.teammobot.dev
```

### Current DNS Configuration

```
*.uat.teammobot.dev  →  test-tickets-uat-alb-1212893351.us-east-1.elb.amazonaws.com
```

### VPC Configuration

**IMPORTANT**: The infrastructure runs in the **test-tickets VPC**, NOT aws0:

| VPC | ID | CIDR | Purpose |
|-----|-----|------|---------|
| test-tickets | vpc-0fde49947ce39aec4 | 10.0.0.0/16 | Claude-cloud-agent, test-tickets |
| aws0 | vpc-06d947778e171c58f | 10.208.0.0/16 | Admin hosts, regional gateways |

Subnets used by ECS:
- `subnet-0ede91cdf265af677` - test-tickets-public-1a (10.0.1.0/24, us-east-1a)
- `subnet-016037c717cc89fb2` - test-tickets-public-1b (10.0.2.0/24, us-east-1b)

---

## Infrastructure Components

### CloudFormation Stack: `claude-cloud-agent`

**Location:** `/Users/dave/git/claude-cloud-agent/version-two/infrastructure/template.yaml`
**Last Updated:** 2026-01-23T15:05:39

| Resource | Logical ID | Physical ID | Status |
|----------|------------|-------------|--------|
| ECS Cluster | ECSCluster | claude-cloud-agent | ✅ Exists |
| Agent Task Def | AgentTaskDefinition | claude-agent:9 | ✅ Exists |
| Proxy Task Def | UatProxyTaskDefinition | claude-cloud-agent-uat-proxy:2 | ⚠️ Missing env var |
| Proxy Service | UatProxyService | claude-cloud-agent-uat-proxy | ✅ Running |
| Proxy Target Group | UatAlbTargetGroup | claude-cloud-agent-uat-proxy | ✅ Exists |
| **ALB** | **UatAlb** | **claude-cloud-agent-uat** | **❌ DELETED** |
| ALB Security Group | UatAlbSecurityGroup | sg-061405fda3c0f416c | ⚠️ Orphaned |
| Proxy Security Group | UatProxySecurityGroup | sg-04918a5bd8c94e634 | ⚠️ Wrong rules |
| Sessions Table | SessionsTable | claude-cloud-agent-sessions | ✅ Exists |

### Actual ALBs in Account

| ALB Name | Purpose | DNS |
|----------|---------|-----|
| test-tickets-uat-alb | test_tickets UAT environments | test-tickets-uat-alb-1212893351.us-east-1.elb.amazonaws.com |
| test-tickets-alb | Production test_tickets | - |
| mobot-agents-staging-alb | Staging environment | - |
| ai-driver-dev-alb | AI driver dev | - |

**Note:** `claude-cloud-agent-uat` ALB (created by CloudFormation) no longer exists.

---

## Issues Discovered

### Issue 1: CloudFormation ALB Deleted

**Symptom:** CloudFormation resource `UatAlb` shows `CREATE_COMPLETE` but ALB doesn't exist.

```bash
# CloudFormation thinks this exists:
arn:aws:elasticloadbalancing:us-east-1:678954237808:loadbalancer/app/claude-cloud-agent-uat/a4d18bb256b32be1

# But it returns LoadBalancerNotFound
```

**Impact:** The entire routing infrastructure managed by CloudFormation is orphaned.

### Issue 2: DNS Points to Wrong ALB

**Symptom:** `*.uat.teammobot.dev` DNS record points to `test-tickets-uat-alb`, not the CloudFormation-managed ALB.

**Route53 Hosted Zone:** Z02075312HY2WNK7FBP01

```
*.uat.teammobot.dev  →  test-tickets-uat-alb-1212893351.us-east-1.elb.amazonaws.com
```

### Issue 3: No ALB Routing Rule for Proxy

**Symptom:** `test-tickets-uat-alb` had no listener rule routing `*.uat.teammobot.dev` to the proxy target group.

**Before fix:**
```
Listener Rules:
  - Priority: default → test-tickets-uat-tg (port 3001)
```

**After fix:**
```
Listener Rules:
  - Priority: 10 → claude-cloud-agent-uat-proxy (*.uat.teammobot.dev)
  - Priority: default → test-tickets-uat-tg
```

### Issue 4: Security Group Mismatch

**Symptom:** Proxy security group (`sg-04918a5bd8c94e634`) only allowed port 8080 from the old/deleted ALB's security group (`sg-061405fda3c0f416c`), not from the actual ALB's security group (`sg-01e33c097eb569074`).

**SAM Template says:**
```yaml
SecurityGroupIngress:
- IpProtocol: tcp
  FromPort: 8080
  ToPort: 8080
  CidrIp: '0.0.0.0/0'  # Allow from anywhere
```

**Actually deployed:**
```
Ingress: tcp/8080 from sg-061405fda3c0f416c only
```

**Fix applied:**
```bash
aws ec2 authorize-security-group-ingress \
  --group-id sg-04918a5bd8c94e634 \
  --protocol tcp --port 8080 \
  --source-group sg-01e33c097eb569074
```

### Issue 5: Missing Environment Variable

**Symptom:** Proxy task definition revision 2 only had `SESSION_TABLE`, but the proxy code reads `SESSIONS_TABLE`.

**SAM Template (correct):**
```yaml
Environment:
- Name: SESSIONS_TABLE
  Value: !Ref SessionsTable
```

**Deployed revision 2:**
```json
"environment": [
  {"name": "SESSION_TABLE", "value": "claude-cloud-agent-sessions"}
]
```

**Fix applied:** Created revision 3 with both variables:
```bash
aws ecs register-task-definition ... \
  --container-definitions '[{
    "environment": [
      {"name": "SESSION_TABLE", "value": "claude-cloud-agent-sessions"},
      {"name": "SESSIONS_TABLE", "value": "claude-cloud-agent-sessions"}
    ]
  }]'
```

---

## Temporary Fixes Applied (via AWS CLI)

### 1. ALB Listener Rule Created

```bash
aws elbv2 create-rule \
  --listener-arn "arn:aws:elasticloadbalancing:us-east-1:678954237808:listener/app/test-tickets-uat-alb/7e47b6b368ee29e1/9b650b437fde8e6f" \
  --priority 10 \
  --conditions Field=host-header,Values='*.uat.teammobot.dev' \
  --actions Type=forward,TargetGroupArn="arn:aws:elasticloadbalancing:us-east-1:678954237808:targetgroup/claude-cloud-agent-uat-proxy/1948de628b6d1ae1"
```

**Rule ARN:** `arn:aws:elasticloadbalancing:us-east-1:678954237808:listener-rule/app/test-tickets-uat-alb/7e47b6b368ee29e1/9b650b437fde8e6f/d8e00e24bf7a5ab0`

### 2. Security Group Rule Added

```bash
aws ec2 authorize-security-group-ingress \
  --group-id sg-04918a5bd8c94e634 \
  --protocol tcp --port 8080 \
  --source-group sg-01e33c097eb569074
```

**Rule ID:** `sgr-09fe84306ec3ffb8e`

### 3. Task Definition Updated

```bash
aws ecs register-task-definition \
  --family claude-cloud-agent-uat-proxy \
  --task-role-arn "arn:aws:iam::678954237808:role/claude-cloud-agent-UatProxyTaskRole-ZSnBzm1J8It5" \
  --execution-role-arn "arn:aws:iam::678954237808:role/claude-cloud-agent-AgentExecutionRole-vG6Q8UHDw5CW" \
  --network-mode awsvpc \
  --requires-compatibilities FARGATE \
  --cpu 256 --memory 512 \
  --container-definitions '[{"name":"uat-proxy",...,"environment":[{"name":"SESSION_TABLE","value":"claude-cloud-agent-sessions"},{"name":"SESSIONS_TABLE","value":"claude-cloud-agent-sessions"}],...}]'
```

**New Revision:** 3

### 4. ECS Service Updated

```bash
aws ecs update-service \
  --cluster claude-cloud-agent \
  --service claude-cloud-agent-uat-proxy \
  --task-definition claude-cloud-agent-uat-proxy:3 \
  --force-new-deployment
```

---

## Permanent Fix Options

### Option A: Reconcile with Existing Infrastructure

Update the infrastructure-as-code to use `test-tickets-uat-alb` instead of creating a separate ALB. This requires:

1. **Remove ALB resources from CloudFormation** (they're already deleted)
2. **Add ALB listener rule to test-tickets-uat-alb** via Terraform/IaC
3. **Update security group** to allow from test-tickets-uat-alb's SG
4. **Fix task definition** to include `SESSIONS_TABLE`

**Pros:** Uses existing infrastructure, simpler
**Cons:** Couples claude-cloud-agent to test-tickets infrastructure

### Option B: Recreate CloudFormation ALB

Redeploy the CloudFormation stack to recreate the deleted ALB and update DNS.

1. **Delete orphaned CloudFormation resources** or update stack
2. **Redeploy SAM template** to recreate ALB
3. **Update DNS** to point to new ALB

**Pros:** Matches original design, self-contained
**Cons:** More complex, requires DNS change

### Option C: Migrate to Terraform

Move all infrastructure to Terraform in `/Users/dave/git/mobot/uat.tm.com-wildcard/` for consistency.

**Pros:** Single IaC tool, better drift detection
**Cons:** Migration effort

---

## Work Items

### Immediate (to prevent regression)

- [x] Document the manual fixes in runbook (this document)
- [ ] Add monitoring/alerting for proxy health
- [x] Consider adding the fixes to IaC → Created Terraform

### Short-term (infrastructure cleanup)

- [x] Decide on Option A, B, or C above → Option A (use existing ALB) + Terraform
- [ ] Update CloudFormation stack status (delete or update)
- [x] Ensure task definition has correct env vars in IaC → SESSIONS_TABLE in ecs.tf
- [x] Fix security group rules in IaC → security_groups.tf

### Long-term (reliability)

- [ ] Add CloudFormation drift detection (or remove CloudFormation entirely)
- [ ] Implement infrastructure tests
- [x] Document the full system architecture → README.md

---

## Terraform Cloud Deployment

### Workflow

The mobot repo uses git worktrees for Terraform Cloud deployment:

```
/Users/dave/git/mobot/
├── claude-cloude-agent-infra/    # Feature branch worktree
└── terraform-cloud/              # terraform-cloud branch worktree
```

**To deploy changes:**

```bash
# 1. Commit on feature branch
cd /Users/dave/git/mobot/claude-cloude-agent-infra
git add infrastructure/aws-projects/claude-cloud-agent/
git commit -m "Description"

# 2. Merge to terraform-cloud
cd /Users/dave/git/mobot/terraform-cloud
git merge claude-cloude-agent-infra -m "Merge description"

# 3. Push to trigger Terraform Cloud
git push origin terraform-cloud

# 4. Review plan at https://app.terraform.io/app/mobot/workspaces
# 5. Click "Confirm & Apply" in Terraform Cloud UI
```

### Workspace Configuration

- **Organization**: `mobot`
- **Workspace**: `aws-projects__claude-cloud-agent`
- **VCS**: `team-mobot/mobot`, branch `terraform-cloud`
- **Working directory**: `infrastructure/aws-projects/claude-cloud-agent`
- **Environment variables**: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`

### First-Time Setup: Import Existing Resources

Before Terraform can manage existing resources, they must be imported:

```bash
cd /Users/dave/git/mobot/claude-cloude-agent-infra/infrastructure/aws-projects/claude-cloud-agent
./import.sh
```

Or run imports via Terraform Cloud CLI.

---

## Key Resource Reference

### Security Groups

| Name | ID | Purpose |
|------|-----|---------|
| claude-cloud-agent-UatProxySecurityGroup | sg-04918a5bd8c94e634 | Proxy ECS tasks |
| claude-cloud-agent-UatAlbSecurityGroup | sg-061405fda3c0f416c | Orphaned (old ALB deleted) |
| test-tickets-uat-alb SG | sg-01e33c097eb569074 | Current ALB for *.uat.teammobot.dev |
| emulator-host-sg | sg-00cf50da34a0c1ab7 | Agent containers |

### Target Groups

| Name | ARN | Port | Health |
|------|-----|------|--------|
| claude-cloud-agent-uat-proxy | .../claude-cloud-agent-uat-proxy/1948de628b6d1ae1 | 8080 | /health |
| test-tickets-uat-tg | .../test-tickets-uat-tg/2f134c8b54fbcb18 | 3001 | - |

### ECS Resources

| Resource | Value |
|----------|-------|
| Cluster | claude-cloud-agent |
| Proxy Service | claude-cloud-agent-uat-proxy |
| Proxy Task Def | claude-cloud-agent-uat-proxy:3 |
| Agent Task Def | claude-agent:9 |

### DynamoDB

| Table | Purpose |
|-------|---------|
| claude-cloud-agent-sessions | Session state (IP, status, PR info) |

---

## Repository and Directory Structure

### Key Repositories

| Repository | Path | Purpose |
|------------|------|---------|
| claude-cloud-agent | `/Users/dave/git/claude-cloud-agent/main` | Agent orchestration code (webhook, Lambda, etc.) |
| cli-pr-agent | `/Users/dave/git/spencer-test-tickets/cli-pr-agent` | Example repo the agent works on |
| mobot (claude-cloude-agent-infra) | `/Users/dave/git/mobot/claude-cloude-agent-infra` | Main mobot repo with Terraform |

### Infrastructure-as-Code Locations

| IaC Type | Path | Project | Notes |
|----------|------|---------|-------|
| SAM/CloudFormation | `/Users/dave/git/claude-cloud-agent/version-two/infrastructure/template.yaml` | claude-cloud-agent | **Current IaC** - but drifted |
| Terraform | `/Users/dave/git/mobot/claude-cloude-agent-infra/infrastructure/aws-projects/ai-driver-webapp/` | ai-driver-webapp | Different project, not claude-cloud-agent |

### Critical Finding: No Terraform for claude-cloud-agent

The Terraform directory at `/Users/dave/git/mobot/claude-cloude-agent-infra/infrastructure/aws-projects/ai-driver-webapp/` is for the **ai-driver-webapp** project, NOT claude-cloud-agent. Key indicators:

```hcl
# From main.tf
locals {
  project_name = "ai-driver"
  vpc_cidr = "10.100.0.0/16"  # Different VPC
}
```

The claude-cloud-agent infrastructure is currently managed by:
- **CloudFormation/SAM** template at `version-two/infrastructure/template.yaml`
- **Manual AWS CLI fixes** applied during this incident

### Terraform for claude-cloud-agent (CREATED)

Terraform has been created at:
```
/Users/dave/git/mobot/claude-cloude-agent-infra/infrastructure/aws-projects/claude-cloud-agent/
```

Files:
- `main.tf` - Provider, backend, locals
- `variables.tf` - Input variables
- `data.tf` - Data sources for existing resources
- `dynamodb.tf` - Sessions table
- `iam.tf` - IAM roles for ECS tasks
- `security_groups.tf` - Security groups with correct rules
- `ecs.tf` - Cluster, task definitions, services
- `alb.tf` - Listener rule on existing test-tickets-uat-alb
- `outputs.tf` - Outputs for Lambda integration
- `README.md` - Comprehensive documentation
- `dev.tfvars.example` - Example variable values

---

## Related Files

- SAM Template: `/Users/dave/git/claude-cloud-agent/version-two/infrastructure/template.yaml`
- Design Doc: `/Users/dave/git/claude-cloud-agent/main/docs/DESIGN.md`
- UAT Docs: `/Users/dave/git/claude-cloud-agent/main/docs/TEST-TICKETS-UAT.md`
- CLAUDE.md: `/Users/dave/git/claude-cloud-agent/main/CLAUDE.md`

---

## Appendix: Session 09e22a8b Details

The session that triggered this investigation:

| Field | Value |
|-------|-------|
| Session ID | 09e22a8b |
| PR | team-mobot/test_tickets#261 |
| Branch | claude/agnts-124 |
| JIRA Issue | AGNTS-124 |
| Container IP | 10.0.2.133 |
| UAT URL | https://09e22a8b.uat.teammobot.dev |
| Status | RUNNING |
| Task ARN | arn:aws:ecs:us-east-1:678954237808:task/claude-cloud-agent/dd767722600d45a987fbda6e87bb88de |

The agent successfully implemented the Workload Balancer feature and pushed commits:
- `6764234 chore: initialize Claude agent session for AGNTS-124`
- `a8df2cb feat(AGNTS-124): Add Workload Balancer view to Assignments page`

**Note:** The PR comment incorrectly shows AGNTS-121 commits - this is a separate bug in `github_reporter.py` in the claude-agent container.
