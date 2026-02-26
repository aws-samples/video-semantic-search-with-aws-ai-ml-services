import json
import logging
import boto3
from boto3.dynamodb.conditions import Key
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sf_client = boto3.client("stepfunctions")
rek_client = boto3.client("rekognition")


def lambda_handler(event, context):
    dynamodb_table = os.environ["vss_dynamodb_table"]
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(dynamodb_table)

    message = json.loads(event["Records"][0]["Sns"]["Message"])

    rekognitionTaskId = message["JobId"]
    response = table.query(
        IndexName="RekognitionGSI",
        KeyConditionExpression=Key("RekognitionTaskId").eq(rekognitionTaskId),
    )
    item = response["Items"][0]
    jobId = item["JobId"]
    video_name = item["Input"]

    shots = getShotDetectionResults(jobId, video_name, rekognitionTaskId)

    # Send shots back to Step Functions via the task token
    message["Shots"] = shots
    sf_client.send_task_success(
        taskToken=item["LambdaRekognitionTaskToken"],
        output=json.dumps(message),
    )

    return {"statusCode": 200}


def getShotDetectionResults(jobId, video_name, rekognitionTaskId):
    maxResults = 1000
    paginationToken = ""

    response = rek_client.get_segment_detection(
        JobId=rekognitionTaskId, MaxResults=maxResults, NextToken=paginationToken
    )

    shots = []
    for i, shot in enumerate(response["Segments"]):
        shot_startTime = 0 if i == 0 else shot["StartTimestampMillis"]
        shot_endTime = shot["EndTimestampMillis"]

        shots.append(
            {
                "jobId": jobId,
                "video_name": video_name,
                "shot_id": f"shot_{shot_startTime}_{shot_endTime}",
                "shot_startTime": shot_startTime,
                "shot_endTime": shot_endTime,
            }
        )

    return shots
