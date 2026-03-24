import json
import logging
import os
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth
from strands import tool

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Shared configuration (from AgentCore environment variables)
# ---------------------------------------------------------------------------
CONFIG = {
    "region": os.environ.get("AWS_REGION", "us-east-1"),
    "aoss_host": os.environ.get("AOSS_HOST", ""),
    "aoss_visual_index": os.environ.get("AOSS_VISUAL_INDEX", "vss-visual-index"),
    "aoss_audio_index": os.environ.get("AOSS_AUDIO_INDEX", "vss-audio-index"),
    "embedding_model": os.environ.get("EMBEDDING_MODEL", "amazon.nova-2-multimodal-embeddings-v1:0"),
    "neptune_graph_id": os.environ.get("NEPTUNE_GRAPH_ID", ""),
}

DEFAULT_VISUAL_RESULTS = 100
DEFAULT_AUDIO_RESULTS = 50

# ---------------------------------------------------------------------------
# AWS clients (module-level for container reuse)
# ---------------------------------------------------------------------------
bedrock_config = Config(
    read_timeout=60,
    max_pool_connections=50,
    retries={"max_attempts": 5, "mode": "adaptive"},
)
bedrock_client = boto3.client("bedrock-runtime", config=bedrock_config)
neptune_client = boto3.client("neptune-graph", region_name=CONFIG["region"])

# ---------------------------------------------------------------------------
# Tool 1: list_known_faces
# ---------------------------------------------------------------------------

@tool
def list_known_faces() -> str:
    """Get all known/labeled faces in the video library. Use this first if the query mentions a person's name.

    Returns a JSON list of faces with faceId, label, and isCelebrity fields.
    """
    try:
        results = _execute_neptune_query(
            "MATCH (f:Face) WHERE NOT f.label STARTS WITH 'Unknown-' "
            "RETURN f.faceId AS faceId, f.label AS label, f.isCelebrity AS isCelebrity"
        )
        faces = []
        for row in results:
            faces.append({
                "faceId": row.get("faceId", ""),
                "label": row.get("label", ""),
                "isCelebrity": _parse_is_celebrity(row.get("isCelebrity", "false")),
            })
        return json.dumps({"faces": faces, "count": len(faces)})
    except Exception as e:
        logger.error("list_known_faces failed: %s", str(e))
        return json.dumps({"error": str(e), "faces": []})


# ---------------------------------------------------------------------------
# Tool 2: find_person_segments
# ---------------------------------------------------------------------------

@tool
def find_person_segments(person_name: str) -> str:
    """Find all video segments where a specific person appears. Searches by face label in the graph database.

    Use this after list_known_faces() to get segments for a matched person.

    Args:
        person_name: Name of the person to find (supports partial matching via CONTAINS)
    """
    try:
        face_results = _execute_neptune_query(
            "MATCH (f:Face) WHERE toLower(f.label) CONTAINS toLower($name) "
            "RETURN f.faceId AS faceId, f.label AS label, f.isCelebrity AS isCelebrity",
            parameters={"name": person_name},
        )
        if not face_results:
            return json.dumps({"message": f"No face found matching '{person_name}'", "segments": []})

        matched_faces = []
        segment_map = {}

        for face in face_results:
            face_id = face.get("faceId", "")
            label = face.get("label", "")
            matched_faces.append({"faceId": face_id, "label": label})

            if not face_id:
                segment_results = _execute_neptune_query(
                    "MATCH (f:Face {label: $label})-[:APPEARS_IN_SEGMENT]->(s:Segment)"
                    "<-[:HAS_SEGMENT]-(v:Video) "
                    "RETURN s.segmentId AS segmentId, v.jobId AS jobId",
                    parameters={"label": label},
                )
            else:
                segment_results = _execute_neptune_query(
                    "MATCH (f:Face {faceId: $faceId})-[:APPEARS_IN_SEGMENT]->(s:Segment)"
                    "<-[:HAS_SEGMENT]-(v:Video) "
                    "RETURN s.segmentId AS segmentId, v.jobId AS jobId",
                    parameters={"faceId": face_id},
                )

            for row in segment_results:
                seg_id = row.get("segmentId", "")
                job_id = row.get("jobId", "")
                shot_id = ""
                if seg_id and job_id and seg_id.startswith(job_id + "_"):
                    shot_id = seg_id[len(job_id) + 1:]
                key = (job_id, shot_id)
                segment_map[key] = {"jobId": job_id, "shot_id": shot_id}

        all_segments = list(segment_map.values())

        return json.dumps({
            "matched_faces": matched_faces,
            "segments": _group_by_job_id(all_segments),
            "count": len(all_segments),
        })
    except Exception as e:
        logger.error("find_person_segments failed: %s", str(e))
        return json.dumps({"error": str(e), "segments": []})


# ---------------------------------------------------------------------------
# Tool 3: search_segments
# ---------------------------------------------------------------------------

@tool
def search_segments(query: str, search_visual: bool = True, search_audio: bool = False,
                    max_visual_results: int = DEFAULT_VISUAL_RESULTS,
                    max_audio_results: int = DEFAULT_AUDIO_RESULTS) -> str:
    """Search video segments by visual content and/or audio transcript, then return
    enriched details (description, transcript, faces, adjacency) for relevance judging.

    Args:
        query: Search query (e.g., "person presenting on stage", "cloud computing discussion")
        search_visual: Search visual descriptions (default True)
        search_audio: Search audio transcripts (default False)
        max_visual_results: Maximum visual search results
        max_audio_results: Maximum audio search results
    """
    try:
        client = _get_opensearch_client()
        seen = set()
        results = []

        # --- Visual search ---
        if search_visual:
            query_embedding = _generate_text_embedding(query)
            video_query_embedding = _generate_video_query_embedding(query)
            aoss_query = {
                "size": max_visual_results,
                "query": {
                    "hybrid": {
                        "queries": [
                            {"match": {"shot_description": query}},
                            {"knn": {"shot_desc_vector": {"vector": query_embedding, "k": max_visual_results}}},
                            {"knn": {"shot_video_vector": {"vector": video_query_embedding, "k": max_visual_results}}},
                        ],
                    }
                },
                "_source": ["jobId", "shot_id"],
            }
            response = client.search(
                body=aoss_query,
                index=CONFIG["aoss_visual_index"],
                params={"search_pipeline": "vss-hybrid-search-pipeline"},
            )
            for hit in response.get("hits", {}).get("hits", []):
                src = hit["_source"]
                job_id = src.get("jobId", "")
                shot_id = src.get("shot_id", "")
                key = (job_id, shot_id)
                if key not in seen:
                    seen.add(key)
                    results.append({"jobId": job_id, "shot_id": shot_id})

        # --- Audio search ---
        if search_audio:
            if not search_visual:
                query_embedding = _generate_text_embedding(query)
            aoss_query = {
                "size": max_audio_results,
                "query": {
                    "knn": {
                        "transcript_vector": {
                            "vector": query_embedding,
                            "k": max_audio_results,
                        }
                    }
                },
                "_source": ["jobId", "transcript_startTime", "transcript_endTime"],
            }
            response = client.search(body=aoss_query, index=CONFIG["aoss_audio_index"])
            hits = response.get("hits", {}).get("hits", [])

            job_time_ranges = {}
            for hit in hits:
                src = hit["_source"]
                job_id = src.get("jobId", "")
                if not job_id:
                    continue
                job_time_ranges.setdefault(job_id, []).append({
                    "start": float(src.get("transcript_startTime", 0)),
                    "end": float(src.get("transcript_endTime", 0)),
                })

            for job_id, time_ranges in job_time_ranges.items():
                segments = _execute_neptune_query(
                    "MATCH (v:Video {jobId: $jobId})-[:HAS_SEGMENT]->(s:Segment) "
                    "RETURN s.segmentId AS segmentId, s.startTime AS startTime, s.endTime AS endTime",
                    parameters={"jobId": job_id},
                )
                for seg in segments:
                    seg_start = float(seg.get("startTime", 0))
                    seg_end = float(seg.get("endTime", 0))
                    seg_id = seg.get("segmentId", "")
                    for tr in time_ranges:
                        if seg_start <= tr["end"] and seg_end >= tr["start"]:
                            shot_id = seg_id[len(job_id) + 1:] if seg_id.startswith(job_id + "_") else ""
                            key = (job_id, shot_id)
                            if key not in seen:
                                seen.add(key)
                                results.append({"jobId": job_id, "shot_id": shot_id})
                            break

        if not results:
            return json.dumps({"segments": [], "count": 0})

        # --- Build segment IDs and fetch enrichment from Neptune ---
        segment_ids = [f"{r['jobId']}_{r['shot_id']}" for r in results]
        segment_ids = list(dict.fromkeys(segment_ids))

        metadata_results = _execute_neptune_query(
            "MATCH (v:Video)-[:HAS_SEGMENT]->(s:Segment) "
            "WHERE s.segmentId IN $segmentIds "
            "RETURN s.segmentId AS segmentId, v.jobId AS jobId, "
            "s.description AS description, s.transcript AS transcript",
            parameters={"segmentIds": segment_ids},
        )
        metadata_map = {row.get("segmentId", ""): row for row in metadata_results}

        face_results = _execute_neptune_query(
            "MATCH (f:Face)-[:APPEARS_IN_SEGMENT]->(s:Segment) "
            "WHERE s.segmentId IN $segmentIds "
            "RETURN s.segmentId AS segmentId, f.label AS label, f.isCelebrity AS isCelebrity",
            parameters={"segmentIds": segment_ids},
        )
        face_map = {}
        for fr in face_results:
            sid = fr.get("segmentId", "")
            face_map.setdefault(sid, []).append(fr.get("label", "Unknown"))

        adjacency_results = _execute_neptune_query(
            "MATCH (s:Segment) WHERE s.segmentId IN $segmentIds "
            "OPTIONAL MATCH (prev:Segment)-[:NEXT_SEGMENT]->(s) "
            "OPTIONAL MATCH (s)-[:NEXT_SEGMENT]->(nxt:Segment) "
            "RETURN s.segmentId AS segmentId, "
            "prev.segmentId AS prevSegmentId, nxt.segmentId AS nextSegmentId",
            parameters={"segmentIds": segment_ids},
        )
        adjacency_map = {}
        for ar in adjacency_results:
            sid = ar.get("segmentId", "")
            adjacency_map[sid] = {
                "prev": ar.get("prevSegmentId") or None,
                "next": ar.get("nextSegmentId") or None,
            }

        # --- Build output ---
        output_grouped = {}
        for seg_id in segment_ids:
            meta = metadata_map.get(seg_id, {})
            job_id = meta.get("jobId", "")
            shot_id = seg_id[len(job_id) + 1:] if seg_id and job_id and seg_id.startswith(job_id + "_") else ""

            faces = face_map.get(seg_id, [])
            face_labels = [f for f in faces if not f.startswith("Unknown")]

            shot_data = {
                "shot_id": shot_id,
                "description": meta.get("description", "") or "",
                "transcript": meta.get("transcript", "") or "",
                "faces": face_labels,
            }

            adj = adjacency_map.get(seg_id, {})
            prev_raw = adj.get("prev") or None
            next_raw = adj.get("next") or None
            prev_id = (prev_raw[len(job_id) + 1:] if prev_raw and prev_raw.startswith(job_id + "_") else prev_raw) if prev_raw else None
            next_id = (next_raw[len(job_id) + 1:] if next_raw and next_raw.startswith(job_id + "_") else next_raw) if next_raw else None
            shot_data["adjacentSegments"] = {"prev": prev_id, "next": next_id}

            if job_id not in output_grouped:
                output_grouped[job_id] = {"jobId": job_id, "shots": []}
            output_grouped[job_id]["shots"].append(shot_data)

        return json.dumps({"segments": list(output_grouped.values()), "count": len(segment_ids)})
    except Exception as e:
        logger.error("search_segments failed: %s", str(e))
        return json.dumps({"error": str(e), "segments": []})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_opensearch_client():
    """Create an OpenSearch client with AWS SigV4 auth for serverless."""
    host = CONFIG["aoss_host"]
    region = CONFIG["region"]
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


def _execute_neptune_query(query, parameters=None):
    """Execute an openCypher query against Neptune Analytics and return results."""
    kwargs = {
        "graphIdentifier": CONFIG["neptune_graph_id"],
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


def _generate_text_embedding(text):
    """Generate a text embedding using Nova v2 multimodal embeddings."""
    if not text:
        text = " "
    body = json.dumps({
        "taskType": "SINGLE_EMBEDDING",
        "singleEmbeddingParams": {
            "embeddingPurpose": "TEXT_RETRIEVAL",
            "embeddingDimension": 3072,
            "text": {
                "truncationMode": "END",
                "value": text,
            },
        },
    })
    response = bedrock_client.invoke_model(
        body=body,
        modelId=CONFIG["embedding_model"],
        accept="application/json",
        contentType="application/json",
    )
    response_body = json.loads(response["body"].read())
    return response_body["embeddings"][0]["embedding"]


def _generate_video_query_embedding(text):
    """Generate a text query embedding with VIDEO_RETRIEVAL purpose for searching video vectors."""
    if not text:
        text = " "
    body = json.dumps({
        "taskType": "SINGLE_EMBEDDING",
        "singleEmbeddingParams": {
            "embeddingPurpose": "VIDEO_RETRIEVAL",
            "embeddingDimension": 3072,
            "text": {
                "truncationMode": "END",
                "value": text,
            },
        },
    })
    response = bedrock_client.invoke_model(
        body=body,
        modelId=CONFIG["embedding_model"],
        accept="application/json",
        contentType="application/json",
    )
    response_body = json.loads(response["body"].read())
    return response_body["embeddings"][0]["embedding"]


def _group_by_job_id(flat_results):
    """Group flat [{jobId, shot_id}, ...] into [{jobId, shots: [...]}, ...] with deduplication."""
    grouped = {}
    for item in flat_results:
        job_id = item.get("jobId", "")
        shot_id = item.get("shot_id", "")
        if job_id not in grouped:
            grouped[job_id] = []
        if shot_id not in grouped[job_id]:
            grouped[job_id].append(shot_id)
    return [{"jobId": jid, "shots": shots} for jid, shots in grouped.items()]


def _parse_is_celebrity(raw):
    """Normalize Neptune isCelebrity field (may be string or bool)."""
    if isinstance(raw, str):
        return raw.lower() == "true"
    return bool(raw)

