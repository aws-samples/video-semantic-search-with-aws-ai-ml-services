import json
import logging
import boto3
from botocore.exceptions import ClientError
import os
import subprocess
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
import io
import math

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client("s3")

FFMPEG_PATH = "/opt/bin/ffmpeg"
MAX_CLIP_DURATION_SEC = 30
MAX_COMPOSITE_DIMENSION = 8000
MAX_COMPOSITE_SIZE_BYTES = 3.75 * 1024 * 1024
COMPOSITE_BORDER_SIZE = 5
DEFAULT_FRAMES_PER_SHOT = 3
MAX_FRAME_WIDTH = 1280


def lambda_handler(event, context):
    jobId = event["jobId"]
    video_name = event["video_name"]
    config = event.get("config", {})
    segments = event["segments"]
    frames_per_shot = config.get("framesPerShot", DEFAULT_FRAMES_PER_SHOT)

    bucket_videos = os.environ["bucket_videos"]
    bucket_images = os.environ["bucket_images"]
    bucket_shots = os.environ["bucket_shots"]
    tmp_dir = os.environ["tmp_dir"]

    # Download the video file from S3
    tmp_video_dir = os.path.join(tmp_dir, "video")
    os.makedirs(tmp_video_dir, exist_ok=True)
    local_video_path = os.path.join(tmp_video_dir, video_name)

    if not os.path.exists(local_video_path):
        logger.info(f"Downloading video: {video_name}")
        s3_client.download_file(bucket_videos, video_name, local_video_path)
        logger.info(f"Video downloaded to: {local_video_path}")

    # Create working directories
    tmp_frames_dir = os.path.join(tmp_dir, jobId)
    tmp_clips_dir = os.path.join(tmp_dir, "clips", jobId)
    os.makedirs(tmp_frames_dir, exist_ok=True)
    os.makedirs(tmp_clips_dir, exist_ok=True)

    # Collect ALL timestamps across all segments for global frame extraction
    all_timestamps = []
    for segment in segments:
        shot_startTime = segment["shot_startTime"]
        shot_endTime = segment["shot_endTime"]
        timestamps = get_timestamps(shot_startTime, shot_endTime, frames_per_shot)
        segment["_timestamps"] = timestamps
        all_timestamps.extend(timestamps)

    # Deduplicate and extract all frames at once so -sseof only applies to the video's final timestamp
    unique_timestamps = sorted(set(all_timestamps))
    extract_frames_global(
        timestamps=unique_timestamps,
        local_video_path=local_video_path,
        tmp_frames_dir=tmp_frames_dir,
    )

    # Upload all individual frames to S3
    upload_frames(
        frame_paths=[os.path.join(tmp_frames_dir, f"{ts}.png") for ts in unique_timestamps],
        timestamps=unique_timestamps,
        bucket_images=bucket_images,
        jobId=jobId,
    )

    # Now build composites and clips per segment (in parallel)
    def process_segment(segment):
        shot_id = segment["shot_id"]
        timestamps = segment["_timestamps"]
        frame_paths = [os.path.join(tmp_frames_dir, f"{ts}.png") for ts in timestamps]

        create_composite(
            frame_paths=frame_paths,
            bucket_images=bucket_images,
            jobId=jobId,
            shot_id=shot_id,
        )

        cut_clip(
            local_video_path=local_video_path,
            tmp_clips_dir=tmp_clips_dir,
            bucket_shots=bucket_shots,
            jobId=jobId,
            shot_id=shot_id,
            shot_startTime=segment["shot_startTime"],
            shot_endTime=segment["shot_endTime"],
        )

        return {
            "jobId": jobId,
            "video_name": video_name,
            "shot_id": shot_id,
            "shot_startTime": segment["shot_startTime"],
            "shot_endTime": segment["shot_endTime"],
            "frames": timestamps,
            "composite_key": f"{jobId}/{shot_id}_composite.png",
            "clip_key": f"{jobId}/{shot_id}_clip.mp4",
        }

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(process_segment, segments))

    # Clean up local video file
    try:
        if os.path.exists(local_video_path):
            os.remove(local_video_path)
    except OSError:
        pass

    return {
        "jobId": jobId,
        "video_name": video_name,
        "config": config,
        "TranscribeParams": event.get("TranscribeParams", {}),
        "segments": results,
    }


def get_timestamps(start_time, end_time, N):
    """Calculate N evenly-distributed timestamps within [startTime, endTime]."""
    if N <= 1:
        return [start_time]

    step = (end_time - start_time) / (N - 1)
    timestamps = [int(start_time + i * step) for i in range(N)]
    return timestamps


def extract_frames_global(timestamps, local_video_path, tmp_frames_dir):
    """Extract individual frames at the given timestamps using ffmpeg in parallel."""
    sorted_timestamps = sorted(timestamps)
    last_timestamp = sorted_timestamps[-1] if sorted_timestamps else None

    def extract_frame(timestamp_ms):
        output_file = os.path.join(tmp_frames_dir, f"{timestamp_ms}.png")
        # Handling the last timestamp for edge case.
        if timestamp_ms == last_timestamp:
            subprocess.run(
                [
                    FFMPEG_PATH,
                    "-sseof", "-0.1",
                    "-i", local_video_path,
                    "-vf", f"scale='min({MAX_FRAME_WIDTH},iw):-1'",
                    "-update", "1",
                    "-frames:v", "1",
                    "-q:v", "2",
                    "-y",
                    output_file,
                ],
                stderr=subprocess.PIPE,
            )
        else:
            timestamp_sec = timestamp_ms / 1000.0
            subprocess.run(
                [
                    FFMPEG_PATH,
                    "-ss", f"{timestamp_sec:.3f}",
                    "-i", local_video_path,
                    "-vf", f"scale='min({MAX_FRAME_WIDTH},iw):-1'",
                    "-vframes", "1",
                    "-q:v", "2",
                    "-y",
                    output_file,
                ],
                stderr=subprocess.PIPE,
            )
        return output_file

    frame_paths = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_ts = {
            executor.submit(extract_frame, ts): ts for ts in timestamps
        }
        concurrent.futures.wait(future_to_ts)
        # Return paths in the same order as the input timestamps
        for ts in timestamps:
            frame_paths.append(os.path.join(tmp_frames_dir, f"{ts}.png"))

    return frame_paths


def upload_frames(frame_paths, timestamps, bucket_images, jobId):
    """Upload individual frame PNGs to S3 in parallel."""
    extra_args = {"ContentType": "image/png"}

    with ThreadPoolExecutor(max_workers=10) as executor:
        upload_futures = []
        for i, frame_path in enumerate(frame_paths):
            if os.path.exists(frame_path):
                s3_key = f"{jobId}/{timestamps[i]}.png"
                upload_futures.append(
                    executor.submit(
                        s3_client.upload_file,
                        frame_path,
                        bucket_images,
                        s3_key,
                        ExtraArgs=extra_args,
                    )
                )
        concurrent.futures.wait(upload_futures)


def create_composite(frame_paths, bucket_images, jobId, shot_id):
    """Create a horizontal composite tile image from frames and upload to S3."""
    images = []
    for frame_path in frame_paths:
        if os.path.exists(frame_path):
            images.append(Image.open(frame_path))

    if not images:
        logger.warning(f"No frames available for composite: {shot_id}")
        return f"{jobId}/{shot_id}_composite.png"

    # Horizontal grid layout
    grid_width = sum(image.width + COMPOSITE_BORDER_SIZE for image in images) - COMPOSITE_BORDER_SIZE
    grid_height = max(image.height for image in images)
    grid_image = Image.new("RGB", (grid_width, grid_height))
    x_offset = 0
    for image in images:
        grid_image.paste(image, (x_offset, 0))
        x_offset += image.width + COMPOSITE_BORDER_SIZE

    # Maximum resolution constraint
    if grid_image.width > MAX_COMPOSITE_DIMENSION or grid_image.height > MAX_COMPOSITE_DIMENSION:
        scale_factor = min(
            MAX_COMPOSITE_DIMENSION / grid_image.width,
            MAX_COMPOSITE_DIMENSION / grid_image.height,
        )
        new_width = int(grid_image.width * scale_factor)
        new_height = int(grid_image.height * scale_factor)
        grid_image = grid_image.resize((new_width, new_height), Image.LANCZOS)

    # Check size and resize if necessary
    buffer = io.BytesIO()
    grid_image.save(buffer, format="PNG")
    size = buffer.tell()

    if size > MAX_COMPOSITE_SIZE_BYTES:
        resize_factor = math.sqrt(MAX_COMPOSITE_SIZE_BYTES / size) * 0.9
        new_width = int(grid_image.width * resize_factor)
        new_height = int(grid_image.height * resize_factor)
        grid_image = grid_image.resize((new_width, new_height), Image.LANCZOS)

        buffer = io.BytesIO()
        grid_image.save(buffer, format="PNG")
        size = buffer.tell()

        if size > MAX_COMPOSITE_SIZE_BYTES:
            width, height = grid_image.size
            while size > MAX_COMPOSITE_SIZE_BYTES:
                width = int(width * 0.9)
                height = int(height * 0.9)
                grid_image = grid_image.resize((width, height), Image.LANCZOS)
                buffer = io.BytesIO()
                grid_image.save(buffer, format="PNG")
                size = buffer.tell()

    buffer.seek(0)
    composite_key = f"{jobId}/{shot_id}_composite.png"
    s3_client.upload_fileobj(
        buffer,
        bucket_images,
        composite_key,
        ExtraArgs={"ContentType": "image/png"},
    )

    # Close opened images
    for image in images:
        image.close()

    return composite_key


def cut_clip(
    local_video_path,
    tmp_clips_dir,
    bucket_shots,
    jobId,
    shot_id,
    shot_startTime,
    shot_endTime,
):
    """Cut a video clip for the segment and upload to S3."""
    start_sec = shot_startTime / 1000.0
    duration_sec = (shot_endTime - shot_startTime) / 1000.0

    # Cap clip duration at MAX_CLIP_DURATION_SEC
    if duration_sec > MAX_CLIP_DURATION_SEC:
        duration_sec = MAX_CLIP_DURATION_SEC

    output_file = os.path.join(tmp_clips_dir, f"{shot_id}_clip.mp4")

    subprocess.run(
        [
            FFMPEG_PATH,
            "-ss", f"{start_sec:.3f}",
            "-i", local_video_path,
            "-t", f"{duration_sec:.3f}",
            "-c:v", "libx264",
            "-c:a", "aac",
            "-y",
            output_file,
        ],
        stderr=subprocess.PIPE,
    )

    clip_key = f"{jobId}/{shot_id}_clip.mp4"
    if os.path.exists(output_file):
        s3_client.upload_file(
            output_file,
            bucket_shots,
            clip_key,
            ExtraArgs={"ContentType": "video/mp4"},
        )
        # Clean up local clip file
        try:
            os.remove(output_file)
        except OSError:
            pass
    else:
        logger.error(f"Failed to create clip for {shot_id}")

    return clip_key
