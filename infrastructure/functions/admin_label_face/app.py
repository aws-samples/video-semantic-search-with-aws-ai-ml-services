import json
import logging
import boto3
from botocore.exceptions import ClientError
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

neptune_client = None

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "PUT,OPTIONS",
    "Content-Type": "application/json",
}


def lambda_handler(event, context):
    try:
        if not is_admin(event):
            return {
                "statusCode": 403,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "Forbidden: admin group membership required"}),
            }

        face_id = event.get("pathParameters", {}).get("faceId")
        if not face_id:
            return {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "Missing required path parameter: faceId"}),
            }

        body = json.loads(event.get("body", "{}"))
        label = body.get("label")
        if not label or not isinstance(label, str) or not label.strip():
            return {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "Missing or empty 'label' in request body"}),
            }
        label = label.strip()

        neptune_graph_id = os.environ["neptune_graph_id"]
        region = os.environ["region"]

        global neptune_client
        if neptune_client is None:
            neptune_client = boto3.client("neptune-graph", region_name=region)

        # Update Neptune Face node label
        update_face_label(neptune_graph_id, face_id, label)

        # Find how many segments this face appears in
        segment_count = count_segments_for_face(neptune_graph_id, face_id)

        logger.info(
            "Face %s labeled as '%s', appears in %d segments",
            face_id, label, segment_count,
        )

        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps({
                "faceId": face_id,
                "label": label,
                "segmentCount": segment_count,
            }),
        }

    except json.JSONDecodeError:
        return {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "Invalid JSON in request body"}),
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


def update_face_label(neptune_graph_id, face_id, label):
    """Update the label property on a Neptune Face node."""
    query = "MATCH (f:Face {faceId: $faceId}) SET f.label = $label RETURN f.faceId AS faceId"

    response = neptune_client.execute_query(
        graphIdentifier=neptune_graph_id,
        queryString=query,
        parameters={"faceId": face_id, "label": label},
        language="OPEN_CYPHER",
    )

    payload = json.loads(response["payload"].read().decode("utf-8"))
    results = payload.get("results", [])
    if not results:
        logger.warning("No Face node found with faceId=%s", face_id)

    logger.info("Neptune Face node updated: faceId=%s label=%s", face_id, label)


def count_segments_for_face(neptune_graph_id, face_id):
    """Count how many segments a face appears in."""
    query = (
        "MATCH (f:Face {faceId: $faceId})-[:APPEARS_IN_SEGMENT]->(s:Segment) "
        "RETURN count(s) AS segmentCount"
    )

    response = neptune_client.execute_query(
        graphIdentifier=neptune_graph_id,
        queryString=query,
        parameters={"faceId": face_id},
        language="OPEN_CYPHER",
    )

    payload = json.loads(response["payload"].read().decode("utf-8"))
    results = payload.get("results", [])
    if results:
        return results[0].get("segmentCount", 0)
    return 0
