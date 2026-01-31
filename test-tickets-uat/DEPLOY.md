# test-tickets-uat Deployment

## Prerequisites

- AWS CLI configured with appropriate credentials
- Docker installed and running
- Access to AWS account 678954237808

## Staging/Production Workflow

Always use the staging workflow for container changes:

```bash
# 1. Build and push to :staging
./deploy.sh staging

# 2. Test by adding 'uat-staging' label to a PR in test_tickets repo

# 3. After testing passes, promote to production
./deploy.sh promote
```

### Image Tags

| Tag | Purpose | How to Use |
|-----|---------|------------|
| `:staging` | Testing new container changes | `uat-staging` label on PR |
| `:latest` | Production | `uat` label on PR |
| `:{git-sha}` | Immutable rollback reference | Created automatically |

### Deploy Script Commands

```bash
./deploy.sh staging   # Build and push to :staging (default)
./deploy.sh promote   # Copy :staging manifest to :latest (no rebuild)
./deploy.sh latest    # Emergency hotfix - build direct to :latest
```

### How Staging Works

1. `deploy.sh staging` builds the image and pushes with `:staging` and `:{git-sha}` tags
2. When a PR is labeled `uat-staging`, the Lambda creates a new ECS task definition revision with the `:staging` image
3. The ECS task launches with that revision
4. After testing, `deploy.sh promote` copies the `:staging` manifest to `:latest` in ECR (no rebuild needed)
5. Future `uat` labels use the updated `:latest` image

### Rollback

If a promoted image has issues:

```bash
# List available image tags
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

This container runs:
- `entrypoint.sh` - Container initialization (clones repo, builds app, sets up ALB routing)
- `prompt-server.js` - HTTP server for health, status, version, and prompt endpoints

The container is launched by the webhook Lambda when:
- `uat` label is added to a PR (uses `:latest` image)
- `uat-staging` label is added to a PR (uses `:staging` image)

## Container Endpoints

| Endpoint | Port | Description |
|----------|------|-------------|
| `/health` | 8080 | Health check |
| `/status` | 8080 | Queue status |
| `/version` | 8080 | Git commit info of cloned code |
| `/prompt` | 8080 | Submit prompt for Claude |
| App | 3001 | The test_tickets application |

## Related Files

- `Dockerfile` - Container image definition
- `entrypoint.sh` - Main entrypoint script
- `prompt-server.js` - Claude Code integration server
- `deploy.sh` - Build and deployment script
- `../docs/TEST-TICKETS-UAT.md` - Full documentation
