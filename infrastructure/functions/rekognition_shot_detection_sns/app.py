import json
import logging
import re
import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key
import os
import time
import subprocess

sf_client = boto3.client("stepfunctions")
rek_client = boto3.client("rekognition")
s3_client = boto3.client("s3")


def lambda_handler(event, context):
    dynamodb_table = os.environ["vss_dynamodb_table"]
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(dynamodb_table)
    SNSTopic = os.environ["SNSTopic"]

    message = json.loads(event["Records"][0]["Sns"]["Message"])

    rekognitionTaskId = message["JobId"]
    response = table.query(
        IndexName="RekognitionGSI",
        KeyConditionExpression=Key("RekognitionTaskId").eq(rekognitionTaskId),
    )
    item = response["Items"][0]
    jobId = item["JobId"]
    video_name = item["Input"]

    frames, shots = getShotDetectionResults(jobId, video_name, rekognitionTaskId)

    generateImages(
        jobId,
        os.environ["bucket_videos"],
        video_name,
        frames,
        os.environ["tmp_dir"],
        os.environ["bucket_images"],
    )

    message = event["Records"][0]["Sns"]["Message"]
    message = json.loads(message)
    message["Shots"] = shots
    message = json.dumps(message)

    sfResponse = sf_client.send_task_success(
        taskToken=item["LambdaRekognitionTaskToken"], output=message
    )

    return {"statusCode": 200}


def getShotDetectionResults(jobId, video_name, rekognitionTaskId):
    maxResults = 1000
    paginationToken = ""

    response = rek_client.get_segment_detection(
        JobId=rekognitionTaskId, MaxResults=maxResults, NextToken=paginationToken
    )

    frames = []
    shots = []
    delta = response["Segments"][0]["StartTimestampMillis"]

    def get_frames(shot, N):
        start_frame = shot["StartFrameNumber"]
        end_frame = max(
            start_frame, shot["EndFrameNumber"] - 1
        )  # frame - 1 to avoid bug not getting the last frame of the video
        step = (end_frame - start_frame) / (N - 1)
        frames = [int(start_frame + i * step) for i in range(N)]
        return frames

    for shot in response["Segments"]:
        shot_frames = get_frames(shot, 3)
        frames.extend(shot_frames)
        frames.append(shot["StartFrameNumber"])

        shot_startTime = shot["StartTimestampMillis"] - delta
        shot_endTime = shot["EndTimestampMillis"] - delta

        shots.append(
            {
                "jobId": jobId,
                "video_name": video_name,
                "shot_startTime": shot_startTime,
                "shot_endTime": shot_endTime,
                "frames": shot_frames,
            }
        )

    return frames, shots


def generateImages(jobId, bucket_videos, video_name, frames, tmp_dir, bucket_images):
    tmp_video_dir = tmp_dir + "/video/"
    tmp_frames_dir = tmp_dir + "/" + jobId + "/"
    os.makedirs(tmp_video_dir, exist_ok=True)
    os.makedirs(tmp_frames_dir, exist_ok=True)
    ffmpeg_path = "/opt/bin/ffmpeg"
    local_video_path = os.path.join(tmp_video_dir, video_name)
    s3_client.download_file(bucket_videos, video_name, local_video_path)

    frame_list = "+".join([f"eq(n,{frame})" for frame in frames])
    output_pattern = f"{tmp_frames_dir}%d.png"
    subprocess.run(
        [
            ffmpeg_path,
            "-i",
            local_video_path,
            "-vf",
            f"select='{frame_list}'",
            "-vsync",
            "0",
            "-frame_pts",
            "1",
            output_pattern,
        ],
        stderr=subprocess.PIPE,
    )

    extra_args = {"ContentType": "image/png"}
    for frame_file in os.listdir(tmp_frames_dir):
        frame_path = os.path.join(tmp_frames_dir, frame_file)
        s3_client.upload_file(
            frame_path, bucket_images, f"{jobId}/{frame_file}", ExtraArgs=extra_args
        )
