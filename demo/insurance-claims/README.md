# Insurance Claims Triage — VSS as a sub-agent

A working scaffold for an end-to-end insurance claims demo where:

- The **NVIDIA VSS Agent** (running on your AKS cluster) acts as a
  specialized video-understanding sub-agent.
- A **Microsoft Foundry master agent** orchestrates the full workflow:
  policy lookup → damage analysis (delegated to VSS) → cost estimation →
  claim PDF.
- VSS is integrated via **MCP** so the same wrapper works with Foundry,
  Claude, OpenAI Agents SDK, etc.

See [architecture.md](architecture.md) for the sequence diagram and design
rationale.

## Files

| File | What it is |
|---|---|
| [vss_mcp_server.py](vss_mcp_server.py) | MCP server wrapping the VSS Agent (`vss_upload_video`, `vss_analyze_video`) |
| [tools.py](tools.py) | Mock Python tools — policy lookup, repair-cost estimator, claim-PDF writer |
| [agent_instructions.md](agent_instructions.md) | System prompt for the master agent (Foundry-deployable as-is) |
| [agent.yaml](agent.yaml) | Microsoft Foundry hosted-agent definition |
| [local_runner.py](local_runner.py) | Smoke-test harness using Anthropic SDK — no Foundry required |
| [foundry_deploy.md](foundry_deploy.md) | Step-by-step Foundry deployment guide |
| [sample_data/policies.json](sample_data/policies.json) | Three mock policies (one VIN match per supported demo video) |

## Prerequisites

- The VSS LVS blueprint deployed and reachable. Verify by hitting
  `http://vss.<EXTERNAL_HOST>.nip.io/` from your shell — should return HTTP 200.
  (Setup steps for that live in [`../../README.md`](../../README.md).)
- Python 3.11+
- Azure OpenAI access to a deployed chat model. Configure via env vars:
  - `AZURE_OPENAI_ENDPOINT` — `https://<account>.cognitiveservices.azure.com/`
  - `AZURE_OPENAI_DEPLOYMENT` — e.g. `gpt-4.1`
  - `AZURE_OPENAI_API_KEY` *(optional)* — if unset, `az login` + `DefaultAzureCredential` is used

## Quick start — smoke test locally (uses Azure OpenAI `gpt-5.4`)

```bash
cd demo/insurance-claims

pip install -r requirements.txt

# Required — point at your VSS deployment + your Azure OpenAI model
export VSS_BASE_URL=http://vss.<EXTERNAL_HOST>.nip.io
export AZURE_OPENAI_ENDPOINT=https://<account>.cognitiveservices.azure.com/
export AZURE_OPENAI_DEPLOYMENT=gpt-4.1

# Option A: API-key auth (simplest)
export AZURE_OPENAI_API_KEY=$(az cognitiveservices account keys list \
  -g <YOUR_RG> -n <YOUR_AI_SERVICES_ACCOUNT> --query key1 -o tsv)

# Option B: Entra credentials — leave AZURE_OPENAI_API_KEY unset and just
# `az login` first. The runner falls back to DefaultAzureCredential.

# Use any short MP4 — a car damage walk-around is ideal. The VIN visible in
# any frame should match a policy in sample_data/policies.json (use a sticker
# or edit policies.json to add your VIN).
python local_runner.py path/to/damage.mp4
```

The runner prints the agent's reasoning trace, every tool call, and the final
draft-claim summary. A PDF (or .txt fallback) is written to `./output/`.

> **Want to use public OpenAI** (api.openai.com) instead? Set
> `USE_PUBLIC_OPENAI=1`, `OPENAI_API_KEY=...`, and `OPENAI_MODEL=gpt-4o`
> (or any other tool-use-capable model).

## Quick start — deploy to Foundry

See [foundry_deploy.md](foundry_deploy.md). TL;DR:

1. Containerize (Dockerfile snippet in the deploy guide).
2. Push to your Foundry-attached ACR.
3. Register the agent via the Foundry SDK or the `azure:microsoft-foundry`
   skill (recommended — it handles auth + project plumbing).
4. Send a test message; watch the trace in the Foundry portal.

## What to expect on the first call

When the master agent gets `"video_id=claim-test-1, run triage"`:

1. It calls `vss_analyze_video(video_id, question)` with a strict JSON-only
   prompt. VSS runs cosmos-reason2-8b against the video frames and replies
   with `{"vin": ..., "damage": [...]}`.
2. The master agent calls `lookup_policy(vin=...)`. With the bundled mock,
   `4T1G11AK7NU012345` returns the Alex Romero / 2022 Camry policy.
3. `estimate_repair_cost(damage_items=...)` returns a parts+labor breakdown.
4. `draft_claim_pdf(...)` writes `output/CLM-YYYYMMDD-44912.pdf`.
5. The agent replies with a one-screen summary including the payable
   amount after deductible.

Roughly 30–60 seconds end-to-end depending on video length (most of it is
the VLM pass on cosmos).

## Adapting the demo

- **Swap the master model**: edit `agent.yaml` `model.deployment_name`. Any
  Foundry-supported tool-use LLM works.
- **Swap the policy backend**: replace `lookup_policy` in `tools.py` with a
  call to your CRM. The function signature should stay the same so the
  agent's tool schema doesn't change.
- **Add new VSS capabilities**: add tools to `vss_mcp_server.py`. Good
  candidates from the LVS profile:
  - `vss_list_videos()` — show recent uploads
  - `vss_get_clip(video_id, start, end)` — pull a specific moment
  - `vss_search_alerts(query)` — query the Elasticsearch index VSS maintains
- **Add evals**: feed `(video_path, expected_damage_json)` pairs to Foundry's
  continuous-eval feature; the `azure:microsoft-foundry` skill scaffolds the
  dataset format.
