"""MCP server wrapping the VSS Agent for an Insurance Claims demo.

Exposes two tools to any MCP client (Microsoft Foundry agent, Claude, OpenAI
Agents SDK, etc.):

  - vss_upload_video(filename) -> {upload_url, video_id}
      Calls VSS POST /api/v1/videos. The caller then PUTs the video bytes to
      `upload_url` (returned signed VST URL). `video_id` is the canonical name
      VSS will use to reference the video in subsequent chat calls.

  - vss_analyze_video(video_id, question) -> str
      Calls VSS POST /chat/stream with an OpenAI-style messages payload that
      instructs the VSS agent to analyze the named video. Aggregates the SSE
      stream into a single answer string. The VSS agent internally drives the
      VLM (cosmos-reason2-8b) for video understanding.

Run locally:

    pip install -r requirements.txt
    export VSS_BASE_URL=http://vss.<EXTERNAL_HOST>.nip.io
    python vss_mcp_server.py

Or via uv:

    uv run vss_mcp_server.py

The server speaks MCP over stdio. To register it with a Foundry agent, point
the agent's MCP tool config at `python /abs/path/vss_mcp_server.py`.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# VSS's LVS chat agent wraps its chain-of-thought + internal tool traces in
# <agent-think>...<agent-think-step>...</agent-think-step>...</agent-think>
# blocks. The actual user-facing answer comes AFTER the closing tag. We strip
# the planning blocks so callers see only the final response.
_AGENT_THINK_RE = re.compile(r"<agent-think\b.*?</agent-think>", flags=re.DOTALL | re.IGNORECASE)

VSS_BASE_URL = os.environ.get("VSS_BASE_URL", "http://vss.104.45.71.11.nip.io").rstrip("/")
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)

mcp = FastMCP("vss-insurance-claims")


@mcp.tool()
def vss_upload_video(filename: str) -> dict[str, str]:
    """Request a presigned VST upload URL for a video file.

    The caller is expected to PUT the video bytes to `upload_url` after this
    returns. The returned `video_id` is what to pass to `vss_analyze_video`.

    Args:
        filename: A short name for the video (e.g. "claim-2026-04-22.mp4").
                  Does not need to be unique; VSS adds a timestamp suffix.

    Returns:
        {"upload_url": "<signed PUT URL>", "video_id": "<short filename stem>"}
    """
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        r = client.post(f"{VSS_BASE_URL}/api/v1/videos", json={"filename": filename})
        r.raise_for_status()
        data = r.json()
    url = data["url"]
    # VST URL is like .../v1/storage/file/<video_id>/<timestamp>; the second-to-last
    # path segment is the canonical video_id VSS uses for chat references.
    video_id = url.rstrip("/").split("/")[-2]
    return {"upload_url": url, "video_id": video_id}


@mcp.tool()
def vss_analyze_video(video_id: str, question: str) -> str:
    """Ask the VSS agent a question about a previously uploaded video.

    The video must have been uploaded via `vss_upload_video` first (i.e. its
    bytes are stored in VST and reachable by the agent).

    Args:
        video_id: The `video_id` returned by `vss_upload_video`.
        question: Plain-English question. For insurance claims, prefer
                  structured prompts like:
                  "Analyze the damage shown in video '<video_id>'. List each
                  visibly damaged panel with severity (minor/moderate/severe)
                  and a brief description. If a VIN is visible in any frame,
                  transcribe it."

    Returns:
        The final answer text from the VSS agent (after aggregating the SSE
        stream from /chat/stream).
    """
    prompt = f"Video reference: '{video_id}'.\n\n{question}"
    payload = {"messages": [{"role": "user", "content": prompt}]}

    final_chunks: list[str] = []

    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        with client.stream(
            "POST",
            f"{VSS_BASE_URL}/chat/stream",
            json=payload,
            headers={"Accept": "text/event-stream"},
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                # The VSS agent emits SSE-flavored lines:
                #   intermediate_data: {...trace...}      -> skip (tool traces)
                #   data: {...openai-chunk...}            -> aggregate
                # plus occasional plain JSON lines on terminal events.
                if line.startswith("intermediate_data:"):
                    continue
                if line.startswith("data:"):
                    body = line.removeprefix("data:").strip()
                    if body == "[DONE]":
                        break
                    try:
                        chunk = json.loads(body)
                    except json.JSONDecodeError:
                        final_chunks.append(body)
                        continue
                    delta = _extract_chunk_text(chunk)
                    if delta:
                        final_chunks.append(delta)

    raw = "".join(final_chunks).strip()
    # Strip VSS's <agent-think> chain-of-thought / tool-trace blocks so the
    # caller (master agent) sees only the final user-facing answer.
    text = _AGENT_THINK_RE.sub("", raw).strip()
    if not text:
        # Either nothing streamed, or the agent only emitted reasoning blocks.
        # Return the raw payload so callers can debug rather than failing silently.
        text = raw or "[no streamed content; check /chat/stream raw response]"
    return text


def _extract_chunk_text(chunk: dict[str, Any]) -> str:
    """Pull assistant text out of an OpenAI-style streaming chunk."""
    choices = chunk.get("choices") or []
    if not choices:
        return chunk.get("content", "") or ""
    delta = choices[0].get("delta") or {}
    return delta.get("content", "") or choices[0].get("message", {}).get("content", "") or ""


if __name__ == "__main__":
    mcp.run()
