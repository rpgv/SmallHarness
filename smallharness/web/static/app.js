"use strict";
// SmallHarness web interface: Chat, Research, Notes, Planner, Models, Settings, and
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
    ["instances fit", s.ram_managed ? `${s.capacity} (the planner uses up to this many)` : "1 (RAM not managed)"],
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
$("chat-title").addEventListener("change", async () => {
  const title = $("chat-title").value.trim();
  if (!chat || !title) return;
  try { chat = await api("PUT", `/api/chats/${chat.id}`, { title }); loadChats(); } catch (e) { toast(e.message, true); }
});

// ------------------------------------------------------------- showing
const chatLine = (thread) => (chat ? chat.messages.filter((m) => m.thread === thread) : []);
function who(m) {
  if (m.role === "user") return "you";
  if (m.role === "assistant") return { research: "research", plan: "plan", agent: "model + tools" }[m.kind] || "model";
  return { info: "help", error: "error", note: "notes", recall: "recall", plan: "plan run",
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
  if (m.role === "user") return [userText(m.text)];
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
    out.push(el("div", { class: "cmsg-foot" }, el("button", { class: "small link", text: `open plan ${m.meta.plan} in the planner`,
      onclick: () => { show("planner"); openPlan(m.meta.plan); } })));
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
    el("div", { class: "attached-head", text: `Pinned: in every prompt (${pins.length})` }),
    ...pins.map((m) => el("div", { class: "attached-item" },
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

// ================================================================ planner
let plan = null;
async function loadPlans() {
  try {
    const data = await api("GET", "/api/plans");
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
loaders.planner = loadPlans;
async function openPlan(id, keepLog = false) {
  try {
    renderPlan(await api("GET", `/api/plans/${id}`));
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
  try { plan = await api("PUT", `/api/plans/${plan.id}/persistence`, { persistence: Number(e.target.value) }); }
  catch (err) { toast(err.message, true); }
  showPersistence(plan);
});
async function markSteps(steps, done) {
  if (!plan) return;
  try { renderPlan(await api("PUT", `/api/plans/${plan.id}/steps`, { steps, done })); loadPlans(); }
  catch (e) { toast(e.message, true); openPlan(plan.id, true); }
}
$("plan-untick").addEventListener("click", () => {
  if (plan && confirm("Untick every step? The next run does them all again.")) markSteps(null, false);
});
async function deletePlan(id, goal) {
  if (running && plan && plan.id === id) { toast("this plan is running; wait for the run to end", true); return; }
  if (!confirm(`Delete the plan “${goal}”? Files its runs wrote stay.`)) return;
  try {
    await api("DELETE", `/api/plans/${id}`);
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
    const p = await api("POST", "/api/plans", { goal });
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
    const p = planEditing ? await api("PUT", `/api/plans/${planEditing}`, body) : await api("POST", "/api/plans", body);
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
  if (reveal) show("planner");
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
  await startRun("expand", plan.id, `/api/plans/${plan.id}/expand`, body);
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
  const body = { mode: $("run-mode").value, parallel: $("run-parallel").checked, persistence: Number($("run-persist").value) };
  await startRun("run", plan.id, `/api/plans/${plan.id}/run`, body);
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
// The files plans wrote: listed under the tabs, opened and edited here.
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
  try {
    $("relic-text").value = (await api("GET", "/api/relics/file?" + new URLSearchParams({ path }))).text;
    $("relic-text").disabled = $("relic-save").disabled = false;
  } catch (e) {
    $("relic-text").value = "";
    $("relic-text").disabled = $("relic-save").disabled = true;
    $("relic-status").textContent = e.message;
  }
}
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
$("relic-text").addEventListener("input", () => { relicDirty = true; $("relic-status").textContent = "unsaved"; });
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
loaders.settings = () => { refreshState(); loadSettings(); loadTools(); };
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
show(["chat", "research", "notes", "planner", "relics", "models", "settings"].includes(recall("sh-view", "")) ? recall("sh-view", "") : "chat");
showPane(recall("sh-pane", "") === "bibliography" ? "bibliography" : "ask");
setInterval(() => { if (!document.hidden && !running) refreshState(); }, 5000);
