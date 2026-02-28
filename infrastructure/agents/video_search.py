import os

from botocore.config import Config
from strands import Agent
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from tools import (
    list_known_faces,
    find_person_segments,
    search_visual_index,
    search_audio_index,
    get_segment_details,
)

config = Config(
    read_timeout=900,
    connect_timeout=900,
    retries={"max_attempts": 20, "mode": "standard"},
)

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """You are a Video Search agent. Given a user's search query, use the available tools to find the most relevant video segments.

## Strategy:
1. If the query mentions a person's name → call list_known_faces() first, then find_person_segments() for matches
2. If the query describes visual content → call search_visual_index()
3. If the query is about spoken content or topics discussed → call search_audio_index()
4. For combined queries (e.g., "Ashley talking about AWS"), use multiple tools and intersect results
5. Call get_segment_details() when you need more context (e.g., faces, descriptions) to reason about relevance

## Response Format:
Return ONLY segment IDs and scores. Do NOT include descriptions, transcripts, or other metadata — the caller handles enrichment. Respond with a JSON object wrapped in <JSON> tags:
<JSON>
{
  "results": [
    {"jobId": "...", "shot_id": "...", "score": 0.95}
  ]
}
</JSON>

## Rules:
- Rank results by relevance to the query
- For person queries, ALL segments where the person appears are relevant — score them highly
- Score: 1.0 for exact matches, 0.5+ for partial matches
- Maximum 50 results
- If no results are found, return an empty results array
"""

agent = Agent(
    model=BedrockModel(
        model_id=os.environ.get("BEDROCK_LLM_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        temperature=0.1,
        boto_client_config=config,
    ),
    system_prompt=SYSTEM_PROMPT,
    tools=[list_known_faces, find_person_segments, search_visual_index, search_audio_index, get_segment_details],
)


@app.entrypoint
def invoke(payload):
    prompt = payload.get("prompt", "")
    print(f"Processing search query: {prompt}")

    result = agent(prompt)
    return result.message["content"][0]["text"]


if __name__ == "__main__":
    app.run()
