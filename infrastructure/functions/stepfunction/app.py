import json
import logging
import os
from decimal import Decimal

import boto3

sf_client = boto3.client("stepfunctions")
dynamodb_resource = boto3.resource("dynamodb")


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            if obj % 1 == 0:
                return int(obj)
            return float(obj)
        return super().default(obj)


def lambda_handler(event, context):
    records = event["Records"]
    message = records[0]["body"]

    # Deserialize the message body from the string representation
    message_body = json.loads(message)

    # Access the values in the JSON payload
    jobId = records[0]["messageId"]
    video_name = message_body["video_name"]

    # Read IndexingConfig from DynamoDB
    dynamodb_table = os.environ["vss_dynamodb_table"]
    table = dynamodb_resource.Table(dynamodb_table)
    response = table.get_item(Key={"JobId": jobId})
    item = response.get("Item", {})
    config = item.get("IndexingConfig", {})

    vss_input = {"jobId": jobId, "video_name": video_name, "config": config}

    sfResponse = sf_client.start_execution(
        stateMachineArn=os.environ["StepFunction"],
        input=json.dumps(vss_input, cls=DecimalEncoder),
    )

    return {"statusCode": 200}
