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


def main():
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    )

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
