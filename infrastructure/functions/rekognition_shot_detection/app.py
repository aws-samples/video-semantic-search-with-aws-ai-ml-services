import json
import logging
import re
import boto3
from botocore.exceptions import ClientError
import os
import time

dynamodb_client = boto3.resource("dynamodb")
rek_client = boto3.client("rekognition")


def lambda_handler(event, context):
    bucket_videos = os.environ["bucket_videos"]
    video_name = event["vssParams"]["video_name"]
    jobId = event["vssParams"]["jobId"]
    vss_sns_rekognition_topic_arn = os.environ["vss_sns_rekognition_topic_arn"]
    vss_sns_rekognition_role = os.environ["vss_sns_rekognition_role"]

    rekJobId = startSegmentDetection(
        bucket_videos,
        video_name,
        vss_sns_rekognition_topic_arn,
        vss_sns_rekognition_role,
    )
    add_rekognition_jobid(
        jobId, os.environ["vss_dynamodb_table"], rekJobId, event["TaskToken"]
    )

    return {"statusCode": 200}


def add_rekognition_jobid(jobId, dynamodb_table, rekognition_job_id, sf_job_token):
    table = dynamodb_client.Table(dynamodb_table)
    dynamodbResponse = table.update_item(
        Key={"JobId": jobId},
        UpdateExpression="SET RekognitionTaskId = :value1, LambdaRekognitionTaskToken = :value2",
        ExpressionAttributeValues={
            ":value1": rekognition_job_id,
            ":value2": sf_job_token,
        },
    )


def startSegmentDetection(
    bucket_videos, video_name, vss_sns_rekognition_topic_arn, vss_sns_rekognition_role
):

    min_Technical_Cue_Confidence = 80.0
    min_Shot_Confidence = 80.0
    max_pixel_threshold = 0.1
    min_coverage_percentage = 60

    response = rek_client.start_segment_detection(
        Video={"S3Object": {"Bucket": bucket_videos, "Name": video_name}},
        NotificationChannel={
            "RoleArn": vss_sns_rekognition_role,
            "SNSTopicArn": vss_sns_rekognition_topic_arn,
        },
        SegmentTypes=["SHOT"],
        Filters={
            "ShotFilter": {"MinSegmentConfidence": min_Shot_Confidence},
        },
    )

    startJobId = response["JobId"]
    return startJobId
