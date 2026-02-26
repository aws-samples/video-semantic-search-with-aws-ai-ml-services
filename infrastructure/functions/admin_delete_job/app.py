import json
import logging
import boto3
from botocore.exceptions import ClientError
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth, NotFoundError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client("s3")
dynamodb_resource = boto3.resource("dynamodb")
neptune_client = boto3.client("neptune-graph")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "DELETE,OPTIONS",
    "Content-Type": "application/json",
}


def lambda_handler(event, context):
    try:
        # Verify admin authorization
        if not is_admin(event):
            return {
                "statusCode": 403,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "Forbidden: admin group membership required"}),
            }

        job_id = event.get("pathParameters", {}).get("jobId")
        if not job_id:
            return {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "Missing required path parameter: jobId"}),
            }

        aoss_host = os.environ["aoss_host"]
        aoss_visual_index = os.environ["aoss_visual_index"]
        aoss_audio_index = os.environ["aoss_audio_index"]
        region = os.environ["region"]
        neptune_graph_id = os.environ["neptune_graph_id"]
        dynamodb_table = os.environ["vss_dynamodb_table"]
        bucket_shots = os.environ["bucket_shots"]
        bucket_images = os.environ["bucket_images"]
        bucket_transcripts = os.environ["bucket_transcripts"]
        bucket_videos = os.environ["bucket_videos"]
        face_collection_id = os.environ.get("face_collection_id", "")

        errors = []

        # 0. Look up video name and mark as "Deleting" before we start
        video_name = get_video_name(dynamodb_table, job_id)
        set_deleting_status(dynamodb_table, job_id)

        # Run all deletion steps in parallel to stay under API Gateway 30s timeout
        def delete_opensearch_visual():
            delete_opensearch_documents(aoss_host, region, aoss_visual_index, job_id)

        def delete_opensearch_audio():
            delete_opensearch_documents(aoss_host, region, aoss_audio_index, job_id)

        def delete_neptune():
            delete_neptune_data(neptune_graph_id, job_id, face_collection_id)

        def delete_s3_all():
            for bucket_name, bucket_label in [
                (bucket_shots, "shots"),
                (bucket_images, "images"),
                (bucket_transcripts, "transcripts"),
            ]:
                try:
                    delete_s3_prefix(bucket_name, job_id)
                except Exception as e:
                    logger.error("Failed to delete from S3 bucket %s: %s", bucket_label, str(e))
                    errors.append(f"S3 {bucket_label}: {str(e)}")

            for ext in [".srt", ".json"]:
                try:
                    s3_client.delete_object(Bucket=bucket_transcripts, Key=f"{job_id}{ext}")
                except Exception as e:
                    logger.warning("Failed to delete transcript file %s%s: %s", job_id, ext, str(e))

            if video_name:
                try:
                    s3_client.delete_object(Bucket=bucket_videos, Key=video_name)
                    logger.info("Deleted video s3://%s/%s", bucket_videos, video_name)
                except Exception as e:
                    logger.error("Failed to delete video file: %s", str(e))
                    errors.append(f"S3 video: {str(e)}")

        # Phase 1: Delete data from all services in parallel (except DynamoDB)
        tasks = {
            "OpenSearch visual": delete_opensearch_visual,
            "OpenSearch audio": delete_opensearch_audio,
            "Neptune": delete_neptune,
            "S3": delete_s3_all,
        }

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(fn): label for label, fn in tasks.items()}
            for future in as_completed(futures):
                label = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logger.error("Failed to delete %s: %s", label, str(e))
                    errors.append(f"{label}: {str(e)}")

        # Phase 2: Only delete DynamoDB entry after all other services are cleaned up
        if not errors:
            try:
                delete_dynamodb_item(dynamodb_table, job_id)
            except Exception as e:
                logger.error("Failed to delete DynamoDB: %s", str(e))
                errors.append(f"DynamoDB: {str(e)}")
        else:
            logger.warning(
                "Skipping DynamoDB deletion for job %s due to prior errors: %s",
                job_id, errors,
            )

        if errors:
            logger.warning("Job %s deleted with partial errors: %s", job_id, errors)
            return {
                "statusCode": 207,
                "headers": CORS_HEADERS,
                "body": json.dumps({
                    "status": "deleted",
                    "warnings": errors,
                }),
            }

        logger.info("Successfully deleted all data for job %s", job_id)
        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps({"status": "deleted"}),
        }

    except ClientError as e:
        logger.error("AWS service error: %s", str(e))
        return {
            "statusCode": 502,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "AWS service error", "detail": str(e)}),
        }
    except Exception as e:
        logger.error("Unexpected error: %s", str(e), exc_info=True)
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "Internal server error", "detail": str(e)}),
        }


def is_admin(event):
    """Check if the caller belongs to the 'admin' Cognito group."""
    try:
        claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
        groups = claims.get("cognito:groups", "")
        if isinstance(groups, list):
            return "admin" in groups
        if isinstance(groups, str):
            cleaned = groups.strip("[] ")
            group_list = [g.strip() for g in cleaned.split(",") if g.strip()]
            return "admin" in group_list
        return False
    except (KeyError, AttributeError):
        return False


def get_opensearch_client(host, region):
    """Create an OpenSearch client with AWS IAM authentication."""
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


def delete_opensearch_documents(aoss_host, region, index, job_id):
    """Delete all documents matching jobId from an OpenSearch index.

    OpenSearch Serverless vector search collections don't support delete_by_query
    or custom document IDs. We search for matching docs, collect their auto-generated
    _ids, then delete each by ID.
    """
    client = get_opensearch_client(aoss_host, region)

    if not client.indices.exists(index=index):
        logger.info("OpenSearch index %s does not exist, skipping", index)
        return

    search_query = {
        "query": {"term": {"jobId": job_id}},
        "_source": False,
        "size": 1000,
    }

    # Phase 1: Collect all matching document IDs (paginate with search_after)
    doc_ids = []
    search_after = None
    while True:
        query = {
            "query": {"term": {"jobId": job_id}},
            "_source": False,
            "size": 1000,
            "sort": [{"_id": "asc"}],
        }
        if search_after:
            query["search_after"] = search_after
        response = client.search(index=index, body=query)
        hits = response.get("hits", {}).get("hits", [])
        if not hits:
            break
        doc_ids.extend(h["_id"] for h in hits)
        search_after = hits[-1]["sort"]

    if not doc_ids:
        logger.info("OpenSearch index %s: no documents to delete for job=%s", index, job_id)
        return

    # Phase 2: Delete all collected IDs in parallel
    def delete_doc(doc_id):
        try:
            client.delete(index=index, id=doc_id, ignore=[404])
            return True
        except NotFoundError:
            return False

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(delete_doc, doc_ids))
    deleted = sum(1 for r in results if r)

    logger.info("OpenSearch delete on index=%s: found=%d deleted=%d", index, len(doc_ids), deleted)


def delete_neptune_data(neptune_graph_id, job_id, face_collection_id=""):
    """Delete Video node and all connected Segment, Frame nodes, edges, and orphaned Face nodes."""

    # 1. Collect faceIds linked to this job's segments BEFORE deleting them
    orphan_face_ids = []
    try:
        collect_query = (
            "MATCH (f:Face)-[:APPEARS_IN_SEGMENT]->(s:Segment)<-[:HAS_SEGMENT]-(v:Video {jobId: $jobId}) "
            "RETURN DISTINCT f.faceId AS faceId"
        )
        response = neptune_client.execute_query(
            graphIdentifier=neptune_graph_id,
            queryString=collect_query,
            parameters={"jobId": job_id},
            language="OPEN_CYPHER",
        )
        payload = json.loads(response["payload"].read().decode("utf-8"))
        orphan_face_ids = [r["faceId"] for r in payload.get("results", []) if r.get("faceId")]
    except Exception as e:
        logger.warning("Failed to collect face IDs for job=%s: %s", job_id, str(e))

    # 2. Delete graph nodes: Frame → Segment → Video
    graph_queries = [
        (
            "MATCH (v:Video {jobId: $jobId})-[:HAS_SEGMENT]->(s:Segment)-[:HAS_FRAME]->(f:Frame) "
            "DETACH DELETE f"
        ),
        (
            "MATCH (v:Video {jobId: $jobId})-[:HAS_SEGMENT]->(s:Segment) "
            "DETACH DELETE s"
        ),
        "MATCH (v:Video {jobId: $jobId}) DETACH DELETE v",
    ]

    for query in graph_queries:
        neptune_client.execute_query(
            graphIdentifier=neptune_graph_id,
            queryString=query,
            parameters={"jobId": job_id},
            language="OPEN_CYPHER",
        )
    logger.info("Neptune graph nodes deleted for job=%s", job_id)

    # 3. Delete Face nodes that are now orphaned (no remaining edges)
    rek_client = boto3.client("rekognition") if face_collection_id else None
    for face_id in orphan_face_ids:
        try:
            check_query = (
                "MATCH (f:Face {faceId: $faceId})-[r]-() "
                "RETURN count(r) AS edgeCount"
            )
            response = neptune_client.execute_query(
                graphIdentifier=neptune_graph_id,
                queryString=check_query,
                parameters={"faceId": face_id},
                language="OPEN_CYPHER",
            )
            payload = json.loads(response["payload"].read().decode("utf-8"))
            edge_count = payload.get("results", [{}])[0].get("edgeCount", 1)

            if edge_count == 0:
                # Delete orphaned Face node from Neptune
                neptune_client.execute_query(
                    graphIdentifier=neptune_graph_id,
                    queryString="MATCH (f:Face {faceId: $faceId}) DELETE f",
                    parameters={"faceId": face_id},
                    language="OPEN_CYPHER",
                )
                logger.info("Deleted orphaned Face node: %s", face_id)

                # Delete from Rekognition collection (non-celebrity faces only)
                if rek_client and not face_id.startswith("celeb:"):
                    try:
                        rek_client.delete_faces(
                            CollectionId=face_collection_id,
                            FaceIds=[face_id],
                        )
                        logger.info("Deleted face from Rekognition collection: %s", face_id)
                    except Exception as e:
                        logger.warning("Failed to delete face %s from Rekognition: %s", face_id, str(e))

        except Exception as e:
            logger.warning("Failed to clean up face %s: %s", face_id, str(e))


def set_deleting_status(dynamodb_table, job_id):
    """Set job status to 'Deleting' so the UI reflects progress even if API Gateway times out."""
    try:
        table = dynamodb_resource.Table(dynamodb_table)
        table.update_item(
            Key={"JobId": job_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "Status"},
            ExpressionAttributeValues={":s": "Deleting"},
        )
        logger.info("Set status to 'Deleting' for JobId=%s", job_id)
    except Exception as e:
        logger.warning("Failed to set Deleting status for job=%s: %s", job_id, str(e))


def get_video_name(dynamodb_table, job_id):
    """Look up the video file name from DynamoDB."""
    try:
        table = dynamodb_resource.Table(dynamodb_table)
        response = table.get_item(Key={"JobId": job_id})
        return response.get("Item", {}).get("Input", "")
    except Exception as e:
        logger.warning("Failed to look up video name for job=%s: %s", job_id, str(e))
        return ""


def delete_dynamodb_item(dynamodb_table, job_id):
    """Delete the DynamoDB item for the given job."""
    table = dynamodb_resource.Table(dynamodb_table)
    table.delete_item(Key={"JobId": job_id})
    logger.info("DynamoDB item deleted for JobId=%s", job_id)


def delete_s3_prefix(bucket, job_id):
    """Delete all S3 objects under the {job_id}/ prefix."""
    prefix = f"{job_id}/"
    deleted_count = 0

    paginator = s3_client.get_paginator("list_objects_v2")
    page_iterator = paginator.paginate(Bucket=bucket, Prefix=prefix)

    for page in page_iterator:
        contents = page.get("Contents", [])
        if not contents:
            continue

        # S3 delete_objects accepts up to 1000 keys per call
        objects_to_delete = [{"Key": obj["Key"]} for obj in contents]
        response = s3_client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": objects_to_delete, "Quiet": True},
        )

        errors = response.get("Errors", [])
        if errors:
            logger.warning(
                "S3 delete errors in bucket %s: %s",
                bucket,
                json.dumps(errors),
            )

        deleted_count += len(objects_to_delete) - len(errors)

    logger.info(
        "Deleted %d objects from s3://%s/%s",
        deleted_count,
        bucket,
        prefix,
    )
