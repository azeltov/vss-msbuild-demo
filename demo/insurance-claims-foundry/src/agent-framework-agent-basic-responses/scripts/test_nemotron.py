"""Diagnostic: exercise tool-calling against the Nemotron NIM deployment.

Runs three escalating probes against the configured Nemotron endpoint:

  1. Plain chat completion — no tools, baseline reachability.
  2. Direct OpenAI SDK call with tools — bypasses Agent Framework so we
     can see exactly what the server returns. Useful for isolating whether
     a failure is in the agent abstraction or the NIM deployment itself.
  3. Agent Framework end-to-end — the real path main.py uses, with the
     four insurance-triage tools (lookup_policy, vss_analyze_video,
     estimate_repair_cost, draft_claim_pdf).

All VSS calls are mocked via fixtures/vss/<video_id>.txt (MOCK_VSS=true),
so this runs offline relative to AKS. Policy / pricing data also comes
from the in-repo mocks in tools.py.

Run:

    cd demo/insurance-claims-foundry/src/agent-framework-agent-basic-responses
    python scripts/test_nemotron.py

Reads NEMOTRON_ENDPOINT_URL, NEMOTRON_API_KEY, NEMOTRON_MODEL_NAME from
.env (gitignored). The script forces MODEL_BACKEND=nemotron + MOCK_VSS=true
regardless of what .env says, so it always targets the NIM path with
fixtures.

The script prints PASS / FAIL / SERVER_BLOCKED per probe and exits 0 if at
least the plain-chat probe works (so CI can tell the endpoint is reachable
even when tool calling is blocked at the server).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

# Force Nemotron + mock VSS before any imports that might read env.
os.environ["MODEL_BACKEND"] = "nemotron"
os.environ["MOCK_VSS"] = "true"

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.chdir(HERE)

from dotenv import load_dotenv

load_dotenv()

# Re-assert overrides — load_dotenv() won't override existing env, so the
# os.environ writes above stick. This is belt-and-suspenders.
os.environ["MODEL_BACKEND"] = "nemotron"
os.environ["MOCK_VSS"] = "true"

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
    banner("Probe 1: plain chat completion (no tools)")
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
    banner("Probe 2: direct OpenAI SDK call with a tool schema")
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
    banner("Probe 3: full Agent Framework with all 4 insurance tools")

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


async def main() -> int:
    print(f"Endpoint: {ENDPOINT}")
    print(f"Model:    {MODEL}")
    print(f"Key:      {API_KEY[:8]}...{API_KEY[-4:]}")

    p1 = probe_1_plain_chat()
    p2 = probe_2_direct_tool_calling()
    p3 = await probe_3_full_agent_with_tools()

    banner("Summary")
    print(f"  Probe 1 (plain chat):           {'PASS' if p1 else 'FAIL'}")
    print(f"  Probe 2 (direct tool calling):  {'PASS' if p2 else 'BLOCKED/FAIL'}")
    print(f"  Probe 3 (agent + 4 tools):      {'PASS' if p3 else 'BLOCKED/FAIL'}")
    print()
    if p1 and not p2:
        print("Verdict: endpoint reachable, but tool calling is server-blocked.")
        print("The deployed NIM needs --enable-auto-tool-choice + --tool-call-parser")
        print("set as container startup args. Until then this Nemotron deployment")
        print("cannot drive the insurance agent; use MODEL_BACKEND=foundry (gpt-4.1)")
        print("or redeploy a Nemotron NIM image with those flags baked in.")
    elif p1 and p2 and p3:
        print("Verdict: Nemotron is fully wired for the insurance agent.")
    return 0 if p1 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
