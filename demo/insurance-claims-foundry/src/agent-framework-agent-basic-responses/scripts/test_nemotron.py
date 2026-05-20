"""Diagnostic: exercise tool-calling against a Nemotron endpoint.

Runs three escalating probes against the configured endpoint:

  1. Plain chat completion — no tools, baseline reachability.
  2. Direct OpenAI SDK call with tools — bypasses Agent Framework so we
     can see exactly what the server returns. Useful for isolating whether
     a failure is in the agent abstraction or the server deployment itself.
  3. Agent Framework end-to-end — the real path main.py uses, with the
     four insurance-triage tools (lookup_policy, vss_analyze_video,
     estimate_repair_cost, draft_claim_pdf).

Two providers selectable via CLI:

    python scripts/test_nemotron.py            # default: foundry
    python scripts/test_nemotron.py foundry    # Nemotron NIM on Foundry
    python scripts/test_nemotron.py build      # build.nvidia.com hosted API

The `build` path targets https://integrate.api.nvidia.com/v1 (NVIDIA-hosted
inference, OpenAI-compatible) — useful when the Foundry NIM is offline or
isn't configured for tool calling. Useful baseline: build.nvidia.com NIMs
ship with tool-calling enabled, so a failure there points at our code, not
the deployment.

All VSS calls are mocked via fixtures/vss/<video_id>.txt (MOCK_VSS=true),
so this runs offline relative to AKS. Policy / pricing data also comes
from the in-repo mocks in tools.py.

Env vars (from .env, gitignored):
  - Foundry NIM:  NEMOTRON_ENDPOINT_URL, NEMOTRON_API_KEY, NEMOTRON_MODEL_NAME
  - build.nvidia: NVIDIA_BUILD_API_KEY (required),
                  NVIDIA_BUILD_BASE_URL (default integrate.api.nvidia.com/v1),
                  NVIDIA_BUILD_MODEL_NAME (default nvidia/nemotron-3-super-120b-a12b)

The script prints PASS / FAIL / SERVER_BLOCKED per probe and exits 0 if at
least the plain-chat probe works.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

# Force mock VSS before any imports that might read env.
os.environ["MOCK_VSS"] = "true"

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.chdir(HERE)

from dotenv import load_dotenv

load_dotenv()
os.environ["MOCK_VSS"] = "true"  # re-assert; load_dotenv doesn't override

PROVIDER = (sys.argv[1] if len(sys.argv) > 1 else "foundry").strip().lower()
if PROVIDER not in ("foundry", "build"):
    print(f"unknown provider {PROVIDER!r}; expected 'foundry' or 'build'")
    sys.exit(2)

if PROVIDER == "build":
    # build.nvidia.com is OpenAI-compatible — funnel through the same code
    # path main.py already uses for "nemotron" (OpenAIChatCompletionClient)
    # by swapping the NEMOTRON_* vars to the build.nvidia.com credentials.
    # No changes to main.py needed.
    os.environ["NEMOTRON_ENDPOINT_URL"] = os.environ.get(
        "NVIDIA_BUILD_BASE_URL", "https://integrate.api.nvidia.com/v1"
    )
    os.environ["NEMOTRON_API_KEY"] = os.environ["NVIDIA_BUILD_API_KEY"]
    os.environ["NEMOTRON_MODEL_NAME"] = os.environ.get(
        "NVIDIA_BUILD_MODEL_NAME", "nvidia/nemotron-3-super-120b-a12b"
    )
os.environ["MODEL_BACKEND"] = "nemotron"

ENDPOINT = os.environ["NEMOTRON_ENDPOINT_URL"].rstrip("/")
if ENDPOINT.endswith("/chat/completions"):
    ENDPOINT = ENDPOINT[: -len("/chat/completions")]
API_KEY = os.environ["NEMOTRON_API_KEY"]
MODEL = os.environ.get("NEMOTRON_MODEL_NAME", "nvidia/nemotron-3-super-120b-a12b")

SEPARATOR = "=" * 70


def banner(title: str) -> None:
    print(f"\n{SEPARATOR}\n{title}\n{SEPARATOR}")


def probe_1_plain_chat() -> bool:
    """Baseline: does the endpoint answer a plain chat completion?"""
    banner("Probe 1 [Chat Completions API]: plain call (no tools)")
    print("  POST /v1/chat/completions  (openai.OpenAI().chat.completions.create)")
    from openai import OpenAI

    client = OpenAI(base_url=ENDPOINT, api_key=API_KEY)
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "Reply concisely."},
                {"role": "user", "content": "What is 2 + 2? Answer with a single digit."},
            ],
            max_tokens=200,
        )
        msg = resp.choices[0].message.content or ""
        print(f"  raw response: {msg!r}")
        print("  PASS: endpoint is reachable, model name accepted, key valid")
        return True
    except Exception as e:
        print(f"  FAIL: {e}")
        return False


def probe_2_direct_tool_calling() -> bool:
    """Skip the Agent Framework — call the OpenAI SDK directly with a tool
    schema. Isolates whether the failure is server-side (NIM missing
    --enable-auto-tool-choice) vs. client abstraction."""
    banner("Probe 2 [Chat Completions API]: direct OpenAI SDK + 1 tool")
    print("  POST /v1/chat/completions  (openai.OpenAI().chat.completions.create + tools)")
    from openai import OpenAI

    client = OpenAI(base_url=ENDPOINT, api_key=API_KEY)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup_policy",
                "description": "Look up an auto insurance policy by VIN or policy number.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "policy_number": {
                            "type": "string",
                            "description": "Policy number e.g. POL-2025-44912",
                        },
                    },
                    "required": ["policy_number"],
                },
            },
        }
    ]
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "Use the lookup_policy tool when the user asks about a policy."},
                {"role": "user", "content": "What vehicle is on policy POL-2025-44912?"},
            ],
            tools=tools,
            tool_choice="auto",
            max_tokens=200,
        )
        choice = resp.choices[0]
        tcalls = choice.message.tool_calls or []
        if tcalls:
            tc = tcalls[0]
            print(f"  tool_call: {tc.function.name}({tc.function.arguments})")
            print("  PASS: server emitted a tool call — tool calling is working")
            return True
        print(f"  WARN: no tool calls; raw content: {choice.message.content!r}")
        print("  PARTIAL: server accepted tools but didn't call one")
        return False
    except Exception as e:
        # Surface the server-side error code/message verbatim — that's what
        # tells us whether the NIM needs to be re-deployed with extra flags.
        msg = str(e)
        if "enable-auto-tool-choice" in msg or "tool-call-parser" in msg:
            print(f"  SERVER_BLOCKED: NIM is missing tool-calling flags")
            print(f"  Full error: {msg}")
            print()
            print("  Fix: redeploy the NIM with vLLM args:")
            print("    --enable-auto-tool-choice")
            print("    --tool-call-parser <hermes|llama3_json|nemotron>")
            return False
        print(f"  FAIL: {msg}")
        return False


async def probe_3_full_agent_with_tools() -> bool:
    """End-to-end through Agent Framework with the four insurance tools.
    This is what the deployed agent would actually do at request time."""
    banner("Probe 3 [Chat Completions API]: Agent Framework + 4 tools")
    print("  POST /v1/chat/completions  (agent_framework.openai.OpenAIChatCompletionClient)")

    from agent_framework import Agent

    from main import _build_chat_client
    from tools import (
        draft_claim_pdf,
        estimate_repair_cost,
        lookup_policy,
        vss_analyze_video,
    )

    client = _build_chat_client()
    print(f"  client type: {type(client).__name__}")

    instructions = (HERE / "instructions.md").read_text()
    agent = Agent(
        client=client,
        instructions=instructions,
        tools=[vss_analyze_video, lookup_policy, estimate_repair_cost, draft_claim_pdf],
        default_options={"store": False},
    )

    # End-to-end insurance prompt: triggers lookup_policy → vss_analyze_video
    # (mocked) → estimate_repair_cost → draft_claim_pdf. Mirrors what the
    # Streamlit UI sends to the deployed agent.
    prompt = (
        "I'm filing a claim for policy POL-2025-44912. The damage video is "
        "stored under video_id 'toyota'. Please run the full triage: "
        "look up the policy, analyze the damage, estimate cost, and draft "
        "the claim PDF."
    )
    print(f"  prompt: {prompt[:80]}...")
    try:
        response = await agent.run(prompt)
        text = response.text if hasattr(response, "text") else str(response)
        print(f"  response (truncated): {text[:400]}...")
        print("  PASS: full insurance flow ran")
        return True
    except Exception as e:
        msg = str(e)
        if "enable-auto-tool-choice" in msg or "tool-call-parser" in msg:
            print("  SERVER_BLOCKED: same NIM tool-calling flag issue as probe 2")
        else:
            print(f"  FAIL: {msg}")
            traceback.print_exc()
        return False


# ---------------------------------------------------------------------------
# Responses API suite — same three escalations as the Chat-Completions suite
# above, but hit /v1/responses instead of /v1/chat/completions. Lets us tell
# whether the endpoint supports the newer Responses surface (gpt-4.1 yes,
# most NIMs no as of writing) and whether our agent code works through it.
# ---------------------------------------------------------------------------


def probe_4_plain_responses() -> bool:
    """Same as probe 1 but uses POST /v1/responses (OpenAI's Responses API)."""
    banner("Probe 4 [Responses API]: plain call (no tools)")
    print("  POST /v1/responses  (openai.OpenAI().responses.create)")
    from openai import OpenAI

    client = OpenAI(base_url=ENDPOINT, api_key=API_KEY)
    try:
        resp = client.responses.create(
            model=MODEL,
            input="What is 2 + 2? Answer with a single digit.",
        )
        text = getattr(resp, "output_text", None) or str(resp.output)
        print(f"  raw response: {text!r}")
        print("  PASS: endpoint supports the Responses API")
        return True
    except Exception as e:
        msg = str(e)
        if "404" in msg or "Not Found" in msg or "does not exist" in msg:
            print(f"  NOT_SUPPORTED: endpoint does not implement /v1/responses")
            print(f"  Full error: {msg}")
            return False
        print(f"  FAIL: {msg}")
        return False


def probe_5_direct_responses_tool_calling() -> bool:
    """Responses API + one tool schema, no Agent Framework."""
    banner("Probe 5 [Responses API]: direct OpenAI SDK + 1 tool")
    print("  POST /v1/responses  (openai.OpenAI().responses.create + tools)")
    from openai import OpenAI

    client = OpenAI(base_url=ENDPOINT, api_key=API_KEY)
    # Responses-API tool schema is flat (no nested "function" wrapper as in
    # chat completions); type is "function" at the top level.
    tools = [
        {
            "type": "function",
            "name": "lookup_policy",
            "description": "Look up an auto insurance policy by VIN or policy number.",
            "parameters": {
                "type": "object",
                "properties": {
                    "policy_number": {
                        "type": "string",
                        "description": "Policy number e.g. POL-2025-44912",
                    },
                },
                "required": ["policy_number"],
            },
        }
    ]
    try:
        resp = client.responses.create(
            model=MODEL,
            instructions="Use the lookup_policy tool when the user asks about a policy.",
            input="What vehicle is on policy POL-2025-44912?",
            tools=tools,
        )
        # Walk resp.output for function-call items.
        tool_calls = [item for item in resp.output if getattr(item, "type", "") == "function_call"]
        if tool_calls:
            tc = tool_calls[0]
            print(f"  tool_call: {tc.name}({tc.arguments})")
            print("  PASS: server emitted a tool call via Responses API")
            return True
        text = getattr(resp, "output_text", None)
        print(f"  WARN: no tool calls; raw text: {text!r}")
        print("  PARTIAL: server accepted tools but didn't call one")
        return False
    except Exception as e:
        msg = str(e)
        if "404" in msg or "Not Found" in msg:
            print(f"  NOT_SUPPORTED: endpoint does not implement /v1/responses")
            return False
        if "enable-auto-tool-choice" in msg or "tool-call-parser" in msg:
            print(f"  SERVER_BLOCKED: NIM is missing tool-calling flags")
            return False
        print(f"  FAIL: {msg}")
        return False


async def probe_6_full_agent_via_responses() -> bool:
    """Full Agent Framework run — but using OpenAIChatClient (Responses
    variant) instead of OpenAIChatCompletionClient. Tests the deployed-style
    path since Foundry hosted agents are themselves served via Responses."""
    banner("Probe 6 [Responses API]: Agent Framework + 4 tools")
    print("  POST /v1/responses  (agent_framework.openai.OpenAIChatClient)")

    from agent_framework import Agent
    from agent_framework.openai import OpenAIChatClient

    from tools import (
        draft_claim_pdf,
        estimate_repair_cost,
        lookup_policy,
        vss_analyze_video,
    )

    client = OpenAIChatClient(
        model=MODEL,
        base_url=ENDPOINT,
        api_key=API_KEY,
    )
    print(f"  client type: {type(client).__name__}")

    instructions = (HERE / "instructions.md").read_text()
    agent = Agent(
        client=client,
        instructions=instructions,
        tools=[vss_analyze_video, lookup_policy, estimate_repair_cost, draft_claim_pdf],
        default_options={"store": False},
    )

    prompt = (
        "I'm filing a claim for policy POL-2025-44912. The damage video is "
        "stored under video_id 'toyota'. Please run the full triage: "
        "look up the policy, analyze the damage, estimate cost, and draft "
        "the claim PDF."
    )
    print(f"  prompt: {prompt[:80]}...")
    try:
        response = await agent.run(prompt)
        text = response.text if hasattr(response, "text") else str(response)
        print(f"  response (truncated): {text[:400]}...")
        print("  PASS: full insurance flow ran via Responses API")
        return True
    except Exception as e:
        msg = str(e)
        if "404" in msg or "Not Found" in msg:
            print(f"  NOT_SUPPORTED: endpoint does not implement /v1/responses")
        elif "enable-auto-tool-choice" in msg or "tool-call-parser" in msg:
            print("  SERVER_BLOCKED: same NIM tool-calling flag issue")
        else:
            print(f"  FAIL: {msg}")
            traceback.print_exc()
        return False


async def main() -> int:
    # api: 'chat' (probes 1-3), 'responses' (4-6), or 'both' (default).
    api_arg = (sys.argv[2] if len(sys.argv) > 2 else "both").strip().lower()
    if api_arg not in ("chat", "responses", "both"):
        print(f"unknown api {api_arg!r}; expected 'chat', 'responses', or 'both'")
        return 2

    print(f"Provider: {PROVIDER}")
    print(f"API:      {api_arg}")
    print(f"Endpoint: {ENDPOINT}")
    print(f"Model:    {MODEL}")
    print(f"Key:      {API_KEY[:8]}...{API_KEY[-4:]}")

    results: dict[str, bool] = {}
    if api_arg in ("chat", "both"):
        results["1: Chat Completions /v1/chat/completions — plain"] = probe_1_plain_chat()
        results["2: Chat Completions /v1/chat/completions — direct tool"] = probe_2_direct_tool_calling()
        results["3: Chat Completions /v1/chat/completions — agent + 4 tools"] = await probe_3_full_agent_with_tools()
    if api_arg in ("responses", "both"):
        results["4: Responses /v1/responses — plain"] = probe_4_plain_responses()
        results["5: Responses /v1/responses — direct tool"] = probe_5_direct_responses_tool_calling()
        results["6: Responses /v1/responses — agent + 4 tools"] = await probe_6_full_agent_via_responses()

    banner("Summary")
    for name, ok in results.items():
        print(f"  Probe {name:58s} {'PASS' if ok else 'FAIL/BLOCKED'}")
    print()
    chat_results = {k: v for k, v in results.items() if k.startswith(("1", "2", "3"))}
    resp_results = {k: v for k, v in results.items() if k.startswith(("4", "5", "6"))}
    if chat_results and all(chat_results.values()):
        print("Chat Completions API (/v1/chat/completions): fully wired for the insurance agent.")
    elif chat_results:
        print("Chat Completions API (/v1/chat/completions): at least one probe failed — see output above.")
    if resp_results and all(resp_results.values()):
        print("Responses API (/v1/responses): fully wired for the insurance agent.")
    elif resp_results:
        print("Responses API (/v1/responses): at least one probe failed — see output above.")
    return 0 if any(results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
