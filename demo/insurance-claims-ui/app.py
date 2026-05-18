"""Insurance Claims Processing — Customer Rep Web UI.

Streamlit app that:
  1. Lists customer policies from policies.json (sidebar table + selector)
  2. Lets a customer rep upload a damage video for the selected policy
  3. PUTs the video to the VSS Agent (AKS) via VST presigned URL
  4. Calls the deployed Microsoft Foundry agent (insurance-claims-triage)
     with the video_id + policy number
  5. Parses the agent's structured response (regex)
  6. Renders the draft claim PDF locally using the parsed fields
  7. Embeds the PDF preview + offers a download button

Run:
    cd demo/insurance-claims-ui
    uv venv .venv && source .venv/bin/activate
    uv pip install -r requirements.txt
    az login                     # for DefaultAzureCredential
    streamlit run app.py
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import streamlit as st
from azure.identity import DefaultAzureCredential

# ---------------------------------------------------------------------------
# Config — override via env vars if endpoints change
# ---------------------------------------------------------------------------

HERE = Path(__file__).parent
POLICIES_PATH = HERE / "policies.json"
OUTPUT_DIR = HERE / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

VSS_BASE_URL = os.environ.get(
    "VSS_BASE_URL", "http://vss.104.45.71.11.nip.io"
).rstrip("/")

FOUNDRY_ENDPOINT = os.environ.get(
    "FOUNDRY_AGENT_ENDPOINT",
    "https://ai-account-rux2wabbpeht4.services.ai.azure.com/api/projects/ai-project-insurance-claims-foundry-dev/agents/insurance-claims-triage/endpoint/protocols/openai/responses?api-version=2025-11-15-preview",
)

FOUNDRY_TOKEN_SCOPE = "https://ai.azure.com/.default"

HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@st.cache_resource
def get_token_credential() -> DefaultAzureCredential:
    return DefaultAzureCredential()


@st.cache_data
def load_policies() -> list[dict]:
    return json.loads(POLICIES_PATH.read_text())


def upload_video_to_vss(filename: str, video_bytes: bytes) -> str:
    """Get a VST presigned upload URL, PUT the bytes, return the video_id."""
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        r = client.post(f"{VSS_BASE_URL}/api/v1/videos", json={"filename": filename})
        r.raise_for_status()
        upload_url = r.json()["url"]
        # VST URL is .../v1/storage/file/<video_id>/<timestamp>; the second-to-last
        # path segment is the canonical video_id the agent will reference.
        video_id = upload_url.rstrip("/").split("/")[-2]
        put = client.put(
            upload_url, content=video_bytes, headers={"Content-Type": "video/mp4"}
        )
        put.raise_for_status()
    return video_id


def call_foundry_agent(prompt: str) -> dict:
    """POST to the deployed Foundry agent's /responses endpoint.

    Returns the full Responses-API JSON. The agent's final text is at
    `output[-1].content[0].text` for completed responses.
    """
    token = get_token_credential().get_token(FOUNDRY_TOKEN_SCOPE).token
    payload = {"input": prompt, "stream": False}
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        r = client.post(
            FOUNDRY_ENDPOINT,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        r.raise_for_status()
    return r.json()


def extract_agent_text(resp_json: dict) -> str:
    """Pull the assistant's final text message from a Responses API result.

    The response shape is `output: [...]` where each item has `type` and
    `content`. The last item with `type == "message"` carries the user-facing
    text.
    """
    output = resp_json.get("output") or []
    for item in reversed(output):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    return c.get("text", "")
    # Fallback: stringify the whole thing so the user can see what happened.
    return json.dumps(resp_json, indent=2)


# Regexes for the agent's structured reply. The system prompt
# (instructions.md) demands this exact format, so the regexes are stable.
_CLAIM_RE = re.compile(r"Claim drafted:\s*(\S+)", re.IGNORECASE)
_VEH_RE = re.compile(r"Vehicle:\s*(.+?)\s*\(VIN\s*(\S+?)\)", re.IGNORECASE)
_DAMAGE_RE = re.compile(r"Damage:\s*(.+?)(?:\r?\n)", re.IGNORECASE)
_REPAIR_RE = re.compile(r"Estimated repair:\s*\$?([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_DEDUCT_RE = re.compile(r"Deductible:\s*\$?([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_PAYABLE_RE = re.compile(r"Payable:\s*\$?([\d,]+(?:\.\d+)?)", re.IGNORECASE)


# Phrases that strongly suggest the customer uploaded the wrong kind of video —
# something other than a damaged vehicle. We treat any match as a hard stop:
# the UI surfaces a clear "wrong video" message and does NOT generate a PDF.
_REJECT_PHRASES = (
    "no damage",
    "no damage visible",
    "no damage observed",
    "no vehicle",
    "no car",
    "not a car",
    "not a vehicle",
    "not a damage",
    "doesn't show a car",
    "does not show a car",
    "doesn't show a vehicle",
    "does not show a vehicle",
    "doesn't show vehicle damage",
    "does not show vehicle damage",
    "cannot find any damage",
    "cannot identify any damage",
    "unable to identify damage",
    "unable to detect damage",
    "please resubmit",
    "please advise the customer to resubmit",
    "wrong video",
    "invalid video",
)


def validate_claim_for_pdf(claim: dict, agent_text: str) -> tuple[bool, str]:
    """Sanity-check that the agent actually found vehicle damage to claim against.

    Returns (is_valid, reason). When `is_valid` is False, the UI surfaces the
    reason as a "we couldn't process this video" message and skips the PDF.
    """
    text_lower = (agent_text or "").lower()
    for phrase in _REJECT_PHRASES:
        if phrase in text_lower:
            return False, (
                f"VSS / agent indicated the video may not show vehicle damage "
                f"(found phrase: “{phrase}” in the reply)."
            )

    summary = (claim.get("damage_summary") or "").strip()
    repair_total = claim.get("repair_total")

    # Missing both the damage prose AND the cost is a strong signal the agent
    # didn't have enough to work with. (We accept either as evidence; e.g. a
    # genuine claim might have summary + 0-cost if everything is "minor".)
    if not summary and (repair_total is None or repair_total <= 0):
        return False, (
            "The agent returned no damage summary and no repair estimate. "
            "Either the video doesn't show vehicle damage or the agent "
            "couldn't parse the scene."
        )

    return True, ""


def parse_agent_summary(text: str, policy: dict) -> dict:
    """Extract claim fields from the agent's final-format summary."""

    def _money(s: str | None) -> float | None:
        if not s:
            return None
        try:
            return float(s.replace(",", ""))
        except ValueError:
            return None

    return {
        "claim_id": (m.group(1) if (m := _CLAIM_RE.search(text)) else ""),
        "vehicle": (m.group(1) if (m := _VEH_RE.search(text)) else policy["vehicle"]),
        "vin": (m.group(2) if (m := _VEH_RE.search(text)) else policy["vin"]),
        "damage_summary": (m.group(1) if (m := _DAMAGE_RE.search(text)) else ""),
        "repair_total": _money(m.group(1) if (m := _REPAIR_RE.search(text)) else None),
        "deductible": _money(m.group(1) if (m := _DEDUCT_RE.search(text)) else None)
        or float(policy.get("deductible_usd", 0)),
        "payable": _money(m.group(1) if (m := _PAYABLE_RE.search(text)) else None),
    }


def render_claim_pdf(claim: dict, policy: dict, full_agent_text: str) -> Path:
    """Render a one-page PDF from the parsed claim. Returns the file path."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    claim_id = claim.get("claim_id") or "CLM-DRAFT"
    path = OUTPUT_DIR / f"{claim_id}.pdf"

    # Coerce numeric fields to floats with safe defaults — the regex parser
    # returns None when the agent's reply doesn't carry that line, and we
    # can't `:,.2f`-format a None.
    repair_total = float(claim.get("repair_total") or 0)
    deductible = float(claim.get("deductible") or policy.get("deductible_usd", 0) or 0)
    payable = claim.get("payable")
    if payable is None:
        payable = max(repair_total - deductible, 0)
    else:
        payable = float(payable)

    lines = [
        ("Heading", f"INSURANCE CLAIM — DRAFT"),
        ("", ""),
        ("", f"Claim ID:        {claim_id}"),
        ("", f"Date filed:      {datetime.now(timezone.utc).isoformat(timespec='seconds')}"),
        ("", f"Customer:        {policy['customer_name']}"),
        ("", f"Email:           {policy.get('customer_email', '')}"),
        ("", f"Policy:          {policy['policy_number']} ({policy['coverage_tier']})"),
        ("", f"Vehicle:         {claim['vehicle']}"),
        ("", f"VIN:             {claim['vin']}"),
        ("", ""),
        ("Heading", "Damage Summary"),
        ("", claim.get("damage_summary", "(none)")),
        ("", ""),
        ("Heading", "Cost"),
        ("", f"  Estimated repair:  ${repair_total:,.2f}"),
        ("", f"  Deductible:        ${deductible:,.2f}"),
        ("", f"  Payable:           ${payable:,.2f}"),
        ("", ""),
        ("", "Status: DRAFT — pending adjuster review."),
        ("", ""),
        ("Heading", "Agent Trace (verbatim)"),
    ]

    c = canvas.Canvas(str(path), pagesize=LETTER)
    y = 750
    for kind, line in lines:
        font = "Helvetica-Bold" if kind == "Heading" else "Helvetica"
        c.setFont(font, 11 if kind == "Heading" else 10)
        c.drawString(50, y, line[:120])
        y -= 16
        if y < 80:
            c.showPage()
            y = 750
    # Append the agent's verbatim text in a smaller font
    c.setFont("Helvetica", 8)
    for raw in full_agent_text.splitlines():
        # word-wrap-ish: keep lines under 120 chars
        for chunk_start in range(0, len(raw), 120):
            c.drawString(50, y, raw[chunk_start : chunk_start + 120])
            y -= 11
            if y < 60:
                c.showPage()
                c.setFont("Helvetica", 8)
                y = 750
    c.save()
    return path


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Insurance Claims Triage",
    page_icon="🚗",
    layout="wide",
)

st.title("🚗 Insurance Claims Triage")
st.caption(
    "Customer rep portal. Upload a damage video → VSS analyzes it on AKS → "
    "Foundry agent orchestrates policy lookup + cost estimate + claim draft."
)

policies = load_policies()

# ---- Sidebar: customer / policy list ------------------------------------

with st.sidebar:
    st.header("Customer Policies")
    st.dataframe(
        [
            {
                "Policy": p["policy_number"],
                "Customer": p["customer_name"],
                "Vehicle": p["vehicle"],
                "Deductible": f"${p['deductible_usd']}",
            }
            for p in policies
        ],
        hide_index=True,
        width="stretch",
    )

    st.markdown("---")
    st.subheader("Service endpoints")
    st.code(f"VSS:     {VSS_BASE_URL}", language="text")
    short = FOUNDRY_ENDPOINT.split("/api/projects/")[-1].split("/agents/")
    if len(short) == 2:
        st.code(
            f"Foundry: {short[0]}\n"
            f"Agent:   {short[1].split('/endpoint')[0]}",
            language="text",
        )

# ---- Main: claim filing form --------------------------------------------

left, right = st.columns([1, 1])

with left:
    st.subheader("File a claim")
    selected_id = st.selectbox(
        "Customer policy",
        options=[p["policy_number"] for p in policies],
        format_func=lambda pn: next(
            f"{p['policy_number']} — {p['customer_name']} ({p['vehicle']})"
            for p in policies
            if p["policy_number"] == pn
        ),
    )
    selected = next(p for p in policies if p["policy_number"] == selected_id)

    with st.expander("Policy details", expanded=True):
        st.json(selected)

    video_file = st.file_uploader(
        "Damage video", type=["mp4", "mov", "m4v"], accept_multiple_files=False
    )

    submit = st.button(
        "Submit claim ↗",
        type="primary",
        disabled=video_file is None,
        width="stretch",
    )

with right:
    st.subheader("Result")
    placeholder = st.empty()
    if not submit:
        placeholder.info("Fill out the form on the left and submit to file a claim.")

if submit and video_file:
    with right:
        with st.status("Filing claim…", expanded=True) as status:
            try:
                st.write(f"📤 Uploading **{video_file.name}** to VSS…")
                video_id = upload_video_to_vss(video_file.name, video_file.getvalue())
                st.success(f"video_id = `{video_id}`")

                prompt = (
                    f"A customer just submitted a damage video. "
                    f"video_id={video_id}. "
                    f"If the VIN is not visible, use policy "
                    f"{selected['policy_number']} as a fallback. "
                    f"Run the triage workflow."
                )
                st.write("🤖 Sending this prompt to the Foundry agent:")
                st.code(prompt, language="text")
                with st.spinner("Agent thinking (VSS analysis dominates ~30-60s)…"):
                    raw_resp = call_foundry_agent(prompt)

                agent_text = extract_agent_text(raw_resp)
                st.write("✅ Agent finished. Parsing reply…")

                claim = parse_agent_summary(agent_text, selected)

                # Reject videos that don't show vehicle damage — skip PDF.
                is_valid, reject_reason = validate_claim_for_pdf(claim, agent_text)
                pdf_path = None
                if not is_valid:
                    status.update(
                        label="Video rejected — not a vehicle-damage clip.",
                        state="error",
                    )
                else:
                    pdf_path = render_claim_pdf(claim, selected, agent_text)
                    status.update(
                        label=f"Done — claim {claim.get('claim_id') or 'DRAFT'} drafted.",
                        state="complete",
                    )
            except httpx.HTTPStatusError as e:
                status.update(label="HTTP error from upstream service.", state="error")
                st.error(
                    f"Upstream returned {e.response.status_code}:\n```\n"
                    f"{e.response.text[:1000]}\n```"
                )
                st.stop()
            except Exception as e:
                status.update(label="Unexpected error.", state="error")
                st.exception(e)
                st.stop()

        # ---- Display results ------------------------------------------------
        if not is_valid:
            st.error(
                "⚠ **This video doesn't appear to show vehicle damage.**\n\n"
                f"{reject_reason}\n\n"
                "Please upload a clear video of the damaged vehicle — "
                "ideally a slow walk-around with the camera focused on each "
                "affected panel."
            )

        st.markdown("### Agent reply")
        st.code(agent_text, language="text")

        st.markdown("### Parsed claim fields")
        st.json(claim)

        if not is_valid:
            # Skip PDF rendering entirely — show the reason instead.
            st.info(
                "No draft PDF was generated because the video failed validation. "
                "Adjust the upload and resubmit to try again."
            )
            st.stop()

        st.markdown("### Draft PDF")
        pdf_bytes = pdf_path.read_bytes()
        st.download_button(
            "⬇ Download PDF",
            data=pdf_bytes,
            file_name=pdf_path.name,
            mime="application/pdf",
            width="stretch",
        )
        b64 = base64.b64encode(pdf_bytes).decode()
        st.markdown(
            f'<iframe src="data:application/pdf;base64,{b64}" '
            f'width="100%" height="700" type="application/pdf"></iframe>',
            unsafe_allow_html=True,
        )
