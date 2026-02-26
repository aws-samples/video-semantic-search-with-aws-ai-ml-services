import json
import logging
import base64
import os
import time
from decimal import Decimal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth

logger = logging.getLogger()
logger.setLevel(logging.INFO)

bedrock_config = Config(
    read_timeout=900,
    retries={"max_attempts": 10, "mode": "adaptive"},
)

bedrock_client = boto3.client(service_name="bedrock-runtime", config=bedrock_config)
s3_client = boto3.client("s3")
dynamodb_resource = boto3.resource("dynamodb")


def lambda_handler(event, context):
    """Consolidates embedding generation, OpenSearch indexing, Neptune graph writing,
    and S3 JSON saving for a single video shot."""

    # Environment variables
    region = os.environ["region"]
    bucket_videos = os.environ["bucket_videos"]
    bucket_shots = os.environ["bucket_shots"]
    bucket_images = os.environ["bucket_images"]
    embedding_model = os.environ["embedding_model"]
    aoss_host = os.environ["aoss_host"]
    aoss_visual_index = os.environ["aoss_visual_index"]
    neptune_graph_id = os.environ["neptune_graph_id"]
    vss_dynamodb_table = os.environ["vss_dynamodb_table"]

    # Event fields
    jobId = event["jobId"]
    video_name = event["video_name"]
    shot_id = event["shot_id"]
    shot_startTime = event["shot_startTime"]
    shot_endTime = event["shot_endTime"]
    shot_description = event.get("shot_description", "")
    shot_transcript = event.get("shot_transcript", "")
    frames = event.get("frames", [])
    composite_key = event.get("composite_key", "")
    clip_key = event.get("clip_key", "")
    faces = event.get("faces", [])

    segment_id = f"{jobId}_{shot_id}"
    bedrock_calls = []

    try:
        # ------------------------------------------------------------------
        # 1. Generate Nova v2 embeddings
        # ------------------------------------------------------------------

        # 1a. Text embedding (shot description)
        logger.info("Generating text embedding for %s/%s", jobId, shot_id)
        text_embedding, text_call_cost = generate_text_embedding(
            embedding_model, shot_description
        )
        bedrock_calls.append(text_call_cost)

        # 1b. Image embedding (composite tile image)
        logger.info("Generating image embedding for %s/%s", jobId, shot_id)
        image_embedding, image_call_cost = generate_image_embedding(
            embedding_model, bucket_images, composite_key
        )
        bedrock_calls.append(image_call_cost)

        # 1c. Video embedding (clip with audio)
        logger.info("Generating video embedding for %s/%s", jobId, shot_id)
        video_embedding, video_call_cost = generate_video_embedding(
            embedding_model, bucket_shots, clip_key
        )
        bedrock_calls.append(video_call_cost)

        # ------------------------------------------------------------------
        # 2. Index to OpenSearch Serverless (visual index)
        # ------------------------------------------------------------------
        logger.info("Indexing to OpenSearch for %s/%s", jobId, shot_id)
        aoss_client = get_opensearch_client(aoss_host, region)

        doc = {
            "jobId": jobId,
            "shot_id": shot_id,
            "shot_description": shot_description,
            "shot_desc_vector": text_embedding,
            "shot_image_vector": image_embedding,
            "shot_video_vector": video_embedding,
        }

        aoss_client.index(
            index=aoss_visual_index,
            body=json.dumps(doc),
            params={"timeout": 60},
        )
        logger.info("OpenSearch indexing complete for %s/%s", jobId, shot_id)

        # ------------------------------------------------------------------
        # 3. Write Neptune graph nodes and edges
        # ------------------------------------------------------------------
        logger.info("Writing Neptune graph for %s/%s", jobId, shot_id)
        write_neptune_graph(
            neptune_graph_id,
            region,
            jobId,
            video_name,
            shot_id,
            segment_id,
            shot_startTime,
            shot_endTime,
            shot_description,
            shot_transcript,
            frames,
            faces,
            bucket_images,
        )
        logger.info("Neptune graph write complete for %s/%s", jobId, shot_id)

        # ------------------------------------------------------------------
        # 4. Track Bedrock API costs in DynamoDB
        # ------------------------------------------------------------------
        track_bedrock_costs(vss_dynamodb_table, jobId, bedrock_calls)

        logger.info("Ingest complete for %s/%s", jobId, shot_id)
        return {"jobId": jobId, "shot_id": shot_id, "status": "indexed"}

    except Exception as e:
        logger.error(
            "Failed to ingest data for %s/%s: %s", jobId, shot_id, str(e), exc_info=True
        )
        raise


# ==========================================================================
# Embedding generation helpers
# ==========================================================================


def generate_text_embedding(model_id, text):
    """Generate a text embedding using Nova v2 multimodal embeddings."""
    if not text:
        text = " "

    body = json.dumps(
        {
            "taskType": "SINGLE_EMBEDDING",
            "singleEmbeddingParams": {
                "embeddingPurpose": "GENERIC_INDEX",
                "embeddingDimension": 3072,
                "text": {
                    "truncationMode": "END",
                    "value": text,
                },
            },
        }
    )

    start_time = time.time()
    response = bedrock_client.invoke_model(
        body=body,
        modelId=model_id,
        accept="application/json",
        contentType="application/json",
    )
    latency = time.time() - start_time

    response_body = json.loads(response["body"].read())
    embedding = response_body["embeddings"][0]["embedding"]

    call_cost = {
        "type": "text_embedding",
        "model": model_id,
        "latency_seconds": round(latency, 3),
    }

    return embedding, call_cost


def generate_image_embedding(model_id, bucket, composite_key):
    """Generate an image embedding from the composite tile image in S3."""
    s3_obj = s3_client.get_object(Bucket=bucket, Key=composite_key)
    image_bytes = s3_obj["Body"].read()
    base64_image = base64.b64encode(image_bytes).decode()

    # Determine format from key extension
    img_format = "png" if composite_key.lower().endswith(".png") else "jpeg"

    body = json.dumps(
        {
            "taskType": "SINGLE_EMBEDDING",
            "singleEmbeddingParams": {
                "embeddingPurpose": "GENERIC_INDEX",
                "embeddingDimension": 3072,
                "image": {
                    "format": img_format,
                    "source": {"bytes": base64_image},
                },
            },
        }
    )

    start_time = time.time()
    response = bedrock_client.invoke_model(
        body=body,
        modelId=model_id,
        accept="application/json",
        contentType="application/json",
    )
    latency = time.time() - start_time

    response_body = json.loads(response["body"].read())
    embedding = response_body["embeddings"][0]["embedding"]

    call_cost = {
        "type": "image_embedding",
        "model": model_id,
        "latency_seconds": round(latency, 3),
    }

    return embedding, call_cost


def generate_video_embedding(model_id, bucket, clip_key):
    """Generate a video embedding from the shot clip stored in S3."""
    s3_uri = f"s3://{bucket}/{clip_key}"

    body = json.dumps(
        {
            "taskType": "SINGLE_EMBEDDING",
            "singleEmbeddingParams": {
                "embeddingPurpose": "GENERIC_INDEX",
                "embeddingDimension": 3072,
                "video": {
                    "format": "mp4",
                    "embeddingMode": "AUDIO_VIDEO_COMBINED",
                    "source": {
                        "s3Location": {"uri": s3_uri},
                    },
                },
            },
        }
    )

    start_time = time.time()
    response = bedrock_client.invoke_model(
        body=body,
        modelId=model_id,
        accept="application/json",
        contentType="application/json",
    )
    latency = time.time() - start_time

    response_body = json.loads(response["body"].read())
    embedding = response_body["embeddings"][0]["embedding"]

    call_cost = {
        "type": "video_embedding",
        "model": model_id,
        "latency_seconds": round(latency, 3),
    }

    return embedding, call_cost


# ==========================================================================
# OpenSearch helper
# ==========================================================================


def get_opensearch_client(host, region):
    """Create an OpenSearch client with AWS SigV4 auth for serverless."""
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


# ==========================================================================
# Neptune graph helpers
# ==========================================================================


def get_neptune_client(region):
    """Create a Neptune Analytics client."""
    return boto3.client("neptune-graph", region_name=region)


def execute_query(neptune_client, graph_id, query, parameters=None):
    """Execute an openCypher query against Neptune Analytics."""
    kwargs = {
        "graphIdentifier": graph_id,
        "language": "OPEN_CYPHER",
        "queryString": query,
    }
    if parameters:
        kwargs["parameters"] = parameters

    try:
        response = neptune_client.execute_query(**kwargs)
        return response
    except ClientError as e:
        logger.error("Neptune query failed: %s | Query: %s", str(e), query)
        raise


def write_neptune_graph(
    graph_id,
    region,
    jobId,
    video_name,
    shot_id,
    segment_id,
    shot_startTime,
    shot_endTime,
    shot_description,
    shot_transcript,
    frames,
    faces,
    bucket_images,
):
    """Write Segment, Frame, and Face nodes/edges to Neptune Analytics."""
    neptune_client = get_neptune_client(region)

    # Derive shot ordering from shot_id (e.g., "shot_0" -> 0)
    try:
        shot_order = int(shot_id.split("_")[-1])
    except (ValueError, IndexError):
        shot_order = 0

    # 3a. Create/merge Segment node
    segment_query = (
        "MERGE (s:Segment {segmentId: $segmentId}) "
        "SET s.startTime = $startTime, "
        "s.endTime = $endTime, "
        "s.description = $description, "
        "s.transcript = $transcript"
    )
    execute_query(
        neptune_client,
        graph_id,
        segment_query,
        parameters={
            "segmentId": segment_id,
            "startTime": shot_startTime,
            "endTime": shot_endTime,
            "description": shot_description if shot_description else "",
            "transcript": shot_transcript if shot_transcript else "",
        },
    )

    # 3b. Link Video -> Segment
    video_segment_query = (
        "MATCH (v:Video {jobId: $jobId}) "
        "MATCH (s:Segment {segmentId: $segmentId}) "
        "MERGE (v)-[:HAS_SEGMENT {sequenceOrder: $order}]->(s)"
    )
    execute_query(
        neptune_client,
        graph_id,
        video_segment_query,
        parameters={
            "jobId": jobId,
            "segmentId": segment_id,
            "order": shot_order,
        },
    )

    # 3c. Create Frame nodes and link to Segment
    for position, frame_timestamp in enumerate(frames):
        frame_id = f"{segment_id}_frame_{position}"
        s3_key = f"{jobId}/{frame_timestamp}.png"

        frame_query = (
            "MERGE (f:Frame {frameId: $frameId}) "
            "SET f.timestampMs = $timestamp, f.s3Key = $s3Key "
            "WITH f "
            "MATCH (s:Segment {segmentId: $segmentId}) "
            "MERGE (s)-[:HAS_FRAME {position: $position}]->(f)"
        )
        execute_query(
            neptune_client,
            graph_id,
            frame_query,
            parameters={
                "frameId": frame_id,
                "timestamp": frame_timestamp,
                "s3Key": s3_key,
                "segmentId": segment_id,
                "position": position,
            },
        )

    # 3d. Link Face -> Segment (APPEARS_IN_SEGMENT)
    for face in faces:
        face_id = face.get("faceId", "")
        label = face.get("label", "Unknown")
        is_celebrity = face.get("isCelebrity", False)

        if not face_id:
            continue

        face_segment_query = (
            "MERGE (face:Face {faceId: $faceId}) "
            "ON CREATE SET face.label = $label, face.isCelebrity = $isCelebrity "
            "WITH face "
            "MATCH (s:Segment {segmentId: $segmentId}) "
            "MERGE (face)-[:APPEARS_IN_SEGMENT]->(s)"
        )
        execute_query(
            neptune_client,
            graph_id,
            face_segment_query,
            parameters={
                "faceId": face_id,
                "label": label,
                "isCelebrity": str(is_celebrity).lower(),
                "segmentId": segment_id,
            },
        )

    # 3e. Create NEXT_SEGMENT edge from previous segment (if order > 0)
    if shot_order > 0:
        prev_shot_id = f"shot_{shot_order - 1}"
        prev_segment_id = f"{jobId}_{prev_shot_id}"

        next_segment_query = (
            "MATCH (prev:Segment {segmentId: $prevSegmentId}) "
            "MATCH (curr:Segment {segmentId: $currSegmentId}) "
            "MERGE (prev)-[:NEXT_SEGMENT]->(curr)"
        )
        execute_query(
            neptune_client,
            graph_id,
            next_segment_query,
            parameters={
                "prevSegmentId": prev_segment_id,
                "currSegmentId": segment_id,
            },
        )


# ==========================================================================
# Cost tracking helper
# ==========================================================================


def track_bedrock_costs(table_name, jobId, bedrock_calls):
    """Append Bedrock API call cost metadata to DynamoDB for the job."""
    try:
        table = dynamodb_resource.Table(table_name)

        cost_entry = {
            "function": "ingest_data",
            "timestamp": int(time.time()),
            "calls": bedrock_calls,
        }

        # Convert floats to Decimal for DynamoDB compatibility
        cost_entry_sanitized = json.loads(json.dumps(cost_entry), parse_float=Decimal)

        table.update_item(
            Key={"JobId": jobId},
            UpdateExpression=(
                "SET #bc = list_append(if_not_exists(#bc, :empty_list), :cost)"
            ),
            ExpressionAttributeNames={"#bc": "BedrockCosts"},
            ExpressionAttributeValues={
                ":cost": [cost_entry_sanitized],
                ":empty_list": [],
            },
        )

        logger.info("Cost tracking updated for job %s", jobId)
    except ClientError as e:
        logger.warning(
            "Failed to track Bedrock costs for job %s: %s", jobId, str(e)
        )
