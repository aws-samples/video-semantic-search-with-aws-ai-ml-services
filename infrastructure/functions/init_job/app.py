import json
import logging
import boto3
from botocore.exceptions import ClientError
import os
import datetime
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb_client = boto3.resource("dynamodb")
neptune_client = boto3.client("neptune-graph")

DEFAULT_CONFIG = {
    "segmentationMode": "shot",
    "intervalSeconds": 10,
    "framesPerShot": 3,
    "transcription": True,
    "faceRecognition": True,
    "celebrityDetection": True,
}


def lambda_handler(event, context):
    dynamodb_table = os.environ["vss_dynamodb_table"]
    neptune_graph_id = os.environ["neptune_graph_id"]
    region = os.environ["region"]
    aoss_host = os.environ["aoss_host"]
    aoss_visual_index = os.environ.get("aoss_visual_index", "vss-visual-index")
    aoss_audio_index = os.environ.get("aoss_audio_index", "vss-audio-index")
    embedding_dimension = int(os.environ.get("embedding_dimension", "3072"))

    jobId = event["jobId"]
    video_name = event["video_name"]
    config = event.get("config", {})

    # Merge provided config with defaults
    merged_config = {**DEFAULT_CONFIG, **config}

    started = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Step 1: Initialize cost tracking on DynamoDB job item
    try:
        init_cost_tracking(dynamodb_table, jobId)
        logger.info("Initialized cost tracking for job %s", jobId)
    except Exception as e:
        logger.error("Failed to initialize cost tracking for job %s: %s", jobId, e)
        raise

    # Step 2: Create Video node in Neptune Analytics graph
    try:
        create_neptune_video_node(neptune_graph_id, jobId, video_name, started)
        logger.info("Created Neptune Video node for job %s", jobId)
    except Exception as e:
        logger.error("Failed to create Neptune Video node for job %s: %s", jobId, e)
        raise

    # Step 3: Create OpenSearch indices if they don't exist
    client = get_opensearch_client(aoss_host, region)

    try:
        create_visual_index(client, aoss_visual_index, embedding_dimension)
    except Exception as e:
        logger.error("Failed to create visual index: %s", e)
        raise

    try:
        create_audio_index(client, aoss_audio_index, embedding_dimension)
    except Exception as e:
        logger.error("Failed to create audio index: %s", e)
        raise

    # Step 4: Create hybrid search pipeline (idempotent)
    try:
        create_hybrid_search_pipeline(client)
        logger.info("Hybrid search pipeline created or already exists")
    except Exception as e:
        logger.error("Failed to create hybrid search pipeline: %s", e)
        raise

    # Return event with merged config for downstream steps
    return {
        "jobId": jobId,
        "video_name": video_name,
        "config": merged_config,
    }


def init_cost_tracking(dynamodb_table, jobId):
    """Add an empty CostEntries list to the DynamoDB job item."""
    table = dynamodb_client.Table(dynamodb_table)
    table.update_item(
        Key={"JobId": jobId},
        UpdateExpression="SET CostEntries = :empty_list",
        ExpressionAttributeValues={":empty_list": []},
    )


def create_neptune_video_node(graph_id, jobId, video_name, started):
    """Create or update a Video node in Neptune Analytics graph."""
    neptune_client.execute_query(
        graphIdentifier=graph_id,
        queryString=(
            "MERGE (v:Video {jobId: $jobId}) "
            "SET v.videoName = $videoName, v.uploadTime = $uploadTime, v.status = 'Indexing'"
        ),
        parameters={
            "jobId": jobId,
            "videoName": video_name,
            "uploadTime": started,
        },
        language="OPEN_CYPHER",
    )


def get_opensearch_client(host, region):
    """Create and return an OpenSearch client with AWS v4 signing."""
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

    return client


def create_visual_index(client, index, embedding_dimension):
    """Create the visual index for shot-level data if it does not exist."""
    exist = client.indices.exists(index=index)
    if not exist:
        logger.info("Creating visual index: %s", index)
        index_body = {
            "mappings": {
                "properties": {
                    "jobId": {"type": "keyword"},
                    "shot_id": {"type": "keyword"},
                    "shot_description": {"type": "text"},
                    "shot_desc_vector": {
                        "type": "knn_vector",
                        "dimension": embedding_dimension,
                        "method": {
                            "engine": "faiss",
                            "space_type": "innerproduct",
                            "name": "hnsw",
                            "parameters": {"ef_construction": 512, "m": 16},
                        },
                    },
                    "shot_image_vector": {
                        "type": "knn_vector",
                        "dimension": embedding_dimension,
                        "method": {
                            "engine": "faiss",
                            "space_type": "innerproduct",
                            "name": "hnsw",
                            "parameters": {"ef_construction": 512, "m": 16},
                        },
                    },
                    "shot_video_vector": {
                        "type": "knn_vector",
                        "dimension": embedding_dimension,
                        "method": {
                            "engine": "faiss",
                            "space_type": "innerproduct",
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
        client.indices.create(index=index, body=index_body)
        logger.info("Visual index created: %s", index)
    else:
        logger.info("Visual index already exists: %s", index)


def create_audio_index(client, index, embedding_dimension):
    """Create the audio index for transcript-level data if it does not exist."""
    exist = client.indices.exists(index=index)
    if not exist:
        logger.info("Creating audio index: %s", index)
        index_body = {
            "mappings": {
                "properties": {
                    "jobId": {"type": "keyword"},
                    "video_name": {"type": "text"},
                    "transcript_id": {"type": "keyword"},
                    "transcript_startTime": {"type": "long"},
                    "transcript_endTime": {"type": "long"},
                    "transcript": {"type": "text"},
                    "transcript_vector": {
                        "type": "knn_vector",
                        "dimension": embedding_dimension,
                        "method": {
                            "engine": "faiss",
                            "space_type": "innerproduct",
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
        client.indices.create(index=index, body=index_body)
        logger.info("Audio index created: %s", index)
    else:
        logger.info("Audio index already exists: %s", index)


def create_hybrid_search_pipeline(client):
    """Create the hybrid search pipeline idempotently via PUT."""
    pipeline_body = {
        "phase_results_processors": [
            {
                "normalization-processor": {
                    "normalization": {"technique": "min_max"},
                    "combination": {
                        "technique": "arithmetic_mean",
                        "parameters": {"weights": [0.2, 0.5, 0.3]},
                    },
                }
            }
        ]
    }

    pipeline_id = "vss-hybrid-search-pipeline"
    response = client.transport.perform_request(
        "PUT",
        f"/_search/pipeline/{pipeline_id}",
        body=pipeline_body,
    )
    logger.info("Hybrid search pipeline response: %s", response)
    return response
