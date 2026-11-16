"""LLM reasoning layer (provider-pluggable: OpenAI or Anthropic).

The deterministic engine (engine.py) produces the facts; this module turns them
into investigator-ready narrative: a short summary, a working hypothesis, and a
refined next action. It also powers the per-case chat.

Provider selection (first match wins):
- OPENAI_API_KEY set     -> OpenAI (model: OPENAI_MODEL, default gpt-4o-mini)
- ANTHROPIC_API_KEY set  -> Anthropic (model: claude-opus-4-8)
- neither                -> deterministic template fallback (app stays usable)

Grounding rules:
- The LLM only ever sees the structured engine output + raw case fields.
- Risk score / tier / signal values shown in the UI come from the engine, never
  from the LLM, so a hallucinated number can't reach the investigator.
- Batch assessments use structured output (JSON schema) so responses parse
  deterministically.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

ANTHROPIC_MODEL = "claude-opus-4-8"
BATCH_SIZE = 5


def openai_model() -> str:
    return os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def provider() -> str:
    """'openai' | 'anthropic' | 'fallback'"""
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "fallback"


# Models tend to emit typographic punctuation (em dashes, curly quotes).
# Normalize everything user-facing to plain ASCII so generated text reads
# the same as the rest of the product copy.
_TYPOGRAPHY = {
    "—": " - ",   # em dash
    "–": "-",     # en dash
    "‘": "'", "’": "'",
    "“": '"', "”": '"',
    "…": "...",
    "·": ", ",    # middle dot
    "→": "->",
    " ": " ",
}


def _clean_text(s: str) -> str:
    for ch, repl in _TYPOGRAPHY.items():
        s = s.replace(ch, repl)
    while "  " in s:
        s = s.replace("  ", " ")
    return s.replace(" ,", ",").strip()


def _clean_assessment(a: dict) -> dict:
    for key in ("summary", "hypothesis", "next_action"):
        if isinstance(a.get(key), str):
            a[key] = _clean_text(a[key])
    return a

ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "summary": {"type": "string"},
                    "hypothesis": {"type": "string"},
                    "next_action": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["case_id", "summary", "hypothesis", "next_action", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["assessments"],
    "additionalProperties": False,
}

SYSTEM_ASSESS = """You are a senior insurance fraud analyst writing triage notes for human investigators.

You will receive structured case data: raw claim attributes, a deterministic risk score with
per-signal contributions, and any detected patterns. Your job is to narrate, not to re-score.

Rules:
- Ground every statement in the provided fields. Never invent values, providers, names, or events.
- Reference concrete signal values (e.g. "billed 146% above peers", "246 miles from the member").
- summary: 2-3 sentences a busy investigator can scan in ten seconds. Lead with the verdict.
- hypothesis: one sentence naming the most likely fraud scheme (or why it is likely benign).
- next_action: one concrete step. Stay consistent with the provided baseline action class
  (close / desk review / full investigation / escalate) but make it specific to this case's signals.
- confidence: how confident you are in the triage call given the available signals. Low-risk clean
  cases and saturated multi-signal cases are high confidence; mixed or single-signal cases are lower.
- Return one assessment object per input case, matching case_id exactly."""


def _case_payload(row: dict, analysis: dict, baseline_action: str) -> dict:
    return {
        "case_id": row["case_id"],
        "claim": {k: row[k] for k in
                  ("claim_number", "claim_date", "care_type", "claim_amount_usd", "state")},
        "risk_score_0_100": analysis["score"],
        "risk_tier": analysis["tier"],
        "signals": [
            {"label": c["label"], "value": c["value"], "normal_range": c["normal"],
             "severity_0_1": c["severity"]}
            for c in analysis["contributions"] if c["triggered"]
        ] or "all signals within normal range",
        "patterns": [{"name": p["name"], "detail": p["detail"]} for p in analysis["patterns"]],
        "baseline_action": baseline_action,
    }


# ---------------------------------------------------------------- fallback ---

def _fallback_assessment(row: dict, analysis: dict, baseline_action: str) -> dict:
    top = [c for c in analysis["contributions"] if c["triggered"]][:3]
    tier = analysis["tier"]
    if tier == "low" and not analysis["patterns"]:
        summary = (f"Routine {row['care_type']} claim of ${row['claim_amount_usd']:,.0f} in "
                   f"{row['state']} with all nine signals in normal range. "
                   "Consistent with a false-positive referral.")
        hypothesis = "No indicators of fraud; referral appears to be a false positive."
        confidence = "high"
    else:
        parts = []
        for c in top:
            if c["unit"] == "" and c["value"] in (0.0, 1.0) and c["normal"] == "not triggered":
                parts.append(c["label"].lower())
            else:
                parts.append(f"{c['label'].lower()} at {c['value']:g}"
                             f"{(' ' + c['unit']) if c['unit'] else ''} (normal: {c['normal']})")
        pat = analysis["patterns"][0]["name"].lower() if analysis["patterns"] else None
        summary = (f"{tier.capitalize()}-risk {row['care_type']} claim of "
                   f"${row['claim_amount_usd']:,.0f} ({row['state']}). "
                   f"Driven by {'; '.join(parts)}."
                   + (f" Pattern detected: {pat}." if pat else ""))
        hypothesis = (analysis["patterns"][0]["detail"] if analysis["patterns"]
                      else "Anomalous billing volume/amount without a hard fraud marker; "
                           "could be upcoding or legitimate intensive care.")
        confidence = "high" if analysis["score"] >= 70 or tier == "low" else "medium"
    return {
        "case_id": row["case_id"],
        "summary": summary,
        "hypothesis": hypothesis,
        "next_action": baseline_action,
        "confidence": confidence,
        "generated_by": "deterministic-fallback",
    }


# -------------------------------------------------------------- LLM calls ---

def _assess_batch_anthropic(client, batch: list[dict]) -> list[dict]:
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=16000,
        system=[{"type": "text", "text": SYSTEM_ASSESS,
                 "cache_control": {"type": "ephemeral"}}],
        output_config={"format": {"type": "json_schema", "schema": ASSESSMENT_SCHEMA}},
        messages=[{"role": "user", "content": json.dumps({"cases": batch})}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    out = [_clean_assessment(a) for a in json.loads(text)["assessments"]]
    for a in out:
        a["generated_by"] = ANTHROPIC_MODEL
    return out


def _assess_batch_openai(client, batch: list[dict]) -> list[dict]:
    response = client.chat.completions.create(
        model=openai_model(),
        messages=[
            {"role": "system", "content": SYSTEM_ASSESS},
            {"role": "user", "content": json.dumps({"cases": batch})},
        ],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "triage_assessments",
                                         "strict": True,
                                         "schema": ASSESSMENT_SCHEMA}},
    )
    out = [_clean_assessment(a)
           for a in json.loads(response.choices[0].message.content)["assessments"]]
    for a in out:
        a["generated_by"] = openai_model()
    return out


def _make_client():
    if provider() == "openai":
        from openai import OpenAI
        return OpenAI(), _assess_batch_openai
    import anthropic
    return anthropic.Anthropic(), _assess_batch_anthropic


def generate_assessments(rows: list[dict], analyses: dict, baseline_actions: dict,
                         progress=None) -> dict:
    """Return {case_id: assessment}. Uses the configured LLM provider,
    otherwise the deterministic fallback."""
    payloads = [_case_payload(r, analyses[r["case_id"]], baseline_actions[r["case_id"]])
                for r in rows]

    if provider() == "fallback":
        return {r["case_id"]: _fallback_assessment(r, analyses[r["case_id"]],
                                                   baseline_actions[r["case_id"]])
                for r in rows}

    client, assess_batch = _make_client()
    batches = [payloads[i:i + BATCH_SIZE] for i in range(0, len(payloads), BATCH_SIZE)]
    results: dict[str, dict] = {}

    def run(batch):
        try:
            return assess_batch(client, batch)
        except Exception as exc:  # fall back per-batch, never fail the triage
            print(f"[llm] batch failed ({exc}); using fallback for {len(batch)} cases")
            ids = {b["case_id"] for b in batch}
            return [_fallback_assessment(r, analyses[r["case_id"]],
                                         baseline_actions[r["case_id"]])
                    for r in rows if r["case_id"] in ids]

    with ThreadPoolExecutor(max_workers=3) as pool:
        for batch_result in pool.map(run, batches):
            for a in batch_result:
                results[a["case_id"]] = a
            if progress:
                progress(len(results), len(payloads))

    # Safety net: any case the model skipped gets the fallback.
    for r in rows:
        if r["case_id"] not in results:
            results[r["case_id"]] = _fallback_assessment(
                r, analyses[r["case_id"]], baseline_actions[r["case_id"]])
    return results


# ------------------------------------------------------------------- chat ---

SYSTEM_CHAT = """You are an AI co-investigator embedded in a fraud case-review tool, chatting with
a human investigator about ONE specific case. You have the case's raw data, the deterministic
risk analysis, the AI triage assessment, and summary context about the rest of the queue.

Rules:
- Answer only from the provided data. If asked something the data cannot answer (e.g. the
  provider's name, past investigations), say plainly that this information is not in the case
  file and suggest where the investigator could obtain it.
- Be concise and concrete: cite signal values when making a point.
- You may reason about what evidence would confirm or refute the fraud hypothesis.
- Never claim certainty of fraud; frame conclusions as findings to verify."""


def chat_reply(row: dict, analysis: dict, assessment: dict, queue_context: dict,
               history: list[dict], message: str) -> str:
    if provider() == "fallback":
        return ("LLM chat is not configured (set OPENAI_API_KEY or ANTHROPIC_API_KEY and "
                "restart to enable it). Deterministic facts for this case: risk score "
                f"{analysis['score']}/100 ({analysis['tier']}), triggered signals: "
                + (", ".join(f"{c['label']} = {c['value']:g}" for c in analysis["contributions"]
                             if c["triggered"]) or "none")
                + ". Recommended action: " + assessment["next_action"])

    context = {
        "case": {k: row[k] for k in row},
        "deterministic_analysis": analysis,
        "ai_assessment": {k: assessment[k] for k in
                          ("summary", "hypothesis", "next_action", "confidence")},
        "queue_context": queue_context,
    }
    turns = [{"role": "user",
              "content": "CASE FILE (JSON):\n" + json.dumps(context, default=str)},
             {"role": "assistant",
              "content": "Case file loaded. Ready to discuss this case."}]
    for turn in history[-20:]:
        turns.append({"role": turn["role"], "content": turn["content"]})
    turns.append({"role": "user", "content": message})

    if provider() == "openai":
        from openai import OpenAI
        client = OpenAI()
        response = client.chat.completions.create(
            model=openai_model(),
            messages=[{"role": "system", "content": SYSTEM_CHAT}] + turns,
        )
        return _clean_text(response.choices[0].message.content)

    import anthropic
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=16000,
        system=[{"type": "text", "text": SYSTEM_CHAT,
                 "cache_control": {"type": "ephemeral"}}],
        messages=turns,
    )
    return _clean_text(next(b.text for b in response.content if b.type == "text"))
