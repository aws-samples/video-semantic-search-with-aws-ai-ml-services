import json
import logging
import re
import boto3
from botocore.exceptions import ClientError
import os
import time
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth
import base64

bedrock_client = boto3.client(service_name="bedrock-runtime")
s3_client = boto3.client("s3")


def lambda_handler(event, context):
    bucket_shots = os.environ["bucket_shots"]
    jobId = event["jobId"]
    video_name = event["video_name"]
    shot_id = event["shot_id"]
    shot_startTime = event["shot_startTime"]
    shot_endTime = event["shot_endTime"]
    (
        shot_frames,
        shot_description,
        shot_publicFigures,
        shot_privateFigures,
        shot_transcript,
    ) = get_shot_metadata(bucket_shots, jobId, shot_id)

    shot_desc_embedding = get_text_embedding(
        os.environ["text_embedding_model"], shot_description
    )
    shot_image_embedding = get_image_embedding(bucket_shots, jobId, shot_id)

    embedding_request_body = json.dumps(
        {
            "jobId": jobId,
            "video_name": video_name,
            "shot_id": shot_id,
            "shot_startTime": shot_startTime,
            "shot_endTime": shot_endTime,
            "shot_description": shot_description,
            "shot_publicFigures": shot_publicFigures,
            "shot_privateFigures": shot_privateFigures,
            "shot_transcript": shot_transcript,
            "shot_desc_vector": shot_desc_embedding,
            "shot_image_vector": shot_image_embedding,
        }
    )

    documentId = f"{video_name}-{shot_id}"
    client = get_opensearch_client(os.environ["aoss_host"], os.environ["region"])

    response = client.index(
        index=os.environ["aoss_index"],
        body=embedding_request_body,
        params={"timeout": 60},
    )

    return {"status": 200}


def get_shot_metadata(bucket_shots, jobId, shot_id):
    response = s3_client.get_object(Bucket=bucket_shots, Key=f"{jobId}/{shot_id}.json")

    shot_json = response["Body"].read().decode("utf-8")

    shot_metadata = json.loads(shot_json)

    return (
        shot_metadata["shot_frames"],
        shot_metadata["shot_description"],
        shot_metadata["shot_publicFigures"],
        shot_metadata["shot_privateFigures"],
        shot_metadata["shot_transcript"],
    )


def get_text_embedding(text_embedding_model, text):
    accept = "application/json"
    content_type = "application/json"
    if text_embedding_model.startswith("amazon.titan-embed-text"):
        body = json.dumps({"inputText": text, "dimensions": 1024, "normalize": True})
        response = bedrock_client.invoke_model(
            body=body,
            modelId=text_embedding_model,
            accept=accept,
            contentType=content_type,
        )
        response_body = json.loads(response["body"].read())
        embedding = response_body.get("embedding")
    else:
        if len(text) > 2048:
            text = text[:2048]
        body = json.dumps({"texts": [text], "input_type": "search_document"})
        response = bedrock_client.invoke_model(
            body=body,
            modelId=text_embedding_model,
            accept=accept,
            contentType=content_type,
        )
        response_body = json.loads(response["body"].read())
        embedding = response_body.get("embeddings")[0]

    return embedding


def get_image_embedding(bucket, jobId, image):
    s3_object = s3_client.get_object(Bucket=bucket, Key=f"{jobId}/{image}.png")
    image_content = s3_object["Body"].read()
    base64_image_string = base64.b64encode(image_content).decode()

    accept = "application/json"
    content_type = "application/json"

    body = json.dumps({"inputImage": base64_image_string})

    response = bedrock_client.invoke_model(
        body=body,
        modelId=os.environ["image_embedding_model"],
        accept=accept,
        contentType=content_type,
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
