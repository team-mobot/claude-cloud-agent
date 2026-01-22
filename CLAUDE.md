# Claude Cloud Agent

Autonomous development agent that runs Claude Code in ECS Fargate tasks, triggered by GitHub issues or JIRA tickets.

## Architecture

```
GitHub/JIRA Webhook → API Gateway → Lambda (handler.py) → ECS Fargate Task (entrypoint.sh + Claude Code)
                                         ↓
                                    DynamoDB (sessions)
```

## Key Components

| Component | Location |
|-----------|----------|
| Webhook handler | `webhook/handler.py` |
| Agent entrypoint | `agent/entrypoint.sh` |
| Dockerfile | `agent/Dockerfile` |
| Infrastructure | `infrastructure/template.yaml` |

## Deployment

### Lambda (webhook handler)

```bash
# Package and deploy
cd webhook
rm -f handler.zip
cd package && zip -rq ../handler.zip . && cd ..
zip -g handler.zip handler.py

# Deploy via AWS CLI (replace function name as needed)
aws lambda update-function-code \
  --function-name claude-dev-webhook \
  --zip-file fileb://handler.zip
```

### Docker Image (agent)

**IMPORTANT: Must build for linux/amd64 since ECS Fargate runs x86_64, not ARM.**

```bash
# Build for correct platform (even on Mac ARM)
docker buildx build --platform linux/amd64 \
  -t <account>.dkr.ecr.<region>.amazonaws.com/<repo>:latest \
  agent/ --push
```

### Verify Deployment

After pushing a new image, verify ECS tasks are using it:
```bash
aws ecs describe-tasks --cluster <cluster> --tasks <task-id> \
  --query 'tasks[0].containers[0].imageDigest'
```

## Common Mistakes to Avoid

### 1. Wrong ECR Repository
The ECS task definition specifies which ECR repository to pull from. Always verify:
```bash
aws ecs describe-task-definition --task-definition <name> \
  --query 'taskDefinition.containerDefinitions[0].image'
```
Push to the repository specified in the task definition, not a different one.

### 2. ARM vs AMD64 Architecture
Mac ARM (M1/M2/M3) builds ARM images by default. ECS Fargate needs linux/amd64.
- **Wrong:** `docker build -t <image> .`
- **Right:** `docker buildx build --platform linux/amd64 -t <image> . --push`

### 3. Lambda Code Not Auto-Deployed
Changes to `webhook/handler.py` are NOT automatically deployed. You must:
1. Re-package the zip with dependencies
2. Upload to Lambda via CLI or console

### 4. ECS Image Caching
Fargate may cache old images even with `:latest` tag. If changes aren't taking effect:
- Check the `imageDigest` on running tasks
- Compare with the digest shown after `docker push`
- Consider using specific tags instead of `:latest`

### 5. Environment Variable Formatting
When updating Lambda env vars via CLI with comma-separated values, use JSON format:
```bash
aws lambda update-function-configuration \
  --function-name <name> \
  --environment '{
    "Variables": {
      "SUBNETS": "subnet-xxx,subnet-yyy"
    }
  }'
```

## Environment Variables

### Lambda
- `GITHUB_SECRET_ARN` - GitHub App credentials
- `JIRA_SECRET_ARN` - JIRA API credentials (optional)
- `SESSION_TABLE` - DynamoDB table name
- `ECS_CLUSTER` - ECS cluster name
- `AGENT_TASK_DEFINITION` - ECS task definition name
- `SUBNETS` - Comma-separated subnet IDs
- `SECURITY_GROUPS` - Security group ID
- `TRIGGER_LABEL` - Label that triggers agent (default: `claude-dev`)
- `CONTAINER_NAME` - Container name in task definition

### ECS Task (passed via container overrides)
- `SESSION_ID`, `PR_NUMBER`, `BRANCH`, `REPO`, `PROMPT`, `RESUME`
- `SOURCE` - 'github' or 'jira'
- `JIRA_ISSUE_KEY`, `JIRA_SITE`, `JIRA_SECRET_ARN` (for JIRA sessions)

## Secrets Manager

### GitHub App (`claude-dev/github-app`)
```json
{
  "app_id": "...",
  "private_key": "-----BEGIN RSA PRIVATE KEY-----\n...",
  "webhook_secret": "..."
}
```

### JIRA (`claude-cloud-agent/jira`)
```json
{
  "site": "yoursite.atlassian.net",
  "email": "user@example.com",
  "api_token": "...",
  "webhook_secret": "...",
  "repo_custom_field_id": "customfield_XXXXX"
}
```

## JIRA Integration

### Setup Requirements
1. Custom field "GitHub Repository" in JIRA (text field)
2. Webhook pointing to Lambda URL with events: Issue updated, Comment created
3. JIRA secret in Secrets Manager with API token
4. DynamoDB GSI `jira-issue-index` on `jira_issue_key`

### Workflow
1. User sets "GitHub Repository" field to `owner/repo`
2. User adds `claude-dev` label
3. Lambda creates branch and draft PR on GitHub
4. Lambda posts initial comment to JIRA with PR link
5. ECS task runs Claude Code
6. Progress posted to both GitHub PR and JIRA ticket
7. Comments on either platform resume the agent

## Testing

### Trigger from JIRA
```bash
# Add comment via API to trigger agent
curl -X POST \
  -u "email:api_token" \
  -H "Content-Type: application/json" \
  "https://site.atlassian.net/rest/api/3/issue/PROJ-123/comment" \
  -d '{"body":{"type":"doc","version":1,"content":[{"type":"paragraph","content":[{"type":"text","text":"Your instruction here"}]}]}}'
```

### Check Lambda Logs
```bash
aws logs tail /aws/lambda/claude-dev-webhook --follow
```

### Check ECS Task Logs
```bash
aws logs tail /ecs/claude-dev-agent --follow
```
