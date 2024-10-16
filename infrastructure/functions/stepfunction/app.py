import json
import logging
import re
import boto3
from botocore.exceptions import ClientError
import os
import datetime
import time
import uuid

sf_client = boto3.client("stepfunctions")


def lambda_handler(event, context):
    records = event["Records"]
    message = records[0]["body"]

    # Deserialize the message body from the string representation
    message_body = json.loads(message)

    # Access the values in the JSON payload
    jobId = records[0]["messageId"]
    video_name = message_body["video_name"]

    vsh_input = {"jobId": jobId, "video_name": video_name}

    sfResponse = sf_client.start_execution(
        stateMachineArn=os.environ["StepFunction"], input=json.dumps(vsh_input)
    )

    return {"statusCode": 200}
