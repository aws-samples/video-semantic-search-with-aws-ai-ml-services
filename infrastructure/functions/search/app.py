import json
import logging
import re
import os
import subprocess
import base64
import glob
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_VISUAL_RESULTS = 100
DEFAULT_AUDIO_RESULTS = 50
DEFAULT_IMAGE_RESULTS = 100
MAX_RERANK_RESULTS = 100
OPENSEARCH_RELEVANCE_THRESHOLD = 0.0
RERANK_RELEVANCE_THRESHOLD = 0.0
MAX_CLIPSEARCH_RELEVANCE_THRESHOLD = 0.75
PRESIGNED_URL_EXPIRY = 3600

OPENSEARCH_SOURCE_FIELDS = [
    "jobId",
    "shot_id",
    "shot_description",
]

bedrock_config = Config(
    read_timeout=300,
    retries={"max_attempts": 5, "mode": "adaptive"},
)
bedrock_client = boto3.client(service_name="bedrock-runtime", config=bedrock_config)
s3_client = boto3.client("s3")


def generate_presigned_url(bucket, key):
    try:
        return s3_client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=PRESIGNED_URL_EXPIRY
        )
    except ClientError:
        return ""

def lambda_handler(event, context):
    """Route incoming API Gateway requests to the appropriate search mode."""
    region = os.environ["region"]
    aoss_visual_index = os.environ["aoss_visual_index"]
    aoss_host = os.environ["aoss_host"]

    client = _get_opensearch_client(aoss_host, region)

    http_method = event.get("requestContext", {}).get("http", {}).get("method", "GET")

    if http_method == "GET":
        query_type = event["queryStringParameters"]["type"]
        user_query = event["queryStringParameters"]["query"]
        strategy = event["queryStringParameters"].get("strategy", "default")

        if query_type == "text":
            if strategy == "agentic":
                response = search_by_text_agentic(user_query)
            else:
                response = search_by_text(aoss_visual_index, client, user_query)
        else:
            response = search_by_clip(aoss_visual_index, client, user_query)
    else:
        # POST -- image search
        request_data = json.loads(event["body"])
        user_query = request_data["query"]
        img_format = "jpeg"
        if user_query.startswith("data:image"):
            # Extract format from data URL: data:image/png;base64,...
            mime_part = user_query.split(";")[0]  # data:image/png
            if "png" in mime_part:
                img_format = "png"
            elif "webp" in mime_part:
                img_format = "webp"
            elif "gif" in mime_part:
                img_format = "gif"
            user_query = user_query.split(",")[1]
        response = search_by_image(aoss_visual_index, client, user_query, img_format)

    bucket_images = os.environ.get("bucket_images", "")
    if bucket_images and isinstance(response, list):
        for item in response:
            ck = item.get("composite_key", "")
            item["composite_url"] = generate_presigned_url(bucket_images, ck) if ck else ""

    return {"statusCode": 200, "body": json.dumps(response)}


# ===================================================================
# Text search
# ===================================================================

def search_by_text(aoss_visual_index, client, user_query):
    """Fast text search: hybrid OpenSearch + entity detection + audio + Cohere rerank."""
    region = os.environ["region"]
    neptune_graph_id = os.environ["neptune_graph_id"]
    embedding_model = os.environ["embedding_model"]
    neptune_client = _get_neptune_client(region)

    # 1. Generate embeddings (text retrieval for description, video retrieval for video)
    query_embedding = _generate_text_embedding(embedding_model, user_query)
    video_query_embedding = _generate_video_query_embedding(embedding_model, user_query)

    # 2. Hybrid OpenSearch search (visual index)
    aoss_query = {
        "size": DEFAULT_VISUAL_RESULTS,
        "_source": OPENSEARCH_SOURCE_FIELDS,
        "query": {
            "hybrid": {
                "queries": [
                    {"match": {"shot_description": user_query}},
                    {"knn": {"shot_desc_vector": {"vector": query_embedding, "k": DEFAULT_VISUAL_RESULTS}}},
                    {"knn": {"shot_video_vector": {"vector": video_query_embedding, "k": DEFAULT_VISUAL_RESULTS}}},
                ],
            }
        },
    }
    response = client.search(
        body=aoss_query, index=aoss_visual_index,
        params={"search_pipeline": "vss-hybrid-search-pipeline"},
    )
    unranked_results = []
    for hit in response.get("hits", {}).get("hits", []):
        if hit["_score"] >= OPENSEARCH_RELEVANCE_THRESHOLD:
            src = hit["_source"]
            unranked_results.append({
                "jobId": src.get("jobId", ""),
                "shot_id": src.get("shot_id", ""),
                "shot_description": src.get("shot_description", ""),
            })

    # 3. Entity detection — find segments for people mentioned by name
    unranked_results = _simple_entity_expansion(
        user_query, unranked_results, neptune_client, neptune_graph_id
    )

    # 4. Audio index search (always)
    aoss_audio_index = os.environ.get("aoss_audio_index", "")
    if aoss_audio_index:
        transcript_embedding = query_embedding
        audio_hits = _search_audio_index(client, aoss_audio_index, transcript_embedding)
        audio_shots = _map_audio_hits_to_shots(audio_hits, neptune_client, neptune_graph_id)
        existing_ids = {(r["jobId"], r["shot_id"]) for r in unranked_results}
        for shot in audio_shots:
            if (shot["jobId"], shot["shot_id"]) not in existing_ids:
                unranked_results.append(shot)
                existing_ids.add((shot["jobId"], shot["shot_id"]))

    if not unranked_results:
        return []

    # 5. Neptune enrichment
    enriched_results = _enrich_results_from_neptune(unranked_results, neptune_graph_id, region)

    # 6. Cohere Rerank
    ranked_results = _rerank(user_query, enriched_results, MAX_RERANK_RESULTS)

    # 7. Filter + merge adjacent
    filtered = [r for r in ranked_results if r.get("score", 0) > 0]
    return _merge_adjacent_results(filtered)


# ===================================================================
# Agentic text search (via AgentCore Runtime)
# ===================================================================

def search_by_text_agentic(user_query):
    """Invoke the AgentCore video search agent for agentic RAG search."""
    agentcore_runtime_arn = os.environ.get("agentcore_runtime_arn", "")
    region = os.environ["region"]

    if not agentcore_runtime_arn:
        logger.warning("agentcore_runtime_arn not set, falling back to default search")
        aoss_visual_index = os.environ["aoss_visual_index"]
        aoss_host = os.environ["aoss_host"]
        client = _get_opensearch_client(aoss_host, region)
        return search_by_text(aoss_visual_index, client, user_query)

    agentcore_config = Config(read_timeout=900)
    agentcore_client = boto3.client("bedrock-agentcore", region_name=region, config=agentcore_config)

    try:
        t0 = time.time()
        response = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=agentcore_runtime_arn,
            qualifier="DEFAULT",
            payload=json.dumps({"prompt": user_query}),
            runtimeSessionId=str(uuid.uuid4()),
        )

        events = []
        for event in response.get("response", []):
            events.append(event)
        raw = b"".join(events).decode("utf-8")

        # AgentCore wraps the response as a JSON string — unwrap to get plain text
        try:
            agent_text = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            agent_text = raw.strip('"').replace('\\"', '"').replace('\\n', '\n')

        elapsed = time.time() - t0
        logger.info("AgentCore search completed in %.2fs, response length: %d", elapsed, len(agent_text))

        # Extract JSON from <JSON> tags in agent response
        json_match = re.search(r"<JSON>\s*(.*?)\s*</JSON>", agent_text, re.DOTALL)
        if not json_match:
            logger.warning("No <JSON> block found in agent response")
            return []

        agent_results = json.loads(json_match.group(1))
        raw_results = agent_results.get("results", [])

        # Flatten grouped format: [{jobId, shots: [{shot_id, score}]}] → [{jobId, shot_id, score}]
        results_list = []
        for group in raw_results:
            job_id = group.get("jobId", "")
            for shot in group.get("shots", []):
                results_list.append({
                    "jobId": job_id,
                    "shot_id": shot.get("shot_id", ""),
                    "score": shot.get("score", 0),
                })

        # Enrich via Neptune
        region = os.environ["region"]
        neptune_graph_id = os.environ["neptune_graph_id"]
        enriched = _enrich_results_from_neptune(results_list, neptune_graph_id, region)
        return _merge_adjacent_results(enriched)

    except Exception:
        logger.error("Agentic search failed, falling back to default search", exc_info=True)
        aoss_visual_index = os.environ["aoss_visual_index"]
        aoss_host = os.environ["aoss_host"]
        client = _get_opensearch_client(aoss_host, region)
        return search_by_text(aoss_visual_index, client, user_query)


# ===================================================================
# Image search
# ===================================================================

def search_by_image(aoss_visual_index, client, base64_image, img_format="jpeg"):
    """Search by image: generate image embedding, kNN search, enrich."""
    region = os.environ["region"]
    neptune_graph_id = os.environ["neptune_graph_id"]
    embedding_model = os.environ["embedding_model"]

    image_embedding = _generate_image_embedding(embedding_model, base64_image, img_format)

    aoss_query = {
        "size": DEFAULT_IMAGE_RESULTS,
        "query": {
            "knn": {
                "shot_image_vector": {
                    "vector": image_embedding,
                    "k": DEFAULT_IMAGE_RESULTS,
                }
            }
        },
        "_source": OPENSEARCH_SOURCE_FIELDS,
    }

    try:
        response = client.search(body=aoss_query, index=aoss_visual_index)
    except Exception as e:
        logger.error("OpenSearch image search failed: %s", str(e), exc_info=True)
        raise

    hits = response.get("hits", {}).get("hits", [])
    results = []
    for hit in hits:
        src = hit["_source"]
        results.append(
            {
                "jobId": src.get("jobId", ""),
                "shot_id": src.get("shot_id", ""),
                "shot_description": src.get("shot_description", ""),
                "score": hit["_score"],
            }
        )

    # Normalize kNN scores to 0–1 range
    if results:
        max_score = max(r["score"] for r in results)
        if max_score > 0:
            for r in results:
                r["score"] = round(r["score"] / max_score, 4)

    # Neptune enrichment adds video_name, timestamps, faces, adjacency
    enriched_results = _enrich_results_from_neptune(
        results, neptune_graph_id, region
    )

    return enriched_results


# ===================================================================
# Clip search
# ===================================================================

def search_by_clip(aoss_visual_index, client, user_query):
    """Search by video clip: extract frames, image-search per frame, aggregate."""
    region = os.environ["region"]
    neptune_graph_id = os.environ["neptune_graph_id"]

    tmp_clip_dir = os.environ["tmp_dir"] + "/clip/"
    tmp_frames_dir = os.environ["tmp_dir"] + "/" + user_query + "/"
    os.makedirs(tmp_clip_dir, exist_ok=True)
    os.makedirs(tmp_frames_dir, exist_ok=True)
    ffmpeg_path = "/opt/bin/ffmpeg"
    local_clip_path = os.path.join(tmp_clip_dir, user_query)

    s3_client.download_file(
        os.environ["bucket_clip_search"], user_query, local_clip_path
    )

    output_pattern = f"{tmp_frames_dir}%03d.png"
    try:
        subprocess.run(
            [
                ffmpeg_path,
                "-i",
                local_clip_path,
                "-vf",
                "fps=1,select='lte(n,10)'",  # 1 FPS, up to 10 frames
                "-vsync",
                "0",
                "-q:v",
                "1",
                output_pattern,
            ],
            stderr=subprocess.PIPE,
            check=False,
        )

        extracted_frames = glob.glob(f"{tmp_frames_dir}*.png")
        num_frames = len(extracted_frames)
        if num_frames == 0:
            logger.warning("No frames extracted from clip: %s", user_query)
            return []

        all_frame_search_res = []
        with ThreadPoolExecutor(max_workers=num_frames) as executor:
            futures = {
                executor.submit(_search_single_frame, aoss_visual_index, client, fp): fp
                for fp in extracted_frames
            }
            for future in as_completed(futures):
                try:
                    all_frame_search_res.append(future.result())
                except Exception as e:
                    logger.error(
                        "Frame search failed for %s: %s",
                        futures[future],
                        str(e),
                    )

        # Aggregate results across frames by jobId
        aggregated_results = {}
        for index, frame_search_res in enumerate(all_frame_search_res):
            processed_jobs = set()
            for item in frame_search_res:
                job_id = item["jobId"]
                if job_id not in processed_jobs:
                    processed_jobs.add(job_id)

                    if job_id not in aggregated_results:
                        aggregated_results[job_id] = {
                            "scores": [0] * num_frames,
                            "data": item,
                        }
                    aggregated_results[job_id]["scores"][index] = item.get(
                        "score", 0
                    )

        # Calculate score averages and find the best result
        response = []
        for job_id, result in aggregated_results.items():
            result["average_score"] = sum(result["scores"]) / num_frames

        if aggregated_results:
            best_result = max(
                aggregated_results.values(), key=lambda x: x["average_score"]
            )
            best_result["data"]["average_score"] = best_result["average_score"]
            best_result["data"]["occurrence_count"] = sum(
                score > 0 for score in best_result["scores"]
            )
            if best_result["data"]["average_score"] >= MAX_CLIPSEARCH_RELEVANCE_THRESHOLD:
                clip_result = {
                    "jobId": best_result["data"].get("jobId", ""),
                    "shot_id": best_result["data"].get("shot_id", ""),
                    "shot_description": best_result["data"].get("shot_description", ""),
                    "score": best_result["data"]["average_score"],
                }

                # Neptune enrichment adds video_name, timestamps, faces
                enriched = _enrich_results_from_neptune(
                    [clip_result], neptune_graph_id, region
                )
                response = enriched

        return response

    finally:
        # Clean up temporary files
        for frame_path in glob.glob(f"{tmp_frames_dir}*.png"):
            try:
                os.remove(frame_path)
            except OSError:
                pass
        if os.path.exists(local_clip_path):
            try:
                os.remove(local_clip_path)
            except OSError:
                pass

def _search_single_frame(aoss_visual_index, client, frame_path):
    """Encode a frame and run lightweight image search."""
    base64_image = base64.b64encode(open(frame_path, "rb").read()).decode()
    return _search_by_image_for_clip(aoss_visual_index, client, base64_image, "png")

def _search_by_image_for_clip(aoss_visual_index, client, base64_image, img_format="jpeg"):
    """Lightweight image search: embedding + kNN only for clip search."""
    embedding_model = os.environ["embedding_model"]
    image_embedding = _generate_image_embedding(embedding_model, base64_image, img_format)

    aoss_query = {
        "size": DEFAULT_IMAGE_RESULTS,
        "query": {"knn": {"shot_image_vector": {"vector": image_embedding, "k": DEFAULT_IMAGE_RESULTS}}},
        "_source": OPENSEARCH_SOURCE_FIELDS,
    }

    response = client.search(body=aoss_query, index=aoss_visual_index)
    hits = response.get("hits", {}).get("hits", [])
    results = [
        {
            "jobId": hit["_source"].get("jobId", ""),
            "shot_id": hit["_source"].get("shot_id", ""),
            "shot_description": hit["_source"].get("shot_description", ""),
            "score": hit["_score"],
        }
        for hit in hits
    ]

    # Normalize kNN scores to 0–1 range
    if results:
        max_score = max(r["score"] for r in results)
        if max_score > 0:
            for r in results:
                r["score"] = round(r["score"] / max_score, 4)

    return results

# ===================================================================
# Embedding generation helpers
# ===================================================================

def _generate_text_embedding(model_id, text):
    """Generate a text embedding using Nova v2 multimodal embeddings with TEXT_RETRIEVAL purpose."""
    if not text:
        text = " "

    body = json.dumps(
        {
            "taskType": "SINGLE_EMBEDDING",
            "singleEmbeddingParams": {
                "embeddingPurpose": "TEXT_RETRIEVAL",
                "embeddingDimension": 3072,
                "text": {
                    "truncationMode": "END",
                    "value": text,
                },
            },
        }
    )

    try:
        response = bedrock_client.invoke_model(
            body=body,
            modelId=model_id,
            accept="application/json",
            contentType="application/json",
        )
        response_body = json.loads(response["body"].read())
        return response_body["embeddings"][0]["embedding"]
    except ClientError as e:
        logger.error("Failed to generate text embedding: %s", str(e), exc_info=True)
        raise


def _generate_video_query_embedding(model_id, text):
    """Generate a text query embedding with VIDEO_RETRIEVAL purpose for searching video vectors."""
    if not text:
        text = " "

    body = json.dumps(
        {
            "taskType": "SINGLE_EMBEDDING",
            "singleEmbeddingParams": {
                "embeddingPurpose": "VIDEO_RETRIEVAL",
                "embeddingDimension": 3072,
                "text": {
                    "truncationMode": "END",
                    "value": text,
                },
            },
        }
    )

    try:
        response = bedrock_client.invoke_model(
            body=body,
            modelId=model_id,
            accept="application/json",
            contentType="application/json",
        )
        response_body = json.loads(response["body"].read())
        return response_body["embeddings"][0]["embedding"]
    except ClientError as e:
        logger.error("Failed to generate video query embedding: %s", str(e), exc_info=True)
        raise


def _generate_image_embedding(model_id, base64_image, img_format="jpeg"):
    """Generate an image embedding using Nova v2 multimodal embeddings with IMAGE_RETRIEVAL purpose."""
    body = json.dumps(
        {
            "taskType": "SINGLE_EMBEDDING",
            "singleEmbeddingParams": {
                "embeddingPurpose": "IMAGE_RETRIEVAL",
                "embeddingDimension": 3072,
                "image": {
                    "format": img_format,
                    "source": {"bytes": base64_image},
                },
            },
        }
    )

    try:
        response = bedrock_client.invoke_model(
            body=body,
            modelId=model_id,
            accept="application/json",
            contentType="application/json",
        )
        response_body = json.loads(response["body"].read())
        return response_body["embeddings"][0]["embedding"]
    except ClientError as e:
        logger.error("Failed to generate image embedding: %s", str(e), exc_info=True)
        raise


# ===================================================================
# Reranking
# ===================================================================

def _rerank(user_query, unranked_results, num_results):
    """Rerank results using Cohere Rerank 3.5 via bedrock-agent-runtime."""
    if not unranked_results:
        return []

    region = os.environ["region"]
    rerank_model_id = os.environ["rerank_model"]
    model_package_arn = (
        f"arn:aws:bedrock:{region}::foundation-model/{rerank_model_id}"
    )

    bedrock_agent_runtime = boto3.client(
        "bedrock-agent-runtime", region_name=region
    )

    sources = []
    for result in unranked_results:
        face_labels = ", ".join(
            f["label"] for f in result.get("faces", []) if f.get("label")
        )
        doc = {
            "shot_description": result.get("shot_description", ""),
            "shot_transcript": result.get("shot_transcript", ""),
            "faces": face_labels,
        }
        sources.append(
            {
                "inlineDocumentSource": {
                    "jsonDocument": doc,
                    "type": "JSON",
                },
                "type": "INLINE",
            }
        )

    try:
        response = bedrock_agent_runtime.rerank(
            queries=[{"type": "TEXT", "textQuery": {"text": user_query}}],
            sources=sources,
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "numberOfResults": min(num_results, len(sources)),
                    "modelConfiguration": {
                        "modelArn": model_package_arn,
                    },
                },
            },
        )
    except ClientError as e:
        logger.error("Reranking failed: %s", str(e), exc_info=True)
        # Fall back to unreranked results with OpenSearch scores
        for i, result in enumerate(unranked_results):
            result["score"] = 1.0 - (i * 0.01)  # Descending placeholder scores
        return unranked_results

    ranked_results = []
    for rerank_result in response.get("results", []):
        relevance_score = rerank_result.get("relevanceScore", 0.0)
        if relevance_score >= RERANK_RELEVANCE_THRESHOLD:
            idx = rerank_result["index"]
            unranked_results[idx]["score"] = relevance_score
            ranked_results.append(unranked_results[idx])

    return ranked_results


# ===================================================================
# OpenSearch client
# ===================================================================

def _get_opensearch_client(host, region):
    """Create an OpenSearch client with AWS SigV4 auth for serverless."""
    host = host.split("://")[1] if "://" in host else host
    credentials = boto3.Session().get_credentials()
    auth = AWSV4SignerAuth(credentials, region, "aoss")

    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        pool_maxsize=20,
    )


# ===================================================================
# Neptune graph helpers
# ===================================================================

def _get_neptune_client(region):
    """Create a Neptune Analytics client."""
    return boto3.client("neptune-graph", region_name=region)


def _execute_neptune_query(neptune_client, graph_id, query, parameters=None):
    """Execute an openCypher query against Neptune Analytics and return results."""
    kwargs = {
        "graphIdentifier": graph_id,
        "language": "OPEN_CYPHER",
        "queryString": query,
    }
    if parameters:
        kwargs["parameters"] = parameters

    try:
        response = neptune_client.execute_query(**kwargs)
        payload = json.loads(response["payload"].read().decode("utf-8"))
        return payload.get("results", [])
    except ClientError as e:
        logger.error("Neptune query failed: %s | Query: %s", str(e), query)
        raise

# -------------------------------------------------------------------
# Entity detection helpers
# -------------------------------------------------------------------

def _fetch_all_face_labels(neptune_client, graph_id):
    """Fetch all labeled (non-unknown) faces from Neptune."""
    try:
        results = _execute_neptune_query(
            neptune_client,
            graph_id,
            "MATCH (f:Face) WHERE NOT f.label STARTS WITH 'Unknown-' "
            "RETURN f.faceId AS faceId, f.label AS label, f.isCelebrity AS isCelebrity",
        )
        return results
    except Exception:
        logger.warning("Failed to fetch face labels from Neptune", exc_info=True)
        return []


def _simple_entity_expansion(user_query, unranked_results, neptune_client, graph_id):
    """Find segments for people mentioned by name in the query."""
    face_labels = _fetch_all_face_labels(neptune_client, graph_id)
    if not face_labels:
        return unranked_results

    query_lower = user_query.lower()
    matched_faces = [f for f in face_labels if f.get("label", "").lower() in query_lower]
    if not matched_faces:
        return unranked_results

    existing_ids = {(r["jobId"], r["shot_id"]) for r in unranked_results}

    for face in matched_faces:
        face_id = face.get("faceId")
        label = face.get("label", "")
        if face_id:
            query = (
                "MATCH (f:Face {faceId: $faceId})-[:APPEARS_IN_SEGMENT]->(s:Segment)"
                "<-[:HAS_SEGMENT]-(v:Video) "
                "RETURN s.segmentId AS segmentId, v.jobId AS jobId, s.description AS description"
            )
            rows = _execute_neptune_query(neptune_client, graph_id, query, parameters={"faceId": face_id})
        else:
            # Celebrity faces may not have faceId
            query = (
                "MATCH (f:Face {label: $label})-[:APPEARS_IN_SEGMENT]->(s:Segment)"
                "<-[:HAS_SEGMENT]-(v:Video) "
                "RETURN s.segmentId AS segmentId, v.jobId AS jobId, s.description AS description"
            )
            rows = _execute_neptune_query(neptune_client, graph_id, query, parameters={"label": label})

        for row in rows:
            seg_id = row.get("segmentId", "")
            job_id = row.get("jobId", "")
            shot_id = seg_id[len(job_id) + 1:] if seg_id and job_id and seg_id.startswith(job_id + "_") else ""
            if (job_id, shot_id) not in existing_ids:
                unranked_results.append({
                    "jobId": job_id,
                    "shot_id": shot_id,
                    "shot_description": row.get("description", ""),
                })
                existing_ids.add((job_id, shot_id))

    return unranked_results


# -------------------------------------------------------------------
# Audio index search and shot mapping
# -------------------------------------------------------------------

def _search_audio_index(client, audio_index, transcript_embedding):
    """Search the audio/transcript index using kNN on transcript vectors."""
    query = {
        "size": DEFAULT_AUDIO_RESULTS,
        "query": {
            "knn": {
                "transcript_vector": {
                    "vector": transcript_embedding,
                    "k": DEFAULT_AUDIO_RESULTS,
                }
            }
        },
        "_source": ["jobId", "transcript_startTime", "transcript_endTime", "transcript"],
    }
    try:
        response = client.search(body=query, index=audio_index)
        hits = response.get("hits", {}).get("hits", [])
        logger.info("Audio index search returned %d hits", len(hits))
        return hits
    except Exception:
        logger.warning("Audio index search failed", exc_info=True)
        return []


def _map_audio_hits_to_shots(audio_hits, neptune_client, graph_id):
    """Map audio index hits (per-sentence with timestamps) to shot-level results via Neptune time overlap.

    Returns a list of dicts with jobId, shot_id, shot_description (empty — filled later by enrichment).
    """
    if not audio_hits:
        return []

    # Group transcript hits by jobId
    job_time_ranges = {}
    for hit in audio_hits:
        src = hit["_source"]
        job_id = src.get("jobId", "")
        if not job_id:
            continue
        start = src.get("transcript_startTime", 0)
        end = src.get("transcript_endTime", 0)
        job_time_ranges.setdefault(job_id, []).append((start, end))

    shot_results = []
    seen_segments = set()

    for job_id, time_ranges in job_time_ranges.items():
        try:
            segments = _execute_neptune_query(
                neptune_client,
                graph_id,
                "MATCH (v:Video {jobId: $jobId})-[:HAS_SEGMENT]->(s:Segment) "
                "RETURN s.segmentId AS segmentId, s.startTime AS startTime, s.endTime AS endTime",
                parameters={"jobId": job_id},
            )

            for seg in segments:
                seg_id = seg.get("segmentId", "")
                if seg_id in seen_segments:
                    continue
                seg_start = seg.get("startTime", 0)
                seg_end = seg.get("endTime", 0)

                # Check if any transcript time range overlaps this segment
                for t_start, t_end in time_ranges:
                    if seg_start <= t_end and seg_end >= t_start:
                        shot_id = ""
                        if seg_id.startswith(job_id + "_"):
                            shot_id = seg_id[len(job_id) + 1:]

                        shot_results.append({
                            "jobId": job_id,
                            "shot_id": shot_id,
                            "shot_description": "",  # Filled by Neptune enrichment
                        })
                        seen_segments.add(seg_id)
                        break
        except Exception:
            logger.warning("Failed to map audio hits to shots for jobId=%s", job_id)
            continue

    logger.info("Audio-to-shot mapping produced %d shots", len(shot_results))
    return shot_results


# -------------------------------------------------------------------
# Post-retrieval: Neptune enrichment
# -------------------------------------------------------------------

def _enrich_results_from_neptune(results, neptune_graph_id, region):
    """Enrich search results with metadata from Neptune: video_name, timestamps, faces, adjacency.

    Uses 3 batched Neptune queries (metadata, faces, adjacency)
    """
    if not results:
        return results

    neptune_client = _get_neptune_client(region)

    # Build list of segment IDs to query in batch
    segment_ids = []
    for r in results:
        job_id = r.get("jobId", "")
        shot_id = r.get("shot_id", "")
        if job_id and shot_id:
            segment_ids.append(f"{job_id}_{shot_id}")
        else:
            segment_ids.append("")

    valid_segment_ids = [sid for sid in segment_ids if sid]

    # Batch query 1: segment metadata + video name
    metadata_map = {}
    if valid_segment_ids:
        try:
            metadata_results = _execute_neptune_query(
                neptune_client, neptune_graph_id,
                "MATCH (v:Video)-[:HAS_SEGMENT]->(s:Segment) "
                "WHERE s.segmentId IN $segmentIds "
                "RETURN s.segmentId AS segmentId, v.videoName AS videoName, "
                "s.startTime AS startTime, s.endTime AS endTime, "
                "s.description AS description, s.transcript AS transcript",
                parameters={"segmentIds": valid_segment_ids},
            )
            metadata_map = {row.get("segmentId", ""): row for row in metadata_results}
        except ClientError:
            logger.warning("Batch metadata query failed for %d segments", len(valid_segment_ids))

    # Batch query 2: faces for all segments
    face_map = {}
    if valid_segment_ids:
        try:
            face_results = _execute_neptune_query(
                neptune_client, neptune_graph_id,
                "MATCH (f:Face)-[:APPEARS_IN_SEGMENT]->(s:Segment) "
                "WHERE s.segmentId IN $segmentIds "
                "RETURN s.segmentId AS segmentId, f.label AS label, f.isCelebrity AS isCelebrity",
                parameters={"segmentIds": valid_segment_ids},
            )
            for fr in face_results:
                sid = fr.get("segmentId", "")
                label = fr.get("label", "Unknown")
                is_celebrity_raw = fr.get("isCelebrity", "false")
                if isinstance(is_celebrity_raw, str):
                    is_celebrity = is_celebrity_raw.lower() == "true"
                else:
                    is_celebrity = bool(is_celebrity_raw)
                face_map.setdefault(sid, []).append({"label": label, "isCelebrity": is_celebrity})
        except ClientError:
            logger.warning("Batch face query failed for %d segments", len(valid_segment_ids))

    # Batch query 3: adjacency for all segments
    adjacency_map = {}
    if valid_segment_ids:
        try:
            adjacency_results = _execute_neptune_query(
                neptune_client, neptune_graph_id,
                "MATCH (s:Segment) WHERE s.segmentId IN $segmentIds "
                "OPTIONAL MATCH (prev:Segment)-[:NEXT_SEGMENT]->(s) "
                "OPTIONAL MATCH (s)-[:NEXT_SEGMENT]->(nxt:Segment) "
                "RETURN s.segmentId AS segmentId, "
                "prev.segmentId AS prevSegmentId, nxt.segmentId AS nextSegmentId",
                parameters={"segmentIds": valid_segment_ids},
            )
            for ar in adjacency_results:
                sid = ar.get("segmentId", "")
                prev_id = ar.get("prevSegmentId") or ""
                next_id = ar.get("nextSegmentId") or ""
                # Extract shot_id from segmentId (format: "{jobId}_{shot_id}")
                prev_shot = prev_id.split("_", 1)[1] if prev_id and "_" in prev_id else None
                next_shot = next_id.split("_", 1)[1] if next_id and "_" in next_id else None
                adjacency_map[sid] = {"prev": prev_shot, "next": next_shot}
        except ClientError:
            logger.warning("Batch adjacency query failed for %d segments", len(valid_segment_ids))

    # Assemble enriched results from the maps
    enriched = []
    for i, result in enumerate(results):
        job_id = result.get("jobId", "")
        shot_id = result.get("shot_id", "")
        segment_id = segment_ids[i]

        meta = metadata_map.get(segment_id, {})
        video_name = meta.get("videoName", "")
        start_time = meta.get("startTime", 0)
        end_time = meta.get("endTime", 0)
        transcript = meta.get("transcript", "")

        faces = face_map.get(segment_id, [])
        adjacent_segments = adjacency_map.get(segment_id, {"prev": None, "next": None})

        # Split faces into public figures (celebrities) and other named faces, deduplicated
        public_figures = list(dict.fromkeys(f["label"] for f in faces if f.get("isCelebrity") and not f.get("label", "").startswith("Unknown")))
        other_faces = list(dict.fromkeys(f["label"] for f in faces if not f.get("isCelebrity") and not f.get("label", "").startswith("Unknown")))

        enriched_result = {
            "jobId": job_id,
            "video_name": video_name,
            "shot_id": shot_id,
            "shot_startTime": start_time,
            "shot_endTime": end_time,
            "shot_description": result.get("shot_description") or meta.get("description", ""),
            "shot_transcript": transcript,
            "composite_key": f"{job_id}/{shot_id}_composite.png" if job_id and shot_id else "",
            "clip_key": f"{job_id}/{shot_id}_clip.mp4" if job_id and shot_id else "",
            "score": result.get("score", 0),
            "faces": faces,
            "shot_publicFigures": ", ".join(public_figures),
            "shot_faces": ", ".join(other_faces),
            "adjacentSegments": adjacent_segments,
        }
        enriched.append(enriched_result)

    return enriched


def _merge_adjacent_results(results):
    """Merge adjacent segments from the same video into continuous time ranges.

    Groups enriched results by jobId, sorts by startTime, and merges consecutive
    shots that are within 1s of each other and have score >= 0.5.
    Uses the highest score from the group, first shot's composite/clip keys,
    and concatenates descriptions/transcripts.
    """
    if not results:
        return results

    # Group by jobId
    groups = {}
    for r in results:
        groups.setdefault(r.get("jobId", ""), []).append(r)

    merged = []
    for job_id, items in groups.items():
        items.sort(key=lambda x: x.get("shot_startTime", 0))

        i = 0
        while i < len(items):
            current = dict(items[i])
            current["faces"] = list(current.get("faces", []))
            j = i + 1

            while j < len(items):
                nxt = items[j]
                gap = nxt.get("shot_startTime", 0) - current.get("shot_endTime", 0)
                if gap <= 1500 and nxt.get("score", 0) >= 0.5:
                    current["shot_endTime"] = max(current["shot_endTime"], nxt.get("shot_endTime", 0))
                    current["score"] = max(current.get("score", 0), nxt.get("score", 0))
                    if nxt.get("shot_description"):
                        current["shot_description"] = current.get("shot_description", "") + "\n----------------------------------------\n" + nxt["shot_description"]
                    if nxt.get("shot_transcript"):
                        current["shot_transcript"] = current.get("shot_transcript", "") + "\n----------------------------------------\n" + nxt["shot_transcript"]
                    existing_labels = {f.get("label") for f in current.get("faces", [])}
                    for face in nxt.get("faces", []):
                        if face.get("label") not in existing_labels:
                            current["faces"].append(face)
                            existing_labels.add(face.get("label"))
                    j += 1
                else:
                    break

            # Rebuild face strings from the merged faces list, deduplicated
            current["shot_publicFigures"] = ", ".join(dict.fromkeys(
                f["label"] for f in current.get("faces", [])
                if f.get("isCelebrity") and not f.get("label", "").startswith("Unknown")
            ))
            current["shot_faces"] = ", ".join(dict.fromkeys(
                f["label"] for f in current.get("faces", [])
                if not f.get("isCelebrity") and not f.get("label", "").startswith("Unknown")
            ))
            merged.append(current)
            i = j

    merged.sort(key=lambda x: x.get("score", 0), reverse=True)
    return merged


