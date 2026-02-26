import json
import logging
import boto3
from botocore.exceptions import ClientError
import os
import datetime
import uuid
import random

sqs_client = boto3.client("sqs")


def lambda_handler(event, context):
    bucket_name = os.environ["bucket_videos"]
    userId = event["queryStringParameters"]["userId"]
    video_name = event["queryStringParameters"]["video_name"]

    # Read indexing config from query parameters
    params = event.get("queryStringParameters", {})
    config = {
        "segmentationMode": params.get("segmentationMode", "shot"),
        "intervalSeconds": int(params.get("intervalSeconds", 10)),
        "framesPerShot": int(params.get("framesPerShot", 3)),
        "transcription": params.get("transcription", "true") == "true",
        "faceRecognition": params.get("faceRecognition", "true") == "true",
        "celebrityDetection": params.get("celebrityDetection", "true") == "true",
    }

    vss_input = {"userId": userId, "video_name": video_name, "config": config}
    sqs_queue_url = os.environ["sqs_queue_url"]
    response = sqs_client.send_message(
        QueueUrl=sqs_queue_url, MessageBody=json.dumps(vss_input)
    )

    jobId = response["MessageId"]

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(os.environ["vss_dynamodb_table"])
    started = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dynamodbResponse = table.put_item(
        Item={
            "JobId": jobId,
            "UserId": userId,
            "Input": video_name,
            "Started": started,
            "EndTime": "-",
            "Status": "Indexing",
            "IndexingConfig": config,
        }
    )

    response = {
        "jobId": jobId,
        "input": video_name,
        "started": started,
        "status": "Indexing",
    }

    return {"statusCode": 200, "body": json.dumps(response)}
