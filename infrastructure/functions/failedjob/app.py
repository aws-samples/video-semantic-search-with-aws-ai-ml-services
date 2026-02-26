import json
import logging
import boto3
import os
import datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)

neptune_client = boto3.client("neptune-graph")


def lambda_handler(event, context):
    dynamodb_table = os.environ["vss_dynamodb_table"]
    # Handle both array and dict input from Step Functions error handling
    if isinstance(event, list) and len(event) > 0:
        jobId = event[0].get("jobId", "unknown")
    elif isinstance(event, dict):
        jobId = event.get("jobId", event.get("Cause", "unknown"))
    else:
        jobId = "unknown"

    status = "Failed"
    endTime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updatejobStatus(dynamodb_table, jobId, status, endTime)

    # Update Neptune Video node status
    try:
        graph_id = os.environ.get("neptune_graph_id")
        if graph_id and jobId != "unknown":
            neptune_client.execute_query(
                graphIdentifier=graph_id,
                queryString="MATCH (v:Video {jobId: $jobId}) SET v.status = 'Failed'",
                parameters={"jobId": jobId},
                language="OPEN_CYPHER",
            )
    except Exception as e:
        logger.error(f"Failed to update Neptune: {e}")

    return {"statusCode": 200}


def updatejobStatus(dynamodb_table, jobId, status, endTime):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(dynamodb_table)
    try:
        table.update_item(
            Key={"JobId": jobId},
            UpdateExpression="SET #st = :value1, #et = :value2",
            ExpressionAttributeValues={":value1": status, ":value2": endTime},
            ExpressionAttributeNames={"#st": "Status", "#et": "EndTime"},
        )
    except Exception as e:
        logger.error(f"Failed to update DynamoDB: {e}")
