import json
import logging
import re
import boto3
from botocore.exceptions import ClientError
import os
import time
import base64
from botocore.config import Config

config = Config(read_timeout=900)

bedrock_client = boto3.client(service_name="bedrock-runtime")
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

    shot_frames = recognise_person_name(bucket_images, jobId, shot_frames)

    return {
        "jobId": jobId,
        "video_name": video_name,
        "shot_id": shot_id,
        "shot_startTime": shot_startTime,
        "shot_endTime": shot_endTime,
        "shot_frames": shot_frames,
    }


def recognise_person_name(bucket_images, jobId, frames):
    shot_frames = []
    prompt = f"""Analyze this image and identify any person names present.
    - If person names are recognized, list them separated by commas, with no additional context or text.
    - If no person names are recognized, respond with "No names recognized."
    - Do not include any other information or context in your response.
    """

    model_id = os.environ["bedrock_model"]
    accept = "application/json"
    content_type = "application/json"
    for frame in frames:
        s3_object = s3_client.get_object(
            Bucket=bucket_images, Key=f"{jobId}/{frame}.png"
        )
        image_content = s3_object["Body"].read()
        base64_image_string = base64.b64encode(image_content).decode()
        body = json.dumps(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": base64_image_string,
                                },
                            },
                        ],
                    }
                ],
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 256,
            }
        )
        response = bedrock_client.invoke_model(
            body=body, modelId=model_id, accept=accept, contentType=content_type
        )
        response_body = json.loads(response["body"].read())
        response_body = response_body["content"][0]["text"]
        if "No names recognized" in response_body:
            response_body = ""
        shot_frames.append({"frame": frame, "frame_privateFigures": response_body})
    return shot_frames
