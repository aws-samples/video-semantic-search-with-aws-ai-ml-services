import json
import logging
import re
import boto3
from botocore.exceptions import ClientError
import os
import time
from PIL import Image
import math
import io
import base64

dynamodb_client = boto3.resource("dynamodb")
rek_client = boto3.client("rekognition")
s3_client = boto3.client("s3")
bedrock_client = boto3.client(service_name="bedrock-runtime")


def lambda_handler(event, context):
    jobId = event["jobId"]
    video_name = event["video_name"]
    bucket_images = os.environ["bucket_images"]
    bucket_shots = os.environ["bucket_shots"]
    shot_startTime = event["shot_startTime"]
    shot_endTime = event["shot_endTime"]
    frames = event["frames"]
    shot_id = f"{shot_startTime}-{shot_endTime}"

    images = []
    for frame in frames:
        obj = s3_client.get_object(Bucket=bucket_images, Key=f"{jobId}/{frame}.png")
        image_data = obj["Body"].read()
        images.append(Image.open(io.BytesIO(image_data)))

    generate_shot_image(jobId, bucket_shots, images, shot_id)

    return {
        "jobId": jobId,
        "video_name": video_name,
        "shot_id": shot_id,
        "shot_startTime": shot_startTime,
        "shot_endTime": shot_endTime,
        "shot_frames": frames,
    }


def generate_shot_image(
    jobId, bucket_shots, images, shot_id, border_size=5, layout="horizontal"
):
    if layout == "horizontal":
        # Horizontal grid layout
        grid_width = sum(image.width + border_size for image in images) - border_size
        grid_height = max(image.height for image in images)
        grid_image = Image.new("RGB", (grid_width, grid_height))
        x_offset = 0
        for image in images:
            grid_image.paste(image, (x_offset, 0))
            x_offset += image.width + border_size
    else:  # tile layout
        num_images = len(images)
        grid_size = math.ceil(math.sqrt(num_images))
        grid_width = grid_size * (images[0].width + border_size) - border_size
        grid_height = grid_size * (images[0].height + border_size) - border_size
        grid_image = Image.new("RGB", (grid_width, grid_height))

        for i, image in enumerate(images):
            row = i // grid_size
            col = i % grid_size
            grid_image.paste(
                image,
                (col * (image.width + border_size), row * (image.height + border_size)),
            )

    # Maximum resolution constraint (8000 x 8000)
    max_dimension = 8000
    if grid_image.width > max_dimension or grid_image.height > max_dimension:
        scale_factor = min(
            max_dimension / grid_image.width, max_dimension / grid_image.height
        )
        new_width = int(grid_image.width * scale_factor)
        new_height = int(grid_image.height * scale_factor)
        grid_image = grid_image.resize((new_width, new_height), Image.LANCZOS)

    # Max size allowed (3.75 MB)
    max_size_bytes = 3.75 * 1024 * 1024

    # Check initial size
    buffer = io.BytesIO()
    grid_image.save(buffer, format="PNG")
    size = buffer.tell()

    # If too large, resize the image
    if size > max_size_bytes:
        resize_factor = math.sqrt(max_size_bytes / size) * 0.9
        new_width = int(grid_image.width * resize_factor)
        new_height = int(grid_image.height * resize_factor)

        grid_image = grid_image.resize((new_width, new_height), Image.LANCZOS)

        buffer = io.BytesIO()
        grid_image.save(buffer, format="PNG")
        size = buffer.tell()

        if size > max_size_bytes:
            width, height = grid_image.size
            while size > max_size_bytes:
                width = int(width * 0.9)
                height = int(height * 0.9)
                grid_image = grid_image.resize((width, height), Image.LANCZOS)
                buffer = io.BytesIO()
                grid_image.save(buffer, format="PNG")
                size = buffer.tell()

    buffer.seek(0)
    image_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    s3_client.upload_fileobj(
        buffer,
        bucket_shots,
        f"{jobId}/{shot_id}.png",
        ExtraArgs={"ContentType": "image/png"},
    )
