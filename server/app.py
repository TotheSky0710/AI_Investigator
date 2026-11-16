"""FastAPI app: serves the triage API + the static web UI.

Run:  py -m uvicorn server.app:app --port 8001   (from the repo root)
Open: http://localhost:8001
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Load API keys from <repo>/.env if present (OPENAI_API_KEY, OPENAI_MODEL, ...).
# Real environment variables take precedence over the file.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from . import engine, llm, store

app = FastAPI(title="Junior AI Investigator")

# ---------------------------------------------------------------- startup ---

ROWS = engine.load_cases()
ROWS_BY_ID = {r["case_id"]: r for r in ROWS}
ANALYSES = engine.analyze_queue(ROWS)
BASELINE_ACTIONS = {cid: engine.recommended_action(a["tier"], a["patterns"])
                    for cid, a in ANALYSES.items()}

ASSESSMENTS: dict = {}
ASSESS_MODE: str = "none"


def ensure_assessments(force: bool = False):
    """Load cached assessments, or (re)generate for the whole queue."""
    global ASSESSMENTS, ASSESS_MODE
    current = llm.provider()
    if not force:
        cached = store.load_assessments()
        # Reuse the cache unless an LLM provider is now available and the cache
        # was produced by a different mode (fallback -> LLM, or provider switch).
        if cached and not (current != "fallback" and cached.get("mode") != current):
            ASSESSMENTS, ASSESS_MODE = cached["assessments"], cached["mode"]
            return
    mode = current
    print(f"[triage] generating assessments for {len(ROWS)} cases (mode={mode})...")
    ASSESSMENTS = llm.generate_assessments(
        ROWS, ANALYSES, BASELINE_ACTIONS,
        progress=lambda done, total: print(f"[triage] {done}/{total}"))
    ASSESS_MODE = mode
    store.save_assessments(ASSESSMENTS, mode)
    print("[triage] done.")


ensure_assessments()

# ------------------------------------------------------------------ models ---

class DecisionBody(BaseModel):
    decision: str | None = None          # confirm_fraud | dismiss | needs_info | reopen
    ai_agreement: str | None = None      # agree | disagree


class NoteBody(BaseModel):
    text: str


class ChatBody(BaseModel):
    message: str


# ------------------------------------------------------------------- views ---

def case_summary(cid: str) -> dict:
    row, analysis = ROWS_BY_ID[cid], ANALYSES[cid]
    st = store.case_state(cid)
    return {
        "case_id": cid,
        "claim_number": row["claim_number"],
        "claim_date": row["claim_date"],
        "care_type": row["care_type"],
        "claim_amount_usd": row["claim_amount_usd"],
        "state": row["state"],
        "score": analysis["score"],
        "tier": analysis["tier"],
        "patterns": [p["name"] for p in analysis["patterns"]],
        "triggered_count": sum(1 for c in analysis["contributions"] if c["triggered"]),
        "summary": ASSESSMENTS.get(cid, {}).get("summary", ""),
        "confidence": ASSESSMENTS.get(cid, {}).get("confidence", ""),
        "decision": st["decision"],
        "ai_agreement": st["ai_agreement"],
        "has_notes": bool(st["notes"]),
    }


def queue_briefing() -> dict:
    tiers = {"high": [], "medium": [], "low": []}
    for cid, a in ANALYSES.items():
        tiers[a["tier"]].append(cid)
    order = sorted(ANALYSES, key=lambda c: -ANALYSES[c]["score"])
    open_high = [c for c in order if ANALYSES[c]["tier"] == "high"
                 and not store.case_state(c)["decision"]]
    total_high_amount = sum(ROWS_BY_ID[c]["claim_amount_usd"] for c in tiers["high"])
    return {
        "counts": {t: len(v) for t, v in tiers.items()},
        "exposure_high_usd": total_high_amount,
        "start_with": open_high[:3],
        "cross_case_alerts": sorted({p["name"] for a in ANALYSES.values()
                                     for p in a["patterns"]
                                     if "Duplicate claim number" in p["name"]}),
        "mode": ASSESS_MODE,
    }


# --------------------------------------------------------------- endpoints ---

@app.get("/api/queue")
def get_queue():
    order = sorted(ANALYSES, key=lambda c: -ANALYSES[c]["score"])
    return {"briefing": queue_briefing(),
            "cases": [case_summary(cid) for cid in order]}


@app.get("/api/cases/{cid}")
def get_case(cid: str):
    if cid not in ROWS_BY_ID:
        raise HTTPException(404, "unknown case")
    return {
        "case": ROWS_BY_ID[cid],
        "analysis": ANALYSES[cid],
        "baseline_action": BASELINE_ACTIONS[cid],
        "assessment": ASSESSMENTS.get(cid),
        "state": store.case_state(cid),
    }


@app.post("/api/cases/{cid}/decision")
def post_decision(cid: str, body: DecisionBody):
    if cid not in ROWS_BY_ID:
        raise HTTPException(404, "unknown case")
    changes = {}
    if body.decision is not None:
        changes["decision"] = None if body.decision == "reopen" else body.decision
    if body.ai_agreement is not None:
        changes["ai_agreement"] = body.ai_agreement
    return store.update_case_state(cid, **changes)


@app.post("/api/cases/{cid}/notes")
def post_note(cid: str, body: NoteBody):
    if cid not in ROWS_BY_ID:
        raise HTTPException(404, "unknown case")
    if not body.text.strip():
        raise HTTPException(400, "empty note")
    return store.append_note(cid, body.text.strip())


@app.post("/api/cases/{cid}/chat")
def post_chat(cid: str, body: ChatBody):
    if cid not in ROWS_BY_ID:
        raise HTTPException(404, "unknown case")
    history = store.case_state(cid)["chat"]
    reply = llm.chat_reply(
        ROWS_BY_ID[cid], ANALYSES[cid], ASSESSMENTS.get(cid, {}),
        queue_context=queue_briefing(),
        history=[{"role": t["role"], "content": t["content"]} for t in history],
        message=body.message,
    )
    store.append_chat(cid, "user", body.message)
    store.append_chat(cid, "assistant", reply)
    return {"reply": reply}


@app.post("/api/retriage")
def retriage():
    ensure_assessments(force=True)
    return {"ok": True, "mode": ASSESS_MODE}


# Static UI last so /api/* wins.
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
