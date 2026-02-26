import json
import logging
import boto3
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

rek_client = boto3.client("rekognition")

MIN_FACE_CONFIDENCE = 90.0
MIN_CELEBRITY_CONFIDENCE = 98.0
IOU_MATCH_THRESHOLD = 0.5


def lambda_handler(event, context):
    bucket_images = os.environ["bucket_images"]
    jobId = event["jobId"]
    frames = event.get("frames", [])
    config = event.get("config", {})
    celebrity_detection_enabled = config.get("celebrityDetection", True)
    face_recognition_enabled = config.get("faceRecognition", True)

    all_celebrity_faces = []
    all_unrecognized_faces = []

    for timestamp in frames:
        frame_key = f"{jobId}/{timestamp}.png"
        celebrities, unrecognized = detect_faces_in_frame(
            bucket_images, frame_key, celebrity_detection_enabled, face_recognition_enabled
        )
        all_celebrity_faces.extend(celebrities)
        all_unrecognized_faces.extend(unrecognized)

    # Deduplicate celebrities across frames (keep highest confidence per ID)
    deduped_celebrities = deduplicate_celebrities(all_celebrity_faces)

    logger.info(
        "Detection for %s: %d unique celebrities, %d unrecognized across %d frames (celeb=%s, face=%s)",
        jobId, len(deduped_celebrities), len(all_unrecognized_faces),
        len(frames), celebrity_detection_enabled, face_recognition_enabled,
    )

    return {
        "celebrityFaces": deduped_celebrities,
        "unrecognizedFaces": all_unrecognized_faces,
    }


def detect_faces_in_frame(bucket_images, frame_key, celebrity_detection_enabled, face_recognition_enabled):
    """Detect faces and/or celebrities in a frame based on config.

    - celeb=true, face=true: DetectFaces + RecognizeCelebrities + IoU matching
    - celeb=true, face=false: RecognizeCelebrities only
    - celeb=false, face=true: DetectFaces only (all faces as unrecognized)
    """
    s3_image = {
        "S3Object": {
            "Bucket": bucket_images,
            "Name": frame_key,
        }
    }

    celebrity_faces = []
    unrecognized_faces = []

    # 1. DetectFaces — run when face recognition is enabled
    detected_faces = []
    if face_recognition_enabled:
        detect_response = rek_client.detect_faces(
            Image=s3_image,
            Attributes=["DEFAULT"]
        )
        for face_detail in detect_response.get("FaceDetails", []):
            if face_detail.get("Confidence", 0) >= MIN_FACE_CONFIDENCE:
                bb = face_detail["BoundingBox"]
                detected_faces.append({
                    "boundingBox": {
                        "Width": bb["Width"],
                        "Height": bb["Height"],
                        "Left": bb["Left"],
                        "Top": bb["Top"],
                    },
                    "frameKey": frame_key,
                })

    # 2. RecognizeCelebrities — run when celebrity detection is enabled
    if celebrity_detection_enabled:
        celeb_response = rek_client.recognize_celebrities(Image=s3_image)

        celeb_results = []
        for celebrity in celeb_response.get("CelebrityFaces", []):
            if celebrity.get("MatchConfidence", 0.0) >= MIN_CELEBRITY_CONFIDENCE:
                if "Face" in celebrity and "BoundingBox" in celebrity["Face"]:
                    rek_celeb_id = celebrity.get("Id", "")
                    celeb_results.append({
                        "name": celebrity["Name"],
                        "rekognitionCelebrityId": rek_celeb_id,
                        "label": celebrity["Name"],
                        "isCelebrity": True,
                        "confidence": celebrity["MatchConfidence"],
                        "boundingBox": celebrity["Face"]["BoundingBox"],
                    })

        if detected_faces:
            # Match each celebrity to detected faces by bounding box overlap
            matched_indices = set()
            for celeb in celeb_results:
                best_iou = 0
                best_idx = -1
                for i, face in enumerate(detected_faces):
                    if i in matched_indices:
                        continue
                    iou = compute_iou(celeb["boundingBox"], face["boundingBox"])
                    if iou > best_iou:
                        best_iou = iou
                        best_idx = i

                if best_idx >= 0 and best_iou >= IOU_MATCH_THRESHOLD:
                    matched_indices.add(best_idx)
                    # Use DetectFaces bounding box
                    celebrity_faces.append({
                        **celeb,
                        "boundingBox": detected_faces[best_idx]["boundingBox"],
                        "frameKey": frame_key,
                    })
                else:
                    # No matching DetectFaces result, use RecognizeCelebrities bounding box
                    celebrity_faces.append({
                        **celeb,
                        "frameKey": frame_key,
                    })

            # All unmatched detected faces are unrecognized
            for i, face in enumerate(detected_faces):
                if i not in matched_indices:
                    unrecognized_faces.append(face)
        else:
            # No DetectFaces results (face recognition off or no faces detected)
            for celeb in celeb_results:
                celebrity_faces.append({
                    **celeb,
                    "frameKey": frame_key,
                })

    elif face_recognition_enabled:
        # Celebrity detection off — all detected faces are unrecognized
        unrecognized_faces = detected_faces

    logger.info(
        "Frame %s: %d detected, %d celebrities, %d unrecognized",
        frame_key, len(detected_faces), len(celebrity_faces), len(unrecognized_faces),
    )
    return celebrity_faces, unrecognized_faces


def compute_iou(box1, box2):
    """Compute Intersection over Union between two bounding boxes (relative coords)."""
    x1 = max(box1["Left"], box2["Left"])
    y1 = max(box1["Top"], box2["Top"])
    x2 = min(box1["Left"] + box1["Width"], box2["Left"] + box2["Width"])
    y2 = min(box1["Top"] + box1["Height"], box2["Top"] + box2["Height"])

    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    area1 = box1["Width"] * box1["Height"]
    area2 = box2["Width"] * box2["Height"]
    union = area1 + area2 - intersection

    if union <= 0:
        return 0.0
    return intersection / union


def deduplicate_celebrities(celebrity_faces):
    """Deduplicate celebrities across frames, keeping the highest confidence detection."""
    best_by_id = {}
    for celeb in celebrity_faces:
        celeb_id = celeb.get("rekognitionCelebrityId") or celeb["name"]
        existing = best_by_id.get(celeb_id)
        if not existing or celeb["confidence"] > existing["confidence"]:
            best_by_id[celeb_id] = celeb
    return list(best_by_id.values())
