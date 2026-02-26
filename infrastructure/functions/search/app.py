import json
import logging
import re
import os
import subprocess
import base64
import glob
import time
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
MAX_OPENSEARCH_RESULTS = 100
OPENSEARCH_RELEVANCE_THRESHOLD = 0.0  # Hybrid search handles scoring
MAX_RERANK_RESULTS = 100
RERANK_RELEVANCE_THRESHOLD = 0.0
MAX_CLIPSEARCH_RELEVANCE_THRESHOLD = 0.75
PRESIGNED_URL_EXPIRY = 3600

OPENSEARCH_SOURCE_FIELDS = [
    "jobId",
    "shot_id",
    "shot_description",
]

# ---------------------------------------------------------------------------
# AWS clients (initialised at module level for Lambda container reuse)
# ---------------------------------------------------------------------------
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


# ===================================================================
# Lambda handler
# ===================================================================

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

        if query_type == "text":
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
    """Full text search pipeline: LLM analysis -> entity detection -> hybrid search -> enrich -> graph expand -> rerank."""
    region = os.environ["region"]
    neptune_graph_id = os.environ["neptune_graph_id"]
    embedding_model = os.environ["embedding_model"]
    query_llm_model = os.environ.get("query_llm_model", "")

    neptune_client = _get_neptune_client(region)

    # ------------------------------------------------------------------
    # 1. LLM query analysis (sequential — embeddings depend on result)
    # ------------------------------------------------------------------
    query_analysis = None

    if query_llm_model:
        try:
            face_labels = _fetch_all_face_labels(neptune_client, neptune_graph_id)
            query_analysis = _llm_query_analysis(query_llm_model, user_query, face_labels)
        except Exception:
            logger.warning("LLM analysis pipeline failed", exc_info=True)
            query_analysis = None

    # ------------------------------------------------------------------
    # 2. Parallel: generate embeddings
    #   - query_embedding: for visual index hybrid search (all 3 sub-queries)
    #   - transcript_embedding: for audio index search (only if speech intent)
    # ------------------------------------------------------------------
    visual_terms = ""
    transcript_terms = ""
    if query_analysis:
        visual_terms = query_analysis.get("visual_terms", "")
        transcript_terms = query_analysis.get("transcript_terms", "")

    futures = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        # Visual index embedding: visual_terms if available, full query as fallback
        futures["query"] = executor.submit(
            _generate_text_embedding, embedding_model,
            visual_terms or user_query,
        )
        # Audio index embedding: only if LLM detected speech/transcript intent
        if transcript_terms:
            futures["transcript"] = executor.submit(
                _generate_text_embedding, embedding_model, transcript_terms,
            )

        query_embedding = futures["query"].result()
        transcript_embedding = futures["transcript"].result() if "transcript" in futures else None

    # ------------------------------------------------------------------
    # 4. Build OpenSearch hybrid query (visual index)
    #    Pipeline weights [0.2, 0.5, 0.3] require exactly 3 sub-queries.
    # ------------------------------------------------------------------
    hybrid_queries = [
        {"match": {"shot_description": user_query}},
        {"knn": {"shot_desc_vector": {"vector": query_embedding, "k": 50}}},
        {"knn": {"shot_video_vector": {"vector": query_embedding, "k": 50}}},
    ]

    hybrid_body = {
        "query": {
            "hybrid": {
                "queries": hybrid_queries,
            }
        }
    }

    # Note: entity relevance is handled post-retrieval by graph expansion
    # (step 9) and LLM reranking (step 10), not by OpenSearch boosting.
    # OpenSearch Serverless does not support nesting hybrid inside bool.

    # ------------------------------------------------------------------
    # 5. Phrase filtering for quoted terms
    # ------------------------------------------------------------------
    phrase_pattern = r'"(.*?)"'
    phrase_matches = re.findall(phrase_pattern, user_query)
    if phrase_matches:
        phrase_must_clauses = []
        for phrase in phrase_matches:
            phrase_must_clauses.append(
                {
                    "match_phrase": {
                        "shot_description": phrase,
                    }
                }
            )

        # Wrap whatever we have in a bool with must for phrase filtering
        existing_query = hybrid_body["query"]
        hybrid_body["query"] = {
            "bool": {
                "must": [existing_query] + phrase_must_clauses,
            }
        }

    aoss_query = {
        "size": MAX_OPENSEARCH_RESULTS,
        "_source": OPENSEARCH_SOURCE_FIELDS,
    }
    aoss_query.update(hybrid_body)

    # Execute with hybrid search pipeline
    try:
        response = client.search(
            body=aoss_query,
            index=aoss_visual_index,
            params={"search_pipeline": "vss-hybrid-search-pipeline"},
        )
    except Exception as e:
        logger.error("OpenSearch hybrid search failed: %s", str(e), exc_info=True)
        raise

    hits = response.get("hits", {}).get("hits", [])

    # ------------------------------------------------------------------
    # 6. Parse results
    # ------------------------------------------------------------------
    unranked_results = []
    for hit in hits:
        if hit["_score"] >= OPENSEARCH_RELEVANCE_THRESHOLD:
            src = hit["_source"]
            unranked_results.append(
                {
                    "jobId": src.get("jobId", ""),
                    "shot_id": src.get("shot_id", ""),
                    "shot_description": src.get("shot_description", ""),
                }
            )

    # ------------------------------------------------------------------
    # 7. Audio index search + merge
    #    Search transcripts for spoken content, map to shots, merge
    # ------------------------------------------------------------------
    if transcript_embedding:
        aoss_audio_index = os.environ.get("aoss_audio_index", "")
        if aoss_audio_index:
            audio_hits = _search_audio_index(client, aoss_audio_index, transcript_embedding)
            audio_shots = _map_audio_hits_to_shots(audio_hits, neptune_client, neptune_graph_id)

            # Merge: add audio-sourced shots not already in visual results
            existing_ids = {(r["jobId"], r["shot_id"]) for r in unranked_results}
            for shot in audio_shots:
                key = (shot["jobId"], shot["shot_id"])
                if key not in existing_ids:
                    unranked_results.append(shot)
                    existing_ids.add(key)

    if not unranked_results:
        return []

    # ------------------------------------------------------------------
    # 8. Neptune graph enrichment (adds transcript, faces, metadata, description)
    # ------------------------------------------------------------------
    enriched_results = _enrich_results_from_neptune(
        unranked_results, neptune_graph_id, region
    )

    # ------------------------------------------------------------------
    # 9. Graph expansion — add missed segments via face co-occurrence
    # ------------------------------------------------------------------
    if query_llm_model and query_analysis:
        enriched_results = _graph_expansion(
            query_analysis, enriched_results, neptune_client, neptune_graph_id
        )

    # ------------------------------------------------------------------
    # 10. Rerank results
    # ------------------------------------------------------------------
    if query_llm_model:
        ranked_results = _llm_rerank(
            query_llm_model, user_query, query_analysis, enriched_results
        )
    else:
        ranked_results = _rerank(user_query, enriched_results, MAX_RERANK_RESULTS)

    # Filter out results with zero relevance score
    return [r for r in ranked_results if r.get("score", 0) > 0]


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
        "size": MAX_OPENSEARCH_RESULTS,
        "query": {
            "knn": {
                "shot_image_vector": {
                    "vector": image_embedding,
                    "k": 50,
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
            future_to_frame = {}
            for frame_path in extracted_frames:
                future = executor.submit(
                    lambda p: base64.b64encode(open(p, "rb").read()).decode(),
                    frame_path,
                )
                future_to_frame[future] = frame_path

            for future in as_completed(future_to_frame):
                try:
                    base64_image = future.result()
                    frame_search_res = search_by_image(
                        aoss_visual_index, client, base64_image, "png"
                    )
                    all_frame_search_res.append(frame_search_res)
                except Exception as e:
                    logger.error(
                        "Frame search failed for %s: %s",
                        future_to_frame[future],
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
# Pre-retrieval: entity detection
# -------------------------------------------------------------------

def _detect_entity_segments(neptune_graph_id, region, user_query):
    """Check if the query contains known face/celebrity names and return matching segment IDs.

    Returns a list of (segmentId, videoId) tuples, or an empty list if no entity found.
    """
    neptune_client = _get_neptune_client(region)

    # Tokenise the query into candidate terms.  We try progressively shorter
    # n-grams starting from the full query down to single words, so that
    # multi-word names (e.g., "Barack Obama") are matched first.
    query_words = user_query.strip().split()
    candidate_terms = []
    for n in range(len(query_words), 0, -1):
        for i in range(len(query_words) - n + 1):
            candidate_terms.append(" ".join(query_words[i : i + n]))

    matched_face_id = None
    matched_label = None

    for term in candidate_terms:
        try:
            face_results = _execute_neptune_query(
                neptune_client,
                neptune_graph_id,
                "MATCH (f:Face) WHERE toLower(f.label) CONTAINS toLower($queryTerm) "
                "RETURN f.label AS label, f.faceId AS faceId",
                parameters={"queryTerm": term},
            )
        except ClientError:
            logger.warning("Entity detection query failed for term: %s", term)
            continue

        if face_results:
            matched_label = face_results[0].get("label")
            matched_face_id = face_results[0].get("faceId")
            logger.info(
                "Entity detected: label=%s, faceId=%s", matched_label, matched_face_id
            )
            break

    if not matched_face_id and not matched_label:
        return []

    # Query for segments where this face appears
    try:
        if matched_face_id:
            segment_results = _execute_neptune_query(
                neptune_client,
                neptune_graph_id,
                "MATCH (f:Face {faceId: $faceId})-[:APPEARS_IN_SEGMENT]->(s:Segment)"
                "<-[:HAS_SEGMENT]-(v:Video) "
                "RETURN s.segmentId AS segmentId, v.jobId AS jobId",
                parameters={"faceId": matched_face_id},
            )
        else:
            # Celebrity faces may not have a faceId; match by label instead
            segment_results = _execute_neptune_query(
                neptune_client,
                neptune_graph_id,
                "MATCH (f:Face {label: $label})-[:APPEARS_IN_SEGMENT]->(s:Segment)"
                "<-[:HAS_SEGMENT]-(v:Video) "
                "RETURN s.segmentId AS segmentId, v.jobId AS jobId",
                parameters={"label": matched_label},
            )
    except ClientError:
        logger.warning("Segment lookup failed for entity: %s", matched_label)
        return []

    entity_segments = []
    for row in segment_results:
        segment_id = row.get("segmentId")
        video_id = row.get("jobId")
        if segment_id:
            entity_segments.append((segment_id, video_id))

    logger.info("Entity segments found: %d", len(entity_segments))
    return entity_segments


# -------------------------------------------------------------------
# LLM-powered query analysis (pre-retrieval)
# -------------------------------------------------------------------

def _fetch_all_face_labels(neptune_client, graph_id):
    """Fetch all labeled (non-unknown) faces from Neptune for LLM context."""
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


def _llm_query_analysis(query_llm_model, user_query, face_labels):
    """Use Claude Haiku to analyze a search query: resolve entities and extract visual terms.

    Returns a dict with keys: entities, visual_terms, query_type
    or None on any failure.
    """
    if not query_llm_model or not face_labels:
        return None

    labels_text = "\n".join(
        f"- faceId={fl.get('faceId','')}, label={fl.get('label','')}, isCelebrity={fl.get('isCelebrity','false')}"
        for fl in face_labels
    )

    system_prompt = (
        "You are a search query analyzer for a video search system. "
        "Given a user query and a list of known faces in the video library, your job is to:\n"
        "1. Match entity references in the query (full names, partial names, nicknames, "
        "descriptions like 'the president', 'the host') against the known face labels.\n"
        "2. Extract visual_terms — what should be SEEN in the video "
        "(actions, objects, settings, visual scenes, body language).\n"
        "3. Extract transcript_terms — what should be HEARD or SPOKEN in the video "
        "(dialog, speech content, topics discussed, words said). "
        "Include synonyms and related phrases for better recall.\n"
        "4. Classify the query type.\n\n"
        "Known faces in the library:\n" + labels_text + "\n\n"
        "Respond with JSON only, no markdown fences, no explanation. Schema:\n"
        '{"entities": [{"faceId": "...", "label": "..."}], '
        '"visual_terms": "...", '
        '"transcript_terms": "...", '
        '"query_type": "entity_only|visual_only|entity_and_visual|intersection"}\n\n'
        "Examples:\n"
        '- "Obama say hello" → visual_terms: "person speaking to audience", '
        'transcript_terms: "hello, hi, greetings, good morning"\n'
        '- "Obama Biden handshake" → visual_terms: "two people shaking hands", '
        'transcript_terms: ""\n'
        '- "discussing healthcare policy" → visual_terms: "", '
        'transcript_terms: "healthcare, medical, health policy, insurance, patients"\n'
        '- "keynote about cloud computing" → visual_terms: "person presenting on stage", '
        'transcript_terms: "cloud computing, AWS, serverless, infrastructure"\n\n'
        "Rules:\n"
        "- 'intersection' means the user wants segments where ALL listed entities co-appear.\n"
        "- 'entity_and_visual' means the user wants an entity doing/in something specific.\n"
        "- visual_terms: empty string if nothing visual is implied.\n"
        "- transcript_terms: empty string if nothing about speech/audio content is implied.\n"
        "- Only match entities you are confident about. Do not guess."
    )

    try:
        t0 = time.time()
        response = bedrock_client.invoke_model(
            modelId=query_llm_model,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_query}],
            }),
        )
        body = json.loads(response["body"].read())
        text = body["content"][0]["text"]
        elapsed = time.time() - t0
        logger.info("LLM query analysis completed in %.2fs: %s", elapsed, text)
        return json.loads(text)
    except Exception:
        logger.warning("LLM query analysis failed", exc_info=True)
        return None


def _resolve_entity_segments_from_analysis(neptune_client, graph_id, query_analysis):
    """Convert LLM-matched entities to (segmentId, jobId) tuples for OpenSearch boosting."""
    if not query_analysis or not query_analysis.get("entities"):
        return []

    all_segments = []
    for entity in query_analysis["entities"]:
        face_id = entity.get("faceId")
        if not face_id:
            continue
        try:
            segment_results = _execute_neptune_query(
                neptune_client,
                graph_id,
                "MATCH (f:Face {faceId: $faceId})-[:APPEARS_IN_SEGMENT]->(s:Segment)"
                "<-[:HAS_SEGMENT]-(v:Video) "
                "RETURN s.segmentId AS segmentId, v.jobId AS jobId",
                parameters={"faceId": face_id},
            )
            for row in segment_results:
                seg_id = row.get("segmentId")
                job_id = row.get("jobId")
                if seg_id:
                    all_segments.append((seg_id, job_id))
        except Exception:
            logger.warning("Entity segment resolution failed for faceId=%s", face_id)
            continue

    logger.info("LLM entity resolution found %d segments", len(all_segments))
    return all_segments


# -------------------------------------------------------------------
# Post-retrieval: graph expansion
# -------------------------------------------------------------------

def _graph_expansion(query_analysis, enriched_results, neptune_client, graph_id):
    """Expand search results by finding segments the vector search missed based on graph structure.

    For intersection queries, finds segments where ALL entities co-appear.
    For single-entity queries, finds ALL segments for that entity.
    New segments are appended with score=0 so the reranker can score them.
    """
    if not query_analysis or not query_analysis.get("entities"):
        return enriched_results

    entities = query_analysis["entities"]
    query_type = query_analysis.get("query_type", "")

    existing_segment_ids = set()
    for r in enriched_results:
        job_id = r.get("jobId", "")
        shot_id = r.get("shot_id", "")
        if job_id and shot_id:
            existing_segment_ids.add(f"{job_id}_{shot_id}")

    try:
        new_segment_rows = []

        if query_type == "intersection" and len(entities) >= 2:
            # Build a multi-MATCH query for co-occurrence
            match_clauses = []
            params = {}
            for i, entity in enumerate(entities):
                face_id = entity.get("faceId")
                if not face_id:
                    continue
                param_name = f"faceId{i}"
                match_clauses.append(
                    f"MATCH (f{i}:Face {{faceId: ${param_name}}})-[:APPEARS_IN_SEGMENT]->(s)"
                )
                params[param_name] = face_id

            if len(match_clauses) >= 2:
                cypher = (
                    " ".join(match_clauses)
                    + " MATCH (v:Video)-[:HAS_SEGMENT]->(s)"
                    " RETURN s.segmentId AS segmentId, v.jobId AS jobId,"
                    " v.videoName AS videoName, s.startTime AS startTime,"
                    " s.endTime AS endTime, s.description AS description,"
                    " s.transcript AS transcript"
                )
                new_segment_rows = _execute_neptune_query(
                    neptune_client, graph_id, cypher, parameters=params
                )
        else:
            # Single entity (or entity_and_visual) — get all segments for first entity
            face_id = entities[0].get("faceId")
            if face_id:
                new_segment_rows = _execute_neptune_query(
                    neptune_client,
                    graph_id,
                    "MATCH (f:Face {faceId: $faceId})-[:APPEARS_IN_SEGMENT]->(s:Segment)"
                    "<-[:HAS_SEGMENT]-(v:Video) "
                    "RETURN s.segmentId AS segmentId, v.jobId AS jobId,"
                    " v.videoName AS videoName, s.startTime AS startTime,"
                    " s.endTime AS endTime, s.description AS description,"
                    " s.transcript AS transcript",
                    parameters={"faceId": face_id},
                )

        # Filter out already-present segments and cap at 50
        candidates = []
        for row in new_segment_rows:
            seg_id = row.get("segmentId", "")
            if seg_id and seg_id not in existing_segment_ids:
                candidates.append(row)
                existing_segment_ids.add(seg_id)
            if len(candidates) >= 50:
                break

        if not candidates:
            return enriched_results

        # Batch-fetch faces for new segments
        candidate_seg_ids = [c["segmentId"] for c in candidates]
        face_map = {}
        try:
            face_results = _execute_neptune_query(
                neptune_client,
                graph_id,
                "MATCH (f:Face)-[:APPEARS_IN_SEGMENT]->(s:Segment) "
                "WHERE s.segmentId IN $segmentIds "
                "RETURN s.segmentId AS segmentId, f.label AS label, f.isCelebrity AS isCelebrity",
                parameters={"segmentIds": candidate_seg_ids},
            )
            for fr in face_results:
                sid = fr.get("segmentId", "")
                is_celeb_raw = fr.get("isCelebrity", "false")
                if isinstance(is_celeb_raw, str):
                    is_celeb = is_celeb_raw.lower() == "true"
                else:
                    is_celeb = bool(is_celeb_raw)
                face_map.setdefault(sid, []).append(
                    {"label": fr.get("label", "Unknown"), "isCelebrity": is_celeb}
                )
        except Exception:
            logger.warning("Failed to batch-fetch faces for expansion candidates", exc_info=True)

        # Build enriched result dicts for new segments
        for row in candidates:
            seg_id = row.get("segmentId", "")
            job_id = row.get("jobId", "")
            # Extract shot_id from segmentId (format: "{jobId}_{shot_id}")
            shot_id = ""
            if seg_id and job_id and seg_id.startswith(job_id + "_"):
                shot_id = seg_id[len(job_id) + 1:]

            enriched_results.append({
                "jobId": job_id,
                "video_name": row.get("videoName", ""),
                "shot_id": shot_id,
                "shot_startTime": row.get("startTime", 0),
                "shot_endTime": row.get("endTime", 0),
                "shot_description": row.get("description", ""),
                "shot_transcript": row.get("transcript", ""),
                "composite_key": f"{job_id}/{shot_id}_composite.png" if job_id and shot_id else "",
                "clip_key": f"{job_id}/{shot_id}_clip.mp4" if job_id and shot_id else "",
                "score": 0,
                "faces": face_map.get(seg_id, []),
                "adjacentSegments": {"prev": None, "next": None},
            })

        logger.info("Graph expansion added %d candidates", len(candidates))

    except Exception:
        logger.warning("Graph expansion failed, returning original results", exc_info=True)

    return enriched_results


# -------------------------------------------------------------------
# Audio index search and shot mapping
# -------------------------------------------------------------------

def _search_audio_index(client, audio_index, transcript_embedding):
    """Search the audio/transcript index using kNN on transcript vectors."""
    query = {
        "size": 50,
        "query": {
            "knn": {
                "transcript_vector": {
                    "vector": transcript_embedding,
                    "k": 50,
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
# LLM reranking (post-Cohere)
# -------------------------------------------------------------------

def _llm_rerank(query_llm_model, user_query, query_analysis, enriched_results, max_results=50):
    """Use Claude Haiku to score and rank enriched results by relevance.

    Processes up to max_results. Results beyond that are appended at the end
    with score=0. Falls back to Cohere reranking on any failure.
    """
    if not query_llm_model or not enriched_results:
        return enriched_results

    llm_candidates = enriched_results[:max_results]
    remainder = enriched_results[max_results:]

    # Build context for each result
    result_descriptions = []
    for i, r in enumerate(llm_candidates):
        face_labels = ", ".join(
            f["label"] for f in r.get("faces", []) if f.get("label")
        )
        result_descriptions.append(
            f"[{i}] Description: {r.get('shot_description', '')}\n"
            f"    Transcript: {r.get('shot_transcript', '')}\n"
            f"    Faces: {face_labels}"
        )

    results_text = "\n".join(result_descriptions)

    entity_context = ""
    if query_analysis and query_analysis.get("entities"):
        entity_names = [e.get("label", "") for e in query_analysis["entities"]]
        entity_context = f"\nThe user is looking for these entities: {', '.join(entity_names)}"

    system_prompt = (
        "You are a search result relevance scorer for a video search system. "
        "Score each result 0.0 to 1.0 for relevance to the user query.\n\n"
        "Consider:\n"
        "- Does the description match the query's intent?\n"
        "- Do the detected faces match any entity the user is searching for?\n"
        "- Does the transcript discuss the topic the user asked about?\n"
        "- Be strict about false positives: a result that mentions a keyword but in an irrelevant context should score low.\n"
        "- Score 0.0 for completely irrelevant results.\n\n"
        "Respond with a JSON array only, no markdown fences, no explanation.\n"
        'Schema: [{"index": 0, "score": 0.85}, ...]\n'
        "You must include an entry for every result index."
    )

    user_message = (
        f"Query: {user_query}{entity_context}\n\n"
        f"Results:\n{results_text}"
    )

    try:
        t0 = time.time()
        response = bedrock_client.invoke_model(
            modelId=query_llm_model,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 4096,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            }),
        )
        body = json.loads(response["body"].read())
        text = body["content"][0]["text"]
        elapsed = time.time() - t0
        logger.info("LLM rerank completed in %.2fs for %d results", elapsed, len(llm_candidates))

        scores = json.loads(text)
        score_map = {item["index"]: item["score"] for item in scores}

        for i, r in enumerate(llm_candidates):
            if i in score_map:
                r["score"] = score_map[i]
            else:
                r["score"] = 0.0

        llm_candidates.sort(key=lambda x: x.get("score", 0), reverse=True)

        # Append remainder with score=0
        for r in remainder:
            r["score"] = 0.0
        return llm_candidates + remainder

    except Exception:
        logger.warning("LLM rerank failed, falling back to Cohere rerank", exc_info=True)
        return _rerank(user_query, enriched_results, MAX_RERANK_RESULTS)


# -------------------------------------------------------------------
# Post-retrieval: Neptune enrichment
# -------------------------------------------------------------------

def _enrich_results_from_neptune(results, neptune_graph_id, region):
    """Enrich search results with metadata from Neptune: video_name, timestamps, faces, adjacency."""
    if not results:
        return results

    neptune_client = _get_neptune_client(region)
    enriched = []

    for result in results:
        job_id = result.get("jobId", "")
        shot_id = result.get("shot_id", "")
        segment_id = f"{job_id}_{shot_id}" if job_id and shot_id else ""

        video_name = ""
        start_time = 0
        end_time = 0
        transcript = ""
        faces = []
        adjacent_segments = {"prev": None, "next": None}
        segment_meta = {}

        if segment_id:
            # Fetch segment metadata and video name
            segment_meta = _get_segment_metadata(
                neptune_client, neptune_graph_id, segment_id
            )
            video_name = segment_meta.get("videoName", "")
            start_time = segment_meta.get("startTime", 0)
            end_time = segment_meta.get("endTime", 0)
            transcript = segment_meta.get("transcript", "")

            # Fetch face labels for this segment
            faces = _get_faces_for_segment(
                neptune_client, neptune_graph_id, segment_id
            )

            # Fetch adjacent segments
            adjacent_segments = _get_adjacent_segments(
                neptune_client, neptune_graph_id, segment_id
            )

        enriched_result = {
            "jobId": job_id,
            "video_name": video_name,
            "shot_id": shot_id,
            "shot_startTime": start_time,
            "shot_endTime": end_time,
            "shot_description": result.get("shot_description") or segment_meta.get("description", ""),
            "shot_transcript": transcript,
            "composite_key": f"{job_id}/{shot_id}_composite.png" if job_id and shot_id else "",
            "clip_key": f"{job_id}/{shot_id}_clip.mp4" if job_id and shot_id else "",
            "score": result.get("score", 0),
            "faces": faces,
            "adjacentSegments": adjacent_segments,
        }
        enriched.append(enriched_result)

    return enriched


def _get_segment_metadata(neptune_client, graph_id, segment_id):
    """Query Neptune for segment metadata including video name and timestamps."""
    try:
        results = _execute_neptune_query(
            neptune_client,
            graph_id,
            "MATCH (v:Video)-[:HAS_SEGMENT]->(s:Segment {segmentId: $segmentId}) "
            "RETURN v.videoName AS videoName, s.startTime AS startTime, "
            "s.endTime AS endTime, s.transcript AS transcript, "
            "s.description AS description",
            parameters={"segmentId": segment_id},
        )
        if results:
            row = results[0]
            return {
                "videoName": row.get("videoName", ""),
                "startTime": row.get("startTime", 0),
                "endTime": row.get("endTime", 0),
                "transcript": row.get("transcript", ""),
                "description": row.get("description", ""),
            }
    except ClientError:
        logger.warning("Failed to fetch segment metadata for: %s", segment_id)

    return {"videoName": "", "startTime": 0, "endTime": 0, "transcript": "", "description": ""}


def _get_faces_for_segment(neptune_client, graph_id, segment_id):
    """Query Neptune for all Face nodes linked to a given segment."""
    try:
        results = _execute_neptune_query(
            neptune_client,
            graph_id,
            "MATCH (f:Face)-[:APPEARS_IN_SEGMENT]->(s:Segment {segmentId: $segmentId}) "
            "RETURN f.label AS label, f.isCelebrity AS isCelebrity",
            parameters={"segmentId": segment_id},
        )

        faces = []
        for row in results:
            label = row.get("label", "Unknown")
            is_celebrity_raw = row.get("isCelebrity", "false")
            # Neptune may store booleans as strings
            if isinstance(is_celebrity_raw, str):
                is_celebrity = is_celebrity_raw.lower() == "true"
            else:
                is_celebrity = bool(is_celebrity_raw)

            faces.append({"label": label, "isCelebrity": is_celebrity})
        return faces
    except ClientError:
        logger.warning("Failed to fetch faces for segment: %s", segment_id)
        return []


def _get_adjacent_segments(neptune_client, graph_id, segment_id):
    """Query Neptune for previous and next segments relative to the given segment."""
    adjacent = {"prev": None, "next": None}

    # Previous segment
    try:
        prev_results = _execute_neptune_query(
            neptune_client,
            graph_id,
            "MATCH (prev:Segment)-[:NEXT_SEGMENT]->(curr:Segment {segmentId: $segmentId}) "
            "RETURN prev.segmentId AS prevSegmentId",
            parameters={"segmentId": segment_id},
        )
        if prev_results:
            prev_segment_id = prev_results[0].get("prevSegmentId", "")
            if prev_segment_id:
                # Extract shot_id from segmentId (format: "{jobId}_{shot_id}")
                parts = prev_segment_id.split("_", 1)
                if len(parts) > 1:
                    adjacent["prev"] = parts[1]
    except ClientError:
        logger.warning("Failed to fetch prev segment for: %s", segment_id)

    # Next segment
    try:
        next_results = _execute_neptune_query(
            neptune_client,
            graph_id,
            "MATCH (curr:Segment {segmentId: $segmentId})-[:NEXT_SEGMENT]->(nxt:Segment) "
            "RETURN nxt.segmentId AS nextSegmentId",
            parameters={"segmentId": segment_id},
        )
        if next_results:
            next_segment_id = next_results[0].get("nextSegmentId", "")
            if next_segment_id:
                parts = next_segment_id.split("_", 1)
                if len(parts) > 1:
                    adjacent["next"] = parts[1]
    except ClientError:
        logger.warning("Failed to fetch next segment for: %s", segment_id)

    return adjacent
