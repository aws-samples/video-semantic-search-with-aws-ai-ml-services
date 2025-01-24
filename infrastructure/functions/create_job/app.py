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
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth

sqs_client = boto3.client("sqs")


def lambda_handler(event, context):
    bucket_name = os.environ["bucket_videos"]
    userId = event["queryStringParameters"]["userId"]
    video_name = event["queryStringParameters"]["video_name"]

    vss_input = {"userId": userId, "video_name": video_name}
    sqs_queue_url = os.environ["sqs_queue_url"]
    response = sqs_client.send_message(
        QueueUrl=sqs_queue_url, MessageBody=json.dumps(vss_input)
    )

    jobId = response["MessageId"]

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(os.environ["vss_dynamodb_table"])
    status = str(random.randint(1, 25)) + "%"
    started = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dynamodbResponse = table.put_item(
        Item={
            "JobId": jobId,
            "UserId": userId,
            "Input": video_name,
            "Started": started,
            "EndTime": "-",
            "Status": "Indexing",
        }
    )

    response = {
        "jobId": jobId,
        "input": video_name,
        "started": started,
        "status": "Indexing",
    }

    try:
        create_opensearch_index(
            os.environ["aoss_host"],
            os.environ["region"],
            os.environ["aoss_index"],
            os.environ["text_embedding_dimension"],
        )
    except Exception as e:
        logging.error(f"An error occurred: {e}")

    try:
        create_shot_collection(
            os.environ["aoss_host"],
            os.environ["region"],
            jobId,
            os.environ["image_embedding_dimension"],
        )
    except Exception as e:
        logging.error(f"An error occurred: {e}")

    return {"statusCode": 200, "body": json.dumps(response)}


def create_opensearch_index(host, region, index, len_embedding):
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
    if not exist:
        print("Creating index")
        index_body = {
            "mappings": {
                "properties": {
                    "jobId": {"type": "text"},
                    "video_name": {"type": "text"},
                    "shot_id": {"type": "text"},
                    "shot_startTime": {"type": "text"},
                    "shot_endTime": {"type": "text"},
                    "shot_description": {"type": "text"},
                    "shot_publicFigures": {"type": "text"},
                    "shot_privateFigures": {"type": "text"},
                    "shot_transcript": {"type": "text"},
                    "shot_image_vector": {
                        "type": "knn_vector",
                        "dimension": len_embedding,
                        "method": {
                            "engine": "nmslib",
                            "space_type": "cosinesimil",
                            "name": "hnsw",
                            "parameters": {"ef_construction": 512, "m": 16},
                        },
                    },
                    "shot_desc_vector": {
                        "type": "knn_vector",
                        "dimension": len_embedding,
                        "method": {
                            "engine": "nmslib",
                            "space_type": "cosinesimil",
                            "name": "hnsw",
                            "parameters": {"ef_construction": 512, "m": 16},
                        },
                    },
                    "shot_transcript_vector": {
                        "type": "knn_vector",
                        "dimension": len_embedding,
                        "method": {
                            "engine": "nmslib",
                            "space_type": "cosinesimil",
                            "name": "hnsw",
                            "parameters": {"ef_construction": 512, "m": 16},
                        },
                    },
                }
            },
            "settings": {
                "index": {
                    "number_of_shards": 2,
                    "knn.algo_param": {"ef_search": 512},
                    "knn": True,
                }
            },
        }
        response = client.indices.create(index, body=index_body)

    return client


def create_shot_collection(host, region, index, len_embedding):
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
    if not exist:
        print("Creating index")
        index_body = {
            "mappings": {
                "properties": {
                    "jobId": {"type": "text"},
                    "video_name": {"type": "text"},
                    "shot_id": {"type": "text"},
                    "shot_startTime": {"type": "text"},
                    "shot_endTime": {"type": "text"},
                    "frame_publicFigures": {"type": "text"},
                    "frame_privateFigures": {"type": "text"},
                    "frame_image_vector": {
                        "type": "knn_vector",
                        "dimension": len_embedding,
                        "method": {
                            "engine": "faiss",
                            "space_type": "l2",
                            "name": "hnsw",
                            "parameters": {"ef_construction": 512, "m": 16},
                        },
                    },
                }
            },
            "settings": {
                "index": {
                    "number_of_shards": 2,
                    "knn.algo_param": {"ef_search": 512},
                    "knn": True,
                }
            },
        }
        response = client.indices.create(index, body=index_body)

    return client
