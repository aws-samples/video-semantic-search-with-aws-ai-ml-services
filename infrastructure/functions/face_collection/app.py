import json
import logging
import re
import boto3
from botocore.exceptions import ClientError
import os
import io
from PIL import Image

logger = logging.getLogger()
logger.setLevel(logging.INFO)

rek_client = boto3.client("rekognition")
s3_client = boto3.client("s3")
dynamodb_resource = boto3.resource("dynamodb")

# Rekognition pricing (us-east-1)
COST_SEARCH_FACES_BY_IMAGE = 0.001  # per call
COST_INDEX_FACES = 0.001  # per call


def lambda_handler(event, context):
    bucket_images = os.environ["bucket_images"]
    collection_id = os.environ["face_collection_id"]
    neptune_graph_id = os.environ["neptune_graph_id"]
    region = os.environ["region"]
    dynamodb_table_name = os.environ["vss_dynamodb_table"]

    jobId = event["jobId"]
    video_name = event["video_name"]
    shot_id = event["shot_id"]
    shot_startTime = event["shot_startTime"]
    shot_endTime = event["shot_endTime"]
    frames = event["frames"]
    celebrity_result = event.get("celebrityDetectionResult", {})
    celebrity_faces = celebrity_result.get("celebrityFaces", [])
    unrecognized_faces = celebrity_result.get("unrecognizedFaces", [])

    table = dynamodb_resource.Table(dynamodb_table_name)

    neptune_client = boto3.client("neptune-graph", region_name=region)

    # Ensure the face collection exists
    ensure_collection_exists(collection_id)

    config = event.get("config", {})
    face_recognition_enabled = config.get("faceRecognition", True)

    faces = []
    api_call_count = {"SearchFacesByImage": 0, "IndexFaces": 0}

    # Cache for downloaded frame images (avoid re-downloading same frame)
    frame_cache = {}

    # Build unified face list: celebrities always, unknowns only when faceRecognition is on
    all_faces = []
    for celeb in celebrity_faces:
        bbox = celeb.get("boundingBox")
        frame_key = celeb.get("frameKey")
        if not bbox or not frame_key:
            continue
        all_faces.append({
            "boundingBox": bbox,
            "frameKey": frame_key,
            "is_celebrity": True,
            "celebrity_name": celeb["name"],
        })

    if face_recognition_enabled:
        for face in unrecognized_faces:
            bbox = face.get("boundingBox")
            frame_key = face.get("frameKey")
            if not bbox or not frame_key:
                continue
            all_faces.append({
                "boundingBox": bbox,
                "frameKey": frame_key,
                "is_celebrity": False,
                "celebrity_name": None,
            })

    # Process all faces through the Rekognition collection
    processed_face_ids = set()

    for entry in all_faces:
        face_bbox = entry["boundingBox"]
        face_frame_key = entry["frameKey"]
        is_celebrity = entry["is_celebrity"]
        celebrity_name = entry["celebrity_name"]

        # Crop from the individual frame where this face was detected
        frame_data = get_frame_image(frame_cache, bucket_images, face_frame_key)
        cropped_bytes = crop_face(frame_data, face_bbox)

        face_id = None
        face_label = None
        is_new_face = False

        # Search for matching face in the collection
        api_call_count["SearchFacesByImage"] += 1
        try:
            search_response = rek_client.search_faces_by_image(
                CollectionId=collection_id,
                Image={"Bytes": cropped_bytes},
                FaceMatchThreshold=95.0,
                MaxFaces=1
            )
        except rek_client.exceptions.InvalidParameterException:
            logger.warning(
                "SearchFacesByImage could not detect face in crop from %s (bbox: L=%.3f T=%.3f W=%.3f H=%.3f), skipping",
                face_frame_key, face_bbox["Left"], face_bbox["Top"], face_bbox["Width"], face_bbox["Height"]
            )
            continue

        matched_faces = search_response.get("FaceMatches", [])

        if matched_faces:
            face_id = matched_faces[0]["Face"]["FaceId"]
            if face_id in processed_face_ids:
                continue
            if is_celebrity:
                # Celebrity match: use celebrity name as label
                face_label = celebrity_name
            else:
                # Unknown match: preserve existing Neptune label (could be admin-labeled or celebrity)
                face_label = get_face_label_by_id(
                    neptune_client, neptune_graph_id, face_id
                )
                if not face_label:
                    face_label = f"Unknown-{face_id[:8]}"
        else:
            is_new_face = True

        # If no match, index the new face
        if is_new_face:
            api_call_count["IndexFaces"] += 1
            index_response = rek_client.index_faces(
                CollectionId=collection_id,
                Image={"Bytes": cropped_bytes},
                MaxFaces=1,
                DetectionAttributes=["DEFAULT"]
            )

            face_records = index_response.get("FaceRecords", [])
            if face_records:
                face_id = face_records[0]["Face"]["FaceId"]
                if is_celebrity:
                    face_label = celebrity_name
                else:
                    face_label = f"Unknown-{face_id[:8]}"
            else:
                logger.warning(
                    f"IndexFaces returned no face records for frame {face_frame_key}"
                )
                continue

        processed_face_ids.add(face_id)

        # Determine celebrity status for this face
        face_is_celebrity = is_celebrity

        # Save face crop image to S3
        face_image_key = ""
        try:
            face_image_key = save_face_image(
                bucket_images, jobId, face_label, cropped_bytes, face_id=face_id
            )
        except Exception as e:
            logger.warning(f"Failed to save face image for '{face_label}': {e}")

        # Merge :Face node in Neptune
        # - Celebrities and new faces: always update label (update_on_match=True)
        # - Matched unknowns: only set on create, preserve existing label (update_on_match=False)
        update_on_match = is_celebrity or is_new_face
        merge_face_node(
            neptune_client, neptune_graph_id,
            face_id=face_id, label=face_label, is_celebrity=face_is_celebrity,
            face_image_key=face_image_key, update_on_match=update_on_match
        )

        faces.append({
            "faceId": face_id,
            "label": face_label,
            "isCelebrity": face_is_celebrity,
            "faceImageKey": face_image_key
        })

    # Track costs
    search_cost = api_call_count["SearchFacesByImage"] * COST_SEARCH_FACES_BY_IMAGE
    index_cost = api_call_count["IndexFaces"] * COST_INDEX_FACES

    if search_cost > 0:
        track_cost(
            table, jobId,
            service="Amazon Rekognition",
            operation="SearchFacesByImage",
            cost_usd=search_cost,
            details=f"{api_call_count['SearchFacesByImage']} call(s)"
        )

    if index_cost > 0:
        track_cost(
            table, jobId,
            service="Amazon Rekognition",
            operation="IndexFaces",
            cost_usd=index_cost,
            details=f"{api_call_count['IndexFaces']} call(s)"
        )

    return {
        "jobId": jobId,
        "video_name": video_name,
        "shot_id": shot_id,
        "shot_startTime": shot_startTime,
        "shot_endTime": shot_endTime,
        "frames": frames,
        "faces": faces
    }


def ensure_collection_exists(collection_id):
    """Create the Rekognition face collection if it does not already exist."""
    try:
        rek_client.create_collection(CollectionId=collection_id)
        logger.info(f"Created face collection: {collection_id}")
    except rek_client.exceptions.ResourceAlreadyExistsException:
        pass
    except ClientError as e:
        logger.error(f"Error ensuring collection exists: {e}")
        raise


def get_frame_image(cache, bucket, key):
    """Download a frame image from S3, with caching."""
    if key not in cache:
        cache[key] = download_image(bucket, key)
    return cache[key]


def download_image(bucket, key):
    """Download an image from S3 and return the raw bytes."""
    obj = s3_client.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read()


def crop_face(image_data, bounding_box):
    """Crop a face region from image bytes using relative bounding box coordinates."""
    image = Image.open(io.BytesIO(image_data))
    img_width, img_height = image.size

    bb_left = float(bounding_box["Left"])
    bb_top = float(bounding_box["Top"])
    bb_width = float(bounding_box["Width"])
    bb_height = float(bounding_box["Height"])

    # Add 10% padding on each side
    pad_x = bb_width * 0.10
    pad_y = bb_height * 0.10

    left = int(max(0.0, bb_left - pad_x) * img_width)
    top = int(max(0.0, bb_top - pad_y) * img_height)
    right = int(min(1.0, bb_left + bb_width + pad_x) * img_width)
    bottom = int(min(1.0, bb_top + bb_height + pad_y) * img_height)

    if right <= left or bottom <= top:
        raise ValueError(
            f"Invalid crop dimensions: left={left}, top={top}, "
            f"right={right}, bottom={bottom}"
        )

    cropped = image.crop((left, top, right, bottom))

    buffer = io.BytesIO()
    cropped.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer.read()


def save_face_image(bucket, job_id, face_label, image_bytes, face_id=""):
    """Save a cropped face image to S3 and return the object key.
    Uses face_id in the key to avoid collisions between different people with the same label."""
    sanitized = re.sub(r'[^a-zA-Z0-9_-]', '_', face_label)
    id_suffix = f"_{face_id[:8]}" if face_id else ""
    key = f"{job_id}/faces/{sanitized}{id_suffix}.png"
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=image_bytes,
        ContentType="image/png",
    )
    logger.info(f"Saved face image: s3://{bucket}/{key}")
    return key


def merge_face_node(neptune_client, graph_id, face_id, label, is_celebrity, face_image_key=None, update_on_match=True):
    """Create or merge a :Face node in Neptune Analytics, keyed by faceId.

    When update_on_match is True, both ON CREATE and ON MATCH update the label/celebrity status.
    When False, only ON CREATE sets these — preserving existing labels from prior indexing.
    """
    set_props = f'label: "{label}", isCelebrity: {str(is_celebrity).lower()}'
    if face_image_key:
        set_props += f', faceImageKey: "{face_image_key}"'

    if update_on_match:
        query = (
            f'MERGE (f:Face {{faceId: "{face_id}"}}) '
            f'ON CREATE SET f += {{{set_props}}} '
            f'ON MATCH SET f += {{{set_props}}} '
            f'RETURN f'
        )
    else:
        # Only update faceImageKey on match (if provided), never overwrite label
        match_props = ""
        if face_image_key:
            match_props = f'ON MATCH SET f.faceImageKey = "{face_image_key}" '
        query = (
            f'MERGE (f:Face {{faceId: "{face_id}"}}) '
            f'ON CREATE SET f += {{{set_props}}} '
            f'{match_props}'
            f'RETURN f'
        )

    try:
        neptune_client.execute_query(
            graphIdentifier=graph_id,
            queryString=query,
            language="OPEN_CYPHER",
            parameters={}
        )
    except ClientError as e:
        logger.error(f"Failed to merge Face node for '{label}' (faceId={face_id}): {e}")
        raise


def get_face_label_by_id(neptune_client, graph_id, face_id):
    """Look up the label of an existing Face node by its faceId."""
    query = (
        f'MATCH (f:Face {{faceId: "{face_id}"}}) '
        f'RETURN f.label AS label'
    )

    try:
        response = neptune_client.execute_query(
            graphIdentifier=graph_id,
            queryString=query,
            language="OPEN_CYPHER",
            parameters={}
        )
        payload = json.loads(response["payload"].read().decode("utf-8"))
        results = payload.get("results", [])
        if results:
            return results[0].get("label")
    except ClientError as e:
        logger.warning(f"Failed to look up face label for faceId {face_id}: {e}")

    return None



def track_cost(table, jobId, service, operation, cost_usd, details=""):
    """Append a cost entry to the DynamoDB job item."""
    try:
        table.update_item(
            Key={"JobId": jobId},
            UpdateExpression=(
                "SET CostEntries = list_append("
                "if_not_exists(CostEntries, :empty), :entry)"
            ),
            ExpressionAttributeValues={
                ":entry": [{
                    "service": service,
                    "operation": operation,
                    "costUsd": str(cost_usd),
                    "details": details
                }],
                ":empty": []
            }
        )
    except ClientError as e:
        logger.error(f"Failed to track cost for job {jobId}: {e}")
