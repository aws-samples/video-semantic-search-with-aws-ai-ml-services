import json
import logging
import re
import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key
import os
import time
import subprocess
import concurrent.futures

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
    def get_timestamps(shot, N):
        start_time = shot["StartTimestampMillis"]
        end_time = shot["EndTimestampMillis"]
        step = int((end_time - start_time) / (N - 1))
        timestamps = [start_time + i * step for i in range(N)]
        return timestamps

    for i, shot in enumerate(response["Segments"]):
        shot_timestamps = get_timestamps(shot, 3)
        frames.extend(shot_timestamps)

        shot_startTime = 0 if i == 0 else shot["StartTimestampMillis"]
        shot_endTime = shot["EndTimestampMillis"]

        shots.append(
            {
                "jobId": jobId,
                "video_name": video_name,
                "shot_startTime": shot_startTime,
                "shot_endTime": shot_endTime,
                "frames": shot_timestamps,
            }
        )

    return frames, shots


def generateImages(jobId, bucket_videos, video_name, timestamps, tmp_dir, bucket_images):
    tmp_video_dir = tmp_dir + "/video/"
    tmp_frames_dir = tmp_dir + "/" + jobId + "/"
    os.makedirs(tmp_video_dir, exist_ok=True)
    os.makedirs(tmp_frames_dir, exist_ok=True)
    ffmpeg_path = "/opt/bin/ffmpeg"
    local_video_path = os.path.join(tmp_video_dir, video_name)
    
    s3_client.download_file(bucket_videos, video_name, local_video_path)
    
    sorted_timestamps = sorted(timestamps)
    last_timestamp = sorted_timestamps[-1]
    
    def extract_frame(timestamp_ms):
        """Process a single timestamp and extract the frame"""
        # Handling the last timestamp for edge case.
        if timestamp_ms == last_timestamp:
            output_file = f"{tmp_frames_dir}{timestamp_ms}.png"
            subprocess.run(
                [
                    ffmpeg_path,
                    "-sseof", "-0.1",
                    "-i", local_video_path,
                    "-vf", "scale='min(1280,iw):-1'",  #
                    "-update", "1",
                    "-frames:v", "1",
                    "-q:v", "2",
                    "-y",
                    output_file
                ],
                stderr=subprocess.PIPE
            )
        else:
            timestamp_sec = timestamp_ms / 1000.0
            output_file = f"{tmp_frames_dir}{timestamp_ms}.png"
            subprocess.run(
                [
                    ffmpeg_path,
                    "-ss", f"{timestamp_sec:.3f}",
                    "-i", local_video_path,
                    "-vf", "scale='min(1280,iw):-1'",
                    "-vframes", "1", 
                    "-q:v", "2",
                    output_file
                ],
                stderr=subprocess.PIPE
            )
        return output_file
    
    # Extract frames in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        frame_futures = [executor.submit(extract_frame, ts) for ts in timestamps]
        concurrent.futures.wait(frame_futures)
    
    extra_args = {"ContentType": "image/png"}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        upload_futures = []
        for frame_file in os.listdir(tmp_frames_dir):
            frame_path = os.path.join(tmp_frames_dir, frame_file)
            upload_futures.append(
                executor.submit(
                    s3_client.upload_file,
                    frame_path,
                    bucket_images,
                    f"{jobId}/{frame_file}",
                    ExtraArgs=extra_args
                )
            )
        concurrent.futures.wait(upload_futures)
