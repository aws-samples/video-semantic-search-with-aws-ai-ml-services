import json
import logging
import re
import boto3
from botocore.exceptions import ClientError
import os
import datetime
import time
import uuid
import random
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth
import base64

bedrock_client = boto3.client(service_name="bedrock-runtime")
s3_client = boto3.client("s3")

def lambda_handler(event, context):
    bucket_images = os.environ["bucket_images"]
    bucket_shots = os.environ["bucket_shots"]
    jobId = event[0]["jobId"]
    video_name = event[0]["video_name"]
    shot_id = event[0]["shot_id"]
    shot_startTime = event[0]["shot_startTime"]
    shot_endTime = event[0]["shot_endTime"]
    shot_frames = []

    for index in range(len(event[0]["shot_frames"])):
        shot_frames.append(
            {
                "frame": event[0]["shot_frames"][index]["frame"],
                "frame_publicFigures": event[0]["shot_frames"][index]["frame_publicFigures"],
                "frame_privateFigures": event[1]["shot_frames"][index]["frame_privateFigures"],
            }
        )
    
    client = get_opensearch_client(os.environ["aoss_host"], os.environ["region"])

    for index, value in enumerate(shot_frames):
        if value["frame_publicFigures"] != "" or value["frame_privateFigures"] != "":
            embedding = get_titan_image_embedding(
                bucket_images, jobId, os.environ["image_embedding_model"], f"{value["frame"]}.png"
            )
            embedding_request_body = json.dumps(
                {
                    "jobId": jobId,
                    "video_name": video_name,
                    "shot_startTime": shot_startTime,
                    "shot_endTime": shot_endTime,
                    "frame_publicFigures": value["frame_publicFigures"],
                    "frame_privateFigures": value["frame_privateFigures"],
                    "frame_image_vector": embedding,
                }
            )
            response = client.index(
                index=jobId,
                body=embedding_request_body,
                params={"timeout": 60},
            )

    shot =  {
        "jobId": jobId,
        "video_name": video_name,
        "shot_id": shot_id,
        "shot_startTime": shot_startTime,
        "shot_endTime": shot_endTime,
        "shot_frames": shot_frames,
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
        "shot_endTime": shot_endTime
    }

def get_titan_image_embedding(bucket_images, jobId, embedding_model, image_name):
    s3_object = s3_client.get_object(Bucket=bucket_images, Key=f"{jobId}/{image_name}")
    image_content = s3_object['Body'].read()
    base64_image_string = base64.b64encode(image_content).decode()

    accept = "application/json"
    content_type = "application/json"
    body = json.dumps(
        {"inputImage": base64_image_string}
    )
    response = bedrock_client.invoke_model(
        body=body, modelId=embedding_model, accept=accept, contentType=content_type
    )
    response_body = json.loads(response["body"].read())
    embedding = response_body.get("embedding")
    return embedding

def get_opensearch_client(host, region):
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

def milliseconds_to_time_format(ms):
    return "{:02d}:{:02d}:{:02d}:{:03d}".format(
        int((ms // 3600000) % 24),  # hours
        int((ms // 60000) % 60),  # minutes
        int((ms // 1000) % 60),  # seconds
        int(ms % 1000),  # milliseconds
    )
