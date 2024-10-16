import json
import logging
import re
import boto3
from botocore.exceptions import ClientError
import os
import datetime
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth


def lambda_handler(event, context):
    dynamodb_table = os.environ["vss_dynamodb_table"]
    jobId = event[0]["jobId"]
    status = "Failed"
    endTime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updatejobStatus(dynamodb_table, jobId, status, endTime)
    delete_shot_collection(os.environ["aoss_host"], os.environ["region"], jobId)
    return {"statusCode": 200}


def updatejobStatus(dynamodb_table, jobId, status, endTime):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(dynamodb_table)
    dynamodbResponse = table.update_item(
        Key={"JobId": jobId},
        UpdateExpression="SET #st = :value1, #et = :value2",
        ExpressionAttributeValues={":value1": status, ":value2": endTime},
        ExpressionAttributeNames={"#st": "Status", "#et": "EndTime"},
    )


def delete_shot_collection(host, region, index):
    host = host.split("://")[1] if "://" in host else host
    credentials = boto3.Session().get_credentials()
    auth = AWSV4SignerAuth(credentials, region, "aoss")

    client = OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        pool_maxsize=20,
    )

    exist = client.indices.exists(index)
    if exist:
        response = client.indices.delete(index=index)
