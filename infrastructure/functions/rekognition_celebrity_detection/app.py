import json
import logging
import re
import boto3
from botocore.exceptions import ClientError
import os
import time

dynamodb_client = boto3.resource("dynamodb")
rek_client = boto3.client("rekognition")
s3_client = boto3.client("s3")


def lambda_handler(event, context):
    bucket_images = os.environ["bucket_images"]
    bucket_shots = os.environ["bucket_shots"]
    jobId = event["jobId"]
    video_name = event["video_name"]
    shot_id = event["shot_id"]
    shot_startTime = event["shot_startTime"]
    shot_endTime = event["shot_endTime"]
    shot_frames = event["shot_frames"]

    shot_frames = startCelebrityDetection(bucket_images, jobId, shot_frames)

    return {
        "jobId": jobId,
        "video_name": video_name,
        "shot_id": shot_id,
        "shot_startTime": shot_startTime,
        "shot_endTime": shot_endTime,
        "shot_frames": shot_frames,
    }


def startCelebrityDetection(bucket_images, jobId, frames):
    shot_frames = []
    for frame in frames:
        response = rek_client.recognize_celebrities(
            Image={
                "S3Object": {"Bucket": bucket_images, "Name": f"{jobId}/{frame}.png"}
            }
        )

        min_confidence = 80.0

        celebrities = set()

        for celebrity in response.get("CelebrityFaces", []):
            if celebrity.get("MatchConfidence", 0.0) >= min_confidence:
                celebrities.add(celebrity["Name"])

        celebrities = ", ".join(celebrities)

        shot_frames.append({"frame": frame, "frame_publicFigures": celebrities})

    return shot_frames
