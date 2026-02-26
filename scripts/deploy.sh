#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Parse arguments
DEPLOY_FRONTEND=false
DEPLOY_BACKEND=false

usage() {
  echo "Usage: ./deploy.sh [OPTIONS]"
  echo ""
  echo "Options:"
  echo "  --frontend, -f    Deploy frontend only"
  echo "  --backend, -b     Deploy backend (SAM stack)"
  echo "  --all             Deploy everything (default if no options)"
  echo "  --help, -h        Show this help message"
  echo ""
  echo "Examples:"
  echo "  ./deploy.sh                  # Deploy all"
  echo "  ./deploy.sh --backend        # Deploy backend only"
  echo "  ./deploy.sh -f               # Deploy frontend only"
  exit 0
}

# Parse command line arguments
if [ $# -eq 0 ]; then
  DEPLOY_FRONTEND=true
  DEPLOY_BACKEND=true
else
  while [[ $# -gt 0 ]]; do
    case $1 in
      --frontend|-f)
        DEPLOY_FRONTEND=true
        shift
        ;;
      --backend|-b)
        DEPLOY_BACKEND=true
        shift
        ;;
      --all)
        DEPLOY_FRONTEND=true
        DEPLOY_BACKEND=true
        shift
        ;;
      --help|-h)
        usage
        ;;
      *)
        echo "Unknown option: $1"
        usage
        ;;
    esac
  done
fi

# Load config from samconfig.toml
REGION=$(grep 'region = ' "$PROJECT_DIR/infrastructure/samconfig.toml" | cut -d'"' -f2)
STACK_NAME=$(grep 'stack_name = ' "$PROJECT_DIR/infrastructure/samconfig.toml" | cut -d'"' -f2)

echo "=== Video Semantic Search v2.0 Deployment ==="
echo "==> Region: $REGION, Stack: $STACK_NAME"
echo "==> Deploying: frontend=$DEPLOY_FRONTEND, backend=$DEPLOY_BACKEND"
echo ""

# Helper function to get stack outputs
get_output() {
  aws cloudformation describe-stacks \
    --stack-name $STACK_NAME \
    --region $REGION \
    --query "Stacks[0].Outputs[?OutputKey==\`$1\`].OutputValue" \
    --output text
}

# Deploy backend (SAM stack)
deploy_backend() {
  echo "==> Building SAM stack..."
  cd "$PROJECT_DIR/infrastructure"
  sam build --use-container

  echo "==> Deploying SAM stack..."
  sam deploy --no-fail-on-empty-changeset

  echo ""
  echo "==> Backend deployment complete!"
}

# Deploy frontend
deploy_frontend() {
  cd "$PROJECT_DIR/frontend"

  echo "==> Installing frontend dependencies..."
  npm install

  echo "==> Generating frontend config from stack outputs..."
  npm run config

  echo "==> Building frontend..."
  npm run build

  echo "==> Uploading frontend to S3..."
  BUCKET=$(node cli.js echo-bucket)
  aws s3 sync dist/ "s3://${BUCKET}" --delete --region $REGION

  echo "==> Invalidating CloudFront cache..."
  DIST_ID=$(aws cloudformation describe-stacks \
    --stack-name $STACK_NAME \
    --region $REGION \
    --query "Stacks[0].Outputs[?OutputKey==\`CloudFrontDistributionId\`].OutputValue" \
    --output text 2>/dev/null)
  if [ -n "$DIST_ID" ] && [ "$DIST_ID" != "None" ]; then
    aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" --region $REGION > /dev/null
    echo "    Invalidation created for distribution $DIST_ID"
  fi

  echo ""
  echo "==> Frontend deployment complete!"
}

# Execute deployments in order
if [ "$DEPLOY_BACKEND" = true ]; then
  deploy_backend
fi

if [ "$DEPLOY_FRONTEND" = true ]; then
  deploy_frontend
fi

# Show outputs
echo ""
echo "==> Deployment complete!"
echo ""
echo "Stack outputs:"
aws cloudformation describe-stacks \
  --stack-name $STACK_NAME \
  --region $REGION \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
  --output table 2>/dev/null || true

# Show the web URL prominently at the end
WEB_URL=$(get_output "WebUrl")
if [ -n "$WEB_URL" ] && [ "$WEB_URL" != "None" ]; then
  echo ""
  echo "============================================"
  echo "  Application URL: https://$WEB_URL"
  echo "============================================"
fi
