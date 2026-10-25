"use strict";
// SmallHarness web interface: Research, Planner, Recipes, Models, Settings. Plain DOM,
// no libraries; every text is set with textContent, so model output and notes are
// never interpreted as HTML.

const $ = (id) => document.getElementById(id);
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids) if (kid != null) n.append(kid);
  return n;
}
const gb = (bytes) => (bytes / 1e9).toFixed(2) + " GB";
const when = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
const secs = (s) => (s < 10 ? s.toFixed(1) : String(Math.round(s))) + " s";
const remember = (key, value) => { try { localStorage.setItem(key, value); } catch (e) { /* this page only */ } };
const recall = (key, fallback) => { try { return localStorage.getItem(key) ?? fallback; } catch (e) { return fallback; } };

function toast(text, bad = false) {
  const t = $("toast");
  t.textContent = text;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), bad ? 6000 : 2500);
}

// ------------------------------------------------------------------ API
async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

// A streamed request: calls onEvent for each NDJSON line; resolves with the last event.
async function stream(path, body, onEvent) {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify(body) });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `${res.status} ${res.statusText}`);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "", last = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (!line) continue;
      const ev = JSON.parse(line);
      last = ev;
      if (ev.type === "permit") askPermission(ev);
      else onEvent(ev);
    }
  }
  if (last && last.type === "error") throw new Error(last.error);
  return last;
}

// ---------------------------------------------------------------- state
let state = null, running = false;
function setBusy(label) {
  $("busy").hidden = !label;
  $("busy-text").textContent = label || "";
}
function renderState(s) {
  if (!s) return;
  state = s;
  $("version").textContent = "v" + s.version;
  $("model-name").textContent = s.model.name;
  $("model-meta").textContent = `${s.model.backend}${s.model.mode ? "(" + s.model.mode + ")" : ""} · ${s.model.context_tokens} ctx`;
  $("model-dot").className = "dot" + (s.model.loaded ? " on" : "");
  $("model-dot").title = s.model.loaded ? "model loaded" : "not loaded: loads when first used";
  $("load-btn").hidden = s.model.loaded;
  $("instances").textContent = s.model.loaded ? `${s.instances}/${s.capacity} instance(s)` : "";
  setBusy(s.busy);
  renderStatus(s);
}
async function refreshState() {
  try { renderState(await api("GET", "/api/state")); } catch (e) { toast(e.message, true); }
}
function renderStatus(s) {
  const rows = [
    ["workspace", s.workspace], ["model", `${s.model.name} (${s.model.backend})`],
    ["loaded", s.model.loaded ? `yes · ${s.instances} instance(s)` : "no: loads when first used"],
    ["instances fit", s.ram_managed ? `${s.capacity} (planner and recipes use up to this many)` : "1 (RAM not managed)"],
    ["context", `${s.model.context_tokens} tokens${s.model.context_note ? " · " + s.model.context_note : ""}`],
    ["search", s.search], ["knowledge base", `${s.knowledge.sources} source(s), ${s.knowledge.chunks} chunks`],
    ["notes", String(s.notes)],
  ];
  if (s.untrusted.length) rows.push(["not trusted", s.untrusted.join("; ")]);
  $("status-list").replaceChildren(...rows.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));
}
$("load-btn").addEventListener("click", async () => {
  setBusy("loading the model");
  try {
    const r = await api("POST", "/api/load");
    renderState(r);
    toast(r.load_s != null ? `model loaded in ${secs(r.load_s)}` : "model loaded");
  } catch (e) { toast(e.message, true); }
  refreshState();
});
$("stop-btn").addEventListener("click", () => api("POST", "/api/stop").catch(() => {}));

// ------------------------------------------------------------------ tabs
const loaders = {};
function show(view) {
  for (const b of $("tabs").children) b.classList.toggle("active", b.dataset.view === view);
  for (const v of document.querySelectorAll(".view")) v.classList.toggle("active", v.id === "view-" + view);
  remember("sh-view", view);
  if (loaders[view]) loaders[view]();
}
$("tabs").addEventListener("click", (e) => { if (e.target.dataset.view) show(e.target.dataset.view); });

// ------------------------------------------------------------ permission
const permitQueue = [];
function askPermission(ev) {
  permitQueue.push(ev);
  if (permitQueue.length === 1) nextPermission();
}
function nextPermission() {
  const ev = permitQueue[0];
  if (!ev) return;
  $("permit-tool").textContent = ev.tool;
  $("permit-desc").textContent = ev.description;
  $("permit-args").textContent = Object.entries(ev.args)
    .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join("\n");
  const dlg = $("permit");
  dlg.returnValue = "";
  dlg.onclose = async () => {
    const allow = dlg.returnValue === "allow";
    permitQueue.shift();
    try { await api("POST", "/api/permit", { id: ev.id, allow }); } catch (e) { toast(e.message, true); }
    nextPermission();
  };
  dlg.showModal();
}

// =============================================================== research
let research = null;   // the session as the server returns it
function showPane(pane) {
  for (const b of $("research-tabs").querySelectorAll("button")) b.classList.toggle("active", b.dataset.pane === pane);
  for (const p of document.querySelectorAll(".rpane")) p.classList.toggle("active", p.id === "pane-" + pane);
  remember("sh-pane", pane);
}
$("research-tabs").addEventListener("click", (e) => { if (e.target.dataset.pane) showPane(e.target.dataset.pane); });

async function loadResearch() {
  try { renderResearch(await api("GET", "/api/research")); } catch (e) { toast(e.message, true); }
}
loaders.research = loadResearch;

function renderResearch(r) {
  research = r;
  $("research-search").textContent = `search: ${r.search}` + (r.embeddings ? "" : " · install fastembed to search by meaning");
  $("scope-line").textContent = r.scope.length ? `· questions search only the ticked ${r.scope.length === 1 ? "one" : r.scope.length}`
                                                : "· questions search everything; tick to narrow";
  $("scope-all").hidden = !r.scope.length;
  renderAttached(r);
  if (!running) renderAnswers(r);
  renderFiles(r);
  renderNotes(r);
  renderRefs(r);
}

// --------------------------------------------------------------- answers
function citedText(text) {
  // The answer, with each [n] a link to reference n in the bibliography.
  const span = el("div", { class: "answer-text" });
  for (const part of text.split(/(\[\d+\])/)) {
    const m = part.match(/^\[(\d+)\]$/);
    span.append(m ? el("a", { class: "cite", href: "#", text: part, title: "open this reference",
                              onclick: (e) => { e.preventDefault(); openRef(Number(m[1])); } })
                  : document.createTextNode(part));
  }
  return span;
}
const speeds = {};   // answer number -> its speed line (the server does not keep timings)
function answerCard(a) {
  return el("div", { class: "answer" + (a.attached ? " attached" : "") },
    el("div", { class: "question", text: a.question }),
    citedText(a.text),
    el("div", { class: "answer-foot" },
      el("span", { class: "muted", text: `A${a.num}` + (a.refs.length ? " · refs " : "") }),
      ...a.refs.map((n) => el("a", { class: "cite", href: "#", text: `[${n}]`,
                                     onclick: (e) => { e.preventDefault(); openRef(n); } })),
      a.scope.length ? el("span", { class: "muted", text: ` · scope ${a.scope.join(", ")}` }) : null,
      el("span", { class: "grow" }),
      el("button", { class: "small " + (a.attached ? "" : "secondary"), text: a.attached ? "Attached" : "Attach",
                     title: a.attached ? "detach: later questions no longer see it"
                                       : "attach: later questions keep this answer in mind",
                     onclick: () => setAttached(a.num, !a.attached) })),
    speeds[a.num] ? el("div", { class: "speed", text: speeds[a.num] }) : null);
}
function renderAnswers(r) {
  const box = $("answers");
  box.replaceChildren(...r.answers.map(answerCard));
  if (!r.answers.length) {
    box.append(el("div", { class: "msg notice placeholder",
      text: "Ask about your files (below) and notes (left). Answers cite the excerpts they rest on." }));
  }
  box.scrollTop = box.scrollHeight;
}
// Attached answers (CAG), above the thread: what later questions keep in mind.
function renderAttached(r) {
  const attached = r.answers.filter((a) => a.attached);
  $("attached").hidden = !attached.length;
  $("attached").replaceChildren(
    el("div", { class: "attached-head", text: `Kept in mind (${attached.length})` }),
    ...attached.map((a) => el("div", { class: "attached-item" },
      el("span", { class: "grow", title: a.text },
        el("strong", { text: `A${a.num} ` }), document.createTextNode(a.question)),
      el("button", { class: "small link", text: "detach", onclick: () => setAttached(a.num, false) }))));
}
async function setAttached(num, attached) {
  try { renderResearch(await api("PUT", `/api/research/answers/${num}/attach`, { attached })); }
  catch (e) { toast(e.message, true); }
}

// speed readout: load, first token, tokens per second, under each answer
let showSpeed = recall("sh-speed", "on") !== "off";
function applySpeed() {
  document.body.classList.toggle("hide-speed", !showSpeed);
  $("show-speed").checked = showSpeed;
}
applySpeed();
$("show-speed").addEventListener("change", () => {
  showSpeed = $("show-speed").checked;
  remember("sh-speed", showSpeed ? "on" : "off");
  applySpeed();
});
function speedText(turn) {
  const parts = [];
  if (turn.load != null) parts.push(`load ${secs(turn.load)}`);
  for (const c of turn.calls) {
    parts.push(`first token ${secs(c.first_s)}`, `${c.estimated ? "~" : ""}${c.tokens} tok`,
               `${c.tokens_per_s} tok/s${c.streamed ? "" : " incl. prompt"}`);
  }
  return parts.join(" · ");
}

async function ask(question) {
  if (running) return;
  running = true;
  $("ask-btn").disabled = true;
  const box = $("answers");
  for (const p of box.querySelectorAll(".placeholder")) p.remove();
  const pending = el("div", { class: "answer pending" }, el("div", { class: "question", text: question }));
  const body = el("div", { class: "answer-text" });
  const speed = el("div", { class: "speed" });
  pending.append(body, speed);
  box.append(pending);
  box.scrollTop = box.scrollHeight;
  const turn = { load: null, calls: [] };
  setBusy("answering");
  try {
    const last = await stream("/api/research/ask", { question }, (ev) => {
      if (ev.type === "text") { body.textContent += ev.text; box.scrollTop = box.scrollHeight; }
      else if (ev.type === "status") body.textContent = ev.text + "…";
      else if (ev.type === "loaded") { turn.load = ev.seconds; body.textContent = ""; }
      else if (ev.type === "stats") { turn.calls.push(ev); speed.textContent = speedText(turn); }
      else if (ev.type === "done") {
        running = false;
        if (turn.calls.length) speeds[ev.answer.num] = speedText(turn);
        pending.replaceWith(answerCard(ev.answer));
        renderResearch(ev.research);
        renderState(ev.state);
      } else if (ev.type === "stopped") { body.textContent += " (stopped)"; renderState(ev.state); }
    });
    if (!last) body.textContent += " (the connection closed early)";
  } catch (e) { body.textContent = e.message; pending.classList.add("failed"); }
  running = false;
  $("ask-btn").disabled = false;
  refreshState();
}
$("ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("ask-input").value.trim();
  if (!q) return;
  $("ask-input").value = "";
  ask(q);
});
$("ask-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("ask-form").requestSubmit(); }
});
$("research-new").addEventListener("click", async () => {
  if (research && research.answers.length && !confirm("Start a new session? Answers, references and attachments are cleared.")) return;
  try {
    for (const k of Object.keys(speeds)) delete speeds[k];
    renderResearch(await api("POST", "/api/research/new"));
    toast("new research session");
  }
  catch (e) { toast(e.message, true); }
});

// ------------------------------------------- files (bottom) and notes (left)
const isNote = (x) => x.kind === "note";
function scopeBox(x, scoped) {
  const box = el("input", { type: "checkbox", checked: scoped.has(x.source), title: "limit questions to this",
    onchange: () => setScope(x.source, box.checked) });
  return box;
}
function renderFiles(r) {
  const scoped = new Set(r.scope);
  const files = r.library.filter((x) => !isNote(x));
  $("files").replaceChildren(...files.map((x) => {
    const box = scopeBox(x, scoped);
    return el("span", { class: "file-chip" + (scoped.has(x.source) ? " active" : ""), title: `${x.source} · ${x.chunks} chunk(s)` },
      el("label", {}, box, document.createTextNode(" " + x.source.replace(/^\.smallharness\/library\//, ""))),
      el("button", { class: "small link", text: "✕", title: "remove from the knowledge base", onclick: () => removeSource(x) }));
  }));
  if (!files.length) $("files").append(el("span", { class: "muted", text: "no files yet: add a path or upload" }));
}
function renderNotes(r) {
  const scoped = new Set(r.scope);
  const notes = r.library.filter(isNote);
  $("notes").replaceChildren(...notes.map((x) => el("li", { class: scoped.has(x.source) ? "active" : "" },
    scopeBox(x, scoped),
    el("a", { href: "#", class: "grow", text: x.title, onclick: (e) => { e.preventDefault(); openNoteById(x.source.slice(5)); } }))));
  if (!notes.length) $("notes").append(el("li", { class: "muted", text: "no notes yet" }));
}
async function setScope(source, on) {
  const scope = new Set(research.scope);
  if (on) scope.add(source); else scope.delete(source);
  try { renderResearch(await api("PUT", "/api/research/scope", { sources: [...scope] })); }
  catch (e) { toast(e.message, true); }
}
$("scope-all").addEventListener("click", async () => {
  try { renderResearch(await api("PUT", "/api/research/scope", { sources: [] })); } catch (e) { toast(e.message, true); }
});
async function removeSource(x) {
  if (!confirm(`Remove ${isNote(x) ? "the note “" + x.title + "”" : x.source} from the knowledge base?`)) return;
  try { renderResearch(await api("POST", "/api/research/remove", { source: x.source })); }
  catch (e) { toast(e.message, true); }
}
function addResult(text) {
  $("add-result").hidden = !text;
  $("add-result").textContent = text;
}
$("add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const path = $("add-path").value.trim();
  if (!path) return;
  setBusy("indexing");
  addResult("indexing…");
  try {
    await stream("/api/research/files", { path }, (ev) => {
      if (ev.type === "status") addResult(ev.text);
      else if (ev.type === "done") {
        addResult(ev.result + (ev.skipped.length ? " · " + ev.skipped.map(([f, why]) => `${f}: ${why}`).join("; ") : ""));
        $("add-path").value = "";
        renderResearch(ev.research);
      }
    });
  } catch (err) { addResult(err.message); }
  refreshState();
});
$("upload").addEventListener("change", async (e) => {
  const files = [...e.target.files];
  setBusy("indexing");
  for (const f of files) {
    addResult(`indexing ${f.name}…`);
    try {
      const res = await fetch("/api/research/upload?" + new URLSearchParams({ name: f.name }),
                              { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: f });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || res.statusText);
      addResult(`${f.name}: ${data.result}` + (data.skipped.length ? " · " + data.skipped.map(([n, why]) => `${n}: ${why}`).join("; ") : ""));
      renderResearch(data.research);
    } catch (err) { addResult(`${f.name}: ${err.message}`); }
  }
  e.target.value = "";
  refreshState();
});

// the note editor: a dialog, so the research page stays uncluttered
let noteId = null, noteDirty = false;
async function openNoteById(id) {
  try { openNote(await api("GET", `/api/notes/${id}`)); } catch (e) { toast(e.message, true); }
}
function openNote(n) {
  noteId = n ? n.id : null;
  $("note-text").value = n ? n.text : "";
  $("note-title").textContent = n ? `[${n.id}] ${n.title}` : "New note";
  $("note-meta").textContent = n ? "saved " + when(n.mtime) : "";
  $("note-delete").hidden = !n;
  $("note-status").textContent = "";
  noteDirty = false;
  const dlg = $("note-dialog");
  if (!dlg.open) dlg.showModal();
  $("note-text").focus();
}
function closeNote() {
  if (noteDirty && !confirm("Discard unsaved changes?")) return;
  noteDirty = false;
  $("note-dialog").close();
}
$("note-new").addEventListener("click", () => openNote(null));
$("note-close").addEventListener("click", closeNote);
$("note-dialog").addEventListener("cancel", (e) => { e.preventDefault(); closeNote(); });   // Escape
$("note-text").addEventListener("input", () => { noteDirty = true; $("note-status").textContent = "unsaved"; });
$("note-text").addEventListener("keydown", (e) => {
  if (e.key === "s" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); saveNote(); }
});
async function saveNote() {
  const text = $("note-text").value;
  if (!text.trim()) { toast("a note needs some text", true); return; }
  try {
    const n = noteId ? await api("PUT", `/api/notes/${noteId}`, { text }) : await api("POST", "/api/notes", { text });
    noteDirty = false;
    openNote(n);
    $("note-status").textContent = "saved and indexed";
    loadResearch();
  } catch (e) { toast(e.message, true); }
}
$("note-save").addEventListener("click", saveNote);
$("note-delete").addEventListener("click", async () => {
  if (!noteId || !confirm(`Delete note ${noteId}?`)) return;
  try {
    await api("DELETE", `/api/notes/${noteId}`);
    noteDirty = false;
    $("note-dialog").close();
    toast("deleted");
    loadResearch();
  } catch (e) { toast(e.message, true); }
});

// ----------------------------------------------------------- bibliography
let refShown = null;
function renderRefs(r) {
  $("refs").replaceChildren(...r.references.map((ref) =>
    el("li", { class: ref.num === refShown ? "active" : "", onclick: () => openRef(ref.num) },
      el("span", {}, el("strong", { text: `[${ref.num}] ` }), document.createTextNode(`${ref.source}:${ref.start_line}`)),
      el("span", { class: "sub", text: ref.text.split(/\s+/).join(" ").slice(0, 90) }))));
  if (!r.references.length) $("refs").append(el("li", { class: "muted", text: "no references yet: ask a question" }));
}
function openRef(num) {
  const ref = research && research.references.find((x) => x.num === num);
  if (!ref) return;
  refShown = num;
  showPane("bibliography");
  $("ref-empty").hidden = true;
  $("ref-detail").hidden = false;
  $("ref-title").textContent = `[${ref.num}]`;
  $("ref-source").textContent = `${ref.source}, from line ${ref.start_line}`;
  $("ref-text").textContent = ref.text;
  renderRefs(research);
}

// ================================================================ planner
let plan = null;
async function loadPlans() {
  try {
    const data = await api("GET", "/api/plans");
    $("plan-list").replaceChildren(...data.plans.map((p) =>
      el("li", { class: plan && p.id === plan.id ? "active" : "", onclick: () => openPlan(p.id) },
        el("span", { text: p.goal }),
        el("span", { class: "sub", text: `${p.id} · ${p.done}/${p.tasks} done${p.finished ? " · finished" : ""}` }))));
    if (!data.plans.length) $("plan-list").append(el("li", { class: "muted", text: "no plans yet" }));
    if (!plan && data.current) openPlan(data.current);
  } catch (e) { toast(e.message, true); }
}
loaders.planner = loadPlans;
async function openPlan(id, keepLog = false) {
  try {
    renderPlan(await api("GET", `/api/plans/${id}`));
    if (!keepLog) $("run-log").replaceChildren();
    loadPlans();
  } catch (e) { toast(e.message, true); }
}
function renderPlan(p) {
  plan = p;
  $("plan-empty").hidden = true;
  $("plan-editor").hidden = true;
  $("plan-detail").hidden = false;
  $("plan-goal-text").textContent = p.goal;
  $("plan-tasks").replaceChildren(...p.tasks.map((t) =>
    el("li", {},
      el("div", { class: "head" },
        el("strong", { text: `${t.num}.` }), el("span", { text: t.task }),
        el("span", { class: "tool-name", text: t.tool }),
        t.after.length ? el("span", { class: "muted", text: `after ${t.after.join(", ")}` }) : null,
        el("span", { class: "grow" }),
        t.depth ? el("span", { class: "depth", text: `pass ${t.depth}`, title: "expansion passes so far" }) : null,
        el("span", { class: "status " + t.status, text: t.status })),
      t.detail ? el("div", { class: "detail", text: t.detail }) : null,
      t.result ? el("div", { class: "result", text: "→ " + t.result }) : null)));
}
$("plan-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const goal = $("plan-goal").value.trim();
  if (!goal) return;
  setBusy("planning");
  try {
    const p = await api("POST", "/api/plans", { goal });
    $("plan-goal").value = "";
    renderPlan(p);
    (p.warnings || []).forEach((w) => toast(w));
    loadPlans();
  } catch (err) { toast(err.message, true); }
  refreshState();
});
// Drafting or editing a plan: the steps as lines, their details indented below them.
let planEditing = null;
async function openPlanEditor(p) {
  planEditing = p ? p.id : null;
  $("plan-editor-title").textContent = p ? `Edit plan ${p.id}` : "Draft a plan";
  $("plan-edit-goal").value = p ? p.goal : $("plan-goal").value.trim();
  $("plan-edit-text").value = p ? p.text : "";
  $("plan-empty").hidden = true;
  $("plan-detail").hidden = true;
  $("plan-editor").hidden = false;
  $("plan-edit-text").focus();
  try {
    const t = await api("GET", "/api/tools");
    $("plan-edit-tools").textContent = "Tools: none, " + t.tools.map((x) => x.name).join(", ");
  } catch (e) { /* the hint is optional */ }
}
function closePlanEditor() {
  $("plan-editor").hidden = true;
  if (plan) $("plan-detail").hidden = false; else $("plan-empty").hidden = false;
}
$("plan-draft").addEventListener("click", () => openPlanEditor(null));
$("plan-edit").addEventListener("click", () => { if (plan && !running) openPlanEditor(plan); });
$("plan-edit-cancel").addEventListener("click", closePlanEditor);
$("plan-edit-text").addEventListener("keydown", (e) => {     // Tab indents: details are indented lines
  if (e.key !== "Tab") return;
  e.preventDefault();
  const t = e.target, at = t.selectionStart;
  t.setRangeText("   ", at, t.selectionEnd, "end");
});
$("plan-edit-save").addEventListener("click", async () => {
  const body = { goal: $("plan-edit-goal").value.trim(), text: $("plan-edit-text").value };
  if (!body.text.trim()) { toast("write at least one step", true); return; }
  try {
    const p = planEditing ? await api("PUT", `/api/plans/${planEditing}`, body) : await api("POST", "/api/plans", body);
    if (!planEditing) $("plan-goal").value = "";
    $("run-log").replaceChildren();
    renderPlan(p);
    (p.warnings || []).forEach((w) => toast(w));
    toast(planEditing ? "plan saved" : "plan created");
    loadPlans();
  } catch (e) { toast(e.message, true); }
});

// A streamed run written into a log: what every run shares (streamed text, tool calls
// and results, notices, Stop, errors, the busy state); `onEvent` gets the run's own.
async function runInLog({ url, body, log, button, busy, onEvent }) {
  if (running) return;
  running = true;
  button.disabled = true;
  log.replaceChildren();
  setBusy(busy);
  let text = null;
  try {
    const last = await stream(url, body, (ev) => {
      if (ev.type === "text") {
        if (!text) { text = el("div"); log.append(text); }
        text.textContent += ev.text;
      } else if (ev.type === "text_end") text = null;
      else if (ev.type === "tool_call") { text = null; logLine(`  ▸ ${ev.name}(${JSON.stringify(ev.args).slice(0, 160)})`, "", log); }
      else if (ev.type === "tool_result") logLine("    └ " + ev.text.split("\n").slice(0, 3).join(" ⏎ ").slice(0, 240), ev.ok ? "ev-dim" : "ev-bad", log);
      else if (ev.type === "notice" || ev.type === "status") logLine("· " + ev.text, "ev-dim", log);
      else if (ev.type === "stopped") { logLine("■ stopped", "ev-bad", log); renderState(ev.state); }
      else if (ev.type !== "stats" && ev.type !== "loaded") { text = null; onEvent(ev); }
    });
    if (!last) logLine("the connection closed early", "ev-bad", log);
  } catch (e) { logLine(e.message, "ev-bad", log); }
  running = false;
  button.disabled = false;
  refreshState();
}
function logLine(text, cls = "", log = $("run-log")) {
  log.append(el("div", { class: cls, text }));
  log.scrollTop = log.scrollHeight;
}
function stepNumbers(text) {
  const nums = text.split(/[\s,]+/).filter((x) => /^\d+$/.test(x)).map(Number);
  return nums.length ? nums : null;          // none: every step
}
$("expand-btn").addEventListener("click", async () => {
  if (!plan) return;
  const log = $("run-log");
  const body = { steps: stepNumbers($("expand-steps").value), times: Number($("expand-times").value) || 1,
                 parallel: $("expand-parallel").checked };
  await runInLog({ url: `/api/plans/${plan.id}/expand`, body, log, button: $("expand-btn"), busy: "expanding the plan",
    onEvent: (ev) => {
      if (ev.type === "step") logLine(`  ✓ step ${ev.num}: pass ${ev.depth}`, "ev-ok", log);
      else if (ev.type === "done") { logLine("■ expanded", "ev-task", log); renderPlan(ev.plan); renderState(ev.state); }
    } });
  if (plan) openPlan(plan.id, true);
});
$("run-btn").addEventListener("click", async () => {
  if (!plan) return;
  const log = $("run-log");
  const body = { mode: $("run-mode").value, parallel: $("run-parallel").checked };
  await runInLog({ url: `/api/plans/${plan.id}/run`, body, log, button: $("run-btn"), busy: "running the plan",
    onEvent: (ev) => {
      if (ev.type === "task_start") logLine(`▶ step ${ev.num}: ${ev.task} [${ev.tool}]`, "ev-task", log);
      else if (ev.type === "task_end") {
        logLine(`  ${ev.status.toUpperCase()}: ${ev.result}`, ev.status === "done" ? "ev-ok" : "ev-bad", log);
        renderPlan(ev.plan);
      } else if (ev.type === "done") {
        logLine(`■ ${ev.reason}`, "ev-task", log);
        renderPlan(ev.plan); renderState(ev.state);
      }
    } });
  if (plan) openPlan(plan.id, true);
});

// ================================================================ recipes
let recipe = null;
async function loadRecipes() {
  try {
    const data = await api("GET", "/api/recipes");
    $("recipe-list").replaceChildren(...data.recipes.map((r) =>
      el("li", { class: recipe && r.name === recipe.name ? "active" : "", onclick: () => openRecipe(r.name) },
        el("span", { text: r.name }),
        el("span", { class: "sub", text: r.error ? r.error : `→ ${r.target} · ${r.mode} · ${r.done}/${r.sections} written` }))));
    if (!data.recipes.length) $("recipe-list").append(el("li", { class: "muted", text: "no recipes yet" }));
  } catch (e) { toast(e.message, true); }
}
loaders.recipes = () => { loadRecipes(); loadLibrary(recipe && recipe.library); };
async function openRecipe(name) {
  try {
    showRecipe(await api("GET", `/api/recipes/${name}`));
    $("recipe-log").replaceChildren();
    loadRecipes();
  } catch (e) { toast(e.message, true); }
}
// The editor shows the sections; target and mode have their own fields.
function showRecipe(r) {
  recipe = r;
  $("recipe-name").value = r ? r.name : "";
  $("recipe-name").readOnly = !!r;
  $("recipe-target").value = r ? r.target : "";
  $("recipe-mode").value = r ? r.mode : "";
  $("recipe-text").value = r ? r.text.replace(/^(\s*(target|mode)\s*:.*\n)+\n*/i, "") : "";
  $("recipe-delete").hidden = !r;
  $("recipe-status").textContent = "";
  renderSections(r);
}
function renderSections(r) {
  $("recipe-sections").replaceChildren(...(r ? r.sections : []).map((s) => {
    const [first, ...rest] = s.text.split("\n");
    return el("li", {},
      el("div", { class: "head" },
        el("strong", { text: `${s.num}.` }), el("span", { text: first }),
        rest.length ? el("span", { class: "muted", text: `+${rest.length} line(s)` }) : null,
        el("span", { class: "grow" }), el("span", { class: "status " + (s.state === "changed" ? "skipped" : s.state), text: s.state })),
      s.note ? el("div", { class: "result", text: "→ " + s.note }) : null,
      s.code ? el("details", { class: "tool" }, el("summary", { text: `${r.mode === "text" ? "text" : "code"} · ${s.code.split("\n").length} line(s)`
          + (s.snippet ? ` · ${s.reused ? "reused " : "library "}${s.snippet}` : "") }),
        el("pre", { text: s.code })) : null);
  }));
  if (r && r.library) loadLibrary(r.library);
}

// the code library: one per language, searched before a section is written
let libraryTimer;
async function loadLibrary(lang) {
  try {
    const data = await api("GET", "/api/snippets");
    const keys = data.languages.map((l) => l.key);
    if (lang && !keys.includes(lang)) keys.push(lang);
    const sel = $("library-lang");
    const current = lang || sel.value || keys[0] || "";
    sel.replaceChildren(...keys.map((k) => el("option", { value: k, text: k })));
    sel.value = current;
    if (!current) { $("snippet-list").replaceChildren(el("li", { class: "muted", text: "empty: recipes in code mode fill it" })); return; }
    const q = $("library-q").value.trim();
    const res = await api("GET", `/api/snippets/${current}` + (q ? "?" + new URLSearchParams({ q }) : ""));
    $("snippet-list").replaceChildren(...res.snippets.map((s) => {
      const code = el("pre", { class: "out", hidden: true });
      return el("li", {},
        el("div", { class: "grow" },
          el("a", { href: "#", text: s.id, onclick: async (e) => {
            e.preventDefault();
            if (code.hidden && !code.textContent) code.textContent = (await api("GET", `/api/snippets/${current}/${s.id}`)).code;
            code.hidden = !code.hidden;
          } }),
          el("span", { class: "sub", text: `${s.description} · used by ${s.recipes.join(", ")}` }), code),
        el("button", { class: "small link", text: "✕", title: "take out of the library; recipes that used it write that section again",
                       onclick: async () => {
                         if (!confirm(`Remove ${s.id} from the ${current} library?`)) return;
                         try { await api("DELETE", `/api/snippets/${current}/${s.id}`); loadLibrary(current); } catch (err) { toast(err.message, true); }
                       } }));
    }));
    if (!res.snippets.length) $("snippet-list").append(el("li", { class: "muted", text: q ? "nothing matches" : "empty" }));
  } catch (e) { toast(e.message, true); }
}
$("library-lang").addEventListener("change", () => loadLibrary($("library-lang").value));
$("library-q").addEventListener("input", () => { clearTimeout(libraryTimer); libraryTimer = setTimeout(() => loadLibrary($("library-lang").value), 250); });
async function saveRecipe() {
  const name = $("recipe-name").value.trim();
  if (!/^[A-Za-z0-9][\w-]{0,63}$/.test(name)) throw new Error("give the recipe a name: letters, digits, - and _");
  const r = await api("PUT", `/api/recipes/${name}`, { text: $("recipe-text").value, target: $("recipe-target").value.trim(),
                                                      mode: $("recipe-mode").value });
  showRecipe(r);
  loadRecipes();
  return r;
}
$("recipe-new").addEventListener("click", () => { showRecipe(null); $("recipe-log").replaceChildren(); $("recipe-name").focus(); loadRecipes(); });
$("recipe-save").addEventListener("click", async () => {
  try { await saveRecipe(); $("recipe-status").textContent = "saved"; } catch (e) { toast(e.message, true); }
});
$("recipe-delete").addEventListener("click", async () => {
  if (!recipe || !confirm(`Delete the recipe ${recipe.name}? The file it wrote stays.`)) return;
  try { await api("DELETE", `/api/recipes/${recipe.name}`); showRecipe(null); loadRecipes(); } catch (e) { toast(e.message, true); }
});
$("recipe-run").addEventListener("click", async () => {
  if (running) return;
  let r;
  try { r = await saveRecipe(); } catch (e) { toast(e.message, true); return; }
  const log = $("recipe-log");
  await runInLog({ url: `/api/recipes/${r.name}/run`, body: { all: $("recipe-all").checked, parallel: $("recipe-parallel").checked },
    log, button: $("recipe-run"), busy: "writing a recipe",
    onEvent: (ev) => {
      if (ev.type === "section_start") logLine(`▶ section ${ev.num}: ${ev.text.split("\n")[0]}`, "ev-task", log);
      else if (ev.type === "section_end") {
        logLine(ev.status === "done" ? `  ✓ section ${ev.num}: ${ev.code.split("\n").length} line(s)` : `  section ${ev.num} FAILED: ${ev.note}`,
                ev.status === "done" ? "ev-ok" : "ev-bad", log);
      } else if (ev.type === "done") {
        const why = { denied: "not allowed: nothing was written", "nothing to write":
          "every section is written already; tick “all sections” to write them again" }[ev.reason];
        logLine("■ " + (why || `${ev.reason}: wrote ${ev.written.length}, failed ${ev.failed.length} → ${ev.recipe.target}`),
                ev.reason === "denied" || ev.failed.length ? "ev-bad" : "ev-task", log);
        showRecipe(ev.recipe); renderState(ev.state);
      }
    } });
  loadRecipes();
});

// ================================================================= models
async function loadModels() {
  try {
    const m = await api("GET", "/api/models");
    $("models-where").textContent = `· ${m.engine} · ${m.where}`;
    $("models-error").hidden = !m.error;
    $("models-error").textContent = m.error;
    $("models-local").replaceChildren(...m.local.map((x) => el("tr", {},
      el("td", { text: x.active ? "●" : "" }), el("td", {}, el("strong", { text: x.name })),
      el("td", { text: gb(x.size) }), el("td", { text: x.max_context ? x.max_context + " ctx" : "?" }),
      el("td", { class: "muted", text: x.source }),
      el("td", { class: "actions" },
        x.active ? null : el("button", { class: "small", text: "Use", onclick: () => useModel(x.name) }), " ",
        x.active ? null : el("button", { class: "small danger", text: "Delete", onclick: () => deleteModel(x.name) })))));
    if (!m.local.length) $("models-local").append(el("tr", {}, el("td", { colspan: 6, class: "muted", text: "none downloaded yet" })));
    $("models-available").replaceChildren(...m.available.map((x) => el("tr", {},
      el("td", {}, el("strong", { text: x.name })), el("td", { text: x.size_gb.toFixed(2) + " GB" }),
      el("td", { class: "muted", text: x.note + (x.gated ? " · gated: needs HF_TOKEN" : "") }),
      el("td", { class: "actions" }, el("button", { class: "small", text: "Pull", onclick: () => pull(x.name) })))));
  } catch (e) { toast(e.message, true); }
}
loaders.models = loadModels;
async function useModel(name) {
  setBusy("switching models");
  try { const r = await api("POST", "/api/model", { target: name }); r.done.forEach((d) => toast(d)); renderState(r.state); loadModels(); }
  catch (e) { toast(e.message, true); }
  refreshState();
}
async function deleteModel(name) {
  if (!confirm(`Delete ${name}?`)) return;
  try { await api("DELETE", `/api/models/${encodeURIComponent(name)}`); toast("deleted"); loadModels(); }
  catch (e) { toast(e.message, true); }
}
async function pull(name) {
  const bar = $("pull-progress");
  bar.hidden = false;
  bar.firstElementChild.style.width = "0";
  bar.lastElementChild.textContent = `${name}: starting…`;
  setBusy("downloading " + name);
  try {
    await stream("/api/pull", { name }, (ev) => {
      if (ev.type === "progress") {
        const pct = ev.total ? (ev.done / ev.total) * 100 : 0;
        bar.firstElementChild.style.width = pct + "%";
        bar.lastElementChild.textContent = `${name}: ${gb(ev.done)}${ev.total ? " / " + gb(ev.total) : ""}`;
      } else if (ev.type === "done") {
        bar.lastElementChild.textContent = `${name}: ${ev.detail}`;
        if (ev.warning) toast(ev.warning, true);
      } else if (ev.type === "notice") toast(ev.text, true);
      else if (ev.type === "stopped") bar.lastElementChild.textContent = `${name}: stopped; pull again to resume`;
    });
    loadModels();
  } catch (e) { bar.lastElementChild.textContent = `${name}: ${e.message}`; }
  refreshState();
}
$("pull-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const name = $("pull-name").value.trim();
  if (name) pull(name);
});

// =============================================================== settings
const edits = {};
let filterTimer;
async function loadSettings() {
  try {
    const c = await api("GET", "/api/config");
    $("config-files").textContent = c.files.length ? "· from " + c.files.join(", ") : "· defaults";
    const filter = $("config-filter").value.trim().toLowerCase();
    const changedOnly = $("config-changed").checked;
    $("config-rows").replaceChildren(...c.settings
      .filter((s) => (!filter || s.key.toLowerCase().includes(filter)) && (!changedOnly || s.changed))
      .map((s) => {
        const shown = typeof s.value === "string" ? s.value : JSON.stringify(s.value);
        const input = el("input", { value: s.key in edits ? edits[s.key] : shown, placeholder: s.secret ? "hidden" : "",
          title: "default: " + JSON.stringify(s.default) });
        const row = el("tr", { class: s.changed ? "changed" : "" }, el("td", { text: s.key }), el("td", {}, input));
        input.addEventListener("input", () => {
          if (input.value === shown) delete edits[s.key]; else edits[s.key] = input.value;
          row.classList.toggle("edited", s.key in edits);
          $("config-save").disabled = !Object.keys(edits).length;
        });
        if (s.key in edits) row.classList.add("edited");
        return row;
      }));
    const t = await api("GET", "/api/tools");
    $("tool-rows").replaceChildren(...t.tools.map((x) => el("tr", {},
      el("td", {}, el("strong", { class: x.risky ? "risky" : "", text: x.name })),
      el("td", { class: "muted", text: x.risky ? "asks first" : "runs freely" }),
      el("td", { text: x.description }))));
  } catch (e) { toast(e.message, true); }
}
loaders.settings = () => { refreshState(); loadSettings(); };
$("config-filter").addEventListener("input", () => { clearTimeout(filterTimer); filterTimer = setTimeout(loadSettings, 200); });
$("config-changed").addEventListener("change", loadSettings);
$("config-save").addEventListener("click", async () => {
  // Sent as typed: the server reads TOML values (4096, true, ["a"]) and keeps anything else as text.
  const values = { ...edits };
  setBusy("applying settings");
  try {
    const r = await api("POST", "/api/config", { values });
    for (const k of Object.keys(edits)) delete edits[k];
    $("config-save").disabled = true;
    (r.done.length ? r.done : ["applied"]).forEach((d) => toast(d));
    renderState(r.state);
    loadSettings();
  } catch (e) { toast(e.message, true); }
  refreshState();
});

// ================================================================== start
refreshState();
show(["research", "planner", "recipes", "models", "settings"].includes(recall("sh-view", "")) ? recall("sh-view", "") : "research");
showPane(recall("sh-pane", "") === "bibliography" ? "bibliography" : "ask");
setInterval(() => { if (!document.hidden && !running) refreshState(); }, 5000);
