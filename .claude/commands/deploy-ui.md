---
description: Deploy the Streamlit customer-rep UI (Azure Container App) and smoke-test the live URL
---

Deploy the **Streamlit customer-rep UI** as an Azure Container App from
[demo/insurance-claims-ui/](demo/insurance-claims-ui/), then verify the live
URL is responding and the new revision picked up.

## What to do

1. **`cd demo/insurance-claims-ui`** (the azd project root — `azure.yaml`
   must be in cwd or `azd` errors with "no project exists").

2. **Verify the azd env exists** (it should be `insurance-claims-ui-dev`
   from the initial provision):
   ```bash
   azd env list
   ```
   If it doesn't exist, the UI hasn't been provisioned yet — bail out and
   tell the user to run the full `azd up` flow (or invoke the
   `azure:azure-prepare` skill) before this command can be useful.

3. **Pre-flight checks**:
   - Confirm the Dockerfile copies `sample-videos/` (the bundled-video
     feature breaks silently in the cloud if those files aren't shipped).
   - Confirm the `policies.json` paths in `sample_video` fields point at
     `sample-videos/<name>.mp4` (relative to the Dockerfile WORKDIR).

4. **Capture the current image tag** so we can confirm the new revision
   actually replaced it:
   ```bash
   CURRENT=$(az containerapp revision list \
     -g rg-insurance-claims-ui-dev \
     -n $(azd env get-values | grep SERVICE_UI_NAME | cut -d'"' -f2) \
     --query "[?properties.active].name" -o tsv | head -1)
   echo "Active revision before deploy: $CURRENT"
   ```

5. **Run the deploy** (long-running, ~2-3 min for ACR remote build + revision
   roll — use background execution so the harness notifies on completion):
   ```bash
   azd deploy
   ```
   Watch for `SUCCESS: Your application was deployed...` and capture the
   `Endpoint:` URL printed below it.

6. **Verify the new revision activated**:
   ```bash
   az containerapp revision list \
     -g rg-insurance-claims-ui-dev \
     -n <SERVICE_UI_NAME> \
     --query "[?properties.active].name" -o tsv
   ```
   Should differ from `$CURRENT`. If not, the deploy didn't roll a new
   revision (image identical to previous, or revision-mode misconfigured) —
   investigate before declaring success.

7. **Smoke-test the live URL**:
   ```bash
   URL=$(azd env get-values | grep SERVICE_UI_URI | cut -d'"' -f2)
   curl -s -o /dev/null -w "root: HTTP %{http_code}\n" --max-time 30 "$URL"
   curl -s --max-time 30 "$URL/_stcore/health"   # Streamlit health probe — should return "ok"
   ```
   - HTTP 200 on root = ingress + container alive
   - `/_stcore/health` returning `ok` = Streamlit process is up (vs. a generic
     hello-world placeholder still serving)
   - **Cold-start note**: scale-to-zero is enabled, so the first request after
     idle may take ~5-10s. Don't treat one slow response as a failure — retry
     once before flagging.

8. **Report to the user**:
   - The revision change (`<old> → <new>`)
   - The live HTTPS URL (always prefix `https://`)
   - HTTP status + Streamlit health-probe result
   - A reminder that `policies.json` is baked into the image — if they
     edited it locally but didn't see the change, the image needs another
     `azd deploy` (this command, again).

## Notes

- The UI calls the Foundry hosted agent via the container's user-assigned
  managed identity (system-assigned MI failed with a 20-min ACR token race —
  see [[feedback-aca-acr-bootstrap]] in memory and the resources.bicep
  comments). The MI has `Foundry User` on `ai-account-rux2wabbpeht4` via
  the cross-RG role assignment in `infra/modules/foundry-rbac.bicep`.
- Sample videos under `sample-videos/` ship inside the image (15-30 MB each
  for `toyota.mp4` and `fiat.mp4`). Future videos go in the same directory
  and `policies.json`.
- Companion command `/deploy-foundry-agent` redeploys the hosted agent —
  use it when `policies.json` or the agent code changed. The two artifacts
  are usually deployed together.
- See [CLAUDE.md](CLAUDE.md) for the wider context: the standardized
  `AZURE_OPENAI_DEPLOYMENT` env var, the gpt-4.1 model choice, and the
  Container Apps + ACR bootstrap pitfall.
