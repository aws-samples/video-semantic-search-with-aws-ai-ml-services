import json
import logging
import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key
import os
import datetime
from decimal import Decimal

dynamodb_resource = boto3.resource("dynamodb")
neptune_client = boto3.client("neptune-graph")


def lambda_handler(event, context):
    dynamodb_table = os.environ["vss_dynamodb_table"]
    neptune_graph_id = os.environ["neptune_graph_id"]
    region = os.environ["region"]

    # Extract jobId from event - may come as list from Step Functions Map state
    if isinstance(event, list):
        jobId = event[0]["jobId"]
    else:
        jobId = event["jobId"]

    table = dynamodb_resource.Table(dynamodb_table)

    # Calculate total cost from CostEntries
    total_cost = calculate_total_cost(table, jobId)

    # Update Neptune Video node status to Completed
    try:
        update_neptune_video_status(neptune_graph_id, jobId)
    except Exception as e:
        logging.error(f"Failed to update Neptune video status: {e}")

    # Add NEXT_SEGMENT edges in Neptune for sequential segments
    try:
        add_next_segment_edges(neptune_graph_id, jobId)
    except Exception as e:
        logging.error(f"Failed to add NEXT_SEGMENT edges: {e}")

    # Update DynamoDB with Completed status, end time, and total cost
    status = "Completed"
    endTime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    update_job_status(table, jobId, status, endTime, total_cost)

    return {"statusCode": 200}


def calculate_total_cost(table, jobId):
    total_cost = Decimal("0")
    try:
        response = table.get_item(Key={"JobId": jobId})
        item = response.get("Item", {})
        cost_entries = item.get("CostEntries", [])
        for entry in cost_entries:
            cost = entry.get("cost", Decimal("0"))
            if isinstance(cost, (int, float, str)):
                cost = Decimal(str(cost))
            total_cost += cost
    except Exception as e:
        logging.error(f"Failed to calculate total cost: {e}")

    return total_cost


def update_neptune_video_status(neptune_graph_id, jobId):
    query = """
        MATCH (v:Video {jobId: $jobId})
        SET v.status = 'Completed'
        RETURN v
    """
    neptune_client.execute_query(
        graphIdentifier=neptune_graph_id,
        queryString=query,
        parameters={"jobId": jobId},
        language="OPEN_CYPHER",
    )


def add_next_segment_edges(neptune_graph_id, jobId):
    query = """
        MATCH (v:Video {jobId: $jobId})-[:HAS_SEGMENT]->(s:Segment)
        WITH s ORDER BY s.startTime
        WITH collect(s) AS segments
        UNWIND range(0, size(segments) - 2) AS i
        WITH segments[i] AS current, segments[i + 1] AS next
        MERGE (current)-[:NEXT_SEGMENT]->(next)
        RETURN count(*) AS edges_created
    """
    neptune_client.execute_query(
        graphIdentifier=neptune_graph_id,
        queryString=query,
        parameters={"jobId": jobId},
        language="OPEN_CYPHER",
    )


def update_job_status(table, jobId, status, endTime, total_cost):
    table.update_item(
        Key={"JobId": jobId},
        UpdateExpression="SET #st = :status, #et = :endTime, #tc = :totalCost",
        ExpressionAttributeValues={
            ":status": status,
            ":endTime": endTime,
            ":totalCost": total_cost,
        },
        ExpressionAttributeNames={
            "#st": "Status",
            "#et": "EndTime",
            "#tc": "TotalCost",
        },
    )
