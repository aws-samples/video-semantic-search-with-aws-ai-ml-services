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

    for frame in frames:
        message = {
            "role": "user",
            "content": [
                {"text": prompt},
            ],
        }
        s3_object = s3_client.get_object(
            Bucket=bucket_images, Key=f"{jobId}/{frame}.png"
        )
        image_content = s3_object["Body"].read()
        message["content"].append(
            {"image": {"format": "png", "source": {"bytes": image_content}}}
        )
        messages = [message]
        inferenceConfig = {"maxTokens": 128}

        response = bedrock_client.converse(
            modelId=model_id, messages=messages, inferenceConfig=inferenceConfig
        )
        output_message = response["output"]["message"]
        output_message = output_message["content"][0]["text"]
        if "No names recognized" in output_message:
            output_message = ""
        shot_frames.append({"frame": frame, "frame_privateFigures": output_message})
    return shot_frames
