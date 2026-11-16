# Junior AI Investigator - AI-First Fraud Case Review

A working prototype of a proactive case-review tool for insurance fraud
investigators. When the investigator opens the tool, all 50 referred claims are
already triaged: each case has a risk score, a plain-language AI assessment,
the key indicators that drove the conclusion, and a recommended next step. The
investigator stays in control: they can drill into any case, interrogate the AI
about it, record a decision (confirm / dismiss / needs info), rate whether the
AI's call was right, and leave notes.

See also: [WRITEUP.md](WRITEUP.md) and [writeup.pptx](writeup.pptx).

---

## Quick start

Requirements: Python 3.11+. No API key is required to run the demo (see
"Modes / providers" below).

```bash
git clone <this repo> && cd AI_investigator
pip install -r requirements.txt

# optional - enables LLM-generated assessments and per-case chat.
# Easiest: copy .env.example to .env and fill in your key:
#   OPENAI_API_KEY=sk-...          (model override: OPENAI_MODEL=gpt-4o)
#   or ANTHROPIC_API_KEY=sk-ant-...
# Environment variables work too and take precedence over .env.

python -m uvicorn server.app:app --port 8001
```

Open http://localhost:8001.

On first start the app triages all 50 cases and caches the result in
`data/assessments.json` (instant on later starts; with an API key the first
run takes 30-60 seconds for 10 batched LLM calls). `POST /api/retriage`, or
deleting the cache file, forces a re-run. If the cache was generated in a
different mode (for example without a key, or with another provider), the app
automatically regenerates with the current provider.

### Modes / providers

| | Scores, tiers, signals, patterns | Assessment narrative | Chat |
|---|---|---|---|
| `OPENAI_API_KEY` set | deterministic engine | OpenAI (`gpt-4o-mini` by default, `OPENAI_MODEL` to override), structured output | OpenAI, grounded in the case file |
| `ANTHROPIC_API_KEY` set | deterministic engine | Claude (claude-opus-4-8), structured output | Claude, grounded in the case file |
| no key | deterministic engine (identical) | template fallback from the same engine output | explains it is offline, still reports facts |

If both keys are set, OpenAI wins (it is the configured chat provider).

This split is deliberate: everything numeric is computed by code, never by the
LLM, so the demo (and any audit) does not depend on model availability.

---

## What to try

1. **Morning briefing** (top bar): tier counts, open-case count, dollar
   exposure in high-risk cases, suggested starting cases, and a cross-case
   alert.
2. Open **C1024**, the worst case in the queue ($66,655, 9 of 9 signals
   triggered, collusion pattern).
3. Open **C1001**. It looks completely clean on its own signals, but the
   engine flags that its claim number `LTC-2034786` also appears on C1031,
   a cross-case check that no per-row score would catch.
4. **Ask the AI** (with an API key): "What evidence should I request?" or use
   the suggestion chips, including "Steelman the benign case", which forces
   the AI to argue against its own triage.
5. **Decide**: confirm and refer to SIU, dismiss as false positive, or request
   more info, and rate the AI's call. Decisions, agreement ratings, and notes
   persist in `data/case_state.json` (the prototype's audit trail and
   feedback loop).

---

## Architecture

```
data/sample_cases.csv
        |
        v
server/engine.py      deterministic signal engine (the grounding layer)
  - per-signal normalization against explicit thresholds and weights
  - pattern detection (collusion, opportunistic timing)
  - cross-case checks (duplicate claim numbers across the queue)
  - score 0-100 -> tier (low / medium / high) -> baseline action class
        |
        v
server/llm.py         LLM reasoning layer (OpenAI or Anthropic, optional)
  - batch triage: 5 cases per call, structured output (json_schema) giving
    summary, hypothesis, refined next action, confidence
  - per-case chat with the full case file and queue context
  - provider-pluggable (OPENAI_API_KEY for OpenAI, else ANTHROPIC_API_KEY
    for Claude); per-batch fallback on any API error
        |
        v
server/app.py         FastAPI: queue/case/decision/notes/chat endpoints
server/store.py       JSON persistence (assessment cache and investigator state)
web/                  single-page UI, vanilla JS, no build step
```

**Grounding / anti-hallucination:** the LLM never produces a number shown in
the UI. It receives the engine's structured output (signal values, severities,
score, tier, patterns, baseline action class) and is instructed to narrate and
refine, not re-score. Structured output (JSON schema) makes responses parse
deterministically; any batch failure falls back to deterministic templates, so
triage can never be "down". The chat system prompt instructs the model to
answer only from the case file and to say when data is not available.

### Signal weighting (the judgment call)

Hard fraud markers dominate: duplicate billing (18), shared member-provider
contact (16), service overlap (13). Each is near-impossible to produce
innocently. Volume and amount anomalies are secondary and scale linearly from
a "normal" threshold to an "extreme" one: amount vs peers (14), visit
frequency (10), distance (9), weekend ratio (8), round-dollar ratio (7).
Context signals, prior claims (3) and recent policy change (2), are weak alone
but participate in pattern bonuses (for example policy change plus elevated
amount = opportunistic timing), which is where combinations matter more than
sums. Thresholds and weights live in one table in `engine.py` and are shown to
the investigator per-signal, with each signal's contribution in points.

---

## Repo layout

```
data/sample_cases.csv     input (50 synthetic referrals)
data/assessments.json     cached AI triage (generated on first run)
data/case_state.json      decisions / notes / chat (generated on use)
server/                   engine, LLM layer, API, store
web/                      UI (index.html, app.js, style.css)
WRITEUP.md                product and architecture write-up
writeup.pptx              3-slide version of the write-up
```

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/queue` | briefing plus triaged queue, sorted by risk |
| `GET /api/cases/{id}` | full case: raw row, engine analysis, AI assessment, investigator state |
| `POST /api/cases/{id}/decision` | `{decision: confirm_fraud\|dismiss\|needs_info\|reopen, ai_agreement: agree\|disagree}` |
| `POST /api/cases/{id}/notes` | `{text}` |
| `POST /api/cases/{id}/chat` | `{message}` returns `{reply}` |
| `POST /api/retriage` | regenerate all assessments |
