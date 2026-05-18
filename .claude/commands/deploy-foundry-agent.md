---
description: Deploy the Microsoft Foundry hosted agent (insurance-claims-triage) and smoke-test the new version
---

Deploy the **Microsoft Foundry hosted agent** (`insurance-claims-triage`) from
[demo/insurance-claims-foundry/](demo/insurance-claims-foundry/), then verify
that the new version actually shipped.

## What to do

1. **`cd demo/insurance-claims-foundry`** (the azd project root — `azure.yaml`
   must be in cwd or `azd` errors with "no project exists").

2. **Capture the current version** so we can confirm the bump:
   ```bash
   azd env get-values | grep AGENT_INSURANCE_CLAIMS_TRIAGE_VERSION
   ```
   Remember this number (call it `V_BEFORE`).

3. **Pre-flight check**: the postdeploy RBAC hook will 404 (and every future
   invoke will return `PermissionDenied`) if `azure.yaml`'s `services.<name>:`
   key drifts from `agent.yaml`'s top-level `name:`. Both should be
   `insurance-claims-triage`. Bail out if they don't match.

4. **Run the deploy** (long-running, ~2 min — use background execution so the
   harness notifies on completion rather than polling):
   ```bash
   azd deploy
   ```
   Watch for the `SUCCESS: Your application was deployed...` line. If you see
   `ContainerAppOperationError` or `failed to fetch agent version`, do NOT
   re-deploy automatically — surface the error to the user.

5. **Verify the version bumped**:
   ```bash
   azd env get-values | grep AGENT_INSURANCE_CLAIMS_TRIAGE_VERSION
   ```
   Should be `V_BEFORE + 1`. If it didn't change, deploy silently no-op'd —
   investigate before declaring success.

6. **Smoke-test the deployed agent** (not `--local`):
   ```bash
   azd ai agent invoke --new-conversation "$ARGUMENTS"
   ```
   If `$ARGUMENTS` is empty, fall back to a default policy-lookup probe that
   exercises the freshly-baked `policies.json`:
   ```bash
   azd ai agent invoke --new-conversation \
     "Look up policy POL-2025-44912. What vehicle is on that policy?"
   ```

7. **Report to the user**:
   - The version bump (`vN → vN+1`)
   - The full agent response text
   - The portal playground URL (printed by `azd deploy`) and the Responses
     endpoint URL — both as fully-qualified `https://` links
   - Whether the smoke-test answer matches the source-of-truth
     `demo/insurance-claims-foundry/.../sample_data/policies.json`; flag any
     drift (e.g., if the agent says "Honda Civic" but the JSON says "Fiat
     Stilo", the deploy didn't pick up the latest code).

## Notes

- The hosted-agent platform requires the caller (you / the azd identity) to
  have either Owner or User Access Administrator at sub scope, plus
  Contributor for provision. The current azd env (`insurance-claims-foundry-dev`)
  was originally provisioned by the user under their CLI session.
- Companion command `/deploy-ui` redeploys the Streamlit Container App when
  the UI code changes — keep them in sync when both have updates.
- See [CLAUDE.md](CLAUDE.md) for the wider deploy context: the standardized
  `AZURE_OPENAI_DEPLOYMENT` env var, the gpt-4.1 model choice, and the
  cross-RG role assignment from the UI deployment.
