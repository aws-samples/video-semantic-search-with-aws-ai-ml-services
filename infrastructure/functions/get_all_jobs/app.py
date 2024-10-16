import json
import logging
import re
import boto3
from botocore.exceptions import ClientError
import os
import datetime
import time
import uuid
import random

dynamodb_client = boto3.resource("dynamodb")


def lambda_handler(event, context):
    table = dynamodb_client.Table(os.environ["vss_dynamodb_table"])

    response = table.scan()
    items = response["Items"]
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response["Items"])

    return {"statusCode": 200, "body": json.dumps(items)}
