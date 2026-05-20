# Copyright (c) Microsoft. All rights reserved.
"""Insurance Claims Triage — Microsoft Foundry hosted agent.

Scaffolded by `azd ai agent init`. Extended to:
  - Load instructions from instructions.md (separate file for prompt-optimizer)
  - Register four function tools from tools.py (1 VSS sub-agent + 3 business)
"""

import os
from pathlib import Path

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework.observability import enable_instrumentation
from agent_framework.openai import OpenAIChatCompletionClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from tools import (
    draft_claim_pdf,
    estimate_repair_cost,
    lookup_policy,
    vss_analyze_video,
)

# Load environment variables from .env file (when running locally)
load_dotenv()


def _setup_observability() -> None:
    # Ship agent-framework GenAI spans/metrics (token usage, tool calls, run
    # durations) to the App Insights instance backing this Foundry project so
    # the agent's Monitor dashboard cards populate. The hosted runtime already
    # injects APPLICATIONINSIGHTS_CONNECTION_STRING; for local `azd ai agent
    # run` this is unset and we skip silently.
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn:
        return
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError:
        return
    configure_azure_monitor(connection_string=conn)
    enable_instrumentation()


def _build_chat_client():
    # MODEL_BACKEND=nemotron routes the agent to a Nemotron NIM deployment
    # exposed as an OpenAI-compatible /v1/chat/completions endpoint (e.g. the
    # NVIDIA Nemotron-3-Super-120B-A12B NIM hosted on Microsoft Foundry).
    # Default (foundry) keeps the existing gpt-4.1 path via FoundryChatClient.
    backend = os.environ.get("MODEL_BACKEND", "foundry").strip().lower()
    if backend == "nemotron":
        base_url = os.environ["NEMOTRON_ENDPOINT_URL"].rstrip("/")
        # The OpenAI SDK appends "/chat/completions"; strip it if present.
        if base_url.endswith("/chat/completions"):
            base_url = base_url[: -len("/chat/completions")]
        return OpenAIChatCompletionClient(
            model=os.environ.get("NEMOTRON_MODEL_NAME", "nvidia/nemotron-3-super-120b-a12b"),
            base_url=base_url,
            api_key=os.environ["NEMOTRON_API_KEY"],
        )
    return FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        credential=DefaultAzureCredential(),
    )


def main():
    _setup_observability()
    client = _build_chat_client()

    instructions = (Path(__file__).parent / "instructions.md").read_text()

    agent = Agent(
        client=client,
        instructions=instructions,
        tools=[
            vss_analyze_video,
            lookup_policy,
            estimate_repair_cost,
            draft_claim_pdf,
        ],
        # History is managed by the hosting infrastructure, not by the agent.
        # See https://developers.openai.com/api/reference/resources/responses/methods/create
        default_options={"store": False},
    )

    server = ResponsesHostServer(agent)
    server.run()


if __name__ == "__main__":
    main()
