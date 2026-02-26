import json
import boto3
import os
import subprocess

s3_client = boto3.client("s3")


def lambda_handler(event, context):
    bucket_videos = os.environ["bucket_videos"]
    tmp_dir = os.environ["tmp_dir"]
    jobId = event["jobId"]
    video_name = event["video_name"]
    config = event.get("config", {})
    interval_seconds = config.get("intervalSeconds", 10)

    tmp_video_dir = tmp_dir + "/video/"
    os.makedirs(tmp_video_dir, exist_ok=True)
    local_video_path = os.path.join(tmp_video_dir, video_name)

    try:
        s3_client.download_file(bucket_videos, video_name, local_video_path)

        duration_ms = get_video_duration_ms(local_video_path)
        interval_ms = interval_seconds * 1000

        segments = []
        index = 0
        start_time = 0

        while start_time < duration_ms:
            end_time = min(start_time + interval_ms, duration_ms)
            segments.append(
                {
                    "jobId": jobId,
                    "video_name": video_name,
                    "shot_startTime": start_time,
                    "shot_endTime": end_time,
                    "shot_id": f"shot_{index}",
                }
            )
            index += 1
            start_time += interval_ms

        return {"segments": segments}

    finally:
        if os.path.exists(local_video_path):
            os.remove(local_video_path)


def get_video_duration_ms(video_path):
    ffprobe_path = "/opt/bin/ffprobe"
    result = subprocess.run(
        [
            ffprobe_path,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        capture_output=True,
        text=True,
    )
    duration_seconds = float(result.stdout.strip())
    duration_ms = int(duration_seconds * 1000)
    return duration_ms
