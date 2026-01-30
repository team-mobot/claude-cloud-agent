#!/bin/bash
set -e

MODE="${1:-staging}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ECR_REPO="678954237808.dkr.ecr.us-east-1.amazonaws.com/test-tickets-uat"
REGION="us-east-1"
GIT_SHA=$(git rev-parse --short HEAD)

# Use docker-based aws CLI if aws not in PATH
if command -v aws &> /dev/null; then
    AWS_CMD="aws"
else
    AWS_CMD="docker run --rm -v $HOME/.aws:/root/.aws amazon/aws-cli"
fi

echo "==> Authenticating with ECR..."
$AWS_CMD ecr get-login-password --region $REGION | docker login --username AWS --password-stdin 678954237808.dkr.ecr.us-east-1.amazonaws.com

case "$MODE" in
  staging)
    echo "==> Building Docker image for linux/amd64..."
    docker build --platform linux/amd64 -t test-tickets-uat "$SCRIPT_DIR"

    echo "==> Tagging and pushing to ECR (:staging and :$GIT_SHA)..."
    docker tag test-tickets-uat:latest "$ECR_REPO:staging"
    docker tag test-tickets-uat:latest "$ECR_REPO:$GIT_SHA"
    docker push "$ECR_REPO:staging"
    docker push "$ECR_REPO:$GIT_SHA"

    echo "==> Done! Deployed :staging and :$GIT_SHA"
    echo "    Use 'uat-staging' label to test, then './deploy.sh promote' to go live."
    ;;

  promote)
    echo "==> Pulling :staging image..."
    docker pull "$ECR_REPO:staging"

    echo "==> Retagging as :latest and pushing..."
    docker tag "$ECR_REPO:staging" "$ECR_REPO:latest"
    docker push "$ECR_REPO:latest"

    echo "==> Done! Promoted :staging to :latest (production)"
    ;;

  latest)
    echo "==> Building Docker image for linux/amd64..."
    docker build --platform linux/amd64 -t test-tickets-uat "$SCRIPT_DIR"

    echo "==> Tagging and pushing directly to :latest (hotfix mode)..."
    docker tag test-tickets-uat:latest "$ECR_REPO:latest"
    docker push "$ECR_REPO:latest"

    echo "==> Done! Deployed directly to :latest (hotfix mode)"
    ;;

  *)
    echo "Usage: $0 [staging|promote|latest]"
    echo ""
    echo "Modes:"
    echo "  staging  - Build and push to :staging tag (default)"
    echo "  promote  - Copy :staging to :latest (production)"
    echo "  latest   - Build and push directly to :latest (emergency hotfix)"
    exit 1
    ;;
esac
