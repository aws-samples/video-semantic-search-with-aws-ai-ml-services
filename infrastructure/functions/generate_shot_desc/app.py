import json
import logging
import boto3
from botocore.exceptions import ClientError
import os
import re
from botocore.config import Config

config = Config(read_timeout=900, retries={
    'max_attempts': 20,
    'mode': 'standard'
})

dynamodb_client = boto3.resource("dynamodb")
bedrock_client = boto3.client(service_name="bedrock-runtime", config=config)
s3_client = boto3.client("s3")


def lambda_handler(event, context):
    bucket_images = os.environ["bucket_images"]
    bucket_shots = os.environ["bucket_shots"]
    bucket_transcripts = os.environ["bucket_transcripts"]
    jobId = event["jobId"]
    video_name = event["video_name"]
    shot_id = event["shot_id"]
    shot_startTime = event["shot_startTime"]
    shot_endTime = event["shot_endTime"]
    frames = event.get("frames", [])
    composite_key = event.get("composite_key", "")
    clip_key = event.get("clip_key", "")
    # Load individual frame images from S3
    frame_images = []
    for timestamp in frames:
        s3_object = s3_client.get_object(
            Bucket=bucket_images, Key=f"{jobId}/{timestamp}.png"
        )
        image_content = s3_object["Body"].read()
        frame_images.append({"timestamp": timestamp, "image": image_content})

    # Generate purely visual description
    shot_description = generate_shot_description(frame_images)

    # Get transcript for the shot time range
    transcript = json.loads(get_subtitle(bucket_transcripts, jobId + ".json"))
    shot_transcript = add_shot_transcript(shot_startTime, shot_endTime, transcript)

    # Pass through all input fields and add description + transcript
    output = dict(event)
    output["shot_description"] = shot_description
    output["shot_transcript"] = shot_transcript

    return output


def generate_shot_description(frame_images):
    prompt = """Provide a detailed but concise description of a video shot based on the given frame images. Focus on creating a cohesive narrative of the entire shot rather than describing each frame individually.

Before describing the shot:
- Identify the primary shot among the given frames.
- Disregard any frames that appear to belong to previous or next shots.

Then, incorporate the following elements:
1. Visual elements:
   - Describe all visible objects, text, and characters in detail.
   - For any characters present, include age, emotional expressions, clothing, physical appearance, actions/movements/gestures.
2. Setting and atmosphere:
   - Provide details about the time, location, and overall ambiance.
   - Mention any relevant background elements.

Skip the preamble; go straight into the description."""

    model_id = os.environ["bedrock_llm"]
    message = {
        "role": "user",
        "content": [
            {"text": prompt},
        ],
    }

    for frame_data in frame_images:
        message["content"].append(
            {"image": {"format": "png", "source": {"bytes": frame_data["image"]}}}
        )

    messages = [message]
    inferenceConfig = {
        "maxTokens": 512,
    }

    response = bedrock_client.converse(
        modelId=model_id, messages=messages, inferenceConfig=inferenceConfig
    )
    output_message = response["output"]["message"]
    output_message = output_message["content"][0]["text"]

    return output_message


def add_shot_transcript(shot_startTime, shot_endTime, transcript):
    relevant_transcript = ""
    for item in transcript:
        if item["sentence_startTime"] >= shot_endTime:
            break
        if item["sentence_endTime"] <= shot_startTime:
            continue
        delta_start = max(item["sentence_startTime"], shot_startTime)
        delta_end = min(item["sentence_endTime"], shot_endTime)
        if delta_end - delta_start >= 500:
            relevant_transcript += item["sentence"] + "; "
    return relevant_transcript


def get_subtitle(bucket_transcripts, transcript_filename):
    try:
        subtitle = (
            s3_client.get_object(Bucket=bucket_transcripts, Key=transcript_filename)["Body"]
            .read()
            .decode("utf-8-sig")
        )
        return subtitle
    except s3_client.exceptions.NoSuchKey:
        return "[]"
