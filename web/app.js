/* Junior AI Investigator, single-page UI (no build step). */

const state = {
  queue: [],
  briefing: null,
  tier: "all",
  selected: null,
  showNormalSignals: false,
};

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => "$" + Number(n).toLocaleString("en-US", { maximumFractionDigits: 0 });

const DECISION_LABELS = {
  confirm_fraud: "Confirmed: refer to SIU",
  dismiss: "Dismissed: false positive",
  needs_info: "Needs more info",
};

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
  return res.json();
}

/* ---------------- queue + briefing ---------------- */

async function loadQueue() {
  const data = await api("/api/queue");
  state.queue = data.cases;
  state.briefing = data.briefing;
  renderBriefing();
  renderQueue();
  renderModeBadge();
}

function renderModeBadge() {
  const el = $("#mode-badge");
  const mode = state.briefing.mode;
  if (mode && mode !== "fallback") {
    el.textContent = "AI reasoning: " + (mode === "openai" ? "OpenAI" : mode === "anthropic" ? "Claude" : mode);
    el.classList.add("llm");
    el.title = "Assessments and chat are LLM-generated, grounded in the deterministic signal engine.";
  } else {
    el.textContent = "AI reasoning: offline fallback";
    el.title = "Set OPENAI_API_KEY (or ANTHROPIC_API_KEY) to enable LLM narratives and chat. Scores and signals are deterministic either way.";
  }
}

function renderBriefing() {
  const b = state.briefing;
  const open = state.queue.filter((c) => !c.decision).length;
  const startLinks = b.start_with
    .map((cid) => `<button class="case-link" data-open="${cid}">${cid}</button>`)
    .join(", ");
  $("#briefing").innerHTML = `
    <div class="stat"><b class="high">${b.counts.high}</b><span>high risk</span></div>
    <div class="stat"><b class="medium">${b.counts.medium}</b><span>medium</span></div>
    <div class="stat"><b class="low">${b.counts.low}</b><span>likely false positive</span></div>
    <div class="stat"><b>${money(b.exposure_high_usd)}</b><span>high-risk exposure</span></div>
    <div class="stat"><b>${open}</b><span>open cases</span></div>
    <div class="start-with">${
      b.start_with.length
        ? "Suggested starting point: " + startLinks
        : "All high-risk cases have a decision. Nice work."
    }</div>
    ${b.cross_case_alerts.length
      ? `<div class="alert">Cross-case alert: duplicate claim number detected</div>` : ""}
  `;
  $("#briefing").querySelectorAll("[data-open]").forEach((btn) =>
    btn.addEventListener("click", () => openCase(btn.dataset.open)));
}

function renderQueue() {
  const list = $("#queue-list");
  let cases = state.queue;
  if (state.tier === "open") cases = cases.filter((c) => !c.decision);
  else if (state.tier !== "all") cases = cases.filter((c) => c.tier === state.tier);

  list.innerHTML = cases.map((c) => `
    <div class="case-card ${c.case_id === state.selected ? "selected" : ""}" data-cid="${c.case_id}">
      <div class="row1">
        <span class="cid">${c.case_id}</span>
        <span class="badges">
          ${c.patterns.length ? `<span class="badge pattern" title="${esc(c.patterns.join("; "))}">pattern</span>` : ""}
          ${c.decision ? `<span class="badge decided">${esc(DECISION_LABELS[c.decision] || c.decision)}</span>` : ""}
          <span class="badge ${c.tier}">${c.tier} ${Math.round(c.score)}</span>
        </span>
      </div>
      <div class="meta">${esc(c.care_type)} | ${money(c.claim_amount_usd)} | ${esc(c.state)} | ${esc(c.claim_date)}</div>
      <div class="snippet">${esc(c.summary)}</div>
    </div>
  `).join("");

  list.querySelectorAll(".case-card").forEach((el) =>
    el.addEventListener("click", () => openCase(el.dataset.cid)));
}

/* ---------------- case detail ---------------- */

async function openCase(cid) {
  state.selected = cid;
  state.showNormalSignals = false;
  renderQueue();
  const data = await api(`/api/cases/${cid}`);
  renderCase(data);
  $("#case-panel").scrollTop = 0;
}

function sevColor(sev) {
  if (sev >= 0.66) return "var(--high)";
  if (sev >= 0.25) return "var(--medium)";
  return "var(--low)";
}

function renderCase(data) {
  const { case: row, analysis, assessment, state: cs, baseline_action } = data;
  const a = assessment || {};
  const triggered = analysis.contributions.filter((c) => c.triggered);
  const normal = analysis.contributions.filter((c) => !c.triggered);

  const signalRow = (c) => `
    <div class="signal-row ${c.triggered ? "" : "normal"}" title="${esc(c.explain)}">
      <div class="name">${esc(c.label)}<small>normal: ${esc(c.normal)}</small></div>
      <div class="sev-bar"><i style="width:${Math.max(3, c.severity * 100)}%;background:${sevColor(c.severity)}"></i></div>
      <div class="val">${c.value}${c.unit ? " " + c.unit : ""}<small>+${c.points} pts</small></div>
    </div>`;

  $("#case-panel").innerHTML = `
    <div class="case-head">
      <div class="case-title">
        <h1>${row.case_id} <span class="badge ${analysis.tier}">${analysis.tier} risk</span></h1>
        <div class="sub">${esc(row.claim_number)} | ${esc(row.care_type)} | ${money(row.claim_amount_usd)}
          | ${esc(row.state)} | filed ${esc(row.claim_date)}</div>
      </div>
      <div class="score-box">
        <div class="score-num ${analysis.tier}">${Math.round(analysis.score)}</div>
        <div class="lbl">risk score / 100</div>
      </div>
    </div>

    <div class="section">
      <h2>AI assessment</h2>
      <div class="card ai-summary">
        <div>${esc(a.summary || "No assessment available.")}</div>
        ${a.hypothesis ? `<div class="hypothesis"><b>Working hypothesis:</b> ${esc(a.hypothesis)}</div>` : ""}
        <div class="ai-meta">
          ${a.confidence ? `<span class="badge decided">confidence: ${esc(a.confidence)}</span>` : ""}
          <span class="badge decided">generated by: ${esc(a.generated_by || "n/a")}</span>
        </div>
      </div>
    </div>

    <div class="section">
      <h2>Recommended next step</h2>
      <div class="card action-card">
        <div class="txt">${esc(a.next_action || baseline_action)}</div>
      </div>
    </div>

    ${analysis.patterns.length ? `
    <div class="section">
      <h2>Detected patterns</h2>
      ${analysis.patterns.map((p) => `
        <div class="card pattern-card"><b>${esc(p.name)}</b><p>${esc(p.detail)}</p></div>`).join("")}
    </div>` : ""}

    <div class="section">
      <h2>Key indicators (${triggered.length} of ${analysis.contributions.length} triggered)</h2>
      <div class="card">
        ${triggered.length ? triggered.map(signalRow).join("")
          : `<div class="meta" style="color:var(--text-dim)">All nine signals are within normal range.</div>`}
        <div id="normal-signals" style="display:${state.showNormalSignals ? "block" : "none"}">
          ${normal.map(signalRow).join("")}
        </div>
        ${normal.length ? `<button class="toggle-normal" id="toggle-normal">
          ${state.showNormalSignals ? "Hide" : "Show"} ${normal.length} normal signals</button>` : ""}
      </div>
    </div>

    <div class="section">
      <h2>Your decision</h2>
      <div class="card">
        <div class="decision-bar">
          <button class="btn danger ${cs.decision === "confirm_fraud" ? "active-decision" : ""}" data-decision="confirm_fraud">Confirm: refer to SIU</button>
          <button class="btn success ${cs.decision === "dismiss" ? "active-decision" : ""}" data-decision="dismiss">Dismiss: false positive</button>
          <button class="btn ${cs.decision === "needs_info" ? "active-decision" : ""}" data-decision="needs_info">Request more info</button>
          ${cs.decision ? `<button class="btn" data-decision="reopen">Reopen</button>` : ""}
          <div class="agreement">
            AI call was:
            <button class="btn ${cs.ai_agreement === "agree" ? "active-decision" : ""}" data-agree="agree">Right</button>
            <button class="btn ${cs.ai_agreement === "disagree" ? "active-decision" : ""}" data-agree="disagree">Wrong</button>
          </div>
        </div>
        ${cs.decision ? `<div class="decided-note">Decision recorded: ${esc(DECISION_LABELS[cs.decision] || cs.decision)}</div>` : ""}
      </div>
    </div>

    <div class="section two-col">
      <div>
        <h2>Case notes</h2>
        <div class="card">
          <div id="notes-list">
            ${cs.notes.length ? cs.notes.map((n) => `
              <div class="note"><time>${new Date(n.at).toLocaleString()}</time>${esc(n.text)}</div>`).join("")
              : `<div style="color:var(--text-dim);font-size:0.85rem">No notes yet.</div>`}
          </div>
          <div class="note-input">
            <input type="text" id="note-text" placeholder="Add a note for the case file...">
            <button class="btn" id="note-add">Add</button>
          </div>
        </div>
      </div>
      <div>
        <h2>Ask the AI about this case</h2>
        <div class="card">
          <div class="chat-suggestions">
            <span class="chip" data-q="Why is this case rated ${analysis.tier} risk?">Why this rating?</span>
            <span class="chip" data-q="What evidence should I request to confirm or clear this case?">What evidence to request?</span>
            <span class="chip" data-q="What is the strongest argument that this is a false positive?">Steelman the benign case</span>
          </div>
          <div class="chat-log" id="chat-log">
            ${cs.chat.map((m) => `<div class="chat-msg ${m.role}">${esc(m.content)}</div>`).join("")}
          </div>
          <div class="chat-input">
            <input type="text" id="chat-text" placeholder="Ask a follow-up question...">
            <button class="btn primary" id="chat-send">Send</button>
          </div>
        </div>
      </div>
    </div>
  `;

  wireCaseEvents(row.case_id);
  const log = $("#chat-log");
  log.scrollTop = log.scrollHeight;
}

function wireCaseEvents(cid) {
  const panel = $("#case-panel");

  const tgl = $("#toggle-normal");
  if (tgl) tgl.addEventListener("click", () => {
    state.showNormalSignals = !state.showNormalSignals;
    $("#normal-signals").style.display = state.showNormalSignals ? "block" : "none";
    tgl.textContent = (state.showNormalSignals ? "Hide" : "Show") +
      tgl.textContent.slice(tgl.textContent.indexOf(" "));
  });

  panel.querySelectorAll("[data-decision]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      await api(`/api/cases/${cid}/decision`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision: btn.dataset.decision }),
      });
      await loadQueue();
      openCase(cid);
    }));

  panel.querySelectorAll("[data-agree]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      await api(`/api/cases/${cid}/decision`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ai_agreement: btn.dataset.agree }),
      });
      openCase(cid);
    }));

  $("#note-add").addEventListener("click", addNote);
  $("#note-text").addEventListener("keydown", (e) => { if (e.key === "Enter") addNote(); });
  async function addNote() {
    const input = $("#note-text");
    if (!input.value.trim()) return;
    await api(`/api/cases/${cid}/notes`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: input.value }),
    });
    await loadQueue();
    openCase(cid);
  }

  panel.querySelectorAll(".chip").forEach((chip) =>
    chip.addEventListener("click", () => sendChat(cid, chip.dataset.q)));
  $("#chat-send").addEventListener("click", () => sendChat(cid));
  $("#chat-text").addEventListener("keydown", (e) => { if (e.key === "Enter") sendChat(cid); });
}

async function sendChat(cid, preset) {
  const input = $("#chat-text");
  const message = preset || input.value.trim();
  if (!message) return;
  input.value = "";
  const log = $("#chat-log");
  log.insertAdjacentHTML("beforeend", `<div class="chat-msg user">${esc(message)}</div>`);
  log.insertAdjacentHTML("beforeend", `<div class="chat-msg assistant spinner" id="chat-wait">Thinking...</div>`);
  log.scrollTop = log.scrollHeight;
  $("#chat-send").disabled = true;
  try {
    const res = await api(`/api/cases/${cid}/chat`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    $("#chat-wait").remove();
    log.insertAdjacentHTML("beforeend", `<div class="chat-msg assistant">${esc(res.reply)}</div>`);
  } catch (err) {
    $("#chat-wait").remove();
    log.insertAdjacentHTML("beforeend",
      `<div class="chat-msg assistant">Error: ${esc(err.message)}</div>`);
  } finally {
    $("#chat-send").disabled = false;
    log.scrollTop = log.scrollHeight;
  }
}

/* ---------------- tabs + boot ---------------- */

$("#tier-tabs").querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    state.tier = tab.dataset.tier;
    $("#tier-tabs").querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    renderQueue();
  }));

loadQueue().catch((err) => {
  $("#case-panel").innerHTML =
    `<div class="placeholder"><p>Failed to load queue: ${esc(err.message)}</p></div>`;
});
