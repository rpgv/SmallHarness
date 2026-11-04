"use strict";
// SmallHarness web interface: Chat, Research, Notes, Code, Models, Settings, and
// Relics (the files plans made) under the tabs. Plain DOM,
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

// A streamed request (a GET when there is no body): calls onEvent for each NDJSON line;
// resolves with the last event. An error reply or error event throws with `server` set;
// a dropped connection throws without it, or resolves with a last event that is not the end.
const serverError = (message) => Object.assign(new Error(message), { server: true });
async function stream(path, body, onEvent) {
  const res = await fetch(path, body === undefined ? {} :
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw serverError(data.error || `${res.status} ${res.statusText}`);
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
      else if (ev.type === "permit_end") dropPermission(ev.id);
      else if (ev.type !== "ping") onEvent(ev);
    }
  }
  if (last && last.type === "error") throw serverError(last.error);
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
let firstState = true;
async function refreshState() {
  try {
    const s = await api("GET", "/api/state");
    renderState(s);
    if (s.job && !s.job.finished && !running) joinRun(s.job, firstState);   // started before this page, or elsewhere
    firstState = false;
  } catch (e) { toast(e.message, true); }
}
function renderStatus(s) {
  const rows = [
    ["workspace", s.workspace], ["model", `${s.model.name} (${s.model.backend})`],
    ["loaded", s.model.loaded ? `yes · ${s.instances} instance(s)` : "no: loads when first used"],
    ["instances fit", s.ram_managed ? `${s.capacity} (Code uses up to this many)` : "1 (RAM not managed)"],
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
  for (const b of $("tabs").querySelectorAll("button[data-view]")) b.classList.toggle("active", b.dataset.view === view);
  for (const v of document.querySelectorAll(".view")) v.classList.toggle("active", v.id === "view-" + view);
  remember("sh-view", view);
  if (loaders[view]) loaders[view]();
}
$("tabs").addEventListener("click", (e) => { if (e.target.dataset.view) show(e.target.dataset.view); });
// sub-tabs: buttons with data-pane in `bar`, each showing the element `prefix + pane`
function subTabs(bar, prefix, key) {
  const panes = [...$(bar).querySelectorAll("button[data-pane]")].map((b) => b.dataset.pane);
  const pick = (pane) => {
    for (const b of $(bar).querySelectorAll("button[data-pane]")) b.classList.toggle("active", b.dataset.pane === pane);
    for (const p of panes) $(prefix + p).classList.toggle("active", p === pane);
    remember(key, pane);
  };
  $(bar).addEventListener("click", (e) => { if (e.target.dataset.pane) pick(e.target.dataset.pane); });
  pick(panes.includes(recall(key, "")) ? recall(key, "") : panes[0]);
  return pick;
}

// ------------------------------------------------------------ permission
const permitQueue = [];
function askPermission(ev) {
  if (permitQueue.some((q) => q.id === ev.id)) return;     // asked again on rejoining a run
  permitQueue.push(ev);
  if (permitQueue.length === 1) nextPermission();
}
// Answered (here or on another page), or it timed out: stop asking.
function dropPermission(id) {
  const i = permitQueue.findIndex((q) => q.id === id);
  if (i > 0) permitQueue.splice(i, 1);
  else if (i === 0) { permitQueue[0].gone = true; $("permit").close(); }
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
    if (!ev.gone) {
      try { await api("POST", "/api/permit", { id: ev.id, allow }); } catch (e) { toast(e.message, true); }
    }
    nextPermission();
  };
  dlg.showModal();
}

// =================================================================== chat
// A conversation: no tools unless a message starts with command lines. Pins stay in
// every prompt; a thread (the panel on the right) has only its message's context;
// rewinding clears everything after a message. Replies run as background jobs, so a
// reload picks them up again.
let chat = null;            // the open chat, as the server returns it
let chatThread = 0;         // the message whose thread is open; 0 = none
let chatPending = null;     // the reply streaming in: {thread, text, node}
let chatHelp = { commands: [], tools: [] };

async function loadChats() {
  try {
    const data = await api("GET", "/api/chats");
    chatHelp = { commands: data.commands, tools: data.tools };
    renderChatHelp();
    $("chat-list").replaceChildren(...data.chats.map((c) =>
      el("li", { class: "item" + (chat && c.id === chat.id ? " active" : ""), onclick: () => openChat(c.id) },
        el("div", { class: "grow" },
          el("span", { text: c.title }),
          el("span", { class: "sub", text: `${c.messages} message(s) · ${when(c.updated)}` })),
        el("button", { class: "small link", text: "✕", title: "delete this chat",
                       onclick: (e) => { e.stopPropagation(); deleteChat(c.id, c.title); } }))));
    if (!data.chats.length) $("chat-list").append(el("li", { class: "muted", text: "no chats yet" }));
    if (!chat) {
      const last = recall("sh-chat", "");
      const pick = data.chats.find((c) => c.id === last) || data.chats[0];
      if (pick) openChat(pick.id); else renderChat();
    }
  } catch (e) { toast(e.message, true); }
}
loaders.chat = loadChats;
async function openChat(id) {
  try {
    const c = await api("GET", `/api/chats/${id}`);
    if (!chat || chat.id !== c.id) chatThread = 0;
    chat = c;
    remember("sh-chat", c.id);
    renderChat();
    loadChats();
  } catch (e) { toast(e.message, true); remember("sh-chat", ""); }
}
async function newChat() {
  try { chat = await api("POST", "/api/chats"); chatThread = 0; renderChat(); loadChats(); $("chat-input").focus(); }
  catch (e) { toast(e.message, true); }
}
async function deleteChat(id, title) {
  if (live && live.kind === "chat" && live.target === id) { toast("the model is answering in this chat", true); return; }
  if (!confirm(`Delete the chat “${title}”?`)) return;
  try {
    await api("DELETE", `/api/chats/${id}`);
    if (chat && chat.id === id) { chat = null; chatThread = 0; renderChat(); }
    toast("chat deleted");
    loadChats();
  } catch (e) { toast(e.message, true); }
}
$("chat-new").addEventListener("click", newChat);
$("chat-delete").addEventListener("click", () => { if (chat) deleteChat(chat.id, chat.title); });
// Keeping context, per chat: a click sets this chat; Alt-click returns it to the default.
$("chat-context").addEventListener("click", async (e) => {
  if (!chat || !chat.messages) { e.preventDefault(); toast("start the chat first", true); return; }
  const value = e.altKey ? null : $("chat-context").checked;
  try { chat = await api("PUT", `/api/chats/${chat.id}`, { keep_context: value }); renderContextToggle(); }
  catch (err) { toast(err.message, true); }
});
function renderContextToggle() {
  const on = chat ? chat.keep_context : true;
  $("chat-context").checked = on;
  $("chat-context").disabled = !chat;
  $("chat-context-label").classList.toggle("off", !on);
  $("chat-context-label").lastChild.textContent = on ? " context" : " no context";
  $("chat-context-label").title = (on ? "on: replies see the conversation so far" :
    "off: replies see only the message they answer, pinned messages and threads")
    + (chat && chat.keep_context_default ? " (the default, set under Settings)" : " (set for this chat; Alt-click: back to the default)");
}
$("chat-title").addEventListener("change", async () => {
  const title = $("chat-title").value.trim();
  if (!chat || !title) return;
  try { chat = await api("PUT", `/api/chats/${chat.id}`, { title }); loadChats(); } catch (e) { toast(e.message, true); }
});

// ------------------------------------------------------------- showing
const chatLine = (thread) => (chat ? chat.messages.filter((m) => m.thread === thread) : []);
function who(m) {
  if (m.role === "user") return "you";
  if (m.role === "assistant") return { research: "research", plan: "plan", agent: "model + tools", gemini: "Gemini" }[m.kind] || "model";
  return { info: "help", error: "error", export: "export", note: "notes", recall: "recall", plan: "plan run",
           research: "excerpts" }[m.kind] || "tool";
}
// A user message: its command lines in red, the question after them.
function userText(text) {
  const box = el("div", { class: "cmsg-text" });
  let commands = true;
  for (const line of text.split("\n")) {
    if (commands && line.startsWith("/")) box.append(el("code", { class: "cmd", text: line }), "\n");
    else { commands = false; box.append(line + "\n"); }
  }
  box.lastChild && box.lastChild.nodeType === 3 && (box.lastChild.textContent = box.lastChild.textContent.replace(/\n$/, ""));
  return box;
}
function chatBody(m) {
  if (m.role === "user") {
    if (!m.meta.relic) return [userText(m.text)];
    return [userText(m.text), el("div", { class: "cmsg-foot" }, el("span", { class: "scope-chip",
      title: "the model also saw these lines of the relic, as they were then",
      text: `with ${m.meta.relic} ` + sectionsLabel(m.meta) }))];
  }
  if (m.kind === "tool") {
    const args = Object.entries(m.meta.args || {}).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(" ");
    const cls = "tool" + (m.meta.tool && m.meta.tool.startsWith("risky_") ? " risky" : "") + (m.meta.ok ? "" : " failed");
    return [el("details", { class: cls },
      el("summary", { text: `${m.meta.tool} ${args}${m.meta.denied ? " · denied" : m.meta.ok ? "" : " · failed"}` }),
      el("pre", { text: m.text }))];
  }
  if (m.kind === "info") return [el("details", { class: "tool", open: true }, el("summary", { text: "commands and tools" }),
                                   el("pre", { text: m.text }))];
  const out = [el("div", { class: "cmsg-text", text: m.text })];
  if (m.meta.refs && m.meta.refs.length) {
    out.push(el("div", { class: "refs" }, "refs ", ...m.meta.refs.map((r) =>
      el("span", { title: r.text, text: `[${r.num}] ${r.source}:${r.line}  ` }))));
  }
  const steps = (m.meta.steps || []).filter((x) => x.role === "tool");
  if (steps.length) {
    out.push(el("details", { class: "tool" },
      el("summary", { text: `used ${steps.length} tool call(s): kept here, not in the context` }),
      el("pre", { text: (m.meta.steps || []).map((x) => (x.role === "tool" ? "└ " : "▸ ") + x.text).join("\n") })));
  }
  if (m.meta.plan) {
    out.push(el("div", { class: "cmsg-foot" }, el("button", { class: "small link", text: `open plan ${m.meta.plan} in Code`,
      onclick: () => { show("code"); openPlan(m.meta.plan); } })));
  }
  return out;
}
function chatCard(m, inThread) {
  const acts = [];
  if (!inThread && !m.thread) acts.push(el("button", { class: "small link", text: "reply", title: "answer this message in a thread: only its context",
                                                       onclick: () => openThread(m.num) }));
  if (m.seen) acts.push(el("button", { class: "small link", text: m.pinned ? "unpin" : "pin",
                                       title: m.pinned ? "stop keeping it in every prompt" : "keep it in every later prompt, threads too (CAG)",
                                       onclick: () => pinMessage(m.num, !m.pinned) }));
  if (m.role === "user") acts.push(el("button", { class: "small link", text: "edit", title: "clear from here on and put this message back in the box",
                                                  onclick: () => editMessage(m) }));
  if (m.role === "assistant" && m.text.trim()) {
    const b = el("button", { class: "small link", text: "speak", title: "read it aloud (text to speech, set under Models → Speech)" });
    b.addEventListener("click", () => speakText(m.text, b));
    acts.push(b);
  }
  if (m.kind === "gemini") acts.push(el("button", { class: "small link", text: "to knowledge base",
                                               title: "put this answer into the knowledge base (/kb), for research",
                                               onclick: () => sendChat(`/kb ${m.num}`, inThread ? m.thread : 0) }));
  if (m.role !== "user" && m.text.trim()) acts.push(el("button", { class: "small link", text: "export",
                                                   title: "save this message, or one of its code blocks, to a file (a relic)",
                                                   onclick: () => exportMessage(m) }));
  acts.push(el("button", { class: "small link", text: "rewind here", title: "clear everything after this message",
                           onclick: () => rewindTo(m) }));
  const replies = !inThread && chat.threads[m.num];
  return el("div", { class: `cmsg ${m.role} ${m.kind}` + (m.pinned ? " pinned" : "") + (m.seen ? "" : " outside"), id: `cmsg-${m.num}` },
    el("div", { class: "cmsg-head" },
      el("span", { class: "cmsg-num", text: `#${m.num}` }), el("span", { class: "who", text: who(m) }),
      m.pinned ? el("span", { class: "pin-mark", text: "pinned" }) : null,
      m.seen ? null : el("span", { text: "not in the context" }),
      el("span", { class: "grow" }), el("span", { class: "cmsg-actions" }, ...acts)),
    ...chatBody(m),
    replies ? el("div", { class: "cmsg-foot" }, el("button", { class: "small secondary", text: `${replies} in thread`,
                                                               onclick: () => openThread(m.num) })) : null);
}
function renderChat() {
  const has = !!chat;
  $("chat-title").value = has ? (chat.title === "New chat" && !chat.messages.length ? "" : chat.title) : "";
  $("chat-title").disabled = !has;
  renderContextToggle();
  $("chat-delete").hidden = !has;
  const box = $("chat-messages");
  box.replaceChildren(...chatLine(0).map((m) => chatCard(m, false)));
  if (!has || !chat.messages.length) {
    box.append(el("div", { class: "msg notice placeholder",
      text: "Talk with the model. It has no tools: start a message with a command line (/read, /ask, /agent …) to bring one in." }));
  }
  const summary = has && chat.summaries["0"];
  $("chat-summary").hidden = !summary;
  if (summary) $("chat-summary").querySelector("pre").textContent = summary;
  const last = chatLine(0).filter((m) => m.meta.context).pop();
  $("chat-meter").textContent = last ? `context ${Math.round(100 * last.meta.context.tokens / last.meta.context.budget)}%` : "";
  renderPins();
  renderThread();
  if (chatPending) pendingNode();
  box.scrollTop = box.scrollHeight;
}
function renderPins() {
  const pins = chat ? chat.messages.filter((m) => m.pinned) : [];
  $("chat-pins").hidden = !pins.length;
  $("chat-pins").replaceChildren(
    el("div", { class: "pins-head", text: `Pinned: in every prompt (${pins.length})` }),
    ...pins.map((m) => el("div", { class: "pins-item" },
      el("a", { href: "#", class: "grow", title: m.text, text: `#${m.num} ${m.text.split("\n")[0].slice(0, 90)}`,
                onclick: (e) => { e.preventDefault(); focusMessage(m); } }),
      el("button", { class: "small link", text: "unpin", onclick: () => pinMessage(m.num, false) }))));
}
function focusMessage(m) {
  if (m.thread && m.thread !== chatThread) openThread(m.thread);
  const node = $(`cmsg-${m.num}`);
  if (!node) return;
  node.scrollIntoView({ block: "center" });
  node.classList.add("focus");
  setTimeout(() => node.classList.remove("focus"), 1500);
}
function openThread(num) {
  chatThread = num;
  renderChat();
  $("thread-input").focus();
}
function renderThread() {
  const root = chat && chatThread ? chat.messages.find((m) => m.num === chatThread) : null;
  if (chatThread && !root) chatThread = 0;
  $("chat-thread").hidden = !root;
  $("chat-layout").classList.toggle("threaded", !!root);
  if (!root) return;
  $("thread-title").textContent = `Thread on #${root.num}`;
  const summary = chat.summaries[String(root.num)];
  $("thread-summary").hidden = !summary;
  if (summary) $("thread-summary").querySelector("pre").textContent = summary;
  const box = $("thread-messages");
  box.replaceChildren(chatCard(root, true), ...chatLine(root.num).map((m) => chatCard(m, true)));
  box.scrollTop = box.scrollHeight;
}
$("thread-close").addEventListener("click", () => { chatThread = 0; renderChat(); });
function renderChatHelp() {
  const rows = chatHelp.commands.map((c) => el("tr", {}, el("td", {}, el("code", { text: c.usage })), el("td", { text: c.about })));
  rows.push(el("tr", {}, el("td", {}, el("code", { text: "/TOOL_NAME ARGS" })),
                el("td", { text: "any tool by its own name: " + chatHelp.tools.join(", ") })));
  $("chat-commands").replaceChildren(...rows);
}

// ------------------------------------------------------ pins and rewind
// Export: the whole message, or one code block, to a workspace file kept as a relic.
const EXT = { python: "py", py: "py", javascript: "js", js: "js", typescript: "ts", ts: "ts", bash: "sh", sh: "sh",
              shell: "sh", json: "json", html: "html", css: "css", markdown: "md", md: "md", toml: "toml", yaml: "yaml",
              yml: "yaml", sql: "sql", c: "c", cpp: "cpp", rust: "rs", go: "go", java: "java" };
function codeBlocks(text) {
  return [...text.matchAll(/```([^\n`]*)\n([\s\S]*?)```/g)].map((x) => ({ lang: (x[1].trim().split(/\s+/)[0] || "").toLowerCase(), body: x[2],
                                                                    start: x.index, end: x.index + x[0].length }));
}
function exportMessage(m) {
  const blocks = codeBlocks(m.text);
  const part = $("export-part");
  part.replaceChildren(el("option", { value: "0", text: "the whole message" }),
    ...blocks.map((b, i) => el("option", { value: String(i + 1),
      text: `code block ${i + 1}${b.lang ? " (" + b.lang + ")" : ""}: ${(b.body.split("\n")[0] || "").slice(0, 40)}` })));
  const suggest = () => {
    const k = Number(part.value);
    $("export-path").value = `exports/${chat.id}-${m.num}` + (k ? `-${k}.${EXT[blocks[k - 1].lang] || "txt"}` : ".md");
  };
  part.value = blocks.length ? "1" : "0";
  part.onchange = suggest;
  suggest();
  $("export-force").checked = false;
  const dlg = $("export");
  dlg.onclose = async () => {
    if (dlg.returnValue !== "save") return;
    const block = Number(part.value) || null;
    try {
      const r = await api("POST", `/api/chats/${chat.id}/export`, { num: m.num, block, path: $("export-path").value.trim(),
                                                                force: $("export-force").checked });
      toast(`saved ${r.path} (${r.chars} chars) · a relic`);
    } catch (e) { toast(e.message, true); }
  };
  dlg.returnValue = "";
  dlg.showModal();
}
async function pinMessage(num, pinned) {
  try { chat = await api("PUT", `/api/chats/${chat.id}/messages/${num}/pin`, { pinned }); renderChat(); }
  catch (e) { toast(e.message, true); }
}
async function rewind(to, thread, what) {
  try {
    chat = await api("POST", `/api/chats/${chat.id}/rewind`, { to, thread });
    renderChat();
    loadChats();
    toast(`${chat.cleared} message(s) cleared` + (what ? `: ${what}` : ""));
  } catch (e) { toast(e.message, true); }
}
function rewindTo(m) {
  const where = m.thread ? `the thread on #${m.thread}` : "the chat (its threads too)";
  if (!confirm(`Clear everything in ${where} after #${m.num}? Answers, pins and context after it go.`)) return;
  rewind(m.num, m.thread);
}
// Edit: clear this message and everything after it, and put its text back in the box.
function editMessage(m) {
  const earlier = (m.thread ? chatLine(m.thread) : chatLine(0)).filter((x) => x.num < m.num).map((x) => x.num);
  const to = earlier.length ? earlier[earlier.length - 1] : (m.thread || 0);
  if (!confirm(`Clear #${m.num} and everything after it, to edit it?`)) return;
  const input = m.thread ? $("thread-input") : $("chat-input");
  rewind(to, m.thread, "edit it and send again").then(() => { input.value = m.text; input.focus(); });
}

// -------------------------------------------------------------- sending
async function sendChat(text, thread) {
  if (running || !text.trim()) return;
  if (!chat) {
    try { chat = await api("POST", "/api/chats"); } catch (e) { toast(e.message, true); return; }
  }
  const cid = chat.id;
  return followChat(cid, (cb) => stream(`/api/chats/${cid}/send`, { text, thread }, cb));
}
async function joinChat(job, reveal) {
  setRunning(true);
  if (reveal) show("chat");
  await openChat(job.target);
  setRunning(false);
  followChat(job.target, (cb) => stream(`/api/jobs/${job.id}/events?since=0`, undefined, cb));
}
// The pane a thread's messages show in: the thread panel if it is open, else the chat.
const chatPane = (thread) => (thread && thread === chatThread ? $("thread-messages") : thread ? null : $("chat-messages"));
function pendingNode() {
  const pane = chatPane(chatPending.thread);
  if (!pane) return null;
  if (!chatPending.node || !chatPending.node.isConnected) {
    for (const p of pane.querySelectorAll(".placeholder")) p.remove();
    chatPending.node = el("div", { class: "cmsg assistant pending" },
      el("div", { class: "cmsg-head" }, el("span", { class: "who", text: "model" })),
      el("div", { class: "cmsg-text", text: chatPending.text || "…" }));
    pane.append(chatPending.node);
  }
  return chatPending.node;
}
function chatNotice(text, thread) {
  const pane = chatPane(thread);
  if (pane) { pane.append(el("div", { class: "msg notice", text })); pane.scrollTop = pane.scrollHeight; }
}
async function followChat(cid, first) {
  live = { id: null, kind: "chat", target: cid };
  setRunning(true);
  setBusy("answering in chat");
  let thread = 0;
  const here = () => chat && chat.id === cid;
  const error = await followJob(first, (ev) => {
    if (ev.type === "message") {
      thread = ev.message.thread;
      chatPending = null;
      if (!here()) return;
      chat.messages = chat.messages.filter((m) => m.num !== ev.message.num).concat([ev.message]).sort((a, b) => a.num - b.num);
      if (ev.message.thread) chat.threads[ev.message.thread] = chatLine(ev.message.thread).length;
      renderChat();
    } else if (ev.type === "text") {
      if (!chatPending) chatPending = { thread, text: "", node: null };
      chatPending.text += ev.text;
      const node = here() && pendingNode();
      if (node) { node.querySelector(".cmsg-text").textContent = chatPending.text; node.parentNode.scrollTop = node.parentNode.scrollHeight; }
    } else if (ev.type === "notice" || ev.type === "status") { if (here()) chatNotice(ev.text, thread); }
    else if (ev.type === "tool_call") { if (here()) chatNotice(`running ${ev.name}`, thread); }
    else if (ev.type === "done") { if (here()) chat = ev.chat; renderState(ev.state); }
    else if (ev.type === "stopped") { if (here()) chatNotice("stopped", thread); renderState(ev.state); }
  });
  chatPending = null;
  live = null;
  setRunning(false);
  if (error) toast(error, true);
  if (here()) await openChat(cid); else loadChats();
  refreshState();
  loadRelics();
}
function composer(form, input, thread) {
  $(form).addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $(input).value.trim();
    if (!text || running) return;
    $(input).value = "";
    hideSuggest();
    sendChat(text, thread());
  });
  $(input).addEventListener("keydown", (e) => {
    if (e.key === "Tab" && !$("chat-suggest").hidden && input === "chat-input") { e.preventDefault(); takeSuggestion(0); return; }
    if (e.key === "Escape") hideSuggest();
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $(form).requestSubmit(); }
  });
}
composer("chat-form", "chat-input", () => 0);
composer("thread-form", "thread-input", () => chatThread);

// Command suggestions: typing "/" at the start of a line lists the commands that match.
function currentLine() {
  const t = $("chat-input"), before = t.value.slice(0, t.selectionStart);
  const start = before.lastIndexOf("\n") + 1;
  return { start, word: before.slice(start) };
}
function suggestions() {
  const { word } = currentLine();
  if (!/^\/[\w-]*$/.test(word)) return [];
  const all = chatHelp.commands.map((c) => ({ name: c.name, about: c.usage }))
    .concat(chatHelp.tools.map((t) => ({ name: t, about: "tool" })));
  return all.filter((c) => ("/" + c.name).startsWith(word)).slice(0, 8);
}
function hideSuggest() { $("chat-suggest").hidden = true; }
function takeSuggestion(i) {
  const pick = suggestions()[i];
  if (!pick) return hideSuggest();
  const t = $("chat-input"), { start } = currentLine();
  t.setRangeText(`/${pick.name} `, start, t.selectionStart, "end");
  hideSuggest();
  t.focus();
}
$("chat-input").addEventListener("input", () => {
  const list = suggestions();
  $("chat-suggest").hidden = !list.length;
  $("chat-suggest").replaceChildren(...list.map((c, i) =>
    el("div", { class: i ? "" : "active", onmousedown: (e) => { e.preventDefault(); takeSuggestion(i); } },
      el("strong", { text: "/" + c.name }), el("span", { text: c.about }))));
});
$("chat-input").addEventListener("blur", () => setTimeout(hideSuggest, 100));

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
  renderPins(r);
  $("research-context-note").hidden = !r.keep_context;
  $("research-context-note").textContent = "Keeping context: each question also sees the latest answers (Settings → Context).";
  if (!running) renderAnswers(r);
  if (!geminiAsking) renderGemini(r);
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
  const web = a.kind === "gemini";
  return el("div", { class: "answer" + (a.pinned ? " pinned" : "") + (web ? " web" : "") },
    el("div", { class: "question", text: (web ? "✦ " : "") + a.question }),
    web ? el("div", { class: "cmsg-text", text: a.text }) : citedText(a.text),
    web && a.sources.length ? el("ol", { class: "sources" }, ...a.sources.map((x) =>
      el("li", {}, el("a", { href: x.url, target: "_blank", rel: "noopener noreferrer", text: x.title })))) : null,
    el("div", { class: "answer-foot" },
      el("span", { class: "muted", text: `A${a.num}` + (a.refs.length ? " · refs " : "") }),
      ...a.refs.map((n) => el("a", { class: "cite", href: "#", text: `[${n}]`,
                                     onclick: (e) => { e.preventDefault(); openRef(n); } })),
      a.scope.length ? el("span", { class: "muted", text: ` · scope ${a.scope.join(", ")}` }) : null,
      web ? el("span", { class: "muted", text: ` · ${a.model || "Gemini"}` }) : null,
      el("span", { class: "grow" }),
      a.saved ? el("span", { class: "muted", text: `in the knowledge base ` })
              : el("button", { class: "small secondary", text: "Save", title: "put this answer into the knowledge base",
                               onclick: () => saveAnswer(a.num) }),
      el("button", { class: "small " + (a.pinned ? "" : "secondary"), text: a.pinned ? "Pinned" : "Pin",
                     title: a.pinned ? "unpin: later questions no longer see it"
                                     : "pin: later questions keep this answer in mind",
                     onclick: () => setPinned(a.num, !a.pinned) })),
    speeds[a.num] ? el("div", { class: "speed", text: speeds[a.num] }) : null);
}
function renderAnswers(r) {
  const box = $("answers"), mine = r.answers.filter((a) => a.kind !== "gemini");
  box.replaceChildren(...mine.map(answerCard));
  if (!mine.length) {
    box.append(el("div", { class: "msg notice placeholder",
      text: "Ask about your files (below) and notes (left). Answers cite the excerpts they rest on." }));
  }
  box.scrollTop = box.scrollHeight;
}
// Pinned answers (CAG), above the thread: what later questions keep in mind.
function renderPins(r) {
  const pinned = r.answers.filter((a) => a.pinned);
  $("research-pins").hidden = !pinned.length;
  $("research-pins").replaceChildren(
    el("div", { class: "pins-head", text: `Pinned: in every question (${pinned.length})` }),
    ...pinned.map((a) => el("div", { class: "pins-item" },
      el("span", { class: "grow", title: a.text },
        el("strong", { text: `A${a.num} ` }), document.createTextNode(a.question)),
      el("button", { class: "small link", text: "unpin", onclick: () => setPinned(a.num, false) }))));
}
async function saveAnswer(num) {
  setBusy("indexing");
  try {
    const r = await api("POST", `/api/research/answers/${num}/save`, {});
    renderResearch(r.research);
    toast(`saved as ${r.saved} · ${r.result}`);
  } catch (e) { toast(e.message, true); }
  setBusy("");
}
// Gemini (its own tab): answers numbered with the others, pinned or saved like them.
let geminiAsking = false;
function renderGemini(r) {
  const box = $("gemini-answers"), mine = r.answers.filter((a) => a.kind === "gemini");
  box.replaceChildren(...mine.map(answerCard));
  if (!mine.length) box.append(el("div", { class: "msg notice placeholder", text: "Nothing asked of Gemini yet." }));
  box.scrollTop = box.scrollHeight;
}
async function askGemini(q) {
  const box = $("gemini-answers");
  for (const p of box.querySelectorAll(".placeholder")) p.remove();
  const pending = el("div", { class: "answer pending web" }, el("div", { class: "question", text: "✦ " + q }),
                     el("div", { class: "muted", text: "asking Gemini…" }));
  box.append(pending);
  box.scrollTop = box.scrollHeight;
  geminiAsking = true;
  $("gemini-btn").disabled = true;
  let failed = false;
  try { const r = await api("POST", "/api/research/gemini", { question: q }); geminiAsking = false; renderResearch(r.research); }
  catch (e) { failed = true; pending.lastChild.textContent = e.message; pending.classList.add("failed"); }
  geminiAsking = false;
  $("gemini-btn").disabled = false;
  if (failed && !$("gemini-input").value) $("gemini-input").value = q;
}
$("gemini-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("gemini-input").value.trim();
  if (!q || geminiAsking) return;
  $("gemini-input").value = "";
  askGemini(q);
});
$("gemini-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("gemini-form").requestSubmit(); }
});

// PubMed: open-access articles, added to the knowledge base by DOI
function pubmedStatus(text) { $("pubmed-status").textContent = text; }
async function addArticles(ids) {
  setBusy("fetching articles");
  try {
    await stream("/api/research/doi", { ids }, (ev) => {
      if (ev.type === "status") pubmedStatus(ev.text + "…");
      else if (ev.type === "done") {
        renderResearch(ev.research);
        pubmedStatus(ev.added.map((a) => `added ${a.title.slice(0, 60)} (${a.full_text ? "full text" : "abstract only"})`).join(" · "));
        for (const a of ev.added) for (const b of document.querySelectorAll(`[data-article="${CSS.escape(a.pmcid || a.doi)}"]`)) {
          b.textContent = "added"; b.disabled = true;
        }
      }
    });
  } catch (e) { pubmedStatus(e.message); }
  setBusy("");
}
$("pubmed-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const query = $("pubmed-query").value.trim();
  if (!query) return;
  pubmedStatus("searching PubMed Central…");
  try {
    const r = await api("POST", "/api/research/pubmed", { query });
    pubmedStatus(r.articles.length ? `${r.articles.length} open-access article(s)` : "no open-access articles match");
    $("pubmed-results").replaceChildren(...r.articles.map((a) => el("li", { class: "article" },
      el("div", { class: "grow" },
        el("strong", { text: a.title }), el("br"),
        el("span", { class: "muted", text: a.cite + " " }),
        a.pmcid ? el("a", { href: `https://pmc.ncbi.nlm.nih.gov/articles/${a.pmcid}/`, target: "_blank",
                            rel: "noopener noreferrer", text: "open" }) : null),
      el("button", { class: "small", text: "Add", "data-article": a.pmcid || a.doi,
                     title: "put it into the knowledge base", onclick: () => addArticles(a.pmcid || a.doi) }))));
  } catch (err) { pubmedStatus(err.message); }
});
$("doi-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const ids = $("doi-input").value.trim();
  if (!ids) return;
  $("doi-input").value = "";
  addArticles(ids);
});

async function setPinned(num, pinned) {
  try { renderResearch(await api("PUT", `/api/research/answers/${num}/pin`, { pinned })); }
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
  if (research && research.answers.length && !confirm("Start a new session? Answers, references and pins are cleared.")) return;
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
    el("a", { href: "#", class: "grow", text: x.title, title: "open in Notes",
              onclick: (e) => { e.preventDefault(); show("notes"); openNoteById(x.source.slice(5)); } }))));
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

// ================================================================== notes
let noteId = null, noteDirty = false, noteTag = "", noteTimer;
async function loadNotes() {
  try {
    const q = $("note-q").value.trim();
    const data = await api("GET", "/api/notes?" + new URLSearchParams({ q, tag: noteTag }));
    if (noteTag && !data.tags.some(([t]) => t === noteTag)) { noteTag = ""; return loadNotes(); }
    $("note-tags").replaceChildren(...data.tags.map(([t, n]) => el("button", {
      class: "chip" + (t === noteTag ? " active" : ""), text: `#${t} ${n}`, title: t === noteTag ? "show every note" : "only notes tagged #" + t,
      onclick: () => { noteTag = noteTag === t ? "" : t; loadNotes(); } })));
    $("note-list").replaceChildren(...data.notes.map((n) =>
      el("li", { class: String(n.id) === String(noteId) ? "active" : "", onclick: () => openNoteById(n.id) },
        el("span", { text: n.title }),
        el("span", { class: "sub", text: n.snippet || `[${n.id}] · ${when(n.mtime)}` }),
        n.tags.length ? el("span", { class: "tags", text: n.tags.map((t) => "#" + t).join(" ") }) : null)));
    if (!data.notes.length) $("note-list").append(el("li", { class: "muted", text: q || noteTag ? "nothing matches" : "no notes yet" }));
  } catch (e) { toast(e.message, true); }
}
loaders.notes = loadNotes;
async function openNoteById(id) {
  if (noteDirty && String(id) !== String(noteId) && !confirm("Discard unsaved changes?")) return;
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
  $("note-text").focus();
  loadNotes();
}
function newNote() {
  if (noteDirty && !confirm("Discard unsaved changes?")) return;
  noteDirty = false;
  openNote(null);
}
$("note-new").addEventListener("click", newNote);
$("research-note-new").addEventListener("click", () => { show("notes"); newNote(); });
$("note-q").addEventListener("input", () => { clearTimeout(noteTimer); noteTimer = setTimeout(loadNotes, 250); });
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
    openNote(null);
    toast("deleted");
    loadResearch();
  } catch (e) { toast(e.message, true); }
});

// ================================================================ code (plans)
let plan = null;
async function loadPlans() {
  try {
    const data = await api("GET", "/api/code");
    $("plan-list").replaceChildren(...data.plans.map((p) =>
      el("li", { class: "item" + (plan && p.id === plan.id ? " active" : ""), onclick: () => openPlan(p.id) },
        el("div", { class: "grow" },
          el("span", { text: p.goal }),
          el("span", { class: "sub", text: `${p.id} · ${p.done}/${p.tasks} done${p.finished ? " · finished" : ""}` })),
        el("button", { class: "small link", text: "✕", title: "delete this plan",
                       onclick: (e) => { e.stopPropagation(); deletePlan(p.id, p.goal); } }))));
    if (!data.plans.length) $("plan-list").append(el("li", { class: "muted", text: "no plans yet" }));
    if (!plan && data.current) openPlan(data.current);
  } catch (e) { toast(e.message, true); }
}
loaders.code = loadPlans;
async function openPlan(id, keepLog = false) {
  try {
    renderPlan(await api("GET", `/api/code/${id}`));
    if (!keepLog) ownLog($("run-log"), id);
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
        el("input", { type: "checkbox", class: "tick", checked: t.status === "done", disabled: running || t.status === "running",
                      title: t.status === "done" ? "untick: the next run does this step again" : "tick: mark this step done",
                      onchange: (e) => markSteps([t.num], e.target.checked) }),
        el("strong", { text: `${t.num}.` }), el("span", { text: t.task }),
        el("span", { class: "tool-name", text: t.tool }),
        t.after.length ? el("span", { class: "muted", text: `after ${t.after.join(", ")}` }) : null,
        el("span", { class: "grow" }),
        t.depth ? el("span", { class: "depth", text: `pass ${t.depth}`, title: "expansion passes so far" }) : null,
        el("span", { class: "status " + t.status, text: t.status })),
      t.detail ? el("div", { class: "detail", text: t.detail }) : null,
      ...t.checks.map((c) => el("div", { class: "check-line " + c.status },
        el("span", { class: "mark", text: { done: "✓", failed: "!" }[c.status] || "·" }),
        el("span", { text: "check" }), el("span", { class: "tool-name", text: c.tool }), el("span", { text: c.task }),
        c.result ? el("span", { class: "muted", text: "→ " + c.result }) : null)),
      t.result ? el("div", { class: "result", text: "→ " + t.result + (t.attempts > 1 ? ` (attempt ${t.attempts})` : "") }) : null)));
  showPersistence(p);
}
// The persistence gauge: retries per failed step; its top means until it succeeds or Stop.
function persistenceLabel(n, max) {
  if (n >= max) return "max: until it succeeds or you press Stop";
  return n ? `${n} retr${n === 1 ? "y" : "ies"} per failed step` : "0: a failed step is not tried again";
}
function showPersistence(p) {
  $("run-perms").value = p.permissions || "ask";
  $("run-perms").classList.toggle("danger", (p.permissions || "ask") !== "ask");
  const g = $("run-persist");
  g.max = p.persistence_max;
  g.value = p.persistence;
  $("run-persist-label").textContent = persistenceLabel(p.persistence, p.persistence_max);
}
$("run-persist").addEventListener("input", (e) => {
  $("run-persist-label").textContent = persistenceLabel(Number(e.target.value), Number(e.target.max));
});
$("run-persist").addEventListener("change", async (e) => {
  if (!plan) return;
  try { plan = await api("PUT", `/api/code/${plan.id}/persistence`, { persistence: Number(e.target.value) }); }
  catch (err) { toast(err.message, true); }
  showPersistence(plan);
});
$("run-perms").addEventListener("change", async (e) => {
  if (!plan) return;
  try { plan = await api("PUT", `/api/code/${plan.id}/permissions`, { permissions: e.target.value }); }
  catch (err) { toast(err.message, true); }
  showPersistence(plan);
});
async function markSteps(steps, done) {
  if (!plan) return;
  try { renderPlan(await api("PUT", `/api/code/${plan.id}/steps`, { steps, done })); loadPlans(); }
  catch (e) { toast(e.message, true); openPlan(plan.id, true); }
}
$("plan-untick").addEventListener("click", () => {
  if (plan && confirm("Untick every step? The next run does them all again.")) markSteps(null, false);
});
async function deletePlan(id, goal) {
  if (running && plan && plan.id === id) { toast("this plan is running; wait for the run to end", true); return; }
  if (!confirm(`Delete the plan “${goal}”? Files its runs wrote stay.`)) return;
  try {
    await api("DELETE", `/api/code/${id}`);
    if (plan && plan.id === id) {
      plan = null;
      $("plan-detail").hidden = true;
      $("plan-editor").hidden = true;
      $("plan-empty").hidden = false;
      $("run-log").replaceChildren();
    }
    toast("plan deleted");
    loadPlans();
  } catch (e) { toast(e.message, true); }
}
$("plan-delete").addEventListener("click", () => { if (plan) deletePlan(plan.id, plan.goal); });
$("plan-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const goal = $("plan-goal").value.trim();
  if (!goal) return;
  setBusy("planning");
  try {
    const p = await api("POST", "/api/code", { goal });
    $("plan-goal").value = "";
    renderPlan(p);
    (p.warnings || []).forEach((w) => toast(w));
    loadPlans();
  } catch (err) { toast(err.message, true); }
  refreshState();
});
// Drafting or editing a plan: a row per step (its tool, what to do), its details and
// checks below it; or, toggled, the same plan as text. Either way the server gets the
// text form, so nothing behind the editor changes.
let planEditing = null, planTools = ["none"], planAsText = false;
const DETAIL_INDENT = "   ";
async function openPlanEditor(p) {
  planEditing = p ? p.id : null;
  $("plan-editor-title").textContent = p ? `Edit plan ${p.id}` : "Draft a plan";
  $("plan-edit-goal").value = p ? p.goal : $("plan-goal").value.trim();
  $("plan-empty").hidden = true;
  $("plan-detail").hidden = true;
  $("plan-editor").hidden = false;
  try { planTools = ["none", ...(await api("GET", "/api/tools")).tools.map((x) => x.name)]; }
  catch (e) { /* steps can still use none, or be edited as text */ }
  const steps = p ? p.tasks.map((t) => ({ tool: t.tool, text: t.task + (t.after.length ? ` (after: ${t.after.join(", ")})` : ""),
                                          detail: t.detail, checks: t.checks.map((c) => ({ tool: c.tool, text: c.task })) }))
                  : [];
  $("plan-edit-text").value = p ? p.text : "";
  setPlanEditMode(false, steps.length ? steps : [blankStep()]);
}
const blankStep = () => ({ tool: "none", text: "", detail: "", checks: [] });
function toolSelect(value) {
  const sel = el("select", { class: "tool-pick", title: "the tool this step uses" },
    ...[...new Set([...planTools, value])].map((t) => el("option", { value: t, text: t })));
  sel.value = value;
  return sel;
}
function checkRow(c) {
  return el("div", { class: "check-row" },
    el("span", { class: "muted", text: "check" }), toolSelect(c.tool),
    el("input", { class: "what", value: c.text, placeholder: "what to verify, e.g. python hello.py prints hello" }),
    el("button", { class: "small link", text: "✕", title: "remove this check", onclick: (e) => e.target.closest(".check-row").remove() }));
}
function stepRow(st) {
  const row = el("div", { class: "step-row" });
  const checks = el("div", { class: "checks" }, ...st.checks.map(checkRow));
  const move = (dir) => {
    const to = dir < 0 ? row.previousElementSibling : row.nextElementSibling;
    if (to) (dir < 0 ? to.before(row) : to.after(row));
    numberSteps();
  };
  row.append(
    el("div", { class: "step-main" },
      el("strong", { class: "num" }), toolSelect(st.tool),
      el("input", { class: "what", value: st.text, placeholder: "what to do (after: N)" }),
      el("button", { class: "small link", text: "+ check", title: "add a check: if it fails, this step is done again",
                     onclick: () => checks.append(checkRow({ tool: "none", text: "" })) }),
      el("button", { class: "small link", text: "↑", title: "move up", onclick: () => move(-1) }),
      el("button", { class: "small link", text: "↓", title: "move down", onclick: () => move(1) }),
      el("button", { class: "small link", text: "✕", title: "remove this step", onclick: () => { row.remove(); numberSteps(); } })),
    el("textarea", { class: "step-detail", rows: 2, spellcheck: "false", placeholder: "details (optional): how to do it" }),
    checks);
  row.querySelector(".step-detail").value = st.detail;
  return row;
}
function numberSteps() {
  [...$("plan-rows").children].forEach((r, i) => { r.querySelector(".num").textContent = `${i + 1}.`; });
}
function renderSteps(steps) {
  $("plan-rows").replaceChildren(...steps.map(stepRow));
  numberSteps();
}
function readSteps() {
  return [...$("plan-rows").children].map((r) => ({
    tool: r.querySelector(".step-main .tool-pick").value,
    text: r.querySelector(".step-main .what").value.trim(),
    detail: r.querySelector(".step-detail").value,
    checks: [...r.querySelectorAll(".check-row")].map((c) => ({ tool: c.querySelector(".tool-pick").value,
                                                                text: c.querySelector(".what").value.trim() }))
                                                 .filter((c) => c.text),
  })).filter((st) => st.text);
}
function stepsToText(steps) {
  return steps.map((st, i) => [`${i + 1}. [${st.tool}] ${st.text.replace(/\s+/g, " ")}`,
    ...st.detail.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => DETAIL_INDENT + l),
    ...st.checks.map((c) => `${DETAIL_INDENT}check: [${c.tool}] ${c.text.replace(/\s+/g, " ")}`)].join("\n")).join("\n");
}
// The text form, read back into rows (the server reads it the same way when saving).
function textToSteps(text) {
  const steps = [];
  for (const raw of text.split("\n")) {
    if (!raw.trim()) continue;
    const st = steps[steps.length - 1];
    if (/^[ \t]/.test(raw) && st) {
      const c = raw.trim().match(/^check\s*:\s*(?:\[([^\]]*)\])?\s*(.+)$/i);
      if (c) st.checks.push({ tool: (c[1] || "none").trim() || "none", text: c[2].trim() });
      else st.detail += (st.detail ? "\n" : "") + raw.trim();
      continue;
    }
    const m = raw.match(/^\s*(?:[-*]\s*)?(?:\d+[.):]\s*)?(?:\[([^\]]*)\])?\s*(.*)$/);
    steps.push({ tool: (m[1] || "none").trim() || "none", text: m[2].trim(), detail: "", checks: [] });
  }
  return steps;
}
function setPlanEditMode(asText, steps) {
  planAsText = asText;
  if (asText) $("plan-edit-text").value = stepsToText(steps || readSteps());
  else renderSteps(steps || (textToSteps($("plan-edit-text").value).length ? textToSteps($("plan-edit-text").value) : [blankStep()]));
  for (const id of ["plan-edit-text", "plan-text-hint"]) $(id).hidden = !asText;
  for (const id of ["plan-rows", "plan-rows-bar"]) $(id).hidden = asText;
  $("plan-edit-mode").textContent = asText ? "edit as steps" : "edit as text";
  (asText ? $("plan-edit-text") : $("plan-rows").querySelector(".what"))?.focus();
}
function closePlanEditor() {
  $("plan-editor").hidden = true;
  if (plan) $("plan-detail").hidden = false; else $("plan-empty").hidden = false;
}
$("plan-draft").addEventListener("click", () => openPlanEditor(null));
$("plan-edit").addEventListener("click", () => { if (plan && !running) openPlanEditor(plan); });
$("plan-edit-cancel").addEventListener("click", closePlanEditor);
$("plan-edit-mode").addEventListener("click", () => setPlanEditMode(!planAsText));
$("plan-add-step").addEventListener("click", () => {
  $("plan-rows").append(stepRow(blankStep()));
  numberSteps();
  $("plan-rows").lastElementChild.querySelector(".what").focus();
});
$("plan-edit-text").addEventListener("keydown", (e) => {     // Tab indents: details are indented lines
  if (e.key !== "Tab") return;
  e.preventDefault();
  const t = e.target, at = t.selectionStart;
  t.setRangeText("   ", at, t.selectionEnd, "end");
});
$("plan-edit-save").addEventListener("click", async () => {
  const text = planAsText ? $("plan-edit-text").value : stepsToText(readSteps());
  const body = { goal: $("plan-edit-goal").value.trim(), text };
  if (!body.text.trim()) { toast("write at least one step", true); return; }
  try {
    const p = planEditing ? await api("PUT", `/api/code/${planEditing}`, body) : await api("POST", "/api/code", body);
    if (!planEditing) $("plan-goal").value = "";
    $("run-log").replaceChildren();
    renderPlan(p);
    (p.warnings || []).forEach((w) => toast(w));
    toast(planEditing ? "plan saved" : "plan created");
    loadPlans();
  } catch (e) { toast(e.message, true); }
});

// ================================================================== runs
// Plan runs and expansions work on the server in the background. The page
// follows a run by its events; if the connection drops (a reload, a phone that put the
// browser to sleep) it joins again where it left off, and a page opened later (or on
// another device) joins from the start. RUNS[kind] has what each kind adds.
let live = null;                       // the run this page follows: {id, kind, target}
const RUNS = {};
function setRunning(on) {
  running = on;
  for (const id of ["run-btn", "expand-btn", "chat-send", "thread-send"]) $(id).disabled = on;
}
const runLog = () => $("run-log");
// A log belongs to the plan its run worked on: hidden while another one is
// open during the run, and cleared when another one is opened after it.
function ownLog(log, shown) {
  const owner = log.dataset.owner;
  if (!owner || owner === shown) { log.hidden = false; return; }
  if (live && live.target === owner && runLog(live.kind) === log) { log.hidden = true; return; }
  log.replaceChildren();
  delete log.dataset.owner;
  log.hidden = false;
}
function startRun(kind, target, url, body) {
  if (running) return;
  return followRun(kind, target, (cb) => stream(url, body, cb));
}
async function joinRun(job, reveal) {
  if (running) return;
  if (job.kind === "chat") return joinChat(job, reveal);
  setRunning(true);                    // claimed, so the next state check does not join twice
  if (reveal) show("code");
  await openPlan(job.target);
  setRunning(false);
  followRun(job.kind, job.target, (cb) => stream(`/api/jobs/${job.id}/events?since=0`, undefined, cb));
}
// Waits until the page is visible again (a sleeping phone cannot connect), then a little.
function rejoinDelay(tries) {
  return new Promise((ok) => {
    const go = () => {
      if (document.hidden) return;
      document.removeEventListener("visibilitychange", go);
      setTimeout(ok, Math.min(500 * tries, 5000));
    };
    document.addEventListener("visibilitychange", go);
    go();
  });
}
// Follows a background job until it ends, joining it again where it left off if the
// connection drops. `first` opens the stream; onEvent gets each event but the job's own.
// Resolves with an error to show, or "".
async function followJob(first, onEvent) {
  let id = null, seq = -1, tries = 0, open = first;
  const each = (ev) => {
    if (ev.type === "job") { id = ev.id; if (live) live.id = id; return; }
    seq = ev.seq;
    tries = 0;
    onEvent(ev);
  };
  for (;;) {
    try {
      const last = await open(each);
      if (last && (last.type === "done" || last.type === "stopped")) return "";
    } catch (e) {
      if (e.server) return e.message;
    }
    if (!id || ++tries > 20) return "lost the connection to the run";
    await rejoinDelay(tries);
    open = (cb) => stream(`/api/jobs/${id}/events?since=${seq + 1}`, undefined, cb);
  }
}
// What every run shares (streamed text, tool calls and results, notices, Stop, errors,
// the busy state); RUNS[kind].onEvent gets the run's own events.
async function followRun(kind, target, first) {
  const r = RUNS[kind], log = runLog(kind);
  live = { id: null, kind, target };
  setRunning(true);
  setBusy(r.busy);
  log.replaceChildren();
  log.dataset.owner = target;
  log.hidden = false;
  let text = null;
  const onEvent = (ev) => {
    if (ev.type === "text") {
      if (!text) { text = el("div"); log.append(text); }
      text.textContent += ev.text;
    } else if (ev.type === "text_end") text = null;
    else if (ev.type === "tool_call") { text = null; logLine(`  ▸ ${ev.name}(${JSON.stringify(ev.args).slice(0, 160)})`, "", log); }
    else if (ev.type === "tool_result") logLine("    └ " + ev.text.split("\n").slice(0, 3).join(" ⏎ ").slice(0, 240), ev.ok ? "ev-dim" : "ev-bad", log);
    else if (ev.type === "notice" || ev.type === "status") logLine("· " + ev.text, "ev-dim", log);
    else if (ev.type === "stopped") { logLine("■ stopped", "ev-bad", log); renderState(ev.state); }
    else if (ev.type !== "stats" && ev.type !== "loaded") { text = null; r.onEvent(ev, log); }
  };
  const error = await followJob(first, onEvent);
  if (error) logLine(error, "ev-bad", log);
  live = null;
  setRunning(false);
  refreshState();
  loadRelics();
  r.after();
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
  const body = { steps: stepNumbers($("expand-steps").value), times: Number($("expand-times").value) || 1,
                 parallel: $("expand-parallel").checked, checks: $("expand-checks").checked };
  await startRun("expand", plan.id, `/api/code/${plan.id}/expand`, body);
});
// A plan shown is redrawn from the run's events only if it is the plan the run is on.
const showRunPlan = (p) => { if (plan && plan.id === p.id) renderPlan(p); };
const afterPlanRun = () => { if (plan) openPlan(plan.id, true); else loadPlans(); };
RUNS.expand = { busy: "expanding the plan", after: afterPlanRun,
  onEvent: (ev, log) => {
    if (ev.type === "step") logLine(`  ✓ step ${ev.num}: pass ${ev.depth}`, "ev-ok", log);
    else if (ev.type === "done") { logLine("■ expanded", "ev-task", log); showRunPlan(ev.plan); renderState(ev.state); }
  } };
$("run-btn").addEventListener("click", async () => {
  if (!plan) return;
  const body = { mode: $("run-mode").value, parallel: $("run-parallel").checked, persistence: Number($("run-persist").value),
                 permissions: $("run-perms").value };
  await startRun("run", plan.id, `/api/code/${plan.id}/run`, body);
});
RUNS.run = { busy: "running the plan", after: afterPlanRun,
  onEvent: (ev, log) => {
    if (ev.type === "task_start") logLine(`▶ step ${ev.num}: ${ev.task} [${ev.tool}]`, "ev-task", log);
    else if (ev.type === "task_end") {
      logLine(`  ${ev.status.toUpperCase()}: ${ev.result}`, ev.status === "done" ? "ev-ok" : "ev-bad", log);
      showRunPlan(ev.plan);
    } else if (ev.type === "done") {
      logLine(`■ ${ev.reason}`, "ev-task", log);
      showRunPlan(ev.plan); renderState(ev.state);
    }
  } };

// ================================================================= relics
// The files plans wrote: listed under the tabs, opened, edited and run here.

// Syntax highlighting: a highlighted copy of the text sits under the (transparent)
// editor, kept in step as you type and scroll. Small and dependency-free: comments,
// strings, numbers, keywords, and names being called, by file type.
const KEYWORDS = {
  py: "False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case self print len range",
  js: "async await break case catch class const continue debugger default delete do else export extends false finally for from function if import in instanceof let new null of return static super switch this throw true try typeof undefined var void while with yield console",
  sh: "if then else elif fi for while until do done case esac function in return exit local export readonly echo cd set unset source shift true false",
  c: "auto break case char const continue default do double else enum extern float for goto if inline int long register return short signed sizeof static struct switch typedef union unsigned void volatile while bool true false NULL class public private protected namespace template typename new delete this virtual override nullptr using std",
  go: "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var nil true false",
  rs: "as async await break const continue crate else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while",
  rb: "alias and begin break case class def defined do else elsif end ensure false for if in module next nil not or redo rescue retry return self super then true undef unless until when while yield puts require",
  sql: "select from where insert into values update set delete create table drop alter add primary key foreign references not null and or as join left right inner outer on group by order having limit offset distinct union all index view is in like between case when then else end",
  lua: "and break do else elseif end false for function goto if in local nil not or repeat return then true until while",
  java: "abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for if implements import instanceof int interface long native new null package private protected public return short static super switch this throw throws try void volatile while true false var",
  toml: "true false", json: "true false null", yaml: "true false null yes no",
};
const LANG = { py: "py", pyw: "py", js: "js", mjs: "js", cjs: "js", ts: "js", tsx: "js", jsx: "js", sh: "sh", bash: "sh",
  zsh: "sh", c: "c", h: "c", cpp: "c", cc: "c", hpp: "c", cs: "c", swift: "c", kt: "java", go: "go", rs: "rs", rb: "rb", sql: "sql",
  lua: "lua", java: "java", toml: "toml", ini: "toml", cfg: "toml", json: "json", yaml: "yaml", yml: "yaml", r: "py",
  html: "html", htm: "html", xml: "html", svg: "html", md: "md", css: "css" };
const HASH_COMMENTS = new Set(["py", "sh", "rb", "toml", "yaml"]);
const highlighters = {};
function highlighter(lang) {
  if (highlighters[lang]) return highlighters[lang];
  const parts = [];
  if (lang === "html") {
    parts.push(["c", "<!--[\\s\\S]*?-->"], ["s", `"[^"]*"|'[^']*'`], ["k", "</?[A-Za-z][\\w:-]*|/?>"], ["f", "\\b[\\w:-]+(?==)"]);
  } else if (lang === "md") {
    parts.push(["s", "```[\\s\\S]*?```|`[^`\\n]*`"], ["k", "^#{1,6} .*$"], ["f", "\\*\\*[^*\\n]+\\*\\*|\\[[^\\]\\n]+\\]\\([^)\\n]+\\)"],
               ["c", "^>.*$"], ["n", "^\\s*(?:[-*+]|\\d+\\.)(?= )"]);
  } else if (lang === "css") {
    parts.push(["c", "/\\*[\\s\\S]*?\\*/"], ["s", `"[^"\\n]*"|'[^'\\n]*'`],
               ["n", "#[0-9a-fA-F]{3,8}\\b|-?\\d+\\.?\\d*(?:px|em|rem|%|vh|vw|s|ms)?\\b"], ["k", "@[\\w-]+|![\\w]+"], ["f", "[\\w-]+(?=\\s*:)"]);
  } else {
    const c = [];
    if (lang === "c") c.push("^\\s*#\\s*\\w+.*$");
    if (HASH_COMMENTS.has(lang)) c.push("#.*$");
    if (["js", "c", "go", "rs", "java"].includes(lang)) c.push("//.*$", "/\\*[\\s\\S]*?\\*/");
    if (lang === "sql" || lang === "lua") c.push("--.*$");
    if (c.length) parts.push(["c", c.join("|")]);
    const strs = [`"(?:\\\\.|[^"\\\\\\n])*"`, `'(?:\\\\.|[^'\\\\\\n])*'`];
    if (lang === "py") strs.unshift(`[rbfuRBFU]{0,2}"""[\\s\\S]*?"""`, `[rbfuRBFU]{0,2}'''[\\s\\S]*?'''`);
    if (lang === "js" || lang === "go") strs.push("`(?:\\\\.|[^`\\\\])*`");
    if (lang === "sh") strs.push("\\$\\{?\\w+\\}?");
    parts.push(["s", strs.join("|")]);
    if (lang === "py") parts.push(["f", "@[\\w.]+"]);
    parts.push(["n", "\\b(?:0[xXbBoO][\\da-fA-F_]+|\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?)\\b"]);
    const kw = (KEYWORDS[lang] || "").split(" ").filter(Boolean);
    if (kw.length) parts.push(["k", `\\b(?:${kw.join("|")})\\b`]);
    if (["toml", "json", "yaml"].includes(lang)) parts.push(["f", "^\\s*[\\w.-]+(?=\\s*[:=])"]);
    else parts.push(["f", "\\b[A-Za-z_]\\w*(?=\\s*\\()"]);
  }
  const names = parts.map(([n]) => n);
  return (highlighters[lang] = { names, re: new RegExp(parts.map(([, r]) => `(${r})`).join("|"), "gm" + (lang === "sql" ? "i" : "")) });
}
const escapeHtml = (t) => t.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
function highlight(text, lang) {
  if (!lang || text.length > 300000) return escapeHtml(text);
  const { names, re } = highlighter(lang);
  let out = "", last = 0;
  for (const m of text.matchAll(re)) {
    if (!m[0]) continue;
    const g = m.slice(1).findIndex((x) => x !== undefined);
    out += escapeHtml(text.slice(last, m.index)) + `<span class="t-${names[g]}">${escapeHtml(m[0])}</span>`;
    last = m.index + m[0].length;
  }
  return out + escapeHtml(text.slice(last));
}
let relicLang = "", hlQueued = false;
function renderHighlight() {
  hlQueued = false;
  // a trailing newline needs a character after it, or the last line is not drawn
  const text = $("relic-text").value;
  $("relic-hl").firstChild.innerHTML = highlight(text, relicLang) + "\n ";
  renderLineNumbers(text);
  syncScroll();
}
let lineCount = 0;
function renderLineNumbers(text) {
  const n = text.split("\n").length;
  if (n === lineCount) return;
  lineCount = n;
  $("relic-wrap").style.setProperty("--gutter", `${String(n).length + 2}ch`);
  $("relic-ln").textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n") + "\n ";
}
function queueHighlight() { if (!hlQueued) { hlQueued = true; requestAnimationFrame(renderHighlight); } }
function syncScroll() {
  const t = $("relic-text");
  $("relic-hl").scrollTop = $("relic-ln").scrollTop = t.scrollTop;
  $("relic-hl").scrollLeft = t.scrollLeft;
}
$("relic-text").addEventListener("scroll", syncScroll);
$("relic-text").addEventListener("keydown", (e) => {          // Tab indents instead of leaving the editor
  if (e.key !== "Tab" || e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
  e.preventDefault();
  document.execCommand("insertText", false, "    ");
});
new ResizeObserver(syncScroll).observe($("relic-text"));
const RELICS_IN_NAV = 12;
let relics = [], relicPath = null, relicDirty = false;
const relicBy = (by) => by.map((b) => b.replace(":", " ")).join(", ");
async function loadRelics() {
  try { relics = (await api("GET", "/api/relics")).relics; } catch (e) { toast(e.message, true); return; }
  renderRelics();
}
loaders.relics = loadRelics;
function renderRelics() {
  const cls = (r) => (r.path === relicPath ? " active" : "") + (r.exists ? "" : " gone");
  $("relic-nav").replaceChildren(
    ...relics.slice(0, RELICS_IN_NAV).map((r) => el("li", { class: cls(r).trim(), text: r.path.split("/").pop(),
      title: `${r.path} · ${relicBy(r.by)}${r.exists ? "" : " · file gone"}`, onclick: () => openRelic(r.path) })));
  if (relics.length > RELICS_IN_NAV) $("relic-nav").append(el("li", { class: "more", text: `+${relics.length - RELICS_IN_NAV} more` }));
  $("relic-list").replaceChildren(...relics.map((r) =>
    el("li", { class: "item" + cls(r), onclick: () => openRelic(r.path) },
      el("div", { class: "grow" },
        el("span", { text: r.path }),
        el("span", { class: "sub", text: `${relicBy(r.by)} · ` + (r.exists ? `${when(r.updated)} · ${r.size} bytes` : "file gone") })),
      el("button", { class: "small link", text: "✕", title: "forget: take it off the list; the file stays",
                     onclick: (e) => { e.stopPropagation(); forgetRelic(r.path); } }))));
  if (!relics.length) $("relic-list").append(el("li", { class: "muted", text: "nothing yet: run a plan that writes files" }));
}
async function openRelic(path, force = false) {
  if (!force && relicDirty && path !== relicPath && !confirm("Discard unsaved changes?")) return;
  if (!$("view-relics").classList.contains("active")) show("relics");
  const r = relics.find((x) => x.path === path);
  relicPath = path;
  relicDirty = false;
  renderRelics();
  $("relic-empty").hidden = true;
  $("relic-detail").hidden = false;
  $("relic-title").textContent = path;
  $("relic-meta").textContent = r ? relicBy(r.by) : "";
  $("relic-note").textContent = "Edits are written to the file when you save. Ctrl+S saves.";
  $("relic-status").textContent = "";
  relicLang = LANG[(path.includes(".") ? path.split(".").pop() : "").toLowerCase()] || "";
  try {
    $("relic-text").value = (await api("GET", "/api/relics/file?" + new URLSearchParams({ path }))).text;
    $("relic-text").disabled = $("relic-save").disabled = false;
    const first = $("relic-text").value.split("\n")[0];
    if (!relicLang && first.startsWith("#!")) relicLang = /python/.test(first) ? "py" : /node/.test(first) ? "js" : "sh";
  } catch (e) {
    $("relic-text").value = "";
    $("relic-text").disabled = $("relic-save").disabled = true;
    $("relic-status").textContent = e.message;
  }
  $("relic-text").scrollTop = $("relic-text").scrollLeft = 0;
  renderHighlight();
  loadRelicChat(path);
  if (!relicRun) {
    try { $("relic-cmd").value = (await api("GET", "/api/relics/command?" + new URLSearchParams({ path }))).command; }
    catch (e) { $("relic-cmd").value = ""; }
  }
}

// Running: the suggested command (or your own) runs in the workspace; output streams below.
let relicRun = null;
$("relic-run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const command = $("relic-cmd").value.trim();
  if (!command || relicRun) return;
  if (relicDirty) await saveRelic();
  const out = $("relic-out"), status = $("relic-run-status");
  $("relic-out-wrap").hidden = false;
  out.textContent = "";
  status.textContent = `$ ${command}  ·  running…`;
  status.className = "muted grow";
  $("relic-run").disabled = true;
  $("relic-stop").hidden = false;
  relicRun = { id: null };
  try {
    await stream("/api/relics/run", { command }, (ev) => {
      if (ev.type === "start") relicRun.id = ev.id;
      else if (ev.type === "output") {
        const atEnd = out.scrollTop + out.clientHeight >= out.scrollHeight - 4;
        out.textContent += ev.text;
        if (atEnd) out.scrollTop = out.scrollHeight;
      } else if (ev.type === "done") {
        const good = ev.code === 0 && !ev.stopped;
        status.textContent = `$ ${command}  ·  ${ev.stopped || "exit code " + ev.code} · ${ev.seconds} s`;
        status.className = "grow " + (good ? "ok" : "error");
        if (!out.textContent) out.textContent = "(no output)";
      }
    });
  } catch (err) { status.textContent = `$ ${command}  ·  ${err.message}`; status.className = "grow error"; }
  relicRun = null;
  $("relic-run").disabled = false;
  $("relic-stop").hidden = true;
  loadRelics();
});
$("relic-stop").addEventListener("click", async () => {
  if (relicRun && relicRun.id) {
    try { await api("POST", "/api/relics/stop", { id: relicRun.id }); } catch (e) { toast(e.message, true); }
  }
});
$("relic-out-clear").addEventListener("click", () => { $("relic-out").textContent = ""; $("relic-out-wrap").hidden = true; });
async function saveRelic() {
  if (!relicPath || $("relic-save").disabled) return;
  try {
    relics = (await api("PUT", "/api/relics/file", { path: relicPath, text: $("relic-text").value })).relics;
    relicDirty = false;
    $("relic-status").textContent = "saved";
    renderRelics();
  } catch (e) { toast(e.message, true); }
}
async function forgetRelic(path) {
  if (!confirm(`Forget ${path}? It leaves this list; the file stays.`)) return;
  try {
    relics = (await api("DELETE", "/api/relics/file?" + new URLSearchParams({ path }))).relics;
    if (path === relicPath) {
      relicPath = null;
      relicDirty = false;
      $("relic-detail").hidden = true;
      $("relic-empty").hidden = false;
    }
    renderRelics();
  } catch (e) { toast(e.message, true); }
}
$("relic-text").addEventListener("input", () => { relicDirty = true; $("relic-status").textContent = "unsaved"; queueHighlight(); });

// Asking about a relic: its own chat. Each message carries the selected lines, or the
// whole file, as it is in the editor; Apply puts a code block from the answer back there.
let relicChat = null, relicPending = null, relicAsking = false;
let relicConvoOpen = true;
try { relicConvoOpen = localStorage.getItem("relic-convo") !== "closed"; } catch (e) { /* no storage */ }
const RELIC_LARGE = 24000;               // characters: past this, suggest selecting lines
function lineAt(text, offset) { let n = 1; for (let i = 0; i < offset; i++) if (text.charCodeAt(i) === 10) n++; return n; }
// The selected lines [first, last] (from 1), or null when nothing is selected.
function relicSelection() {
  const t = $("relic-text"), a = t.selectionStart, b = t.selectionEnd;
  if (a === b) return null;
  const v = t.value;
  const first = lineAt(v, a);
  let last = lineAt(v, b);
  if (last > first && v[b - 1] === "\n") last--;       // a selection ending at a line start does not take that line
  return [first, last];
}
// Sections kept with "+ section", and the selection: a message carries all of them
// (in order, overlaps merged), or the whole file when there are none.
let relicScope = null, relicSections = [], relicSectionsFor = null;
const lineSpan = (x) => `${x[0]}–${x[1]}`;
function mergeSections(list) {
  const out = [];
  for (const [a, b] of [...list].sort((x, y) => x[0] - y[0] || x[1] - y[1])) {
    if (out.length && a <= out[out.length - 1][1]) out[out.length - 1][1] = Math.max(out[out.length - 1][1], b);
    else out.push([a, b]);
  }
  return out;
}
function relicParts() { return mergeSections(relicSections.concat(relicScope ? [relicScope] : [])); }
function renderRelicScope() {
  const v = $("relic-text").value, lines = v.split("\n").length, label = $("relic-scope");
  if (document.activeElement === $("relic-text") || !$("relic-ask").matches(":focus")) relicScope = relicSelection();
  const parts = relicParts();
  label.classList.toggle("relic-scope-warn", !parts.length && v.length > RELIC_LARGE);
  label.textContent = parts.length ? `with lines ${parts.map(lineSpan).join(", ")}` :
    `with the whole file (${lines} lines)` + (v.length > RELIC_LARGE ? ": large for a small model; select lines to send less" : "");
  $("relic-add-section").disabled = !relicScope;
  const chips = $("relic-sections");
  chips.hidden = !relicSections.length;
  chips.replaceChildren(...relicSections.map((x, i) => el("span", { class: "file-chip" },
    el("label", { text: `L${lineSpan(x)}`, title: "select these lines in the editor",
                  onclick: () => selectLines(x) }),
    el("button", { class: "small link", text: "×", title: "drop this section",
                   onclick: () => { relicSections.splice(i, 1); renderRelicScope(); } }))),
    relicSections.length > 1 ? el("button", { class: "small link", text: "clear",
      onclick: () => { relicSections = []; renderRelicScope(); } }) : "");
}
function lineStarts(v) {
  const starts = [0];
  for (let i = 0; i < v.length; i++) if (v.charCodeAt(i) === 10) starts.push(i + 1);
  return starts;
}
function selectLines([a, b]) {
  const t = $("relic-text"), starts = lineStarts(t.value);
  if (b > starts.length) return;
  t.focus();
  t.setSelectionRange(starts[a - 1], b < starts.length ? starts[b] - 1 : t.value.length);
  renderRelicScope();
}
$("relic-add-section").addEventListener("click", () => {
  const sel = relicSelection();
  if (!sel) { toast("select lines in the editor first", true); return; }
  relicSections = mergeSections(relicSections.concat([sel]));
  const t = $("relic-text");
  t.focus();
  t.setSelectionRange(t.selectionEnd, t.selectionEnd);        // kept: select the next section
  renderRelicScope();
});
for (const ev of ["select", "keyup", "mouseup", "input", "focus"]) $("relic-text").addEventListener(ev, renderRelicScope);

async function loadRelicChat(path) {
  relicPending = null;
  if (path !== relicSectionsFor) { relicSections = []; relicSectionsFor = path; }
  try {
    const r = await api("GET", "/api/relics/chat?" + new URLSearchParams({ path }));
    if (path !== relicPath) return;
    relicChat = r.chat;
  } catch (e) { relicChat = null; }
  renderRelicConvo();
  renderRelicScope();
}
// The sections a reply's code blocks replace: those its question carried ([[a, b], …]),
// or null (the whole file). Applying a block shifts the sections after it (kept per question).
const shifted = {};
function replySections(m) {
  const asked = relicChat.messages.filter((x) => x.thread === 0 && x.role === "user" && x.num < m.num).pop();
  if (!asked || !asked.meta.relic) return null;
  const key = `${relicChat.id}:${asked.num}`;
  if (!shifted[key]) {
    const secs = asked.meta.sections || (asked.meta.lines ? [asked.meta.lines] : null);
    if (!secs) return null;
    shifted[key] = secs.map((x) => [...x]);
  }
  return shifted[key];
}
function sectionsLabel(meta) {
  const secs = meta.sections || (meta.lines ? [meta.lines] : null);
  return secs ? secs.map((x) => "L" + lineSpan(x)).join(", ") : "whole file";
}
// Which section a code block is for: the "Lines a-b:" label just before it, else its place.
function blockTarget(text, blocks, i, secs) {
  const before = text.slice(i ? blocks[i - 1].end : 0, blocks[i].start);
  const m = [...before.matchAll(/lines?\s+(\d+)\s*(?:-|–|to)\s*(\d+)/gi)].pop();
  if (m) {
    const k = secs.findIndex((x) => x[0] === Number(m[1]) && x[1] === Number(m[2]));
    if (k >= 0) return k;
  }
  return Math.min(i, secs.length - 1);
}
function relicCard(m) {
  const head = el("div", { class: "cmsg-head" }, el("span", { class: "cmsg-num", text: `#${m.num}` }),
    el("span", { class: "who", text: who(m) }));
  if (m.role === "user") {
    if (m.meta.relic) head.append(el("span", { class: "scope-chip", text: sectionsLabel(m.meta) }));
    return el("div", { class: "cmsg user" }, head, userText(m.text));
  }
  const card = el("div", { class: `cmsg ${m.role} ${m.kind}` }, head, el("div", { class: "cmsg-text", text: m.text }));
  if (m.role === "assistant") {
    const blocks = codeBlocks(m.text), secs = replySections(m);
    if (blocks.length) card.append(el("div", { class: "apply-row" }, ...blocks.map((b, i) => applyControl(m, blocks, i, secs))));
  }
  return card;
}
function renderRelicConvo() {
  const msgs = relicChat ? relicChat.messages.filter((m) => m.thread === 0) : [];
  const box = $("relic-convo"), list = $("relic-convo-list");
  box.hidden = !msgs.length && !relicPending;
  $("relic-convo-toggle").textContent = `${relicConvoOpen ? "▾" : "▸"} Conversation (${msgs.length})` +
    (relicChat && !relicChat.keep_context ? " · each message on its own (Settings → Context)" : "");
  list.hidden = !relicConvoOpen;
  $("relic-open-chat").hidden = !relicChat;
  list.replaceChildren(...msgs.map(relicCard));
  if (relicPending) list.append(el("div", { class: "cmsg assistant pending" },
    el("div", { class: "cmsg-head" }, el("span", { class: "who", text: "model" })),
    el("div", { class: "cmsg-text", text: relicPending.text || "…" })));
  list.scrollTop = list.scrollHeight;
}
$("relic-convo-toggle").addEventListener("click", () => {
  relicConvoOpen = !relicConvoOpen;
  try { localStorage.setItem("relic-convo", relicConvoOpen ? "open" : "closed"); } catch (e) { /* no storage */ }
  renderRelicConvo();
});
$("relic-open-chat").addEventListener("click", (e) => {
  e.preventDefault();
  if (!relicChat) return;
  show("chat");
  openChat(relicChat.id);
});
// One code block's Apply: to the whole file, its section, or (several sections) a section picked.
function applyControl(m, blocks, i, secs) {
  const name = blocks.length > 1 ? `Apply block ${i + 1}` : "Apply";
  const note = "in the editor with this code block (not saved; Ctrl+Z undoes it)";
  if (!secs) return el("button", { class: "small secondary", text: name, title: `replace the whole file ${note}`,
                                   onclick: () => applyBlock(blocks[i].body, null) });
  if (secs.length === 1) return el("button", { class: "small secondary", text: name, title: `replace lines ${lineSpan(secs[0])} ${note}`,
                                               onclick: () => applyBlock(blocks[i].body, secs, 0) });
  const pick = el("select", { class: "small", title: "the section this block replaces" },
    ...secs.map((x, k) => el("option", { value: String(k), text: `lines ${lineSpan(x)}` })));
  pick.value = String(blockTarget(m.text, blocks, i, secs));
  return el("span", { class: "apply-pick" },
    el("button", { class: "small secondary", text: name, title: `replace the section picked ${note}`,
                   onclick: () => { applyBlock(blocks[i].body, secs, Number(pick.value)); renderRelicConvo(); } }), pick);
}
// Replace section k of `secs` (or everything) in the editor, as typing would: Ctrl+Z
// undoes it. The sections after it, and the kept ones, move with the lines.
function applyBlock(body, secs, k) {
  const t = $("relic-text"), v = t.value;
  let code = body.replace(/\n$/, ""), from = 0, to = v.length;
  if (secs) {
    const [a, b] = secs[k], starts = lineStarts(v);
    if (b > starts.length) { toast(`the editor has only ${starts.length} lines now`, true); return; }
    from = starts[a - 1];
    to = b < starts.length ? starts[b] - 1 : v.length;
    const delta = code.split("\n").length - (b - a + 1);
    const move = (x) => { if (x[0] > b) { x[0] += delta; x[1] += delta; } };
    for (const x of relicSections) { if (x[0] === a && x[1] === b) x[1] += delta; else move(x); }
    secs.forEach((x, j) => { if (j === k) x[1] += delta; else move(x); });
  } else if (v.endsWith("\n")) code += "\n";
  t.focus();
  t.setSelectionRange(from, to);
  if (!document.execCommand("insertText", false, code)) t.setRangeText(code, from, to, "end");
  t.setSelectionRange(from, from + code.length);
  relicDirty = true;
  $("relic-status").textContent = "unsaved: applied from the answer";
  renderHighlight();
  renderRelicScope();
}
$("relic-ask-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("relic-ask").value.trim(), path = relicPath;
  if (!text || !path || relicAsking) return;
  if (running) { toast("the model is busy; wait for it or press Stop", true); return; }
  const parts = relicParts();
  const body = { path, text, content: $("relic-text").value };
  if (parts.length) body.sections = parts;
  $("relic-ask").value = "";
  relicAsking = true;
  $("relic-ask-send").disabled = true;
  live = { id: null, kind: "chat", target: relicChat ? relicChat.id : "" };
  setRunning(true);
  setBusy("answering about a relic");
  relicPending = { text: "" };
  renderRelicConvo();
  const here = () => relicPath === path;
  const error = await followJob((cb) => stream("/api/relics/chat", body, cb), (ev) => {
    if (!here()) return;
    if (ev.type === "message") {
      if (!relicChat) relicChat = { id: "", messages: [], keep_context: false };
      relicChat.messages = relicChat.messages.filter((m) => m.num !== ev.message.num).concat([ev.message]);
      if (ev.message.role !== "user") relicPending = null;
      renderRelicConvo();
    } else if (ev.type === "text") {
      if (!relicPending) relicPending = { text: "" };
      relicPending.text += ev.text;
      const node = $("relic-convo-list").querySelector(".pending .cmsg-text");
      if (node) { node.textContent = relicPending.text; $("relic-convo-list").scrollTop = $("relic-convo-list").scrollHeight; }
      else renderRelicConvo();
    } else if (ev.type === "done") { relicChat = ev.chat; renderState(ev.state); }
    else if (ev.type === "stopped") renderState(ev.state);
  });
  relicPending = null;
  live = null;
  relicAsking = false;
  $("relic-ask-send").disabled = false;
  setRunning(false);
  if (error) { toast(error, true); if (here() && !$("relic-ask").value) $("relic-ask").value = text; }
  if (here()) await loadRelicChat(path);
  refreshState();
});
$("relic-ask").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("relic-ask-form").requestSubmit(); }
});
$("relic-text").addEventListener("keydown", (e) => {
  if (e.key === "s" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); saveRelic(); }
});
$("relic-save").addEventListener("click", saveRelic);
$("relic-revert").addEventListener("click", () => {
  if (relicPath && (!relicDirty || confirm("Discard unsaved changes?"))) openRelic(relicPath, true);
});
$("relic-forget").addEventListener("click", () => { if (relicPath) forgetRelic(relicPath); });

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
loaders.models = () => { loadModels(); loadSpeech(); };

// ================================================================ speech
// The Speech card (Models tab): engines, settings, and speech models to pull.
let speechState = null;
async function loadSpeech() {
  try { renderSpeech(await api("GET", "/api/speech")); } catch (e) { toast(e.message, true); }
}
function renderSpeech(st) {
  speechState = st;
  const s = st.settings;
  $("speech-where").textContent = "· " + st.dir;
  const pulled = (engine) => st.local.filter((i) => i.engine === engine).map((i) => i.name.split(":")[1]);
  const fill = (sel, names, current) => {
    const opts = [...new Set([...names, current].filter(Boolean))];
    sel.replaceChildren(...opts.map((n) => el("option", { value: n, text: n + (names.includes(n) ? "" : " (not pulled)") })));
    sel.value = current;
  };
  $("speech-stt").value = s.stt; $("speech-tts").value = s.tts;
  fill($("speech-whisper"), pulled("whisper"), s.whisper_model);
  fill($("speech-vosk"), pulled("vosk"), s.vosk_model);
  fill($("speech-piper"), pulled("piper"), s.piper_voice);
  $("speech-language").value = s.language || "";
  $("speech-bark").value = s.bark_voice || "";
  $("speech-ready").textContent = `Speech to text: ${st.stt_ready || "ready"} · Text to speech: ${st.tts_ready || "ready"}`;
  $("speech-engines").replaceChildren(...Object.entries(st.engines).map(([name, e]) => el("tr", {},
    el("td", { text: name }), el("td", { text: e.kind.toUpperCase() }),
    el("td", { class: e.installed ? "" : "muted", text: e.installed ? "installed" : `not installed: pip install ${e.pip}` }))));
  $("speech-models").replaceChildren(
    ...st.local.map((i) => el("tr", {}, el("td", {}, el("strong", { text: i.name })), el("td", { text: `${i.size_mb} MB` }),
      el("td", { class: "muted", text: "pulled" }),
      el("td", {}, el("button", { class: "small link", text: "delete", onclick: () => deleteSpeech(i.name) })))),
    ...st.available.map((i) => el("tr", {}, el("td", { text: i.name }), el("td", { text: `${i.size_mb} MB` }),
      el("td", { class: "muted", text: i.note }),
      el("td", {}, el("button", { class: "small secondary", text: "Pull", onclick: () => pullSpeech(i.name) })))));
  for (const b of document.querySelectorAll("button.mic")) b.hidden = s.stt === "none";
}
async function setSpeech(key, value) {
  try {
    const r = await api("POST", "/api/config", { values: { ["speech." + key]: value } });
    (r.done.length ? r.done : [`speech.${key} = ${value}`]).forEach((d) => toast(d));
    loadSpeech();
  } catch (e) { toast(e.message, true); }
}
for (const [id, key] of [["speech-stt", "stt"], ["speech-tts", "tts"], ["speech-whisper", "whisper_model"],
                         ["speech-vosk", "vosk_model"], ["speech-piper", "piper_voice"], ["speech-language", "language"],
                         ["speech-bark", "bark_voice"]]) {
  $(id).addEventListener("change", (e) => setSpeech(key, e.target.value.trim()));
}
async function pullSpeech(name) {
  const bar = $("speech-progress");
  bar.hidden = false;
  bar.firstElementChild.style.width = "0";
  bar.lastElementChild.textContent = `${name}: starting…`;
  try {
    await stream("/api/speech/pull", { name }, (ev) => {
      if (ev.type === "progress") {
        bar.firstElementChild.style.width = (ev.total ? (ev.done / ev.total) * 100 : 0) + "%";
        bar.lastElementChild.textContent = `${name}: ${gb(ev.done)}${ev.total ? " / " + gb(ev.total) : ""}`;
      } else if (ev.type === "done") {
        bar.lastElementChild.textContent = `${ev.name}: ${ev.detail}`;
        renderSpeech(ev.speech);
      }
    });
  } catch (e) { bar.lastElementChild.textContent = `${name}: ${e.message}`; }
}
async function deleteSpeech(name) {
  if (!confirm(`Delete ${name}?`)) return;
  try { renderSpeech((await api("DELETE", `/api/speech/models/${encodeURIComponent(name)}`)).speech); toast(`deleted ${name}`); }
  catch (e) { toast(e.message, true); }
}
$("speech-pull-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const name = $("speech-pull-name").value.trim();
  if (name) pullSpeech(name);
});

// Dictation: the mic records 16 kHz mono WAV in the page (no converter needed on the
// machine), sends it to be transcribed, and puts the text into the box.
let recorder = null;
async function startRecording(button) {
  if (!navigator.mediaDevices || !window.isSecureContext) {
    toast("the browser allows the microphone only on https or on this machine (localhost)", true);
    return;
  }
  let media;
  try { media = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } }); }
  catch (e) { toast("no microphone: " + e.message, true); return; }
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const source = ctx.createMediaStreamSource(media);
  const node = ctx.createScriptProcessor(4096, 1, 1);
  const chunks = [];
  node.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  source.connect(node);
  node.connect(ctx.destination);
  recorder = { button, media, ctx, node, source, chunks, rate: ctx.sampleRate };
  button.classList.add("recording");
  button.title = "recording: press again to stop";
}
function wavFrom(chunks, rate, target = 16000) {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const all = new Float32Array(total);
  let off = 0;
  for (const c of chunks) { all.set(c, off); off += c.length; }
  const ratio = rate / target, n = Math.floor(total / ratio);
  const buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
  const str = (o, t) => { for (let i = 0; i < t.length; i++) v.setUint8(o + i, t.charCodeAt(i)); };
  str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVEfmt "); v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, target, true); v.setUint32(28, target * 2, true);
  v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, "data"); v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) {              // average the samples each output sample covers
    const a = Math.floor(i * ratio), b = Math.min(total, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = a; j < b; j++) sum += all[j];
    const x = Math.max(-1, Math.min(1, b > a ? sum / (b - a) : 0));
    v.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}
async function stopRecording() {
  const r = recorder;
  recorder = null;
  r.node.disconnect(); r.source.disconnect();
  for (const t of r.media.getTracks()) t.stop();
  r.ctx.close();
  r.button.classList.remove("recording");
  r.button.classList.add("working");
  r.button.title = "transcribing…";
  try {
    const res = await fetch("/api/speech/transcribe", { method: "POST", headers: { "Content-Type": "application/octet-stream" },
                                                         body: wavFrom(r.chunks, r.rate) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    const box = $(r.button.dataset.for);
    if (data.text) {
      box.value = (box.value.trim() ? box.value.replace(/\s*$/, " ") : "") + data.text;
      box.focus();
      box.dispatchEvent(new Event("input"));
    } else toast("no speech heard");
  } catch (e) { toast("transcription failed: " + e.message, true); }
  r.button.classList.remove("working");
  r.button.title = "speak instead of typing: press, talk, press again";
}
for (const b of document.querySelectorAll("button.mic")) {
  b.addEventListener("click", () => {
    if (recorder && recorder.button === b) stopRecording();
    else if (!recorder && !b.classList.contains("working")) startRecording(b);
  });
}

// Reading answers aloud
let speaking = null;
async function speakText(text, button) {
  if (speaking) { speaking.pause(); speaking = null; if (button) button.textContent = "speak"; return; }
  if (button) button.textContent = "…";
  try {
    const r = await api("POST", "/api/speech/speak", { text });
    speaking = new Audio(`data:${r.type};base64,${r.audio}`);
    if (button) button.textContent = "stop";
    speaking.onended = () => { speaking = null; if (button) button.textContent = "speak"; };
    await speaking.play();
  } catch (e) { toast(e.message, true); speaking = null; if (button) button.textContent = "speak"; }
}
api("GET", "/api/speech").then((st) => { speechState = st; for (const b of document.querySelectorAll("button.mic")) b.hidden = st.settings.stt === "none"; })
  .catch(() => {});
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
  } catch (e) { toast(e.message, true); }
}
async function loadTools() {
  try {
    const t = await api("GET", "/api/tools");
    $("tool-count").textContent = `· ${t.tools.length}`;
    $("tool-warnings").hidden = !t.warnings.length;
    $("tool-warnings").textContent = t.warnings.join("\n");
    $("tool-rows").replaceChildren(...t.tools.map((x) => el("tr", {},
      el("td", {}, el("strong", { class: x.risky ? "risky" : "", text: x.name })),
      el("td", { class: "muted", text: x.risky ? "asks first" : "runs freely" }),
      el("td", { text: x.description }),
      el("td", { class: "params" }, ...Object.entries(x.params).flatMap(([k, v], i) => [i ? el("br") : null,
        el("code", { text: k + ("default" in v ? ` = ${JSON.stringify(v.default)}` : ""), title: `${v.type}: ${v.desc || ""}` })])))));
    if (!t.tools.length) $("tool-rows").append(el("tr", {}, el("td", { colspan: 4, class: "muted", text: "no tools loaded" })));
  } catch (e) { toast(e.message, true); }
}
subTabs("settings-tabs", "spane-", "sh-spane");
async function loadKeys() {
  try {
    const r = await api("GET", "/api/keys");
    $("keys-file").textContent = r.file;
    $("key-rows").replaceChildren(...r.keys.map((k) => {
      const input = el("input", { type: "password", placeholder: k.set ? `set (${k.masked}) · type to replace` : "not set",
                                  autocomplete: "off", spellcheck: "false" });
      const save = async (value) => {
        try { await api("PUT", `/api/keys/${k.name}`, { value }); toast(value ? `${k.name} key saved` : `${k.name} key removed`); loadKeys(); }
        catch (e) { toast(e.message, true); }
      };
      return el("tr", {},
        el("td", {}, el("strong", { text: k.name }), el("br"), el("span", { class: "muted", text: k.about })),
        el("td", {}, input, k.from && k.from.startsWith("$") ? el("div", { class: "muted", text: `from ${k.from}` }) : null),
        el("td", {}, el("button", { class: "small", text: "Save", onclick: () => input.value.trim() && save(input.value.trim()) }),
          k.set && !k.from.startsWith("$") ? el("button", { class: "small link", text: "remove", onclick: () => save("") }) : null));
    }));
  } catch (e) { toast(e.message, true); }
}
async function loadChatDefault() {
  try {
    const r = await api("GET", "/api/chats");
    $("default-context").checked = r.keep_context;
    $("relic-context").checked = r.relic_keep_context;
    $("research-context").checked = r.research_keep_context;
  } catch (e) { toast(e.message, true); }
}
$("research-context").addEventListener("change", async (e) => {
  try {
    const r = await api("PUT", "/api/chats/settings", { research_keep_context: e.target.checked });
    toast(r.research_keep_context ? "research questions see the latest answers" : "research questions see only their excerpts and pins");
    if (research) loadResearch();
  } catch (err) { toast(err.message, true); }
});
$("relic-context").addEventListener("change", async (e) => {
  try {
    const r = await api("PUT", "/api/chats/settings", { relic_keep_context: e.target.checked });
    toast(r.relic_keep_context ? "relic chats keep context" : "relic chats keep no context");
  } catch (err) { toast(err.message, true); }
});
$("default-context").addEventListener("change", async (e) => {
  try {
    const r = await api("PUT", "/api/chats/settings", { keep_context: e.target.checked });
    toast(r.keep_context ? "chats keep context by default" : "chats keep no context by default");
    if (chat) { chat = await api("GET", `/api/chats/${chat.id}`); renderChat(); }
  } catch (err) { toast(err.message, true); }
});
loaders.settings = () => { refreshState(); loadSettings(); loadTools(); loadKeys(); loadChatDefault(); };
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
loadRelics();
show(["chat", "research", "notes", "code", "relics", "models", "settings"].includes(recall("sh-view", "")) ? recall("sh-view", "") : "chat");
showPane(["bibliography", "gemini"].includes(recall("sh-pane", "")) ? recall("sh-pane", "") : "ask");
setInterval(() => { if (!document.hidden && !running) refreshState(); }, 5000);
