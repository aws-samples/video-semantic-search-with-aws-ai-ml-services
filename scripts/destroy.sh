#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

# Load config
REGION=$(grep 'region = ' infrastructure/samconfig.toml | cut -d'"' -f2)
STACK_NAME=$(grep 'stack_name = ' infrastructure/samconfig.toml | cut -d'"' -f2)
FACE_COLLECTION_ID="vss-global-faces"

echo "==> Region: $REGION"
echo "==> Main Stack: $STACK_NAME"
echo "==> AgentCore Stack: ${STACK_NAME}-agentcore"
echo ""
echo "WARNING: This will delete ALL resources including:"
echo "  - All S3 buckets and their contents"
echo "  - OpenSearch Serverless collection"
echo "  - Neptune Analytics graph"
echo "  - DynamoDB tables"
echo "  - Lambda functions"
echo "  - AgentCore runtime and ECR repository"
echo "  - Rekognition face collection"
echo "  - Cognito user pool"
echo "  - CloudFront distribution"
echo ""
read -p "Are you sure you want to proceed? (yes/no): " confirm
if [ "$confirm" != "yes" ]; then
  echo "Aborted."
  exit 0
fi

# Helper function to get stack outputs
get_output() {
  aws cloudformation describe-stacks \
    --stack-name $STACK_NAME \
    --region $REGION \
    --query "Stacks[0].Outputs[?OutputKey==\`$1\`].OutputValue" \
    --output text 2>/dev/null || echo ""
}

# Helper function to get bucket name from stack resource
get_bucket_physical_id() {
  local logical_id=$1
  aws cloudformation describe-stack-resource \
    --stack-name $STACK_NAME \
    --logical-resource-id $logical_id \
    --region $REGION \
    --query "StackResourceDetail.PhysicalResourceId" \
    --output text 2>/dev/null || echo ""
}

# Helper function to empty S3 bucket
empty_bucket() {
  local bucket=$1
  if [ -n "$bucket" ] && [ "$bucket" != "None" ]; then
    echo "  Emptying bucket: $bucket"
    aws s3 rm "s3://$bucket" --recursive --region $REGION 2>/dev/null || true
    # Also delete versions for versioned buckets
    aws s3api list-object-versions --bucket "$bucket" --region $REGION --output json 2>/dev/null | \
      jq -r '.Versions[]? | "\(.Key) \(.VersionId)"' 2>/dev/null | \
      while read key version; do
        aws s3api delete-object --bucket "$bucket" --key "$key" --version-id "$version" --region $REGION 2>/dev/null || true
      done
    aws s3api list-object-versions --bucket "$bucket" --region $REGION --output json 2>/dev/null | \
      jq -r '.DeleteMarkers[]? | "\(.Key) \(.VersionId)"' 2>/dev/null | \
      while read key version; do
        aws s3api delete-object --bucket "$bucket" --key "$key" --version-id "$version" --region $REGION 2>/dev/null || true
      done
  fi
}

echo ""
echo "==> Step 1: Getting S3 bucket names..."
BUCKET_VIDEOS=$(get_bucket_physical_id "S3Videos")
BUCKET_SHOTS=$(get_bucket_physical_id "S3Shots")
BUCKET_IMAGES=$(get_bucket_physical_id "S3Images")
BUCKET_TRANSCRIPTS=$(get_bucket_physical_id "S3Transcripts")
BUCKET_CLIP_SEARCH=$(get_bucket_physical_id "S3ClipSearch")
BUCKET_STATIC_WEB=$(get_bucket_physical_id "S3StaticWeb")
BUCKET_LOGGING=$(get_bucket_physical_id "S3Logging")
BUCKET_AGENTCORE=$(get_bucket_physical_id "AgentCoreCodeBucket")

echo "==> Step 2: Emptying S3 buckets..."
empty_bucket "$BUCKET_VIDEOS"
empty_bucket "$BUCKET_SHOTS"
empty_bucket "$BUCKET_IMAGES"
empty_bucket "$BUCKET_TRANSCRIPTS"
empty_bucket "$BUCKET_CLIP_SEARCH"
empty_bucket "$BUCKET_STATIC_WEB"
empty_bucket "$BUCKET_LOGGING"
empty_bucket "$BUCKET_AGENTCORE"

echo ""
echo "==> Step 3: Deleting Rekognition face collection..."
aws rekognition delete-collection \
  --collection-id "$FACE_COLLECTION_ID" \
  --region $REGION 2>/dev/null || true

echo ""
echo "==> Step 4: Deleting AgentCore stack..."
aws cloudformation delete-stack \
  --stack-name "${STACK_NAME}-agentcore" \
  --region $REGION 2>/dev/null || true

echo "  Waiting for AgentCore stack deletion..."
aws cloudformation wait stack-delete-complete \
  --stack-name "${STACK_NAME}-agentcore" \
  --region $REGION 2>/dev/null || true

echo ""
echo "==> Step 5: Deleting main stack..."
aws cloudformation delete-stack \
  --stack-name $STACK_NAME \
  --region $REGION

echo "  Waiting for main stack deletion..."
aws cloudformation wait stack-delete-complete \
  --stack-name $STACK_NAME \
  --region $REGION

echo ""
echo "==> Cleanup complete!"
echo ""
echo "Note: Some resources may take additional time to fully delete."
echo "Check the AWS Console if you encounter any issues."
