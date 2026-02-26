import json
import logging
import boto3
from botocore.exceptions import ClientError
import os
from decimal import Decimal

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb_resource = boto3.resource("dynamodb")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,OPTIONS",
    "Content-Type": "application/json",
}


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal types from DynamoDB."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            # Return int if no decimal component, otherwise float
            if obj % 1 == 0:
                return int(obj)
            return float(obj)
        return super().default(obj)


def lambda_handler(event, context):
    try:
        # Verify admin authorization
        if not is_admin(event):
            return {
                "statusCode": 403,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "Forbidden: admin group membership required"}),
            }

        job_id = event.get("pathParameters", {}).get("jobId")
        if not job_id:
            return {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "Missing required path parameter: jobId"}),
            }

        dynamodb_table = os.environ["vss_dynamodb_table"]

        # Get the DynamoDB item for this job
        table = dynamodb_resource.Table(dynamodb_table)
        response = table.get_item(Key={"JobId": job_id})

        item = response.get("Item")
        if not item:
            return {
                "statusCode": 404,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": f"Job not found: {job_id}"}),
            }

        # Extract cost entries
        cost_entries = item.get("CostEntries", [])

        # Aggregate costs by service
        by_service = {}
        total_cost = Decimal("0")

        for entry in cost_entries:
            service = entry.get("service", "Unknown")
            cost = entry.get("cost", Decimal("0"))
            if isinstance(cost, (int, float)):
                cost = Decimal(str(cost))

            by_service[service] = by_service.get(service, Decimal("0")) + cost
            total_cost += cost

        # Build response
        result = {
            "jobId": job_id,
            "entries": cost_entries,
            "totalCostUsd": total_cost,
            "byService": by_service,
        }

        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps(result, cls=DecimalEncoder),
        }

    except ClientError as e:
        logger.error("AWS service error: %s", str(e))
        return {
            "statusCode": 502,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "AWS service error", "detail": str(e)}),
        }
    except Exception as e:
        logger.error("Unexpected error: %s", str(e), exc_info=True)
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "Internal server error", "detail": str(e)}),
        }


def is_admin(event):
    """Check if the caller belongs to the 'admin' Cognito group."""
    try:
        claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
        groups = claims.get("cognito:groups", "")
        if isinstance(groups, list):
            return "admin" in groups
        if isinstance(groups, str):
            cleaned = groups.strip("[] ")
            group_list = [g.strip() for g in cleaned.split(",") if g.strip()]
            return "admin" in group_list
        return False
    except (KeyError, AttributeError):
        return False
