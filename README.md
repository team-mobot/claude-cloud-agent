# Claude Cloud Agent

Autonomous development agent powered by Claude Code. When GitHub issues are labeled with a trigger label, this system automatically creates a branch/PR and runs Claude Code to implement the requested changes.

## How It Works

```
GitHub Issue (labeled 'claude-dev')
    ↓
API Gateway → Lambda (webhook handler)
    ↓
Creates branch/PR → Starts ECS Fargate task
    ↓
Claude Code runs autonomously
    ↓
Posts progress to PR comments
    ↓
User feedback via PR comments → Agent resumes
```

## Features

- **Automatic PR creation**: Issues with the trigger label get a branch and draft PR
- **Bidirectional communication**: Agent posts updates to PR, users reply with feedback
- **Full visibility**: All Claude Code actions streamed to PR comments
- **Session management**: DynamoDB tracks session state, handles resume on feedback
- **Clean shutdown**: PR close triggers task termination

## Architecture

| Component | Purpose |
|-----------|---------|
| `webhook/` | Lambda function handling GitHub webhook events |
| `agent/` | ECS Fargate task running Claude Code |
| `infrastructure/` | CloudFormation template for AWS resources |

## Prerequisites

1. **GitHub App** with permissions:
   - Issues: Read & Write
   - Pull Requests: Read & Write
   - Contents: Read & Write
   - Webhooks configured to send: Issues, Issue comments, Pull requests

2. **AWS Resources**:
   - VPC with public subnets
   - S3 bucket for Lambda deployment package
   - Secrets Manager secret with GitHub App credentials

3. **Secrets Manager Secret** structure:
   ```json
   {
     "app_id": "123456",
     "private_key": "-----BEGIN RSA PRIVATE KEY-----\n...",
     "webhook_secret": "your-webhook-secret"
   }
   ```

## Deployment

### 1. Build and push agent container

```bash
# Authenticate to ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com

# Create repository (first time only)
aws ecr create-repository --repository-name claude-cloud-agent --region us-east-1

# Build and push
docker build -t claude-cloud-agent agent/
docker tag claude-cloud-agent:latest <account>.dkr.ecr.us-east-1.amazonaws.com/claude-cloud-agent:latest
docker push <account>.dkr.ecr.us-east-1.amazonaws.com/claude-cloud-agent:latest
```

### 2. Package webhook Lambda

```bash
cd webhook
pip install -r requirements.txt -t package/
cp handler.py package/
cd package && zip -r ../handler.zip . && cd ..
aws s3 cp handler.zip s3://<artifact-bucket>/webhook/handler.zip
```

### 3. Deploy CloudFormation stack

```bash
aws cloudformation deploy \
  --template-file infrastructure/template.yaml \
  --stack-name claude-cloud-agent \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    GitHubSecretArn=arn:aws:secretsmanager:us-east-1:<account>:secret:claude-cloud-agent/github \
    VpcId=vpc-xxx \
    PublicSubnets=subnet-xxx,subnet-yyy \
    ArtifactBucket=<artifact-bucket>
```

### 4. Configure GitHub App webhook

Set the webhook URL to the `WebhookUrl` output from the CloudFormation stack.

## Usage

1. Create an issue in a repository where the GitHub App is installed
2. Add the trigger label (default: `claude-dev`)
3. Agent creates a branch and draft PR
4. Agent runs Claude Code and posts progress as PR comments
5. Reply to the PR to provide feedback - agent will resume
6. Merge or close the PR when done

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `TriggerLabel` | `claude-dev` | GitHub label that triggers agent sessions |

## Target Repository Setup

The target repository should have a `CLAUDE.md` file with instructions for Claude Code. This file should describe:

- Project structure and conventions
- How to build and test
- Coding standards
- Any project-specific instructions

The agent reads this file automatically when working on the repository.

## License

MIT
