"use strict";

/*
 * Wizard SPA — Phase 1 brain dump.
 *
 * State source of truth is the server (sessions/<id>/state.yaml). The UI
 * holds a local mirror, autosaves on edits via debounced PATCH, and
 * reconciles to whatever the server returns.
 */

const SAVE_DEBOUNCE_MS = 600;
const VOICE_OK_KEY = "rb.voicePrivacyAck";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  session: null,
  activeChunkId: null,
  saveTimer: null,
  pendingPatch: {},
  voice: null,
};

// ---------- network ----------

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res.json();
}

async function loadOrCreate() {
  const params = new URLSearchParams(window.location.search);
  let id = params.get("session");
  if (id) {
    try {
      return await api(`/api/wizard/${id}`);
    } catch (err) {
      // Session id was bad — fall through and mint a fresh one.
      console.warn("session not found, creating fresh:", err.message);
    }
  }
  const fresh = await api("/api/wizard", { method: "POST" });
  // Stamp the new id in the URL without reloading.
  const url = new URL(window.location.href);
  url.searchParams.set("session", fresh.id);
  window.history.replaceState({}, "", url);
  return fresh;
}

function status(text, level = "saved") {
  const el = $("#status");
  if (!text) {
    el.hidden = true;
    return;
  }
  el.textContent = text;
  el.className = `status ${level}`;
  el.hidden = false;
}

async function flushSave() {
  state.saveTimer = null;
  const patch = state.pendingPatch;
  if (Object.keys(patch).length === 0) return;
  state.pendingPatch = {};
  status("Saving…", "saving");
  try {
    state.session = await api(`/api/wizard/${state.session.id}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    status("Saved", "saved");
    setTimeout(() => status(null), 1200);
  } catch (err) {
    status(`Save failed: ${err.message}`, "error");
  }
}

function queueSave(patch) {
  Object.assign(state.pendingPatch, patch);
  if (state.saveTimer) clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(flushSave, SAVE_DEBOUNCE_MS);
}

function flushSaveNow() {
  if (state.saveTimer) clearTimeout(state.saveTimer);
  return flushSave();
}

// ---------- rendering ----------

function renderRoleFamily() {
  const rf = state.session.role_family;
  $$("input[name='role_family']").forEach((el) => {
    el.checked = el.value === rf;
  });
  const otherWrap = $("#role-other-wrap");
  otherWrap.hidden = rf !== "other";
  $("#role_family_other").value = state.session.role_family_other || "";

  const chunksFieldset = $("#chunks-section");
  const dumpFieldset = $("#dump-section");
  const picked = rf !== null && rf !== undefined;
  chunksFieldset.disabled = !picked;
  dumpFieldset.disabled = !picked || state.session.chunks.length === 0;
}

function renderCareerStart() {
  $("#career_start").value = state.session.career_start || "";
}

function renderCadencePicker() {
  const select = $("#cadence-select");
  const guidance = $("#cadence-guidance");
  if (!select) return;

  const options = state.session.cadence_options || [];
  if (!options.length) return;

  // Determine the current selection: explicit session cadence > suggested.
  const currentId =
    state.session.cadence
    || state.session.suggested_cadence
    || options[0].id;

  // Re-render options idempotently (so re-renders after save don't lose state).
  if (select.options.length !== options.length) {
    select.innerHTML = "";
    options.forEach((opt) => {
      const o = document.createElement("option");
      o.value = opt.id;
      o.textContent = opt.label;
      select.appendChild(o);
    });
  }
  select.value = currentId;

  if (guidance) {
    const cur = options.find((o) => o.id === currentId);
    if (cur) {
      guidance.innerHTML =
        `<strong>${cur.label}</strong> · `
        + `<em>Best for: ${cur.best_for}</em>. `
        + `<span>${cur.notes}</span>`;
    }
  }
}

function renderChunksSummary() {
  const n = state.session.chunks.length;
  if (!n) {
    $("#chunks-summary").textContent = "";
    return;
  }
  const withNotes = state.session.chunks.filter((c) => (c.raw_notes || "").trim()).length;
  $("#chunks-summary").textContent =
    `${n} chunk${n === 1 ? "" : "s"} · ${withNotes} with notes`;
}

function renderChunksList() {
  const list = $("#chunks-list");
  list.innerHTML = "";
  state.session.chunks.forEach((chunk) => {
    const row = document.createElement("div");
    row.className = "chunk-row";
    row.dataset.chunkId = chunk.id;
    if (chunk.id === state.activeChunkId) row.classList.add("active");

    const label = document.createElement("input");
    label.type = "text";
    label.value = chunk.label;
    label.addEventListener("input", () => {
      chunk.label = label.value;
      queueSave({ chunks: state.session.chunks });
    });

    const start = document.createElement("input");
    start.type = "month";
    start.value = chunk.start;
    start.addEventListener("input", () => {
      chunk.start = start.value;
      queueSave({ chunks: state.session.chunks });
    });

    const end = document.createElement("input");
    end.type = "month";
    end.value = chunk.end;
    end.addEventListener("input", () => {
      chunk.end = end.value;
      queueSave({ chunks: state.session.chunks });
    });

    const buttons = document.createElement("div");
    buttons.className = "chunk-buttons";

    const noteStatus = document.createElement("span");
    noteStatus.className = "chunk-status";
    updateChunkStatusBadge(noteStatus, chunk);

    const focus = document.createElement("button");
    focus.type = "button";
    focus.textContent = chunk.id === state.activeChunkId ? "editing" : "open";
    focus.disabled = chunk.id === state.activeChunkId;
    focus.addEventListener("click", () => setActiveChunk(chunk.id));

    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.title = "Remove chunk";
    remove.addEventListener("click", () => removeChunk(chunk.id));

    buttons.append(noteStatus, focus, remove);
    row.append(label, start, end, buttons);
    list.appendChild(row);
  });

  const addRow = document.createElement("button");
  addRow.type = "button";
  addRow.textContent = "+ add chunk";
  addRow.style.alignSelf = "flex-start";
  addRow.addEventListener("click", addChunk);
  list.appendChild(addRow);
}

function renderPrompts() {
  const list = $("#prompts-list");
  list.innerHTML = "";
  (state.session.prompts || []).forEach((prompt) => {
    const li = document.createElement("li");
    li.textContent = prompt;
    list.appendChild(li);
  });
}

function renderActiveChunk() {
  const chunk = activeChunk();
  if (!chunk) {
    $("#chunk-title").textContent = "Pick a chunk above";
    $("#raw-notes").value = "";
    $("#raw-notes").disabled = true;
    $("#chunk-meta").textContent = "";
    renderExtractSection();
    return;
  }
  $("#raw-notes").disabled = false;
  $("#chunk-title").textContent = `${chunk.label} (${chunk.start} → ${chunk.end})`;
  $("#raw-notes").value = chunk.raw_notes || "";
  $("#chunk-meta").textContent =
    `${(chunk.raw_notes || "").length} characters · ${chunkWordCount(chunk)} words`;
  renderExtractSection();
}

function chunkWordCount(chunk) {
  return (chunk.raw_notes || "").trim().split(/\s+/).filter(Boolean).length;
}

function updateChunkStatusBadge(el, chunk) {
  const text = (chunk.raw_notes || "").trim();
  if (text) {
    el.textContent = `${chunk.raw_notes.length} chars`;
    el.classList.add("has-notes");
  } else {
    el.textContent = "empty";
    el.classList.remove("has-notes");
  }
}

function refreshChunkRowStatus(chunkId) {
  const row = document.querySelector(`.chunk-row[data-chunk-id="${chunkId}"]`);
  if (!row) return;
  const badge = row.querySelector(".chunk-status");
  const chunk = state.session.chunks.find((c) => c.id === chunkId);
  if (badge && chunk) updateChunkStatusBadge(badge, chunk);
}

function renderAll() {
  renderRoleFamily();
  renderCareerStart();
  renderCadencePicker();
  renderChunksSummary();
  renderChunksList();
  renderPrompts();
  renderActiveChunk();
  // renderActiveChunk also calls renderExtractSection
  renderCategorizeSection();
  renderEducationSection();
  renderBasicsSection();
  renderEmploymentSection();
  renderPromoteSection();
}

// ---------- Phase 2: extract ----------

const MIN_CHUNK_CHARS = 80;

function activeChunkDrafts() {
  const chunk = activeChunk();
  if (!chunk) return [];
  return (state.session.drafts || []).filter((d) => d.chunk_id === chunk.id);
}

function renderExtractSection() {
  const fieldset = $("#extract-section");
  const btn = $("#btn-extract");
  const meta = $("#extract-meta");
  const chunk = activeChunk();

  if (!chunk) {
    fieldset.disabled = true;
    btn.disabled = true;
    meta.textContent = "Open a chunk and type at least 80 characters of notes.";
    renderDraftsForChunk(null);
    return;
  }
  fieldset.disabled = false;

  const len = (chunk.raw_notes || "").trim().length;
  const drafts = activeChunkDrafts();
  if (len < MIN_CHUNK_CHARS) {
    btn.disabled = true;
    meta.textContent = `Need ${MIN_CHUNK_CHARS - len} more character${MIN_CHUNK_CHARS - len === 1 ? "" : "s"} of notes (have ${len}/${MIN_CHUNK_CHARS}).`;
  } else if (drafts.length) {
    btn.disabled = false;
    btn.textContent = "Re-extract (replace unconfirmed)";
    const confirmed = drafts.filter((d) => d.user_confirmed).length;
    meta.textContent = `${drafts.length} draft${drafts.length === 1 ? "" : "s"} for this chunk · ${confirmed} confirmed.`;
  } else {
    btn.disabled = false;
    btn.textContent = "Extract accomplishments";
    meta.textContent = `${len} characters of notes ready.`;
  }
  renderDraftsForChunk(chunk);
}

function renderDraftsForChunk(chunk) {
  const list = $("#drafts-list");
  const empty = $("#drafts-empty");
  list.innerHTML = "";
  const drafts = chunk ? activeChunkDrafts() : [];
  if (!drafts.length) {
    list.hidden = true;
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  list.hidden = false;
  drafts.forEach((d) => list.appendChild(renderDraftCard(d)));
}

function renderDraftCard(draft) {
  const card = document.createElement("div");
  card.className = "draft-card";
  card.dataset.draftId = draft.id;
  if (draft.user_confirmed) card.classList.add("confirmed");

  // Header: tier badge + missing tags + confirmed flag
  const header = document.createElement("div");
  header.className = "draft-header";
  const tier = document.createElement("span");
  tier.className = `tier-badge tier-${draft.tier}`;
  tier.textContent = draft.tier;
  header.appendChild(tier);
  if (draft.missing && draft.missing.length) {
    const miss = document.createElement("span");
    miss.className = "missing-tags";
    miss.innerHTML = "Missing: " +
      draft.missing.map((m) => `<code>${m}</code>`).join(" ");
    header.appendChild(miss);
  }
  if (draft.user_confirmed) {
    const ok = document.createElement("span");
    ok.className = "missing-tags";
    ok.textContent = "· confirmed";
    header.appendChild(ok);
  }
  card.appendChild(header);

  // Editable bullet text
  const ta = document.createElement("textarea");
  ta.className = "draft-bullet-input";
  ta.value = draft.draft_bullet;
  ta.rows = 2;
  ta.addEventListener("input", () => {
    draft.draft_bullet = ta.value;
    queueSave({ drafts: state.session.drafts });
  });
  card.appendChild(ta);

  // Raw quote provenance
  const quote = document.createElement("div");
  quote.className = "draft-raw-quote";
  quote.innerHTML = `<strong>Grounded in:</strong> &ldquo;${escapeHtml(draft.raw_quote)}&rdquo;`;
  card.appendChild(quote);

  // Actions: approve / discard / polish
  const actions = document.createElement("div");
  actions.className = "draft-actions";

  const approve = document.createElement("button");
  approve.type = "button";
  approve.className = draft.user_confirmed ? "" : "primary";
  approve.textContent = draft.user_confirmed ? "Unconfirm" : "Approve";
  approve.addEventListener("click", () => {
    draft.user_confirmed = !draft.user_confirmed;
    queueSave({ drafts: state.session.drafts });
    flushSaveNow().then(() => renderExtractSection());
  });

  const discard = document.createElement("button");
  discard.type = "button";
  discard.textContent = "Discard";
  discard.addEventListener("click", () => {
    state.session.drafts = state.session.drafts.filter((d) => d.id !== draft.id);
    queueSave({ drafts: state.session.drafts });
    flushSaveNow().then(() => renderExtractSection());
  });

  // Polish button — only meaningful when the tier is below "awesome".
  const polishBtn = document.createElement("button");
  polishBtn.type = "button";
  polishBtn.textContent = draft.tier === "awesome" ? "Re-polish" : "Polish (XYZ → Awesome)";

  const polishPane = renderPolishPane(draft);
  polishPane.hidden = true;
  polishBtn.addEventListener("click", () => {
    polishPane.hidden = !polishPane.hidden;
    polishBtn.textContent = polishPane.hidden
      ? (draft.tier === "awesome" ? "Re-polish" : "Polish (XYZ → Awesome)")
      : "Close polish pane";
  });

  actions.append(approve, discard, polishBtn);
  card.appendChild(actions);
  card.appendChild(polishPane);

  return card;
}

// ---------- Phase 5: polish ----------

function findDraft(draftId) {
  return (state.session.drafts || []).find((d) => d.id === draftId) || null;
}

const WHERE_TO_LOOK_LABELS = {
  y_metric: "Missing a number / metric — where to look:",
  z_method: "Missing the *how* (method, approach):",
  x_strong_verb: "Replace the weak opener with one of these:",
};

const FOLLOWUP_LABELS = {
  y_metric: "The metric / number (any of: %, $, count, time)",
  z_method: "The how-clause (method, approach, tool, technique)",
  x_strong_verb: "A stronger opening verb",
};

let _whereToLookCache = null;
async function fetchWhereToLook() {
  if (_whereToLookCache) return _whereToLookCache;
  const res = await fetch("/api/wizard/where-to-look");
  if (!res.ok) return {};
  _whereToLookCache = await res.json();
  return _whereToLookCache;
}

function renderPolishPane(draft) {
  const pane = document.createElement("div");
  pane.className = "polish-pane";

  if (!draft.missing || draft.missing.length === 0) {
    const ok = document.createElement("p");
    ok.className = "hint";
    ok.textContent =
      "Already at Awesome tier. Re-run polish to tighten phrasing — no follow-ups required.";
    pane.appendChild(ok);
  }

  const inputs = {};
  (draft.missing || ["y_metric", "z_method", "x_strong_verb"]).forEach((key) => {
    const wrap = document.createElement("div");
    wrap.className = "polish-field";

    const label = document.createElement("label");
    label.textContent = FOLLOWUP_LABELS[key] || key;
    const ta = document.createElement("textarea");
    ta.rows = 1;
    ta.className = "polish-followup";
    ta.dataset.followupKey = key;
    ta.placeholder = _placeholderForFollowup(key);
    label.appendChild(ta);
    inputs[key] = ta;
    wrap.appendChild(label);

    // Where-to-look hint, fetched lazily once per session and surfaced
    // as a collapsible <details> next to the input.
    const hint = document.createElement("details");
    hint.className = "polish-hint";
    const summary = document.createElement("summary");
    summary.textContent = WHERE_TO_LOOK_LABELS[key] || `Tips for ${key}`;
    hint.appendChild(summary);
    const hintList = document.createElement("ul");
    hintList.className = "polish-hint-list";
    hint.appendChild(hintList);
    // Fill the list asynchronously.
    fetchWhereToLook().then((map) => {
      const items = map[key] || [];
      items.forEach((line) => {
        const li = document.createElement("li");
        li.textContent = line;
        hintList.appendChild(li);
      });
      if (!items.length) hint.hidden = true;
    });
    wrap.appendChild(hint);

    pane.appendChild(wrap);
  });

  const actions = document.createElement("div");
  actions.className = "polish-actions";

  const runBtn = document.createElement("button");
  runBtn.type = "button";
  runBtn.className = "primary";
  runBtn.textContent = "Run polish";
  runBtn.addEventListener("click", () => runPolish(draft.id, inputs));

  actions.appendChild(runBtn);
  pane.appendChild(actions);

  // LLM-call transparency
  const llmDetails = document.createElement("details");
  llmDetails.className = "polish-llm-details";
  llmDetails.hidden = true;
  llmDetails.innerHTML = `
    <summary>Show LLM call (for trust / debugging)</summary>
    <h4>System prompt</h4>
    <pre class="llm-pre polish-llm-system"></pre>
    <h4>User message</h4>
    <pre class="llm-pre polish-llm-user"></pre>
    <h4>Raw response</h4>
    <pre class="llm-pre polish-llm-raw"></pre>
    <p class="hint polish-llm-provider"></p>
  `;
  pane.appendChild(llmDetails);

  const warnings = document.createElement("div");
  warnings.className = "polish-fabrication-warn";
  warnings.hidden = true;
  pane.appendChild(warnings);

  return pane;
}

function _placeholderForFollowup(key) {
  if (key === "y_metric") return "e.g. 80%, $5M, 12 weeks, 240 users";
  if (key === "z_method") return "e.g. by rewriting the worker pool in Go";
  if (key === "x_strong_verb") return "e.g. Led, Owned, Shipped, Cut, Hired";
  return "";
}

async function runPolish(draftId, inputs) {
  const draft = findDraft(draftId);
  if (!draft) return;

  const followups = {};
  Object.entries(inputs).forEach(([key, ta]) => {
    const v = (ta.value || "").trim();
    if (v) followups[key] = v;
  });

  status("Polishing…", "saving");
  try {
    const res = await fetch(
      `/api/wizard/${state.session.id}/drafts/${draftId}/polish`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ followups }),
      },
    );
    if (!res.ok) {
      const txt = await res.text();
      throw new Error(`${res.status} ${res.statusText}: ${txt}`);
    }
    const data = await res.json();
    state.session = data.session;
    renderExtractSection();

    // Surface the LLM call + any fabrication warnings on the (now newly
    // rendered) polish pane.
    const card = document.querySelector(`.draft-card[data-draft-id='${draftId}']`);
    if (card) {
      const llm = card.querySelector(".polish-llm-details");
      if (llm) {
        llm.hidden = false;
        llm.querySelector(".polish-llm-system").textContent = data.llm_call.system_prompt;
        llm.querySelector(".polish-llm-user").textContent = data.llm_call.user_message;
        llm.querySelector(".polish-llm-raw").textContent = data.llm_call.raw_response;
        llm.querySelector(".polish-llm-provider").textContent =
          `Provider: ${data.provider.name} — ${data.provider.reason}`;
      }
      const warn = card.querySelector(".polish-fabrication-warn");
      if (warn) {
        if (data.fabrication_warnings && data.fabrication_warnings.length) {
          warn.hidden = false;
          warn.innerHTML = "<strong>Fabrication guard fired:</strong><ul>" +
            data.fabrication_warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("") +
            "</ul>";
        } else {
          warn.hidden = true;
        }
      }
      // Keep the polish pane open so the user can see the result.
      const pane = card.querySelector(".polish-pane");
      if (pane) pane.hidden = false;
    }

    status(
      data.fabrication_warnings && data.fabrication_warnings.length
        ? "Polished — fabrication guard fired, see warnings."
        : "Polished",
      data.fabrication_warnings && data.fabrication_warnings.length ? "error" : "saved",
    );
    setTimeout(() => status(null), 2500);
  } catch (err) {
    status(`Polish failed: ${err.message}`, "error");
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

async function runExtract({ replace = false } = {}) {
  const chunk = activeChunk();
  if (!chunk) return;
  // Make sure the latest typed notes are persisted before we fire.
  await flushSaveNow();
  status("Extracting…", "saving");
  try {
    const res = await fetch(
      `/api/wizard/${state.session.id}/chunks/${chunk.id}/extract`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ replace }),
      },
    );
    if (res.status === 409) {
      const body = await res.json();
      const ok = confirm(
        `${body.existing_count} draft(s) already exist for this chunk ` +
        `(${body.confirmed_count} confirmed). Re-extract? ` +
        `Confirmed drafts will be kept; the rest replaced.`
      );
      if (ok) return runExtract({ replace: true });
      status(null);
      return;
    }
    if (res.status === 422) {
      const body = await res.json();
      status(body.hint || "Chunk too short.", "error");
      return;
    }
    if (!res.ok) {
      const txt = await res.text();
      throw new Error(`${res.status} ${res.statusText}: ${txt}`);
    }
    const data = await res.json();
    state.session = data.session;
    renderLLMCall(data.llm_call, data.provider);
    renderExtractSection();
    renderCategorizeSection();
    status(
      `Extracted ${data.extracted_count} draft${data.extracted_count === 1 ? "" : "s"}`,
      "saved",
    );
    setTimeout(() => status(null), 2000);
  } catch (err) {
    status(`Extract failed: ${err.message}`, "error");
  }
}

function renderLLMCall(call, provider) {
  const details = $("#llm-call-details");
  if (!call) {
    details.hidden = true;
    return;
  }
  $("#llm-call-system").textContent = call.system_prompt || "";
  $("#llm-call-user").textContent = call.user_message || "";
  $("#llm-call-raw").textContent = call.raw_response || "";
  $("#llm-call-provider").textContent =
    provider ? `Provider: ${provider.name} — ${provider.reason}` : "";
  details.hidden = false;
}

// ---------- Phase 3: categorize ----------

const BUCKETS = [
  "experience",
  "projects",
  "education",
  "extracurricular",
  "skills",
  "awards",
  "certifications",
];

const categorizeState = {
  selectedIds: new Set(),  // for merge
  rationales: {},          // draft_id → rationale from last categorize call
};

function renderCategorizeSection() {
  const fieldset = $("#categorize-section");
  const btn = $("#btn-categorize");
  const meta = $("#categorize-meta");
  const pane = $("#buckets-pane");

  const allDrafts = state.session.drafts || [];
  fieldset.disabled = false;

  if (!allDrafts.length) {
    btn.disabled = true;
    meta.textContent = "No drafts to categorize yet. Run Extract on a chunk first.";
    pane.innerHTML = "";
    return;
  }

  const unbucketed = allDrafts.filter((d) => !d.bucket);
  const bucketed = allDrafts.length - unbucketed.length;
  btn.disabled = unbucketed.length === 0;
  if (unbucketed.length === 0) {
    meta.textContent =
      `All ${allDrafts.length} draft${allDrafts.length === 1 ? "" : "s"} categorized. ` +
      `Reassign via the dropdown on any card.`;
  } else {
    const verb = unbucketed.length === 1 ? "needs" : "need";
    meta.textContent =
      `${unbucketed.length} draft${unbucketed.length === 1 ? "" : "s"} ${verb} bucketing ` +
      `· ${bucketed} already assigned.`;
  }

  pane.innerHTML = "";
  // Show one panel per bucket. Unbucketed drafts live in a special "(needs bucket)" panel.
  const byBucket = {};
  for (const b of BUCKETS) byBucket[b] = [];
  byBucket["__unbucketed"] = [];
  for (const d of allDrafts) {
    if (d.bucket && byBucket[d.bucket]) byBucket[d.bucket].push(d);
    else byBucket["__unbucketed"].push(d);
  }

  if (byBucket["__unbucketed"].length) {
    pane.appendChild(renderBucketPanel("__unbucketed", byBucket["__unbucketed"]));
  }
  for (const b of BUCKETS) {
    pane.appendChild(renderBucketPanel(b, byBucket[b]));
  }
}

function renderBucketPanel(bucketId, drafts) {
  const panel = document.createElement("div");
  panel.className = "bucket-panel";
  if (!drafts.length) panel.classList.add("empty");

  const header = document.createElement("div");
  header.className = "bucket-header";

  const title = document.createElement("span");
  title.className = "bucket-title";
  title.textContent = bucketId === "__unbucketed" ? "(needs bucket)" : bucketId;
  header.appendChild(title);

  const count = document.createElement("span");
  count.className = "bucket-count";
  count.textContent = `${drafts.length} draft${drafts.length === 1 ? "" : "s"}`;
  header.appendChild(count);

  const actions = document.createElement("div");
  actions.className = "bucket-actions";
  const mergeBtn = document.createElement("button");
  mergeBtn.type = "button";
  mergeBtn.textContent = "Merge selected";
  const selectedInThisBucket = drafts.filter((d) => categorizeState.selectedIds.has(d.id));
  mergeBtn.disabled = selectedInThisBucket.length !== 2;
  mergeBtn.addEventListener("click", () => mergeSelectedInBucket(drafts));
  actions.appendChild(mergeBtn);
  header.appendChild(actions);

  panel.appendChild(header);

  if (drafts.length) {
    const cards = document.createElement("div");
    cards.className = "bucket-cards";
    for (const d of drafts) cards.appendChild(renderBucketCard(d, bucketId));
    panel.appendChild(cards);
  }
  return panel;
}

function renderBucketCard(draft, panelBucketId) {
  const card = document.createElement("div");
  card.className = "bucket-card";
  if (draft.user_confirmed) card.classList.add("confirmed");
  if (categorizeState.selectedIds.has(draft.id)) card.classList.add("selected");

  const top = document.createElement("div");
  top.className = "bucket-card-top";

  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = categorizeState.selectedIds.has(draft.id);
  cb.addEventListener("change", () => {
    if (cb.checked) categorizeState.selectedIds.add(draft.id);
    else categorizeState.selectedIds.delete(draft.id);
    renderCategorizeSection();
  });
  top.appendChild(cb);

  // Tier badge
  const tier = document.createElement("span");
  tier.className = `tier-badge tier-${draft.tier}`;
  tier.textContent = draft.tier;
  top.appendChild(tier);

  // Bucket dropdown
  const sel = document.createElement("select");
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = "(no bucket)";
  sel.appendChild(empty);
  for (const b of BUCKETS) {
    const opt = document.createElement("option");
    opt.value = b;
    opt.textContent = b;
    if (draft.bucket === b) opt.selected = true;
    sel.appendChild(opt);
  }
  if (!draft.bucket) sel.value = "";
  sel.addEventListener("change", () => {
    draft.bucket = sel.value || null;
    queueSave({ drafts: state.session.drafts });
    flushSaveNow().then(() => renderCategorizeSection());
  });
  top.appendChild(sel);

  card.appendChild(top);

  // Bullet text (read-only for this view; edit in Extract panel)
  const bullet = document.createElement("div");
  bullet.className = "bucket-card-bullet";
  bullet.textContent = draft.draft_bullet;
  card.appendChild(bullet);

  // Rationale from last categorize call, if any
  const rationale = categorizeState.rationales[draft.id];
  if (rationale) {
    const r = document.createElement("div");
    r.className = "bucket-card-rationale";
    r.textContent = `AI: ${rationale}`;
    card.appendChild(r);
  }
  return card;
}

async function runCategorize() {
  await flushSaveNow();
  status("Categorizing…", "saving");
  try {
    const res = await fetch(
      `/api/wizard/${state.session.id}/categorize`,
      { method: "POST", headers: { "content-type": "application/json" } },
    );
    if (!res.ok) {
      const txt = await res.text();
      throw new Error(`${res.status} ${res.statusText}: ${txt}`);
    }
    const data = await res.json();
    state.session = data.session;
    categorizeState.rationales = data.rationales || {};
    renderCategorizeLLMCall(data.llm_call, data.provider);
    renderCategorizeSection();
    renderExtractSection();
    if (data.assigned_count === 0 && data.hint) {
      status(data.hint, "saved");
    } else {
      status(`Categorized ${data.assigned_count} draft${data.assigned_count === 1 ? "" : "s"}`, "saved");
    }
    setTimeout(() => status(null), 2200);
  } catch (err) {
    status(`Categorize failed: ${err.message}`, "error");
  }
}

async function mergeSelectedInBucket(draftsInBucket) {
  const selected = draftsInBucket.filter((d) => categorizeState.selectedIds.has(d.id));
  if (selected.length !== 2) return;
  const [a, b] = selected;
  if (!confirm(
    `Merge these two drafts into one?\n\n` +
    `1) ${a.draft_bullet}\n\n2) ${b.draft_bullet}\n\n` +
    `You'll edit the combined bullet manually after the merge.`
  )) return;

  status("Merging…", "saving");
  try {
    const res = await fetch(
      `/api/wizard/${state.session.id}/drafts/${a.id}/merge`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ with: b.id }),
      },
    );
    if (!res.ok) {
      const txt = await res.text();
      throw new Error(`${res.status} ${res.statusText}: ${txt}`);
    }
    const data = await res.json();
    state.session = data.session;
    categorizeState.selectedIds.clear();
    renderCategorizeSection();
    renderExtractSection();
    status("Merged. Edit the combined bullet in the Extract panel above.", "saved");
    setTimeout(() => status(null), 2500);
  } catch (err) {
    status(`Merge failed: ${err.message}`, "error");
  }
}

function renderCategorizeLLMCall(call, provider) {
  const details = $("#categorize-llm-details");
  if (!call) {
    details.hidden = true;
    return;
  }
  $("#categorize-llm-system").textContent = call.system_prompt || "";
  $("#categorize-llm-user").textContent = call.user_message || "";
  $("#categorize-llm-raw").textContent = call.raw_response || "";
  $("#categorize-llm-provider").textContent =
    provider ? `Provider: ${provider.name} — ${provider.reason}` : "";
  details.hidden = false;
}

// ---------- Phase 4: education ----------

const EDU_STATUSES = [
  { id: "graduated",          label: "Graduated",                   hint: "Standard completed degree." },
  { id: "in_progress",        label: "In progress (still studying)", hint: "Renders as 'in progress' with the expected year." },
  { id: "dropout",            label: "Dropped out",                 hint: "Renders as 'Attended <school>' with year range." },
  { id: "deferred_admit",     label: "Deferred admit",              hint: "Admitted but didn't enroll yet." },
  { id: "rejected_admit",     label: "Declined admit",              hint: "Accepted at an elite place, chose not to attend. Use sparingly." },
  { id: "on_leave",           label: "On leave / sabbatical",       hint: "Mid-degree leave; resumes later." },
  { id: "certification_only", label: "Certification only",          hint: "Coursera, AWS, Google Cloud, NPTEL, etc." },
  { id: "online_only",        label: "Online programme",            hint: "Fully online degree." },
];

function ensureEducationDefaults(edu) {
  // Pydantic shapes optional fields as null on the wire; the form needs
  // sensible strings + arrays to bind to.
  return {
    id: edu.id,
    school: edu.school || "",
    degree: edu.degree || "",
    year: edu.year || "",
    location: edu.location || "",
    notes: edu.notes || "",
    status: edu.status || "graduated",
    gpa: edu.gpa || "",
    reason: edu.reason || "",
    awards: Array.isArray(edu.awards) ? edu.awards.map((a) => ({
      name: a.name || "",
      criteria: a.criteria || "",
      year: a.year || "",
    })) : [],
  };
}

// Position-of-strength prompt per status. Empty string for graduated —
// the form hides the reason input entirely when there's nothing useful to ask.
const REASON_PLACEHOLDERS = {
  graduated: "",
  in_progress: "",
  dropout: "e.g. Left in junior year to co-found Acme — acquired 2022 for ₹40 Cr",
  rejected_admit: "e.g. Declined Stanford MBA to scale a healthtech startup at ₹2 Cr ARR",
  deferred_admit: "e.g. Deferred to take a research role at TIFR before starting",
  on_leave: "e.g. Took a year off to lead the founding team through Series A",
  certification_only: "",
  online_only: "",
};

function renderEducationSection() {
  const list = $("#education-list");
  if (!list) return;
  list.innerHTML = "";
  const entries = state.session.education || [];
  if (!entries.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "No education entries yet. Click ‘Add education entry’ to start.";
    list.appendChild(empty);
    return;
  }
  entries.forEach((edu, idx) => {
    list.appendChild(renderEducationCard(edu, idx));
  });
}

function findEdu(eduId) {
  // Look up the live entry from state.session.education at event time.
  // The server's PATCH response replaces state.session, so closures that
  // captured an entry object reference would otherwise mutate orphaned
  // copies that never reach disk.
  return (state.session.education || []).find((e) => e.id === eduId) || null;
}

function eduMutator(eduId, mutate) {
  return () => {
    const live = findEdu(eduId);
    if (!live) return;
    mutate(live);
    queueSave({ education: state.session.education });
  };
}

function renderEducationCard(edu, idx) {
  const card = document.createElement("div");
  card.className = "education-card";
  card.dataset.eduId = edu.id;

  // Header
  const header = document.createElement("div");
  header.className = "edu-header";
  const title = document.createElement("span");
  title.className = "edu-title";
  const setTitle = () => {
    const live = findEdu(edu.id) || edu;
    title.textContent = `${idx + 1}. ${live.degree || "(no degree)"} — ${live.school || "(no school)"}`;
  };
  setTitle();
  const del = document.createElement("button");
  del.type = "button";
  del.className = "edu-delete";
  del.textContent = "Delete";
  del.addEventListener("click", () => removeEducation(edu.id));
  header.append(title, del);
  card.appendChild(header);

  // 2-column grid of fields
  const grid = document.createElement("div");
  grid.className = "edu-grid";

  grid.appendChild(textField("School / Institution", edu.school, (v) => {
    const live = findEdu(edu.id);
    if (!live) return;
    live.school = v;
    setTitle();
    queueSave({ education: state.session.education });
  }));
  grid.appendChild(textField("Degree / Programme", edu.degree, (v) => {
    const live = findEdu(edu.id);
    if (!live) return;
    live.degree = v;
    setTitle();
    queueSave({ education: state.session.education });
  }));

  // Status
  const statusWrap = document.createElement("label");
  statusWrap.textContent = "Status";
  const statusSel = document.createElement("select");
  EDU_STATUSES.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.label;
    if (edu.status === s.id) opt.selected = true;
    statusSel.appendChild(opt);
  });
  const statusHint = document.createElement("span");
  statusHint.className = "edu-status-hint";
  statusHint.textContent = (EDU_STATUSES.find((s) => s.id === edu.status) || {}).hint || "";
  statusWrap.append(statusSel, statusHint);
  grid.appendChild(statusWrap);

  // Position-of-strength reason (conditional on non-standard statuses)
  const reasonWrap = document.createElement("label");
  reasonWrap.className = "wide edu-reason-wrap";
  reasonWrap.innerHTML = "Reason / your framing <span class='edu-status-hint'>(optional — turns a non-standard status into a strength)</span>";
  const reasonTa = document.createElement("textarea");
  reasonTa.rows = 2;
  reasonTa.className = "edu-reason";
  reasonTa.value = edu.reason || "";
  reasonTa.placeholder = REASON_PLACEHOLDERS[edu.status] || "";
  reasonTa.addEventListener("input", eduMutator(edu.id, (live) => {
    live.reason = reasonTa.value;
  }));
  reasonWrap.appendChild(reasonTa);
  grid.appendChild(reasonWrap);

  const showReason = (status) => {
    const placeholder = REASON_PLACEHOLDERS[status] || "";
    if (placeholder) {
      reasonWrap.hidden = false;
      reasonTa.placeholder = placeholder;
    } else {
      reasonWrap.hidden = true;
    }
  };
  showReason(edu.status);

  // Wire the status dropdown last so it can update both the hint and
  // the reason field's visibility in one go.
  statusSel.addEventListener("change", eduMutator(edu.id, (live) => {
    live.status = statusSel.value;
    statusHint.textContent = (EDU_STATUSES.find((s) => s.id === live.status) || {}).hint || "";
    showReason(live.status);
  }));

  grid.appendChild(textField("Year (or range, e.g. 2018–2022)", edu.year, (v) =>
    eduMutator(edu.id, (live) => { live.year = v; })()));
  grid.appendChild(textField("Location (optional)", edu.location, (v) =>
    eduMutator(edu.id, (live) => { live.location = v; })()));
  grid.appendChild(textField(
    "GPA / CGPA / % (leave blank if below the Bock bar)",
    edu.gpa,
    (v) => eduMutator(edu.id, (live) => { live.gpa = v; })(),
  ));

  const notesWrap = document.createElement("label");
  notesWrap.className = "wide";
  notesWrap.textContent = "Notes (optional — coursework, thesis, etc.)";
  const notesTa = document.createElement("textarea");
  notesTa.rows = 2;
  notesTa.value = edu.notes || "";
  notesTa.addEventListener("input", eduMutator(edu.id, (live) => {
    live.notes = notesTa.value;
  }));
  notesWrap.appendChild(notesTa);
  grid.appendChild(notesWrap);

  card.appendChild(grid);

  // Awards subsection
  card.appendChild(renderAwardsSection(edu));

  return card;
}

function textField(labelText, value, onChange) {
  const wrap = document.createElement("label");
  wrap.textContent = labelText;
  const input = document.createElement("input");
  input.type = "text";
  input.value = value || "";
  input.addEventListener("input", () => onChange(input.value));
  wrap.appendChild(input);
  return wrap;
}

function renderAwardsSection(edu) {
  const section = document.createElement("div");
  section.className = "awards-section";
  const h = document.createElement("h4");
  h.textContent = "Distinctions, awards, and standout achievements";
  section.appendChild(h);
  const sub = document.createElement("p");
  sub.className = "hint";
  sub.innerHTML =
    "Not just medals &mdash; surface anything extraordinary tied to this " +
    "period: <em>All-India Rank 12, JEE Advanced (1st of ~150,000)</em> · " +
    "<em>Captain, inter-college cricket &mdash; led to national semifinal</em> · " +
    "<em>Topped university in CS (1st of cohort of 240)</em>. " +
    "Criteria column is required &mdash; per Bock, a trophy without context is noise.";
  sub.style.marginTop = "0";
  sub.style.marginBottom = "6px";
  section.appendChild(sub);

  const rows = document.createElement("div");
  rows.className = "awards-rows";
  (edu.awards || []).forEach((_award, awardIdx) => {
    rows.appendChild(renderAwardRow(edu.id, awardIdx, rows));
  });
  section.appendChild(rows);

  const add = document.createElement("button");
  add.type = "button";
  add.textContent = "+ Add award";
  add.style.fontSize = "12px";
  add.style.padding = "2px 8px";
  add.addEventListener("click", () => {
    const live = findEdu(edu.id);
    if (!live) return;
    live.awards = live.awards || [];
    live.awards.push({ name: "", criteria: "", year: "" });
    queueSave({ education: state.session.education });
    rows.appendChild(renderAwardRow(edu.id, live.awards.length - 1, rows));
  });
  section.appendChild(add);
  return section;
}

function awardAt(eduId, idx) {
  const live = findEdu(eduId);
  if (!live || !Array.isArray(live.awards) || !live.awards[idx]) return null;
  return live.awards[idx];
}

function renderAwardRow(eduId, idx, rowsContainer) {
  const award = awardAt(eduId, idx);
  if (!award) return document.createElement("div");

  const row = document.createElement("div");
  row.className = "award-row";
  if (!award.criteria) row.classList.add("missing-criteria");

  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.placeholder = "Distinction / award / role";
  nameInput.value = award.name || "";
  nameInput.addEventListener("input", () => {
    const a = awardAt(eduId, idx);
    if (!a) return;
    a.name = nameInput.value;
    queueSave({ education: state.session.education });
  });

  const critInput = document.createElement("input");
  critInput.type = "text";
  critInput.placeholder = "Criteria / scale (e.g. 1st of cohort of 240)";
  critInput.className = "award-criteria";
  critInput.value = award.criteria || "";
  critInput.addEventListener("input", () => {
    const a = awardAt(eduId, idx);
    if (!a) return;
    a.criteria = critInput.value;
    if (a.criteria) row.classList.remove("missing-criteria");
    else row.classList.add("missing-criteria");
    queueSave({ education: state.session.education });
  });

  const yearInput = document.createElement("input");
  yearInput.type = "text";
  yearInput.placeholder = "Year";
  yearInput.value = award.year || "";
  yearInput.addEventListener("input", () => {
    const a = awardAt(eduId, idx);
    if (!a) return;
    a.year = yearInput.value;
    queueSave({ education: state.session.education });
  });

  const del = document.createElement("button");
  del.type = "button";
  del.textContent = "×";
  del.title = "Remove award";
  del.addEventListener("click", () => {
    const live = findEdu(eduId);
    if (!live || !live.awards) return;
    live.awards.splice(idx, 1);
    queueSave({ education: state.session.education });
    rowsContainer.innerHTML = "";
    (live.awards || []).forEach((_a, i) =>
      rowsContainer.appendChild(renderAwardRow(eduId, i, rowsContainer))
    );
  });

  row.append(nameInput, critInput, yearInput, del);
  return row;
}

function addEducationEntry() {
  const id = `edu-${Date.now()}-${Math.floor(Math.random() * 1000)}`;
  state.session.education = state.session.education || [];
  state.session.education.push(ensureEducationDefaults({
    id, school: "", degree: "", year: "",
    location: "", notes: "", status: "graduated",
    gpa: "", awards: [],
  }));
  queueSave({ education: state.session.education });
  renderEducationSection();
}

function removeEducation(id) {
  state.session.education = (state.session.education || []).filter((e) => e.id !== id);
  queueSave({ education: state.session.education });
  renderEducationSection();
}

// ---------- mutations ----------

function activeChunk() {
  return state.session.chunks.find((c) => c.id === state.activeChunkId) || null;
}

function setActiveChunk(id) {
  state.activeChunkId = id;
  $("#dump-section").disabled = false;
  renderChunksList();
  renderActiveChunk();
  $("#raw-notes").focus();
}

function addChunk() {
  const id = `chunk-custom-${Date.now()}`;
  state.session.chunks.push({
    id,
    label: "New chunk",
    start: state.session.career_start || "2020-01",
    end: state.session.career_start || "2020-07",
    raw_notes: "",
  });
  queueSave({ chunks: state.session.chunks });
  setActiveChunk(id);
}

function removeChunk(id) {
  state.session.chunks = state.session.chunks.filter((c) => c.id !== id);
  if (state.activeChunkId === id) state.activeChunkId = null;
  queueSave({ chunks: state.session.chunks });
  renderAll();
}

async function regenerateChunks() {
  await flushSaveNow();
  if (!state.session.career_start) {
    status("Pick a career-start date first.", "error");
    return;
  }
  const cadence = $("#cadence-select")?.value || null;
  status("Regenerating chunks…", "saving");
  try {
    state.session = await api(
      `/api/wizard/${state.session.id}/regenerate-chunks`,
      {
        method: "POST",
        body: JSON.stringify(cadence ? { cadence } : {}),
      },
    );
    if (!state.session.chunks.find((c) => c.id === state.activeChunkId)) {
      state.activeChunkId = state.session.chunks[0]?.id || null;
    }
    renderAll();
    status("Chunks regenerated", "saved");
    setTimeout(() => status(null), 1200);
  } catch (err) {
    status(`Regenerate failed: ${err.message}`, "error");
  }
}

async function importFile(file) {
  status("Importing…", "saving");
  const fd = new FormData();
  fd.append("file", file);
  try {
    const res = await fetch(
      `/api/wizard/${state.session.id}/import-resume`,
      { method: "POST", body: fd },
    );
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    const chunk = activeChunk();
    if (!chunk) {
      status("Pick a chunk first, then import.", "error");
      return;
    }
    const sep = (chunk.raw_notes || "").trim() ? "\n\n---\n\n" : "";
    chunk.raw_notes = (chunk.raw_notes || "") + sep + data.text;
    $("#raw-notes").value = chunk.raw_notes;
    queueSave({ chunks: state.session.chunks });
    status(`Imported ${data.chars} chars from ${data.filename}`, "saved");
    setTimeout(() => status(null), 2000);
  } catch (err) {
    status(`Import failed: ${err.message}`, "error");
  }
}

// ---------- Résumé import (step 0 — the front door) ----------

async function importApply({ file = null, text = null, force = false } = {}) {
  status("Importing résumé…", "saving");
  try {
    let res;
    if (file) {
      const fd = new FormData();
      fd.append("file", file);
      if (force) fd.append("force", "1");
      res = await fetch(`/api/wizard/${state.session.id}/import-apply`, {
        method: "POST",
        body: fd,
      });
    } else {
      res = await fetch(`/api/wizard/${state.session.id}/import-apply`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ text, force }),
      });
    }
    const data = await res.json().catch(() => ({}));
    if (res.status === 409) {
      const replace = confirm(
        "This session already has notes, drafts, or basics. " +
        "Replace them with the imported résumé?",
      );
      if (replace) return importApply({ file, text, force: true });
      status(null);
      return;
    }
    if (!res.ok) {
      throw new Error(data.hint || data.detail || data.error || res.statusText);
    }
    if (data.copy_paste_required) {
      showImportCopyPaste(data);
      status(null);
      return;
    }
    finishImport(data);
  } catch (err) {
    status(`Import failed: ${err.message}`, "error");
  }
}

function finishImport(data) {
  state.session = data.session;
  state.session.education = (state.session.education || []).map(ensureEducationDefaults);
  state.session.basics = ensureBasicsDefaults(state.session.basics);
  state.session.employment = state.session.employment || [];
  if (state.session.chunks.length) {
    state.activeChunkId = state.session.chunks[0].id;
  }
  renderAll();

  const s = data.summary || {};
  const bits = [];
  if (s.employment_chunks) {
    bits.push(`${s.employment_chunks} job${s.employment_chunks === 1 ? "" : "s"} → career chunks`);
  }
  if (s.education_entries) bits.push(`${s.education_entries} education`);
  if (s.skills) bits.push(`${s.skills} skills`);
  if (s.basics_filled) bits.push("basics");
  if (s.summary_filled) bits.push("summary");

  const el = $("#import-result");
  el.hidden = false;
  el.className = "import-result ok";
  el.innerHTML =
    `<strong>Imported:</strong> ${bits.map(escapeHtml).join(" · ") || "nothing recognized"}.` +
    ((s.warnings || []).length
      ? `<div class="import-warnings">${s.warnings.map((w) => `&#9888; ${escapeHtml(w)}`).join("<br>")}</div>`
      : "") +
    `<div class="import-next">Everything below is now a review pass: confirm your ` +
    `role family (step 1), then run <strong>Extract</strong> on each pre-seeded chunk (step 3).</div>`;
  $("#import-copypaste").hidden = true;
  status("Résumé imported", "saved");
  setTimeout(() => status(null), 2500);
}

function showImportCopyPaste(data) {
  const wrap = $("#import-copypaste");
  wrap.hidden = false;
  $("#import_cp_prompt").value =
    `# === SYSTEM ===\n${data.system_prompt}\n\n# === USER MESSAGE ===\n${data.user_message}\n`;
}

async function importApplyResponse() {
  const raw = $("#import_cp_reply").value.trim();
  if (!raw) {
    status("Paste Claude's JSON reply first.", "error");
    return;
  }
  status("Applying reply…", "saving");
  try {
    let res = await fetch(`/api/wizard/${state.session.id}/import-apply-response`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ response_text: raw }),
    });
    let data = await res.json().catch(() => ({}));
    if (res.status === 409) {
      const replace = confirm(
        "This session already has content. Replace it with the imported résumé?",
      );
      if (!replace) { status(null); return; }
      res = await fetch(`/api/wizard/${state.session.id}/import-apply-response`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ response_text: raw, force: true }),
      });
      data = await res.json().catch(() => ({}));
    }
    if (!res.ok) {
      throw new Error(data.hint || data.detail || data.error || res.statusText);
    }
    finishImport(data);
  } catch (err) {
    status(`Apply failed: ${err.message}`, "error");
  }
}

// ---------- sessions gallery ----------

function relativeTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 2) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

async function loadSessionsGallery() {
  let data;
  try {
    data = await api("/api/wizard/sessions");
  } catch (_) {
    return; // gallery is a nicety — never block the wizard on it
  }
  const sessions = data.sessions || [];
  const others = sessions.filter((s) => s.id !== state.session.id);
  const gallery = $("#sessions-gallery");
  if (!others.length) {
    gallery.hidden = true;
    return;
  }
  gallery.hidden = false;
  $("#sessions-count").textContent =
    `${others.length} other${others.length === 1 ? "" : "s"}`;

  const list = $("#sessions-list");
  list.innerHTML = "";
  sessions.forEach((s) => {
    const row = document.createElement("div");
    row.className = "session-row" + (s.id === state.session.id ? " current" : "");
    const progress = s.promoted
      ? "saved to master ✓"
      : `${s.dumped_chunks}/${s.chunks} chunks dumped · ${s.drafts} drafts`;
    row.innerHTML =
      `<span class="session-label">${escapeHtml(s.label)}</span>` +
      `<span class="session-meta">${escapeHtml(relativeTime(s.updated_at))} · ${escapeHtml(progress)}</span>`;

    const actions = document.createElement("span");
    actions.className = "session-actions";
    if (s.id === state.session.id) {
      const chip = document.createElement("span");
      chip.className = "session-chip";
      chip.textContent = "current";
      actions.appendChild(chip);
    } else {
      const openBtn = document.createElement("button");
      openBtn.type = "button";
      openBtn.textContent = "Continue →";
      openBtn.addEventListener("click", () => {
        window.location.href = `/wizard?session=${encodeURIComponent(s.id)}`;
      });
      actions.appendChild(openBtn);
    }
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "session-delete";
    delBtn.textContent = "Delete";
    delBtn.addEventListener("click", async () => {
      if (!confirm(`Delete session "${s.label}"? This cannot be undone.`)) return;
      try {
        const res = await fetch(`/api/wizard/${s.id}`, { method: "DELETE" });
        if (!res.ok) throw new Error(await res.text());
        if (s.id === state.session.id) {
          // Deleted the session we're in — start fresh.
          window.location.href = "/wizard";
          return;
        }
        loadSessionsGallery();
      } catch (err) {
        status(`Delete failed: ${err.message}`, "error");
      }
    });
    actions.appendChild(delBtn);
    row.appendChild(actions);
    list.appendChild(row);
  });
}

// ---------- wiring ----------

function navigateChunk(direction) {
  const idx = state.session.chunks.findIndex((c) => c.id === state.activeChunkId);
  const next = idx + direction;
  if (next < 0 || next >= state.session.chunks.length) return;
  setActiveChunk(state.session.chunks[next].id);
}

function wireEventHandlers() {
  $$("input[name='role_family']").forEach((el) => {
    el.addEventListener("change", () => {
      state.session.role_family = el.value;
      renderRoleFamily();
      // Server merges role_family + sends back refreshed prompts.
      queueSave({ role_family: el.value });
    });
  });

  $("#role_family_other").addEventListener("input", (e) => {
    state.session.role_family_other = e.target.value;
    queueSave({ role_family_other: e.target.value });
  });

  $("#career_start").addEventListener("change", (e) => {
    state.session.career_start = e.target.value;
    queueSave({ career_start: e.target.value });
  });

  $("#btn-regenerate").addEventListener("click", regenerateChunks);

  // Phase 10 item 2: cadence picker — selecting an option re-renders the
  // guidance copy. The actual chunk regeneration runs when the user clicks
  // Regenerate (or when the picker is bound to btn-regenerate via UX prefs).
  $("#cadence-select")?.addEventListener("change", (e) => {
    state.session.cadence = e.target.value;
    renderCadencePicker();
  });

  $("#raw-notes").addEventListener("input", (e) => {
    const chunk = activeChunk();
    if (!chunk) return;
    chunk.raw_notes = e.target.value;
    $("#chunk-meta").textContent =
      `${chunk.raw_notes.length} characters · ${chunkWordCount(chunk)} words`;
    renderChunksSummary();
    refreshChunkRowStatus(chunk.id);
    queueSave({ chunks: state.session.chunks });
  });

  $("#btn-prev-chunk").addEventListener("click", () => navigateChunk(-1));
  $("#btn-next-chunk").addEventListener("click", () => navigateChunk(+1));

  $("#btn-extract").addEventListener("click", () => runExtract());
  $("#btn-categorize").addEventListener("click", () => runCategorize());
  $("#btn-add-education").addEventListener("click", addEducationEntry);

  $("#import_file").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (file) importFile(file);
    e.target.value = "";
  });

  // Step 0 — résumé import front door.
  $("#import_hero_file").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (file) importApply({ file });
    e.target.value = "";
  });
  $("#btn-import-paste-toggle").addEventListener("click", () => {
    const wrap = $("#import-paste-wrap");
    wrap.hidden = !wrap.hidden;
    if (!wrap.hidden) $("#import_paste_text").focus();
  });
  $("#btn-import-run").addEventListener("click", () => {
    const text = $("#import_paste_text").value.trim();
    if (text.length < 200) {
      status("That looks too short to be a résumé — paste the whole thing.", "error");
      return;
    }
    importApply({ text });
  });
  $("#btn-import-cp-copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("#import_cp_prompt").value);
      status("Prompt copied", "saved");
      setTimeout(() => status(null), 1200);
    } catch (_) {
      $("#import_cp_prompt").select();
      document.execCommand("copy");
    }
  });
  $("#btn-import-cp-apply").addEventListener("click", importApplyResponse);

  // Save on tab/visibility change so no edits are lost.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushSaveNow();
  });
  window.addEventListener("beforeunload", () => {
    if (Object.keys(state.pendingPatch).length) flushSaveNow();
  });

  // Cmd/Ctrl+Shift+M toggles dictation without leaving the textarea.
  window.addEventListener("keydown", (e) => {
    if (!e.shiftKey) return;
    if (!(e.metaKey || e.ctrlKey)) return;
    if ((e.key || "").toLowerCase() !== "m") return;
    if (!state.voice) return;
    e.preventDefault();
    handleMicClick();
  });
}

// ---------- voice typing ----------

function handleMicClick() {
  if (!state.voice) return;
  // First click ever in this browser — show the privacy disclosure modal.
  if (!state.voice.isRecording && !localStorage.getItem(VOICE_OK_KEY)) {
    const dlg = $("#voice-privacy-dialog");
    dlg.addEventListener("close", function once() {
      dlg.removeEventListener("close", once);
      if (dlg.returnValue === "ok") {
        localStorage.setItem(VOICE_OK_KEY, "1");
        startDictation();
      }
    });
    if (typeof dlg.showModal === "function") dlg.showModal();
    else startDictation();   // very old browsers — fall through
    return;
  }
  state.voice.toggle();
  if (state.voice.isRecording) state.activeChunkId && $("#raw-notes").focus();
}

function startDictation() {
  if (!state.voice) return;
  state.voice.start();
  if (state.activeChunkId) $("#raw-notes").focus();
}

function initVoice() {
  if (!window.VoiceInput || !VoiceInput.isAvailable()) {
    // Button stays hidden via its `unsupported` state.
    const button = $("#btn-voice");
    if (button) {
      button.hidden = true;
      button.disabled = true;
    }
    return;
  }
  const button = $("#btn-voice");
  const liveRegion = $("#voice-live");
  if (!button) return;
  button.hidden = false;
  state.voice = new VoiceInput($("#raw-notes"), { lang: "en-IN" }).attach({
    button,
    status: liveRegion,
    onError: (msg) => status(msg, "error"),
  });
  button.addEventListener("click", (e) => {
    // VoiceInput.attach already wired a click→toggle. We layer the
    // privacy-modal gate on top, so swallow the inner click and re-route.
    e.stopImmediatePropagation();
    handleMicClick();
  }, true /* capture, run before VoiceInput's listener */);
}

// ---------- boot ----------

// ---------- Phase 6: basics + employment + promote ----------

function ensureBasicsDefaults(b) {
  return {
    name: (b && b.name) || "",
    email: (b && b.email) || "",
    phone: (b && b.phone) || "",
    location: (b && b.location) || "",
    links: Array.isArray(b && b.links) ? b.links.map((l) => ({
      label: l.label || "", url: l.url || "",
    })) : [],
  };
}

function renderBasicsSection() {
  const grid = $("#basics-grid");
  if (!grid) return;
  grid.innerHTML = "";

  if (!state.session.basics) {
    state.session.basics = ensureBasicsDefaults(null);
  }
  const b = state.session.basics;

  const saveBasics = () => queueSave({ basics: state.session.basics });

  grid.appendChild(basicField("Name (required)", b.name, (v) => {
    state.session.basics.name = v;
    saveBasics();
  }));
  grid.appendChild(basicField("Email", b.email, (v) => {
    state.session.basics.email = v;
    saveBasics();
  }, "email"));
  grid.appendChild(basicField("Phone (e.g. +91 98XXX XXXXX)", b.phone, (v) => {
    state.session.basics.phone = v;
    saveBasics();
  }, "tel"));
  grid.appendChild(basicField("Location (city, country)", b.location, (v) => {
    state.session.basics.location = v;
    saveBasics();
  }));

  // Summary (optional one-liner) is wired into basics-grid too because
  // it's another header-area field.
  const summaryWrap = document.createElement("label");
  summaryWrap.className = "wide";
  summaryWrap.textContent = "Summary / headline (optional one-liner)";
  const summaryInput = document.createElement("input");
  summaryInput.type = "text";
  summaryInput.value = state.session.summary || "";
  summaryInput.placeholder = "e.g. Backend engineer · 6 years · distributed systems + payments";
  summaryInput.addEventListener("input", () => {
    state.session.summary = summaryInput.value;
    queueSave({ summary: summaryInput.value });
  });
  summaryWrap.appendChild(summaryInput);
  grid.appendChild(summaryWrap);

  // Links subsection
  const linksSection = document.createElement("div");
  linksSection.className = "basics-links-section";
  const linksH = document.createElement("h4");
  linksH.textContent = "Links (LinkedIn / GitHub / portfolio / etc.)";
  linksSection.appendChild(linksH);
  const linksRows = document.createElement("div");
  linksSection.appendChild(linksRows);
  (b.links || []).forEach((_, idx) =>
    linksRows.appendChild(renderLinkRow(idx, linksRows)));
  const addLink = document.createElement("button");
  addLink.type = "button";
  addLink.textContent = "+ Add link";
  addLink.style.fontSize = "12px";
  addLink.style.padding = "2px 8px";
  addLink.style.marginTop = "4px";
  addLink.addEventListener("click", () => {
    state.session.basics.links.push({ label: "", url: "" });
    saveBasics();
    linksRows.appendChild(renderLinkRow(state.session.basics.links.length - 1, linksRows));
  });
  linksSection.appendChild(addLink);
  grid.appendChild(linksSection);
}

function basicField(label, value, onChange, type = "text") {
  const wrap = document.createElement("label");
  wrap.textContent = label;
  const input = document.createElement("input");
  input.type = type;
  input.value = value || "";
  input.addEventListener("input", () => onChange(input.value));
  wrap.appendChild(input);
  return wrap;
}

function renderLinkRow(idx, rowsContainer) {
  const link = state.session.basics.links[idx];
  if (!link) return document.createElement("div");
  const row = document.createElement("div");
  row.className = "link-row";

  const labelInput = document.createElement("input");
  labelInput.type = "text";
  labelInput.placeholder = "LinkedIn / GitHub / …";
  labelInput.value = link.label || "";
  labelInput.addEventListener("input", () => {
    const live = state.session.basics.links[idx];
    if (!live) return;
    live.label = labelInput.value;
    queueSave({ basics: state.session.basics });
  });

  const urlInput = document.createElement("input");
  urlInput.type = "url";
  urlInput.placeholder = "https://…";
  urlInput.value = link.url || "";
  urlInput.addEventListener("input", () => {
    const live = state.session.basics.links[idx];
    if (!live) return;
    live.url = urlInput.value;
    queueSave({ basics: state.session.basics });
  });

  const del = document.createElement("button");
  del.type = "button";
  del.textContent = "×";
  del.title = "Remove link";
  del.addEventListener("click", () => {
    state.session.basics.links.splice(idx, 1);
    queueSave({ basics: state.session.basics });
    rowsContainer.innerHTML = "";
    state.session.basics.links.forEach((_, i) =>
      rowsContainer.appendChild(renderLinkRow(i, rowsContainer)));
  });

  row.append(labelInput, urlInput, del);
  return row;
}

// ----- Employment: one row per chunk that has experience-bucketed drafts -----

function chunksWithExperienceDrafts() {
  const have = new Set();
  (state.session.drafts || []).forEach((d) => {
    if (d.bucket === "experience") have.add(d.chunk_id);
  });
  return (state.session.chunks || []).filter((c) => have.has(c.id));
}

function findOrCreateEmployment(chunkId) {
  let live = (state.session.employment || []).find((e) => e.chunk_id === chunkId);
  if (!live) {
    live = { chunk_id: chunkId, company: "", role: "",
             location: null, start_override: null, end_override: null };
    state.session.employment = state.session.employment || [];
    state.session.employment.push(live);
  }
  return live;
}

function renderEmploymentSection() {
  const list = $("#employment-list");
  if (!list) return;
  const empty = $("#employment-empty");
  list.innerHTML = "";

  const chunks = chunksWithExperienceDrafts();
  if (!chunks.length) {
    list.appendChild(empty);
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;

  chunks.forEach((chunk) => {
    list.appendChild(renderEmploymentRow(chunk));
  });
}

function renderEmploymentRow(chunk) {
  const emp = findOrCreateEmployment(chunk.id);
  const row = document.createElement("div");
  row.className = "employment-row";

  const draftCount = (state.session.drafts || [])
    .filter((d) => d.chunk_id === chunk.id && d.bucket === "experience").length;
  const labelEl = document.createElement("div");
  labelEl.className = "emp-chunk-label";
  labelEl.innerHTML =
    `<strong>${chunk.label}</strong> (${chunk.start} → ${chunk.end}) · ` +
    `${draftCount} experience draft${draftCount === 1 ? "" : "s"}`;
  row.appendChild(labelEl);

  const grid = document.createElement("div");
  grid.className = "emp-grid";

  const fieldsByKey = [
    { key: "company", label: "Company", placeholder: "e.g. Razorpay" },
    { key: "role", label: "Role / title", placeholder: "e.g. Senior Backend Engineer" },
    { key: "location", label: "Location (optional)", placeholder: "Bengaluru, India" },
    { key: "start_override", label: "Start (YYYY-MM, optional)", placeholder: chunk.start },
    { key: "end_override", label: "End (YYYY-MM, optional)", placeholder: chunk.end },
  ];
  fieldsByKey.forEach(({ key, label, placeholder }) => {
    const wrap = document.createElement("label");
    wrap.textContent = label;
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = placeholder || "";
    input.value = (emp[key] != null ? emp[key] : "");
    input.addEventListener("input", () => {
      const live = findOrCreateEmployment(chunk.id);
      live[key] = input.value || null;
      queueSave({ employment: state.session.employment });
    });
    wrap.appendChild(input);
    grid.appendChild(wrap);
  });

  row.appendChild(grid);
  return row;
}

// ----- Promote / save master -----

function renderPromoteSection() {
  const meta = $("#promote-meta");
  if (!meta) return;
  if (state.session.promoted_master_path) {
    meta.textContent = `Last saved to: ${state.session.promoted_master_path}`;
  } else {
    meta.textContent = "Not yet saved.";
  }
}

async function previewMaster() {
  status("Assembling preview…", "saving");
  try {
    const res = await fetch(
      `/api/wizard/${state.session.id}/promote-preview`,
      { method: "POST" },
    );
    if (!res.ok) {
      const txt = await res.text();
      throw new Error(`${res.status} ${res.statusText}: ${txt}`);
    }
    const data = await res.json();
    const ta = $("#promote-yaml");
    ta.value = data.yaml;
    ta.disabled = false;
    $("#btn-save-master").disabled = false;
    renderPromoteWarnings(data.warnings);
    status("Preview ready — edit freely before saving.", "saved");
    setTimeout(() => status(null), 2000);
  } catch (err) {
    status(`Preview failed: ${err.message}`, "error");
  }
}

function renderPromoteWarnings(warnings) {
  const box = $("#promote-warnings");
  if (!warnings || warnings.length === 0) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  box.innerHTML = "<h4>Heads-up before you save</h4><ul></ul>";
  const ul = box.querySelector("ul");
  warnings.forEach((w) => {
    const li = document.createElement("li");
    li.textContent = w.message;
    ul.appendChild(li);
  });
}

async function saveMaster() {
  const ta = $("#promote-yaml");
  const user_yaml = (ta.value || "").trim();
  if (!user_yaml) {
    status("Click 'Preview master.yaml' first.", "error");
    return;
  }
  if (!confirm(
    "This will write to master.yaml in the project directory. " +
    "Any existing file is backed up first. Continue?"
  )) {
    return;
  }
  status("Saving master.yaml…", "saving");
  try {
    const res = await fetch(
      `/api/wizard/${state.session.id}/promote-save`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ yaml: user_yaml }),
      },
    );
    if (!res.ok) {
      const txt = await res.text();
      throw new Error(`${res.status} ${res.statusText}: ${txt}`);
    }
    const data = await res.json();
    state.session = data.session;
    renderPromoteSection();
    const info = $("#promote-saved-info");
    let msg = `Saved to ${data.saved_path}.`;
    if (data.backup_path) {
      msg += ` Previous version backed up to ${data.backup_path}.`;
    }
    info.textContent = msg;
    info.hidden = false;
    status("Master saved.", "saved");
    setTimeout(() => status(null), 3000);
  } catch (err) {
    status(`Save failed: ${err.message}`, "error");
  }
}

// ---------- Phase 6.5: LinkedIn profile builder ----------

const LINKEDIN_LIMITS = {
  headline: 220,
  about: 2000,
  experienceDescription: 2000,
};

async function buildLinkedIn() {
  await flushSaveNow();
  status("Generating LinkedIn profile — this calls the LLM 4 times (~30-90s)…", "saving");
  const btn = $("#btn-build-linkedin");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(
      `/api/wizard/${state.session.id}/linkedin`,
      { method: "POST" },
    );
    if (!res.ok) {
      const txt = await res.text();
      let detail = txt;
      try {
        const parsed = JSON.parse(txt);
        detail = parsed.hint || parsed.detail || parsed.error || txt;
      } catch (_) { /* keep raw */ }
      throw new Error(detail);
    }
    const data = await res.json();
    renderLinkedInOutput(data);
    status("LinkedIn profile ready — review and copy each block.", "saved");
    setTimeout(() => status(null), 3000);
  } catch (err) {
    status(`LinkedIn failed: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderLinkedInOutput(data) {
  const out = $("#linkedin-output");
  if (!out) return;
  out.hidden = false;

  const profile = data.profile || {};

  const headlineEl = $("#linkedin-headline");
  headlineEl.value = profile.headline || "";
  updateLinkedInCount("#linkedin-headline", "#linkedin-headline-count", LINKEDIN_LIMITS.headline);

  const aboutEl = $("#linkedin-about");
  aboutEl.value = profile.about || "";
  updateLinkedInCount("#linkedin-about", "#linkedin-about-count", LINKEDIN_LIMITS.about);

  renderLinkedInExperience(profile.experience || []);
  renderLinkedInFeatured(profile.featured || []);
  renderLinkedInSkills(profile.skills || [], profile.pinned_skills || []);
  renderLinkedInEducation(profile.education || []);

  $("#linkedin-md").value = data.plain_text || "";

  renderLinkedInWarnings(data.warnings || []);

  const meta = $("#linkedin-meta");
  if (meta) {
    const src = data.master_source === "saved_yaml"
      ? `from saved master ${data.saved_master_path}`
      : "from in-memory promote (save Step 9 first for a stable source)";
    meta.textContent = `Provider: ${data.provider?.name} · ${src}`;
  }
}

function updateLinkedInCount(textareaSel, counterSel, limit) {
  const ta = $(textareaSel);
  const counter = $(counterSel);
  if (!ta || !counter) return;
  const update = () => {
    const len = (ta.value || "").length;
    counter.textContent = `${len} / ${limit} chars`;
    counter.style.color = len > limit ? "var(--accent-red, #b3261e)" : "";
  };
  update();
  ta.addEventListener("input", update);
}

function renderLinkedInExperience(entries) {
  const list = $("#linkedin-experience");
  if (!list) return;
  list.innerHTML = "";
  if (entries.length === 0) {
    list.innerHTML = '<p class="hint">No experience entries in master.</p>';
    return;
  }
  entries.forEach((entry, idx) => {
    const wrap = document.createElement("div");
    wrap.className = "linkedin-role";

    const headline = document.createElement("input");
    headline.type = "text";
    headline.value = entry.headline || "";
    headline.className = "linkedin-role-headline";
    headline.setAttribute("aria-label", `Role ${idx + 1} headline`);

    const desc = document.createElement("textarea");
    desc.rows = 8;
    desc.value = entry.description || "";
    desc.className = "linkedin-role-description";
    desc.setAttribute("aria-label", `Role ${idx + 1} description`);

    const counter = document.createElement("span");
    counter.className = "hint inline";
    const updateCount = () => {
      const len = (desc.value || "").length;
      counter.textContent = `${len} / ${LINKEDIN_LIMITS.experienceDescription} chars`;
      counter.style.color = len > LINKEDIN_LIMITS.experienceDescription
        ? "var(--accent-red, #b3261e)" : "";
    };
    updateCount();
    desc.addEventListener("input", updateCount);

    wrap.appendChild(headline);
    wrap.appendChild(desc);
    wrap.appendChild(counter);
    list.appendChild(wrap);
  });
}

function renderLinkedInFeatured(items) {
  const list = $("#linkedin-featured");
  if (!list) return;
  list.innerHTML = "";
  if (items.length === 0) {
    list.innerHTML = '<p class="hint">No Featured items picked. Add projects to master first.</p>';
    return;
  }
  items.forEach((item, idx) => {
    const wrap = document.createElement("div");
    wrap.className = "linkedin-featured-item";

    const title = document.createElement("input");
    title.type = "text";
    title.value = item.title || "";
    title.className = "linkedin-featured-title";
    title.setAttribute("aria-label", `Featured ${idx + 1} title`);

    const desc = document.createElement("textarea");
    desc.rows = 3;
    desc.value = item.description || "";
    desc.className = "linkedin-featured-description";
    desc.setAttribute("aria-label", `Featured ${idx + 1} description`);

    const meta = document.createElement("p");
    meta.className = "hint inline";
    const bits = [];
    if (item.url) bits.push(`Link: ${item.url}`);
    if (item.suggested_visual) bits.push(`Visual: ${item.suggested_visual}`);
    meta.textContent = bits.join(" · ") || "—";

    wrap.appendChild(title);
    wrap.appendChild(desc);
    wrap.appendChild(meta);
    list.appendChild(wrap);
  });
}

function renderLinkedInSkills(skills, pinned) {
  const ta = $("#linkedin-skills");
  if (ta) ta.value = skills.join("\n");
  const pinnedEl = $("#linkedin-skills-pinned");
  if (pinnedEl) {
    if (pinned.length) {
      pinnedEl.innerHTML = "<strong>Pin these top 3:</strong> "
        + pinned.map((s) => `<code>${escapeHtml(s)}</code>`).join(", ");
    } else {
      pinnedEl.textContent = "No skills captured — add them in your master.";
    }
  }
}

function renderLinkedInEducation(entries) {
  const list = $("#linkedin-education");
  if (!list) return;
  list.innerHTML = "";
  if (entries.length === 0) {
    list.innerHTML = '<p class="hint">No education entries in master.</p>';
    return;
  }
  entries.forEach((edu) => {
    const wrap = document.createElement("div");
    wrap.className = "linkedin-edu-item";
    const head = document.createElement("p");
    head.innerHTML = `<strong>${escapeHtml(edu.school)}</strong> &mdash; `
      + `${escapeHtml(edu.degree)} &middot; ${escapeHtml(edu.year || "")}`;
    wrap.appendChild(head);
    if (edu.notes) {
      const notes = document.createElement("p");
      notes.className = "hint";
      notes.textContent = edu.notes;
      wrap.appendChild(notes);
    }
    list.appendChild(wrap);
  });
}

function renderLinkedInWarnings(warnings) {
  const box = $("#linkedin-warnings");
  if (!box) return;
  if (!warnings.length) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  box.innerHTML = "<h4>Guard warnings &mdash; review before pasting</h4><ul></ul>";
  const ul = box.querySelector("ul");
  warnings.forEach((w) => {
    const li = document.createElement("li");
    li.innerHTML = `<code>${escapeHtml(w.section)}</code>: ${escapeHtml(w.reason)}`;
    ul.appendChild(li);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

async function copyLinkedInMd() {
  const ta = $("#linkedin-md");
  if (!ta) return;
  try {
    await navigator.clipboard.writeText(ta.value || "");
    status("linkedin.md copied to clipboard.", "saved");
    setTimeout(() => status(null), 2000);
  } catch (err) {
    ta.select();
    document.execCommand("copy");
    status("Copied (fallback).", "saved");
    setTimeout(() => status(null), 2000);
  }
}

function downloadLinkedInMd() {
  const ta = $("#linkedin-md");
  if (!ta || !ta.value) return;
  const blob = new Blob([ta.value], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "linkedin.md";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

(async function init() {
  try {
    state.session = await loadOrCreate();
  } catch (err) {
    status(`Could not load session: ${err.message}`, "error");
    return;
  }

  if (state.session.chunks.length) {
    state.activeChunkId = state.session.chunks[0].id;
  }
  // Normalize education entries so optional fields (which pydantic emits
  // as null) bind cleanly to form inputs.
  state.session.education = (state.session.education || []).map(ensureEducationDefaults);
  state.session.basics = ensureBasicsDefaults(state.session.basics);
  state.session.employment = state.session.employment || [];

  wireEventHandlers();
  renderAll();
  initVoice();
  loadSessionsGallery();

  $("#btn-preview-master")?.addEventListener("click", previewMaster);
  $("#btn-save-master")?.addEventListener("click", saveMaster);
  $("#btn-build-linkedin")?.addEventListener("click", buildLinkedIn);
  $("#btn-copy-linkedin-md")?.addEventListener("click", copyLinkedInMd);
  $("#btn-download-linkedin-md")?.addEventListener("click", downloadLinkedInMd);
})();
