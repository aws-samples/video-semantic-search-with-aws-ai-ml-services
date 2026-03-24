import os

from botocore.config import Config
from strands import Agent
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from tools import (
    list_known_faces,
    find_person_segments,
    search_segments,
)

config = Config(
    read_timeout=900,
    connect_timeout=900,
    retries={"max_attempts": 20, "mode": "standard"},
)

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """You are a Video Search agent. Given a user's search query, use the available tools to find the most relevant video segments.

## Strategy:
**Step 1 - Identify people (if needed):**
- If the query mentions a person's name → call list_known_faces() to validate/match names
- If person-only queries (just names, no visual/audio qualifiers, e.g., "Werner Vogels", "find Werner Vogels and Jeff Bezos"):
  → call find_person_segments() — go directly to Response Format
- For all other queries → proceed to Step 2

**Step 2 - Search and retrieve:**
- Call search_segments() with appropriate flags:
  - Visual queries → search_visual=True
  - Audio/speech queries → search_audio=True
  - Both → set both to True
- It returns per segment: description, transcript, faces, and adjacentSegments

**Step 3 - Rerank:**
- Use ALL returned fields (description, transcript, faces, adjacentSegments) to judge relevance
- IMPORTANT: Do NOT match words literally. Descriptions are AI-generated and may use approximate or incorrect terms (e.g., "orange" described as "yellow", "podium" called "stage", "laptop" called "device"). Reason about the INTENT and MEANING of the query:
  - Consider synonyms, related concepts, and visual similarity (e.g., "car" matches "vehicle", "automobile", "sedan")
  - Consider contextual clues across ALL fields — a segment may not match the description but match the transcript or faces
  - Consider the overall scene/activity, not just individual objects or words
  - When descriptions are ambiguous or approximate, give the benefit of the doubt and include the result with a moderate score rather than excluding it
- Score generously for partial matches — a segment that captures the spirit of the query deserves a moderate score (0.4-0.6) even if the exact words don't align
- Reserve high scores (0.8-1.0) for segments that clearly match the query intent across multiple fields
- Reserve low scores (<0.3) only for segments that are clearly unrelated to what the user is looking for

## Response Format:
Return ONLY segment IDs and scores grouped by jobId. Do NOT include descriptions, transcripts, or other metadata. Respond with a JSON object wrapped in <JSON> tags:
<JSON>
{
  "results": [
    {"jobId": "...", "shots": [
      {"shot_id": "...", "score": 0.95},
      {"shot_id": "...", "score": 0.87}
    ]}
  ]
}
</JSON>

## Rules:
- ONLY include segments that are relevant to the query. Omit anything unrelated.
- Rank results by relevance.
- Maximum 100 results
- If no results are found, return an empty results array
"""

agent = Agent(
    model=BedrockModel(
        model_id=os.environ.get("BEDROCK_LLM_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        # additional_request_fields={
        #   "thinking": {"type": "enabled", "budget_tokens": 4096},
        # },
        temperature=0,
        cache_prompt="default",
        cache_tools="default",
        boto_client_config=config,
    ),
    system_prompt=SYSTEM_PROMPT,
    tools=[list_known_faces, find_person_segments, search_segments],
)


@app.entrypoint
def invoke(payload):
    prompt = payload.get("prompt", "")
    result = agent(prompt)
    return result.message["content"][0]["text"]


if __name__ == "__main__":
    app.run()
