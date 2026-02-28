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

# ---------------------------------------------------------------------------
# AWS clients (module-level for container reuse)
# ---------------------------------------------------------------------------
bedrock_config = Config(
    read_timeout=300,
    retries={"max_attempts": 5, "mode": "adaptive"},
)
bedrock_client = boto3.client("bedrock-runtime", config=bedrock_config)
neptune_client = boto3.client("neptune-graph", region_name=CONFIG["region"])


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


def _parse_is_celebrity(raw):
    """Normalize Neptune isCelebrity field (may be string or bool)."""
    if isinstance(raw, str):
        return raw.lower() == "true"
    return bool(raw)


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
        # Step 1: Find matching face(s) by label
        face_results = _execute_neptune_query(
            "MATCH (f:Face) WHERE toLower(f.label) CONTAINS toLower($name) "
            "RETURN f.faceId AS faceId, f.label AS label, f.isCelebrity AS isCelebrity",
            parameters={"name": person_name},
        )
        if not face_results:
            return json.dumps({"message": f"No face found matching '{person_name}'", "segments": []})

        all_segments = []
        matched_faces = []

        for face in face_results:
            face_id = face.get("faceId", "")
            label = face.get("label", "")
            matched_faces.append({"faceId": face_id, "label": label})

            if not face_id:
                # Celebrity faces may not have faceId; match by label
                segment_results = _execute_neptune_query(
                    "MATCH (f:Face {label: $label})-[:APPEARS_IN_SEGMENT]->(s:Segment)"
                    "<-[:HAS_SEGMENT]-(v:Video) "
                    "RETURN s.segmentId AS segmentId, v.jobId AS jobId, "
                    "s.description AS description, s.transcript AS transcript",
                    parameters={"label": label},
                )
            else:
                segment_results = _execute_neptune_query(
                    "MATCH (f:Face {faceId: $faceId})-[:APPEARS_IN_SEGMENT]->(s:Segment)"
                    "<-[:HAS_SEGMENT]-(v:Video) "
                    "RETURN s.segmentId AS segmentId, v.jobId AS jobId, "
                    "s.description AS description, s.transcript AS transcript",
                    parameters={"faceId": face_id},
                )

            for row in segment_results:
                seg_id = row.get("segmentId", "")
                job_id = row.get("jobId", "")
                shot_id = ""
                if seg_id and job_id and seg_id.startswith(job_id + "_"):
                    shot_id = seg_id[len(job_id) + 1:]

                all_segments.append({
                    "jobId": job_id,
                    "shot_id": shot_id,
                    "description": row.get("description", ""),
                    "transcript": row.get("transcript", ""),
                })

        return json.dumps({
            "matched_faces": matched_faces,
            "segments": all_segments,
            "count": len(all_segments),
        })
    except Exception as e:
        logger.error("find_person_segments failed: %s", str(e))
        return json.dumps({"error": str(e), "segments": []})


# ---------------------------------------------------------------------------
# Tool 3: search_visual_index
# ---------------------------------------------------------------------------

@tool
def search_visual_index(query: str, max_results: int = 20) -> str:
    """Search video shots by visual content description. Use for queries about what is SEEN in videos.

    Performs hybrid search combining text matching and vector similarity on shot descriptions.

    Args:
        query: Visual description to search for (e.g., "person presenting on stage", "outdoor aerial view")
        max_results: Maximum number of results to return (default 20, max 50)
    """
    try:
        max_results = min(max_results, 50)
        client = _get_opensearch_client()
        query_embedding = _generate_text_embedding(query)

        aoss_query = {
            "size": max_results,
            "query": {
                "hybrid": {
                    "queries": [
                        {"match": {"shot_description": query}},
                        {"knn": {"shot_desc_vector": {"vector": query_embedding, "k": max_results}}},
                        {"knn": {"shot_video_vector": {"vector": query_embedding, "k": max_results}}},
                    ],
                }
            },
            "_source": ["jobId", "shot_id", "shot_description"],
        }

        response = client.search(
            body=aoss_query,
            index=CONFIG["aoss_visual_index"],
            params={"search_pipeline": "vss-hybrid-search-pipeline"},
        )
        hits = response.get("hits", {}).get("hits", [])

        results = []
        for hit in hits:
            src = hit["_source"]
            results.append({
                "jobId": src.get("jobId", ""),
                "shot_id": src.get("shot_id", ""),
                "shot_description": src.get("shot_description", ""),
                "search_score": hit.get("_score", 0),
            })

        return json.dumps({"results": results, "count": len(results)})
    except Exception as e:
        logger.error("search_visual_index failed: %s", str(e))
        return json.dumps({"error": str(e), "results": []})


# ---------------------------------------------------------------------------
# Tool 4: search_audio_index
# ---------------------------------------------------------------------------

@tool
def search_audio_index(query: str, max_results: int = 20) -> str:
    """Search video transcripts for spoken content. Use for queries about what is SAID or discussed in videos.

    Performs vector similarity search on transcript embeddings.

    Args:
        query: What was said or discussed (e.g., "cloud computing", "machine learning")
        max_results: Maximum number of results to return (default 20, max 50)
    """
    try:
        max_results = min(max_results, 50)
        client = _get_opensearch_client()
        query_embedding = _generate_text_embedding(query)

        aoss_query = {
            "size": max_results,
            "query": {
                "knn": {
                    "transcript_vector": {
                        "vector": query_embedding,
                        "k": max_results,
                    }
                }
            },
            "_source": ["jobId", "shot_id", "transcript"],
        }

        response = client.search(body=aoss_query, index=CONFIG["aoss_audio_index"])
        hits = response.get("hits", {}).get("hits", [])

        results = []
        for hit in hits:
            src = hit["_source"]
            results.append({
                "jobId": src.get("jobId", ""),
                "shot_id": src.get("shot_id", ""),
                "transcript": src.get("transcript", ""),
                "search_score": hit.get("_score", 0),
            })

        return json.dumps({"results": results, "count": len(results)})
    except Exception as e:
        logger.error("search_audio_index failed: %s", str(e))
        return json.dumps({"error": str(e), "results": []})


@tool
def get_segment_details(segment_ids: list) -> str:
    """Get full details for segments including faces, description, transcript, and adjacent segments.

    Use this to enrich results from other tools when you need more context for reasoning
    (e.g., checking which faces appear in visually-matched segments).

    Args:
        segment_ids: List of segment IDs in format "jobId_shotId"
    """
    try:
        if not segment_ids:
            return json.dumps({"segments": []})

        segment_ids = segment_ids[:50]

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
            face_map.setdefault(sid, []).append({
                "label": fr.get("label", "Unknown"),
                "isCelebrity": _parse_is_celebrity(fr.get("isCelebrity", "false")),
            })

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

        segments = []
        for seg_id in segment_ids:
            meta = metadata_map.get(seg_id, {})
            job_id = meta.get("jobId", "")
            shot_id = seg_id[len(job_id) + 1:] if seg_id and job_id and seg_id.startswith(job_id + "_") else ""

            segments.append({
                "jobId": job_id,
                "shot_id": shot_id,
                "description": meta.get("description", ""),
                "transcript": meta.get("transcript", ""),
                "faces": face_map.get(seg_id, []),
                "adjacentSegments": adjacency_map.get(seg_id, {"prev": None, "next": None}),
            })

        return json.dumps({"segments": segments, "count": len(segments)})
    except Exception as e:
        logger.error("get_segment_details failed: %s", str(e))
        return json.dumps({"error": str(e), "segments": []})

