import json
import logging
import re
import boto3
from botocore.exceptions import ClientError
import os
import time
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth

dynamodb_client = boto3.resource("dynamodb")
bedrock_client = boto3.client(service_name="bedrock-runtime")
comprehend_client = boto3.client("comprehend")


def lambda_handler(event, context):
    http_method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    if http_method == "GET":
        aoss_index = event["queryStringParameters"]["index"]
        client = get_opensearch_client(
            os.environ["aoss_host"], os.environ["region"], aoss_index
        )
        query_type = event["queryStringParameters"]["type"]
        user_query = event["queryStringParameters"]["query"]
        response = searchByText(aoss_index, client, user_query)
    else:
        request_data = json.loads(event["body"])
        aoss_index = request_data["index"]
        client = get_opensearch_client(
            os.environ["aoss_host"], os.environ["region"], aoss_index
        )
        query_type = request_data["type"]
        user_query = request_data["query"]
        if user_query.startswith("data:image"):
            user_query = user_query.split(",")[1]
        response = searchByImage(aoss_index, client, user_query)

    return {"statusCode": 200, "body": json.dumps(response)}


def searchByText(aoss_index, client, user_query):
    response = comprehend_client.detect_entities(Text=user_query, LanguageCode="en")
    names = []
    locations = []
    for entity in response["Entities"]:
        if entity["Type"] == "PERSON":
            names.append(entity["Text"])

    entities = names + locations
    if entities:
        entities = ", ".join(entities)
    else:
        entities = ""

    text_embedding = get_text_embedding(os.environ["text_embedding_model"], user_query)

    aoss_query = {
        "size": 50,
        "query": {
            "script_score": {
                "query": {"bool": {"should": []}},
                "script": {
                    "lang": "knn",
                    "source": "knn_score",
                    "params": {
                        "field": "shot_desc_vector",
                        "query_value": text_embedding,
                        "space_type": "cosinesimil",
                    },
                },
            }
        },
        "_source": [
            "jobId",
            "video_name",
            "shot_id",
            "shot_startTime",
            "shot_endTime",
            "shot_description",
            "shot_publicFigures",
            "shot_privateFigures",
            "shot_transcript",
        ],
    }

    pattern = r'"(.*?)"'
    matches = re.findall(pattern, user_query)
    if len(matches) > 0:
        aoss_query["query"]["script_score"]["query"]["bool"]["must"] = []
        for match in matches:
            aoss_query["query"]["script_score"]["query"]["bool"]["must"].append(
                {
                    "multi_match": {
                        "query": match,
                        "fields": [
                            "shot_publicFigures",
                            "shot_privateFigures",
                            "shot_description",
                        ],
                        "type": "phrase",
                    }
                }
            )

    response = client.search(body=aoss_query, index=aoss_index)
    hits = response["hits"]["hits"]
    response = []
    for hit in hits:
        if hit["_score"] >= 0:  # Set score threshold
            response.append(
                {
                    "jobId": hit["_source"]["jobId"],
                    "video_name": hit["_source"]["video_name"],
                    "shot_id": hit["_source"]["shot_id"],
                    "shot_startTime": hit["_source"]["shot_startTime"],
                    "shot_endTime": hit["_source"]["shot_endTime"],
                    "shot_description": hit["_source"]["shot_description"],
                    "shot_publicFigures": hit["_source"]["shot_publicFigures"],
                    "shot_privateFigures": hit["_source"]["shot_privateFigures"],
                    "shot_transcript": hit["_source"]["shot_transcript"],
                    "score": hit["_score"],
                }
            )

    return response


def get_opensearch_client(host, region, index):
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


def searchByImage(aoss_index, client, user_query):
    image_embedding = get_titan_image_embedding(
        os.environ["image_embedding_model"], user_query
    )

    aoss_query = {
        "size": 50,
        "query": {"knn": {"shot_image_vector": {"vector": image_embedding, "k": 50}}},
        "_source": [
            "jobId",
            "video_name",
            "shot_id",
            "shot_startTime",
            "shot_endTime",
            "shot_description",
            "shot_publicFigures",
            "shot_privateFigures",
            "shot_transcript",
        ],
    }

    response = client.search(body=aoss_query, index=aoss_index)
    hits = response["hits"]["hits"]
    response = []
    for hit in hits:
        if hit["_score"] >= 0:  # Set score threshold
            response.append(
                {
                    "jobId": hit["_source"]["jobId"],
                    "video_name": hit["_source"]["video_name"],
                    "shot_id": hit["_source"]["shot_id"],
                    "shot_startTime": hit["_source"]["shot_startTime"],
                    "shot_endTime": hit["_source"]["shot_endTime"],
                    "shot_description": hit["_source"]["shot_description"],
                    "shot_publicFigures": hit["_source"]["shot_publicFigures"],
                    "shot_privateFigures": hit["_source"]["shot_privateFigures"],
                    "shot_transcript": hit["_source"]["shot_transcript"],
                    "score": hit["_score"],
                }
            )

    return response


def get_text_embedding(text_embedding_model, shot_description):
    accept = "application/json"
    content_type = "application/json"
    if text_embedding_model.startswith("amazon.titan-embed-text"):
        body = json.dumps(
            {"inputText": shot_description, "dimensions": 1024, "normalize": True}
        )
        response = bedrock_client.invoke_model(
            body=body,
            modelId=text_embedding_model,
            accept=accept,
            contentType=content_type,
        )
        response_body = json.loads(response["body"].read())
        embedding = response_body.get("embedding")
    else:
        body = json.dumps(
            {"texts": [shot_description], "input_type": "search_document"}
        )
        response = bedrock_client.invoke_model(
            body=body,
            modelId=text_embedding_model,
            accept=accept,
            contentType=content_type,
        )
        response_body = json.loads(response["body"].read())
        embedding = response_body.get("embeddings")[0]

    return embedding


def get_titan_image_embedding(embedding_model, query):
    accept = "application/json"
    content_type = "application/json"
    body = json.dumps({"inputImage": query})
    response = bedrock_client.invoke_model(
        body=body, modelId=embedding_model, accept=accept, contentType=content_type
    )
    response_body = json.loads(response["body"].read())
    embedding = response_body.get("embedding")
    return embedding
