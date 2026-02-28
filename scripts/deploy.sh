#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INFRA_DIR="$PROJECT_DIR/infrastructure"

DEPLOY_FRONTEND=false
DEPLOY_BACKEND=false

usage() {
  echo "Usage: ./deploy.sh [OPTIONS]"
  echo ""
  echo "Options:"
  echo "  --frontend, -f    Deploy frontend only"
  echo "  --backend, -b     Deploy backend (SAM stack + AgentCore)"
  echo "  --all             Deploy everything (default if no options)"
  echo "  --help, -h        Show this help message"
  echo ""
  echo "Examples:"
  echo "  ./deploy.sh                  # Deploy all"
  echo "  ./deploy.sh --backend        # Deploy backend only"
  echo "  ./deploy.sh -f               # Deploy frontend only"
  exit 0
}

if [ $# -eq 0 ]; then
  DEPLOY_FRONTEND=true
  DEPLOY_BACKEND=true
else
  while [[ $# -gt 0 ]]; do
    case $1 in
      --frontend|-f) DEPLOY_FRONTEND=true; shift ;;
      --backend|-b)  DEPLOY_BACKEND=true; shift ;;
      --all)         DEPLOY_FRONTEND=true; DEPLOY_BACKEND=true; shift ;;
      --help|-h)     usage ;;
      *)             echo "Unknown option: $1"; usage ;;
    esac
  done
fi

REGION=$(grep 'region = ' "$INFRA_DIR/samconfig.toml" | cut -d'"' -f2)
STACK_NAME=$(grep 'stack_name = ' "$INFRA_DIR/samconfig.toml" | cut -d'"' -f2)
BUILD_TRIGGER=$(find "$INFRA_DIR/agents" -type f \( -name "*.py" -o -name "Dockerfile" -o -name "requirements.txt" \) -exec cat {} \; 2>/dev/null | shasum -a 256 | cut -d' ' -f1)

echo "=== Video Semantic Search ==="
echo "Region: $REGION | Stack: $STACK_NAME"
echo "Deploy: frontend=$DEPLOY_FRONTEND, backend=$DEPLOY_BACKEND"
echo ""

get_output() {
  aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey==\`$1\`].OutputValue" --output text
}

get_agentcore_output() {
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}-agentcore" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey==\`$1\`].OutputValue" --output text 2>/dev/null || echo ""
}

deploy_backend() {
  AGENTCORE_RUNTIME_ARN=$(get_agentcore_output "AgentRuntimeArn")

  # 1. Build and deploy main SAM stack
  echo "==> Building SAM stack..."
  cd "$INFRA_DIR"
  sam build --use-container

  echo "==> Deploying SAM stack..."
  if [ -n "$AGENTCORE_RUNTIME_ARN" ] && [ "$AGENTCORE_RUNTIME_ARN" != "None" ]; then
    sam deploy --no-fail-on-empty-changeset --parameter-overrides "AgentCoreRuntimeArn=$AGENTCORE_RUNTIME_ARN"
  else
    sam deploy --no-fail-on-empty-changeset
  fi

  # 2. Check if AgentCore code has changed
  BUCKET=$(get_output "AgentCoreCodeBucket")
  AOSS_HOST=$(get_output "AossHost")
  NEPTUNE_GRAPH_ID=$(get_output "NeptuneGraphId")
  EMBEDDING_MODEL=$(get_output "EmbeddingModel")
  BEDROCK_LLM_MODEL=$(get_output "BedrockAgentLlm")

  if [ -z "$BUCKET" ] || [ "$BUCKET" == "None" ]; then
    echo "ERROR: Could not get AgentCoreCodeBucket from stack outputs"
    exit 1
  fi

  DEPLOYED_HASH=$(get_agentcore_output "BuildTrigger" 2>/dev/null || echo "")
  if [ "$BUILD_TRIGGER" = "$DEPLOYED_HASH" ]; then
    echo "==> AgentCore code unchanged (hash: ${BUILD_TRIGGER:0:12}...), skipping AgentCore deployment"
  else
    echo "==> AgentCore code changed (${DEPLOYED_HASH:0:12}... -> ${BUILD_TRIGGER:0:12}...)"

    # 3. Package and upload agent code
    echo ""
    echo "==> Packaging agent code..."
    cd "$INFRA_DIR/agents"
    zip -r "$INFRA_DIR/agents.zip" . \
      -x "*.pyc" "*__pycache__*" "*/.venv/*" "*/.DS_Store" "*.bedrock_agentcore.yaml"
    aws s3 cp "$INFRA_DIR/agents.zip" "s3://$BUCKET/agents.zip" --region "$REGION"
    rm -f "$INFRA_DIR/agents.zip"

    # 4. Build and deploy AgentCore stack (separate build dir to preserve main template)
    echo ""
    echo "==> Deploying AgentCore stack..."
    cd "$INFRA_DIR"
    sam build --use-container --template-file template-agentcore.yaml --build-dir .aws-sam/build-agentcore

    sam deploy \
      --template-file .aws-sam/build-agentcore/template.yaml \
      --stack-name "${STACK_NAME}-agentcore" \
      --capabilities CAPABILITY_NAMED_IAM \
      --parameter-overrides \
        S3BucketName="$BUCKET" \
        AossHost="$AOSS_HOST" \
        NeptuneGraphId="$NEPTUNE_GRAPH_ID" \
        BedrockLlmModel="$BEDROCK_LLM_MODEL" \
        EmbeddingModel="$EMBEDDING_MODEL" \
        BuildTrigger="$BUILD_TRIGGER" \
        ImageTag="$BUILD_TRIGGER" \
      --region "$REGION" \
      --resolve-s3 \
      --no-confirm-changeset \
      --no-fail-on-empty-changeset

    # 5. Link AgentCore Runtime ARN to Search Lambda (first time only)
    if [ -z "$AGENTCORE_RUNTIME_ARN" ] || [ "$AGENTCORE_RUNTIME_ARN" = "None" ]; then
      AGENTCORE_RUNTIME_ARN=$(get_agentcore_output "AgentRuntimeArn")
      if [ -n "$AGENTCORE_RUNTIME_ARN" ] && [ "$AGENTCORE_RUNTIME_ARN" != "None" ]; then
        echo ""
        echo "==> Linking AgentCore Runtime to Search Lambda..."
        sam deploy --no-fail-on-empty-changeset --parameter-overrides "AgentCoreRuntimeArn=$AGENTCORE_RUNTIME_ARN"
      fi
    fi
  fi

  echo ""
  echo "==> Backend deployment complete!"
}

deploy_frontend() {
  cd "$PROJECT_DIR/frontend"

  echo "==> Installing dependencies..."
  npm install

  echo "==> Generating config from stack outputs..."
  npm run config

  echo "==> Building frontend..."
  npm run build

  echo "==> Uploading to S3..."
  BUCKET=$(node cli.js echo-bucket)
  aws s3 sync dist/ "s3://${BUCKET}" --delete --region "$REGION"

  DIST_ID=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey==\`CloudFrontDistributionId\`].OutputValue" \
    --output text 2>/dev/null)
  if [ -n "$DIST_ID" ] && [ "$DIST_ID" != "None" ]; then
    echo "==> Invalidating CloudFront cache..."
    aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" --region "$REGION" > /dev/null
  fi

  echo ""
  echo "==> Frontend deployment complete!"
}

[ "$DEPLOY_BACKEND" = true ] && deploy_backend
[ "$DEPLOY_FRONTEND" = true ] && deploy_frontend

echo ""
echo "==> Done!"
echo ""
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' --output table 2>/dev/null || true
aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}-agentcore" --region "$REGION" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' --output table 2>/dev/null || true

WEB_URL=$(get_output "WebUrl")
if [ -n "$WEB_URL" ] && [ "$WEB_URL" != "None" ]; then
  echo ""
  echo "============================================"
  echo "  https://${WEB_URL}"
  echo "============================================"
fi
