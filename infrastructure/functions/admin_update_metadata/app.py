import json
import logging
import boto3
from botocore.exceptions import ClientError
from botocore.config import Config
import os
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth

logger = logging.getLogger()
logger.setLevel(logging.INFO)

neptune_client = boto3.client("neptune-graph")
bedrock_client = boto3.client(
    "bedrock-runtime",
    config=Config(read_timeout=900, retries={"max_attempts": 3, "mode": "standard"}),
)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "PUT,OPTIONS",
    "Content-Type": "application/json",
}

# Fields that may be updated via this endpoint
ALLOWED_FIELDS = {"shot_description", "shot_publicFigures", "shot_privateFigures", "shot_transcript", "shot_faces"}


def lambda_handler(event, context):
    try:
        # Verify admin authorization
        if not is_admin(event):
            return {
                "statusCode": 403,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "Forbidden: admin group membership required"}),
            }

        shot_id_param = event.get("pathParameters", {}).get("shotId")
        if not shot_id_param:
            return {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "Missing required path parameter: shotId"}),
            }

        body = json.loads(event.get("body", "{}"))
        if not body:
            return {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "Request body is empty"}),
            }

        # Validate that only allowed fields are present
        invalid_fields = set(body.keys()) - ALLOWED_FIELDS
        if invalid_fields:
            return {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": f"Invalid fields: {', '.join(sorted(invalid_fields))}"}),
            }

        # Parse composite shot ID: {jobId}_{shot_id}
        separator_index = shot_id_param.find("_")
        if separator_index == -1:
            return {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "Invalid shotId format. Expected: {jobId}_{shot_id}"}),
            }

        job_id = shot_id_param[:separator_index]
        shot_id = shot_id_param[separator_index + 1:]

        aoss_host = os.environ["aoss_host"]
        aoss_visual_index = os.environ["aoss_visual_index"]
        region = os.environ["region"]
        neptune_graph_id = os.environ["neptune_graph_id"]

        # 1. Update OpenSearch document
        update_opensearch(aoss_host, region, aoss_visual_index, job_id, shot_id, body)

        # 2. Update Neptune Segment node properties
        update_neptune(neptune_graph_id, job_id, shot_id, body)

        logger.info("Successfully updated metadata for job=%s shot=%s", job_id, shot_id)

        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps({"status": "updated"}),
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


def get_opensearch_client(host, region):
    """Create an OpenSearch client with AWS IAM authentication."""
    host = host.split("://")[1] if "://" in host else host
    credentials = boto3.Session().get_credentials()
    auth = AWSV4SignerAuth(credentials, region, "aoss")

    client = OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        pool_maxsize=20,
    )
    return client


def update_opensearch(aoss_host, region, aoss_visual_index, job_id, shot_id, updates):
    """Update OpenSearch document by searching for its auto-generated _id,
    then doing a partial update by ID.

    If shot_description changes, also regenerate shot_desc_vector embedding.
    Image/video embeddings are unchanged (based on composite image and clip).
    """
    client = get_opensearch_client(aoss_host, region)

    # Search for the document's auto-generated _id
    search_body = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"jobId": job_id}},
                    {"term": {"shot_id": shot_id}},
                ]
            }
        },
        "_source": False,
        "size": 1,
    }
    response = client.search(index=aoss_visual_index, body=search_body)
    hits = response.get("hits", {}).get("hits", [])
    if not hits:
        logger.warning("No OpenSearch document found for job=%s shot=%s", job_id, shot_id)
        return

    doc_id = hits[0]["_id"]
    partial_doc = dict(updates)

    # Regenerate text embedding if description changed
    if "shot_description" in updates:
        embedding_model = os.environ["embedding_model"]
        partial_doc["shot_desc_vector"] = generate_text_embedding(
            embedding_model, updates["shot_description"]
        )

    client.update(
        index=aoss_visual_index,
        id=doc_id,
        body={"doc": partial_doc},
        params={"timeout": 60},
    )
    logger.info("OpenSearch document updated for job=%s shot=%s", job_id, shot_id)


def generate_text_embedding(model_id, text):
    """Generate a text embedding using Nova v2 multimodal embeddings."""
    if not text:
        text = " "

    body = json.dumps({
        "taskType": "SINGLE_EMBEDDING",
        "singleEmbeddingParams": {
            "embeddingPurpose": "GENERIC_INDEX",
            "embeddingDimension": 3072,
            "text": {
                "truncationMode": "END",
                "value": text,
            },
        },
    })

    response = bedrock_client.invoke_model(
        body=body,
        modelId=model_id,
        accept="application/json",
        contentType="application/json",
    )
    response_body = json.loads(response["body"].read())
    return response_body["embeddings"][0]["embedding"]


def update_neptune(neptune_graph_id, job_id, shot_id, updates):
    """Update Neptune Segment node properties.

    Segment nodes are keyed by segmentId = "{jobId}_{shot_id}".
    Frontend field names are mapped to Neptune property names:
      shot_description → description
      shot_transcript  → transcript
    """
    # Map frontend field names to Neptune Segment property names
    field_map = {
        "shot_description": "description",
        "shot_transcript": "transcript",
    }

    set_clauses = []
    params = {"segmentId": f"{job_id}_{shot_id}"}

    for field, value in updates.items():
        neptune_prop = field_map.get(field)
        if not neptune_prop:
            continue
        param_name = neptune_prop
        set_clauses.append(f"s.{neptune_prop} = ${param_name}")
        params[param_name] = value

    if not set_clauses:
        return

    query = (
        "MATCH (s:Segment {segmentId: $segmentId}) "
        f"SET {', '.join(set_clauses)} "
        "RETURN s"
    )

    neptune_client.execute_query(
        graphIdentifier=neptune_graph_id,
        queryString=query,
        parameters=params,
        language="OPEN_CYPHER",
    )
    logger.info("Neptune update completed for job=%s shot=%s", job_id, shot_id)


