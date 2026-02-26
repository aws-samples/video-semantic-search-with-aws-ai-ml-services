import json
import logging
import boto3
from botocore.exceptions import ClientError
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client("s3")
neptune_client = None

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,OPTIONS",
    "Content-Type": "application/json",
}

PRESIGNED_URL_EXPIRY = 3600  # 1 hour


def lambda_handler(event, context):
    try:
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

        bucket_images = os.environ["bucket_images"]
        neptune_graph_id = os.environ["neptune_graph_id"]
        region = os.environ["region"]

        global neptune_client
        if neptune_client is None:
            neptune_client = boto3.client("neptune-graph", region_name=region)

        shots = query_all_shots(neptune_graph_id, job_id, bucket_images)

        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps(shots, default=str),
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


def query_all_shots(neptune_graph_id, job_id, bucket_images):
    """Query Neptune for all segments with their frames and faces.

    Uses two queries to avoid cartesian products:
    1. Segments with frames (collected per segment)
    2. All faces grouped by segment
    """
    # Query 1: Get all segments with their frames
    segments_query = (
        "MATCH (v:Video {jobId: $jobId})-[hs:HAS_SEGMENT]->(s:Segment) "
        "OPTIONAL MATCH (s)-[hf:HAS_FRAME]->(f:Frame) "
        "WITH v, s, hs, collect({timestampMs: f.timestampMs, position: hf.position}) AS rawFrames "
        "RETURN v.videoName AS videoName, "
        "s.segmentId AS segmentId, "
        "s.startTime AS startTime, "
        "s.endTime AS endTime, "
        "s.description AS description, "
        "s.transcript AS transcript, "
        "hs.sequenceOrder AS sequenceOrder, "
        "rawFrames AS frames "
        "ORDER BY hs.sequenceOrder"
    )

    seg_response = neptune_client.execute_query(
        graphIdentifier=neptune_graph_id,
        queryString=segments_query,
        parameters={"jobId": job_id},
        language="OPEN_CYPHER",
    )
    seg_payload = json.loads(seg_response["payload"].read().decode("utf-8"))
    segment_rows = seg_payload.get("results", [])

    if not segment_rows:
        return []

    # Query 2: Get all faces for all segments in this job (avoids N+1)
    faces_query = (
        "MATCH (face:Face)-[:APPEARS_IN_SEGMENT]->(s:Segment)<-[:HAS_SEGMENT]-(v:Video {jobId: $jobId}) "
        "RETURN s.segmentId AS segmentId, "
        "face.faceId AS faceId, "
        "face.label AS label, "
        "face.isCelebrity AS isCelebrity, "
        "face.faceImageKey AS faceImageKey"
    )

    faces_response = neptune_client.execute_query(
        graphIdentifier=neptune_graph_id,
        queryString=faces_query,
        parameters={"jobId": job_id},
        language="OPEN_CYPHER",
    )
    faces_payload = json.loads(faces_response["payload"].read().decode("utf-8"))

    # Group faces by segmentId
    faces_by_segment = {}
    for record in faces_payload.get("results", []):
        seg_id = record.get("segmentId", "")
        face_id = record.get("faceId")
        if not face_id:
            continue
        if seg_id not in faces_by_segment:
            faces_by_segment[seg_id] = []

        face_image_key = record.get("faceImageKey", "")
        face_image_url = ""
        if face_image_key:
            face_image_url = generate_presigned_url(bucket_images, face_image_key)

        faces_by_segment[seg_id].append({
            "faceId": face_id,
            "label": record.get("label", ""),
            "isCelebrity": record.get("isCelebrity", "false") == "true",
            "faceImageUrl": face_image_url,
        })

    # Build response
    shots = []
    for row in segment_rows:
        segment_id = row.get("segmentId", "")
        shot_id = segment_id[len(job_id) + 1:] if segment_id.startswith(job_id + "_") else segment_id

        # Process frames: filter nulls from OPTIONAL MATCH, sort by position
        raw_frames = row.get("frames", [])
        valid_frames = [f for f in raw_frames if f.get("timestampMs") is not None]
        valid_frames.sort(key=lambda f: f.get("position", 0))
        frame_timestamps = [f["timestampMs"] for f in valid_frames]

        # Generate presigned URLs for individual frames
        frame_urls = []
        for ts in frame_timestamps:
            frame_key = f"{job_id}/{ts}.png"
            frame_urls.append(generate_presigned_url(bucket_images, frame_key))

        # Derive composite and clip keys from deterministic patterns
        composite_key = f"{job_id}/{shot_id}_composite.png"
        clip_key = f"{job_id}/{shot_id}_clip.mp4"
        composite_url = generate_presigned_url(bucket_images, composite_key)

        shot = {
            "jobId": job_id,
            "video_name": row.get("videoName", ""),
            "shot_id": shot_id,
            "shot_startTime": row.get("startTime"),
            "shot_endTime": row.get("endTime"),
            "shot_description": row.get("description", ""),
            "shot_transcript": row.get("transcript", ""),
            "frames": frame_timestamps,
            "frameUrls": frame_urls,
            "composite_key": composite_key,
            "compositeUrl": composite_url,
            "clip_key": clip_key,
            "faces": faces_by_segment.get(segment_id, []),
        }
        shots.append(shot)

    return shots


def generate_presigned_url(bucket, key):
    """Generate a presigned URL for an S3 object."""
    try:
        url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=PRESIGNED_URL_EXPIRY,
        )
        return url
    except ClientError as e:
        logger.warning("Failed to generate presigned URL for s3://%s/%s: %s", bucket, key, str(e))
        return ""
