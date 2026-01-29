# test-tickets-uat Deployment

## Prerequisites

- AWS CLI configured with appropriate credentials
- Docker installed and running
- Access to AWS account 678954237808

## Deploy

```bash
./deploy.sh
```

New ECS tasks automatically use the latest image. Existing running tasks will need to be stopped to pick up the new image.

## Architecture

This container runs:
- `prompt-server.js` - HTTP server that receives prompts and runs Claude Code
- `dev-proxy.js` - Proxy for development server routing
- `entrypoint.sh` - Container initialization (clones repo, starts services)

The container is launched by the webhook Lambda when the `claude-dev` label is added to a PR.

## Related Files

- `Dockerfile` - Container image definition
- `entrypoint.sh` - Main entrypoint script
- `prompt-server.js` - Claude Code integration server
- `../infrastructure/template.yaml` - SAM template defining ECS task definitions
