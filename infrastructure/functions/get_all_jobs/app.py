import json
import logging
import os
from decimal import Decimal

import boto3

dynamodb_client = boto3.resource("dynamodb")


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            if obj % 1 == 0:
                return int(obj)
            return float(obj)
        return super().default(obj)


def lambda_handler(event, context):
    table = dynamodb_client.Table(os.environ["vss_dynamodb_table"])

    response = table.scan()
    items = response["Items"]
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response["Items"])

    return {"statusCode": 200, "body": json.dumps(items, cls=DecimalEncoder)}
