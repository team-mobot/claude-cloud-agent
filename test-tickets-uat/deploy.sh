#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ECR_REPO="678954237808.dkr.ecr.us-east-1.amazonaws.com/test-tickets-uat"
REGION="us-east-1"

# Use docker-based aws CLI if aws not in PATH
if command -v aws &> /dev/null; then
    AWS_CMD="aws"
else
    AWS_CMD="docker run --rm -v $HOME/.aws:/root/.aws amazon/aws-cli"
fi

echo "==> Authenticating with ECR..."
$AWS_CMD ecr get-login-password --region $REGION | docker login --username AWS --password-stdin 678954237808.dkr.ecr.us-east-1.amazonaws.com

echo "==> Building Docker image..."
docker build -t test-tickets-uat "$SCRIPT_DIR"

echo "==> Tagging and pushing to ECR..."
docker tag test-tickets-uat:latest "$ECR_REPO:latest"
docker push "$ECR_REPO:latest"

echo "==> Done! New tasks will use the updated image."
