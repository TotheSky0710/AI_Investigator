"""Tiny JSON file store for prototype state: cached AI assessments and
per-case investigator state (decisions, notes, chat history).

A real deployment would use a database with an append-only audit log;
for a 50-case prototype, two JSON files keep the repo runnable anywhere.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ASSESSMENTS_PATH = DATA_DIR / "assessments.json"
STATE_PATH = DATA_DIR / "case_state.json"

_lock = threading.Lock()


def _read(path: Path, default):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def _write(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def load_assessments() -> dict | None:
    data = _read(ASSESSMENTS_PATH, None)
    return data


def save_assessments(assessments: dict, mode: str):
    with _lock:
        _write(ASSESSMENTS_PATH, {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "assessments": assessments,
        })


def load_state() -> dict:
    return _read(STATE_PATH, {})


def case_state(case_id: str) -> dict:
    return load_state().get(case_id, {"decision": None, "ai_agreement": None,
                                      "notes": [], "chat": []})


def update_case_state(case_id: str, **changes) -> dict:
    with _lock:
        state = load_state()
        cs = state.get(case_id, {"decision": None, "ai_agreement": None,
                                 "notes": [], "chat": []})
        cs.update(changes)
        state[case_id] = cs
        _write(STATE_PATH, state)
        return cs


def append_note(case_id: str, text: str) -> dict:
    with _lock:
        state = load_state()
        cs = state.get(case_id, {"decision": None, "ai_agreement": None,
                                 "notes": [], "chat": []})
        cs["notes"].append({"text": text,
                            "at": datetime.now(timezone.utc).isoformat()})
        state[case_id] = cs
        _write(STATE_PATH, state)
        return cs


def append_chat(case_id: str, role: str, content: str) -> dict:
    with _lock:
        state = load_state()
        cs = state.get(case_id, {"decision": None, "ai_agreement": None,
                                 "notes": [], "chat": []})
        cs["chat"].append({"role": role, "content": content,
                           "at": datetime.now(timezone.utc).isoformat()})
        state[case_id] = cs
        _write(STATE_PATH, state)
        return cs
