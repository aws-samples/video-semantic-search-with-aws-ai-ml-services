import json
import logging
import boto3
from botocore.exceptions import ClientError
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

neptune_client = None
rekognition_client = None

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "DELETE,OPTIONS",
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

        neptune_graph_id = os.environ["neptune_graph_id"]
        region = os.environ["region"]
        face_collection_id = os.environ["face_collection_id"]

        global neptune_client
        if neptune_client is None:
            neptune_client = boto3.client("neptune-graph", region_name=region)

        # Delete Face node and all its edges from Neptune
        deleted = delete_face_from_neptune(neptune_graph_id, face_id)

        # Delete from Rekognition collection
        delete_face_from_rekognition(face_collection_id, face_id, region)

        logger.info("Face %s deleted (neptune_deleted=%d)", face_id, deleted)

        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps({"status": "deleted", "faceId": face_id}),
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


def delete_face_from_neptune(neptune_graph_id, face_id):
    """Delete a Face node and all its edges from Neptune."""
    query = "MATCH (f:Face {faceId: $faceId}) DETACH DELETE f RETURN count(f) AS deleted"

    response = neptune_client.execute_query(
        graphIdentifier=neptune_graph_id,
        queryString=query,
        parameters={"faceId": face_id},
        language="OPEN_CYPHER",
    )

    payload = json.loads(response["payload"].read().decode("utf-8"))
    results = payload.get("results", [])
    deleted = results[0].get("deleted", 0) if results else 0

    if deleted == 0:
        logger.warning("No Face node found with faceId=%s", face_id)

    return deleted


def delete_face_from_rekognition(face_collection_id, face_id, region):
    """Delete a face from the Rekognition collection."""
    global rekognition_client
    if rekognition_client is None:
        rekognition_client = boto3.client("rekognition", region_name=region)

    try:
        rekognition_client.delete_faces(
            CollectionId=face_collection_id,
            FaceIds=[face_id],
        )
        logger.info("Deleted face from Rekognition collection: %s", face_id)
    except ClientError as e:
        # Face may not exist in Rekognition (e.g. celebrity faces use a different ID scheme)
        if e.response["Error"]["Code"] == "InvalidParameterException":
            logger.info("Face %s not found in Rekognition collection, skipping", face_id)
        else:
            raise
