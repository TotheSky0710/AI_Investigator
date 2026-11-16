"""Deterministic signal-scoring engine.

Every number shown to the investigator comes from this module, not from the LLM.
The LLM layer (llm.py) only narrates and reasons over the output of this engine,
which keeps the assessment grounded and auditable.
"""

from __future__ import annotations

import csv
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "sample_cases.csv"

# Signal metadata: weight is the max contribution to the 0-100 score.
# For scaled signals, risk ramps linearly from 0 at `lo` to 1 at `hi`.
# Rationale for the weighting is in the README / write-up: hard fraud markers
# (duplicate billing, shared contact, service overlap) dominate; volume/amount
# anomalies are secondary; weak context signals (prior claims, policy change)
# contribute little on their own but participate in pattern bonuses.
SIGNALS = {
    "duplicate_service_billed": {
        "label": "Duplicate service billed",
        "kind": "binary", "weight": 18,
        "normal": "not triggered",
        "explain": "The same service appears billed more than once, a direct billing-integrity violation.",
    },
    "shared_contact_with_provider": {
        "label": "Shared contact info with provider",
        "kind": "binary", "weight": 16,
        "normal": "not triggered",
        "explain": "Member and provider share a phone/address/email, a classic collusion marker.",
    },
    "service_overlap_other_provider": {
        "label": "Service overlap with another provider",
        "kind": "binary", "weight": 13,
        "normal": "not triggered",
        "explain": "Services overlap in time with another provider's billing, consistent with phantom or double billing.",
    },
    "amount_vs_peer_avg_pct": {
        "label": "Amount vs peer average",
        "kind": "scaled", "weight": 14, "lo": 20, "hi": 120,
        "unit": "%", "normal": "within +/-20% of peers",
        "explain": "How far the billed amount sits above the peer average for this care type.",
    },
    "weekly_visit_frequency": {
        "label": "Weekly visit frequency",
        "kind": "scaled", "weight": 10, "lo": 5, "hi": 14,
        "unit": "visits/wk", "normal": "2-5 visits/week",
        "explain": "Unusually high visit cadence can indicate billing for services not rendered.",
    },
    "member_provider_distance_miles": {
        "label": "Member-provider distance",
        "kind": "scaled", "weight": 9, "lo": 30, "hi": 200,
        "unit": "mi", "normal": "under ~30 miles",
        "explain": "Long distances make frequent in-person service implausible.",
    },
    "weekend_billing_ratio": {
        "label": "Weekend billing ratio",
        "kind": "scaled", "weight": 8, "lo": 0.15, "hi": 0.55,
        "unit": "", "normal": "under ~15%",
        "explain": "Heavy weekend billing is atypical for scheduled long-term-care services.",
    },
    "round_dollar_billing_ratio": {
        "label": "Round-dollar billing ratio",
        "kind": "scaled", "weight": 7, "lo": 0.25, "hi": 0.8,
        "unit": "", "normal": "under ~25%",
        "explain": "A high share of round-dollar line items suggests fabricated rather than metered charges.",
    },
    "prior_claims_last_12mo": {
        "label": "Prior claims (12 months)",
        "kind": "scaled", "weight": 3, "lo": 3, "hi": 10,
        "unit": "claims", "normal": "0-2 claims",
        "explain": "Claim velocity. Weak alone, meaningful alongside other markers.",
    },
    "recent_policy_change_flag": {
        "label": "Recent policy change",
        "kind": "binary", "weight": 2,
        "normal": "not triggered",
        "explain": "A claim shortly after a policy change can indicate opportunistic timing. Weak alone.",
    },
}

TIER_HIGH = 45
TIER_MEDIUM = 8


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def load_cases() -> list[dict]:
    with open(DATA_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["claim_amount_usd"] = float(r["claim_amount_usd"])
        for key, meta in SIGNALS.items():
            r[key] = float(r[key])
    return rows


def analyze_case(row: dict) -> dict:
    """Score one case. Returns per-signal contributions, patterns, score, tier."""
    contributions = []
    score = 0.0
    for key, meta in SIGNALS.items():
        value = row[key]
        if meta["kind"] == "binary":
            norm = 1.0 if value >= 1 else 0.0
        else:
            norm = _clamp((value - meta["lo"]) / (meta["hi"] - meta["lo"]), 0.0, 1.0)
        points = norm * meta["weight"]
        score += points
        contributions.append({
            "key": key,
            "label": meta["label"],
            "value": value,
            "unit": meta.get("unit", ""),
            "normal": meta["normal"],
            "triggered": norm > 0,
            "severity": round(norm, 3),       # 0..1, how abnormal this value is
            "points": round(points, 1),       # contribution to the 0-100 score
            "explain": meta["explain"],
        })

    # Pattern bonuses: combinations mean more than the sum of their parts.
    patterns = []
    sig = {c["key"]: c for c in contributions}
    if sig["shared_contact_with_provider"]["triggered"] and (
        sig["duplicate_service_billed"]["triggered"]
        or sig["service_overlap_other_provider"]["triggered"]
    ):
        patterns.append({
            "name": "Possible collusion",
            "detail": "Shared contact info combined with duplicate/overlapping billing is the signature of member-provider collusion.",
            "bonus": 8,
        })
    if row["recent_policy_change_flag"] >= 1 and sig["amount_vs_peer_avg_pct"]["severity"] > 0.15:
        patterns.append({
            "name": "Opportunistic timing",
            "detail": "Elevated billing shortly after a policy change suggests the claim was engineered around new coverage.",
            "bonus": 4,
        })
    score += sum(p["bonus"] for p in patterns)

    contributions.sort(key=lambda c: c["points"], reverse=True)
    score = round(_clamp(score, 0, 100), 1)
    tier = "high" if score >= TIER_HIGH else "medium" if score >= TIER_MEDIUM else "low"
    return {"score": score, "tier": tier, "contributions": contributions, "patterns": patterns}


def analyze_queue(rows: list[dict]) -> dict:
    """Cross-case checks + per-case analysis for the whole queue."""
    results = {r["case_id"]: analyze_case(r) for r in rows}

    # Cross-case: the same claim number appearing on two different cases is a
    # data-integrity red flag the per-case signals cannot see.
    by_claim_number: dict[str, list[str]] = {}
    for r in rows:
        by_claim_number.setdefault(r["claim_number"], []).append(r["case_id"])
    for claim_number, ids in by_claim_number.items():
        if len(ids) > 1:
            for cid in ids:
                others = [i for i in ids if i != cid]
                res = results[cid]
                res["patterns"].append({
                    "name": "Duplicate claim number across cases",
                    "detail": f"Claim number {claim_number} also appears on case(s) {', '.join(others)} "
                              "with different claim details, suggesting a recycled or cloned claim.",
                    "bonus": 10,
                })
                res["score"] = round(_clamp(res["score"] + 10, 0, 100), 1)
                res["tier"] = ("high" if res["score"] >= TIER_HIGH
                               else "medium" if res["score"] >= TIER_MEDIUM else "low")

    return results


def recommended_action(tier: str, patterns: list[dict]) -> str:
    """Deterministic baseline recommendation; the LLM may refine the wording
    but the action class always comes from here."""
    pattern_names = {p["name"] for p in patterns}
    if "Duplicate claim number across cases" in pattern_names:
        return ("Escalate to SIU and reconcile the duplicated claim number across cases "
                "before any payment action.")
    if tier == "high":
        return ("Assign to SIU for full investigation: pull visit logs and billing detail, "
                "and run a provider-level pattern review.")
    if tier == "medium":
        return ("Desk review: request supporting documentation (visit logs, invoices) and "
                "verify the anomalous signals before approving.")
    return "Close as likely false positive; no investigator action needed beyond a spot-check."
