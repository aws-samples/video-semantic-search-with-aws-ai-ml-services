import json
import logging
import re
import boto3
from botocore.exceptions import ClientError
import os
import time
import base64
from botocore.config import Config
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth
import re

config = Config(read_timeout=900, retries = {
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

    shot_frames = get_shot_metadata(bucket_shots, jobId, shot_id)

    shot_frames, shot_publicFigures, shot_privateFigures = (
        augment_detection_with_embeddings(bucket_images, jobId, shot_frames)
    )

    transcript = json.loads(get_subtitle(bucket_transcripts, jobId + ".json"))
    shot_transcript = add_shot_transcript(shot_startTime, shot_endTime, transcript)

    shot_description = generate_shot_description(
        bucket_images, jobId, shot_frames, shot_transcript
    )

    shot = {
        "jobId": jobId,
        "video_name": video_name,
        "shot_id": shot_id,
        "shot_startTime": shot_startTime,
        "shot_endTime": shot_endTime,
        "shot_frames": shot_frames,
        "shot_description": shot_description,
        "shot_publicFigures": shot_publicFigures,
        "shot_privateFigures": shot_privateFigures,
        "shot_transcript": shot_transcript,
    }

    shot_json = json.dumps(shot)

    s3_client.put_object(
        Body=shot_json.encode("utf-8"),
        Bucket=bucket_shots,
        Key=f"{jobId}/{shot_id}.json",
        ContentType="application/json",
    )

    return {
        "jobId": jobId,
        "video_name": video_name,
        "shot_id": shot_id,
        "shot_startTime": shot_startTime,
        "shot_endTime": shot_endTime,
    }


def get_shot_metadata(bucket_shots, jobId, shot_id):
    response = s3_client.get_object(Bucket=bucket_shots, Key=f"{jobId}/{shot_id}.json")

    shot_json = response["Body"].read().decode("utf-8")

    shot_metadata = json.loads(shot_json)

    return shot_metadata["shot_frames"]


def augment_detection_with_embeddings(bucket_images, jobId, shot_frames):
    client = get_opensearch_client(os.environ["aoss_host"], os.environ["region"], jobId)
    augmented_shot_frames = []
    shot_publicFigures = set()
    shot_privateFigures = set()

    def from_set_to_str(str):
        if not str:
            return ""
        else:
            return ", ".join(str)
    # Collect all direct detections
    for index, value in enumerate(shot_frames):
        for name in value["frame_publicFigures"].split(","):
            name = name.strip()
            if name:
                shot_publicFigures.add(name)

        for name in value["frame_privateFigures"].split(","):
            name = name.strip()
            if name and name not in shot_publicFigures:
                shot_privateFigures.add(name)

    # Augment with embeddings
    for index, value in enumerate(shot_frames):
        frame_publicFigures = set()
        frame_privateFigures = set()
        for name in value["frame_publicFigures"].split(","):
            name = name.strip()
            if name:
                frame_publicFigures.add(name)

        for name in value["frame_privateFigures"].split(","):
            name = name.strip()
            if name and name not in frame_publicFigures:
                frame_privateFigures.add(name)

        embedding = get_titan_image_embedding(
            bucket_images,
            jobId,
            os.environ["image_embedding_model"],
            f"{value["frame"]}.png",
        )

        query = {
            "size": 100,
            "query": {"knn": {"frame_image_vector": {"vector": embedding, "k": 100}}},
            "_source": [
                "jobId",
                "video_name",
                "shot_startTime",
                "shot_endTime",
                "frame_publicFigures",
                "frame_privateFigures",
            ],
        }
        response = client.search(body=query, index=jobId)
        hits = response["hits"]["hits"]
        for hit in hits:
            if hit["_score"] >= 0.8:
                public_figures = [
                    name.strip()
                    for name in hit["_source"]["frame_publicFigures"].split(",")
                ]
                for name in public_figures:
                    if name and name not in shot_publicFigures:
                        frame_publicFigures.add(name + "*")
                        shot_publicFigures.add(name + "*")

                private_figures = [
                    name.strip()
                    for name in hit["_source"]["frame_privateFigures"].split(",")
                ]
                for name in private_figures:
                    if name and name not in shot_privateFigures and name not in shot_publicFigures:
                        frame_privateFigures.add(name + "*")
                        shot_privateFigures.add(name + "*")

        frame_publicFigures = from_set_to_str(frame_publicFigures)
        frame_privateFigures = from_set_to_str(frame_privateFigures)
        augmented_shot_frames.append(
            {
                "frame": value["frame"],
                "frame_publicFigures": frame_publicFigures,
                "frame_privateFigures": frame_privateFigures,
            }
        )

    shot_publicFigures = from_set_to_str(shot_publicFigures)
    shot_privateFigures = from_set_to_str(shot_privateFigures)

    return augmented_shot_frames, shot_publicFigures, shot_privateFigures


def generate_shot_description(bucket_images, jobId, shot_frames, shot_transcript):
    res = []

    prompt = f"""Provide a detailed but concise description of a video shot based on the given frame images. Focus on creating a cohesive narrative of the entire shot rather than describing each frame individually. If the images contain frames from multiple shots, concentrate on describing the most prominent or central shot.

        Before describing the shot:

        - Identify the primary shot among the given frames.
        - Disregard any frames that appear to belong to previous or next shots.
        - If uncertain about which frames belong to the current shot, describe only the elements that are consistent across multiple frames.
        
        Then, incorporate the following elements in your description: 
        1. Visual elements:
        - Describe all visible objects, text, and characters in detail.
        - For any characters present, include:
            • Age
            • Emotional expressions
            • Clothing and accessories
            • Physical appearance
            • Any actions, movements or gestures

        2. Setting and atmosphere:
        - Provide details about the time, location, and overall ambiance.
        - Mention any relevant background elements that contribute to the scene.

        3. Incorporate provided information:
        - Seamlessly integrate details about public figures and private figures if available.
        - If this information is not provided, rely solely on the visual elements.

        Skip the preamble; go straight into the description."""

    for index, value in enumerate(shot_frames):
        prompt += f"Frame {index}: Public figures: {value["frame_publicFigures"]}; Private figures: {value["frame_privateFigures"]}\n"

    # prompt += f"Audio transcription: {shot_transcript}"

    model_id = os.environ["bedrock_llm"]
    message = {
        "role": "user",
        "content": [
            {"text": prompt},
        ],
    }
    for index, value in enumerate(shot_frames):
        s3_object = s3_client.get_object(
            Bucket=bucket_images, Key=f"{jobId}/{value["frame"]}.png"
        )
        image_content = s3_object["Body"].read()
        message["content"].append(
            {"image": {"format": "png", "source": {"bytes": image_content}}}
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


def get_titan_image_embedding(bucket_images, jobId, embedding_model, image_name):
    s3_object = s3_client.get_object(Bucket=bucket_images, Key=jobId + "/" + image_name)
    image_content = s3_object["Body"].read()
    base64_image_string = base64.b64encode(image_content).decode()

    accept = "application/json"
    content_type = "application/json"
    body = json.dumps({"inputImage": base64_image_string})
    response = bedrock_client.invoke_model(
        body=body, modelId=embedding_model, accept=accept, contentType=content_type
    )
    response_body = json.loads(response["body"].read())
    embedding = response_body.get("embedding")
    return embedding


def get_opensearch_client(host, region, index):
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
    subtitle = (
        s3_client.get_object(Bucket=bucket_transcripts, Key=transcript_filename)["Body"]
        .read()
        .decode("utf-8-sig")
    )
    return subtitle
