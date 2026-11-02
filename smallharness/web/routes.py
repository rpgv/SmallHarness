"""The JSON API, one function per route. Errors from the harness carry their own HTTP
status (errors.py); the server turns them into replies."""
from __future__ import annotations

import re
import time
from dataclasses import asdict
from typing import Any, Callable, Iterator

from ..config import DEFAULTS, flatten, is_secret, parse_value
from ..engines import engine
from ..planning import PlanRunner, clamp_permissions, clamp_persistence, create_plan, draft_plan, edit_plan, expand
from .app import ApiError, Job, WebApp, WebEvents

# ------------------------------------------------------------------ routes
Route = Callable[..., Any]
BINARY_ROUTES = {"/api/research/upload", "/api/speech/transcribe"}    # the body is a file, not JSON


ROUTES: list[tuple[str, re.Pattern, Route]] = []


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn))
        return fn
    return deco


def _numbers(body: dict, key: str) -> list[int] | None:
    """An optional list of step or section numbers (None: all of them)."""
    nums = body.get(key)
    if nums is not None and not (isinstance(nums, list) and all(isinstance(n, int) for n in nums)):
        raise ApiError(400, f"{key!r} is a list of numbers")
    return nums


def _need(body: dict, key: str, kind=str):
    v = body.get(key)
    if not isinstance(v, kind) or (kind is str and not v.strip()):
        raise ApiError(400, f"{key!r} is required")
    return v


@route("GET", "/api/state")
def get_state(app: WebApp, q, body):
    return app.state()


@route("POST", "/api/load")
def post_load(app: WebApp, q, body):
    with app.model_work("loading the model"):
        app.ensure_loaded()
    return {**app.state(), "load_s": app.load_s}


@route("POST", "/api/stop")
def post_stop(app: WebApp, q, body):
    app.stop.set()
    for waiter in list(app.pending.values()):
        waiter[0].set()
    return {"ok": True}


@route("POST", "/api/permit")
def post_permit(app: WebApp, q, body):
    waiter = app.pending.get(_need(body, "id"))
    if not waiter:
        raise ApiError(404, "that request is no longer waiting")
    waiter[1] = bool(body.get("allow"))
    waiter[0].set()
    return {"ok": True}


# -------------------------------------------------------------- research
def _research_dict(app: WebApp) -> dict:
    r = app.research
    return dict(r.to_dict(), library=r.library(), search=app.h.search_mode(), embeddings=bool(app.h.embedder))


@route("GET", "/api/research")
def get_research(app: WebApp, q, body):
    return _research_dict(app)


@route("POST", "/api/research/new")
def post_research_new(app: WebApp, q, body):
    """A new research session: no answers, no references, nothing attached."""
    app.new_research()
    return _research_dict(app)


@route("POST", "/api/research/ask")
def post_research_ask(app: WebApp, q, body):
    question = _need(body, "question")
    app.check_free()

    def stream(ev: WebEvents) -> Iterator[dict]:
        with app.model_work("answering"):
            app.ensure_loaded(ev)
            answer = app.research.ask(question, ev)
            yield {"type": "done", "answer": answer.to_dict(), "research": _research_dict(app),
                   "state": app.state()}
    return stream


@route("POST", "/api/research/gemini")
def post_research_gemini(app: WebApp, q, body):
    """Ask Gemini, a larger model; the answer joins the session like any other."""
    answer = app.research.gemini(_need(body, "question"))
    return {"answer": answer.to_dict(), "research": _research_dict(app)}


@route("POST", r"/api/research/answers/(\d+)/save")
def post_research_save(app: WebApp, q, body, num):
    """Put an answer into the knowledge base, as a Markdown file in the library."""
    answer, res = app.research.save(int(num), str(body.get("name") or ""))
    return {"saved": answer.saved, "result": res.describe(), "research": _research_dict(app)}


@route("POST", "/api/research/pubmed")
def post_research_pubmed(app: WebApp, q, body):
    """Open-access articles in PubMed Central for some words."""
    return {"articles": [a.to_dict() for a in app.research.pubmed(_need(body, "query"))]}


@route("POST", "/api/research/doi")
def post_research_doi(app: WebApp, q, body):
    """Articles into the knowledge base by DOI, PMCID or PMID ('ids': one or more)."""
    ids = _need(body, "ids").replace(",", " ").split()

    def stream(ev: WebEvents) -> Iterator[dict]:
        added = []
        for ident in ids:
            ev.emit({"type": "status", "text": f"fetching {ident}"})
            art, source, res = app.research.add_article(ident)
            added.append(dict(art.to_dict(), source=source, result=res.describe()))
        yield {"type": "done", "added": added, "research": _research_dict(app)}
    return stream


@route("POST", "/api/research/find")
def post_research_find(app: WebApp, q, body):
    refs = app.research.find(_need(body, "query"))
    return {"refs": [r.num for r in refs], "research": _research_dict(app)}


@route("POST", "/api/research/files")
def post_research_files(app: WebApp, q, body):
    """Index a file or folder of the workspace (OCR and embeddings may take a while)."""
    path = _need(body, "path")

    def stream(ev: WebEvents) -> Iterator[dict]:
        res = app.research.add(path, progress=lambda label: ev.emit({"type": "status", "text": label}))
        yield {"type": "done", "result": res.describe(), "skipped": res.skipped, "research": _research_dict(app)}
    return stream


@route("POST", "/api/research/upload")
def post_research_upload(app: WebApp, q, body):
    """A file sent from the page (the body is the file itself; ?name= its name)."""
    name = q.get("name", "").strip()
    if not name:
        raise ApiError(400, "?name= is required")
    res = app.research.add_upload(name, body["data"])
    return {"result": res.describe(), "skipped": res.skipped, "research": _research_dict(app)}


@route("POST", "/api/research/remove")
def post_research_remove(app: WebApp, q, body):
    app.research.remove(_need(body, "source"))
    return _research_dict(app)


@route("PUT", "/api/research/scope")
def put_research_scope(app: WebApp, q, body):
    sources = body.get("sources") or []
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise ApiError(400, "'sources' is a list of sources")
    app.research.set_scope(sources)
    return _research_dict(app)


@route("PUT", r"/api/research/answers/(\d+)/attach")
def put_research_attach(app: WebApp, q, body, num):
    """Keep an answer in the context of later questions (CAG), or take it out."""
    (app.research.attach if body.get("attached", True) else app.research.detach)(int(num))
    return _research_dict(app)


# ---------------------------------------------------------------- notes
@route("GET", "/api/notes")
def get_notes(app: WebApp, q, body):
    notes = app.h.notes
    query, tag = q.get("q", "").strip(), q.get("tag", "").strip() or None
    if query:
        items = [dict(n.to_dict(), snippet=" ".join(c.text.split())[:200]) for n, c in notes.search(query, tag, 20)]
    else:
        items = [n.to_dict() for n in reversed(notes.all(tag))]
    return {"notes": items, "tags": notes.tags().most_common()}


@route("POST", "/api/notes")
def post_note(app: WebApp, q, body):
    return app.h.notes.add(_need(body, "text")).to_dict()


@route("GET", r"/api/notes/(\d+)")
def get_note(app: WebApp, q, body, nid):
    return app.h.notes.get(nid).to_dict()


@route("PUT", r"/api/notes/(\d+)")
def put_note(app: WebApp, q, body, nid):
    return app.h.notes.update(nid, _need(body, "text")).to_dict()


@route("DELETE", r"/api/notes/(\d+)")
def delete_note(app: WebApp, q, body, nid):
    return app.h.notes.delete(nid).to_dict()


# ---------------------------------------------------------------- plans
@route("GET", "/api/code")
def get_plans(app: WebApp, q, body):
    out = []
    for plan in app.h.plans.list(50):
        done = sum(1 for t in plan.tasks if t.status == "done")
        out.append({"id": plan.id, "goal": plan.goal, "tasks": len(plan.tasks), "done": done,
                    "finished": plan.finished})
    return {"plans": out, "current": app.plan.id if app.plan else None,
            "specify": app.h.cfg["plan"].get("specify", "fresh")}


@route("GET", r"/api/code/([\w-]+)")
def get_plan(app: WebApp, q, body, pid):
    live = app.live("run|expand", pid)
    if live:
        return app.plan_dict(live)
    return app.plan_dict(app.load_plan(pid))


@route("POST", "/api/code")
def post_plan(app: WebApp, q, body):
    if isinstance(body.get("text"), str):          # drafted by hand: no model
        app.plan, warnings = draft_plan(app.h, str(body.get("goal") or ""), body["text"])
        return dict(app.plan_dict(app.plan), warnings=warnings)
    goal = _need(body, "goal")
    with app.model_work("planning"):
        app.ensure_loaded()
        app.plan, warnings = create_plan(app.h, goal)
    return dict(app.plan_dict(app.plan), warnings=warnings)


@route("PUT", r"/api/code/([\w-]+)")
def put_plan(app: WebApp, q, body, pid):
    text = _need(body, "text")
    _not_running(app, pid)
    plan = app.load_plan(pid)
    warnings = edit_plan(app.h, plan, str(body.get("goal") or ""), text)
    app.plan = plan
    return dict(app.plan_dict(plan), warnings=warnings)


def _not_running(app: WebApp, pid: str) -> None:
    if app.live("run|expand", pid):
        raise ApiError(409, "this plan is running; wait for the run to end")


@route("DELETE", r"/api/code/([\w-]+)")
def delete_plan(app: WebApp, q, body, pid):
    _not_running(app, pid)
    app.h.plans.delete(pid)
    if app.plan and app.plan.id == pid:
        app.plan = None
    return {"deleted": pid}


@route("PUT", r"/api/code/([\w-]+)/persistence")
def put_plan_persistence(app: WebApp, q, body, pid):
    """How many times a failed step is tried again (persistence_max: until it succeeds)."""
    live = app.live("run", pid)
    plan = live or app.load_plan(pid)
    plan.persistence = clamp_persistence(body.get("persistence"))
    if not live:                      # a running plan is saved by its run, after each step
        app.h.plans.save(plan)
    return app.plan_dict(plan)


@route("PUT", r"/api/code/([\w-]+)/permissions")
def put_plan_permissions(app: WebApp, q, body, pid):
    """What a run does without asking: ask (nothing), write (file writes), all (also commands).
    A running plan takes the new mode from its next tool call."""
    live = app.live("run", pid)
    plan = live or app.load_plan(pid)
    plan.permissions = clamp_permissions(body.get("permissions"))
    if not live:
        app.h.plans.save(plan)
    return app.plan_dict(plan)


@route("PUT", r"/api/code/([\w-]+)/steps")
def put_plan_steps(app: WebApp, q, body, pid):
    """Tick steps off by hand, or untick them so the next run does them again
    ('steps' empty: every step)."""
    _not_running(app, pid)
    plan = app.load_plan(pid)
    plan.mark(_numbers(body, "steps"), bool(body.get("done")))
    app.h.plans.save(plan)
    return app.plan_dict(plan)


@route("POST", r"/api/code/([\w-]+)/run")
def post_run(app: WebApp, q, body, pid):
    plan = app.load_plan(pid)
    mode = body.get("mode", "fresh")
    if mode not in ("fresh", "continuous"):
        raise ApiError(400, "mode must be fresh or continuous")
    # Checked before the run starts, so a bad value is an error here, not in the run.
    persistence = None if body.get("persistence") is None else clamp_persistence(body["persistence"])
    permissions = None if body.get("permissions") is None else clamp_permissions(body["permissions"])

    def produce(ev: WebEvents, job: Job) -> Iterator[dict]:
        app.ensure_loaded(ev)
        app.plan = job.subject = plan

        def start(t):
            ev.emit({"type": "task_start", "num": t.num, "task": t.task, "tool": t.tool, "detail": t.detail})

        def end(t):
            ev.emit({"type": "task_end", "num": t.num, "status": t.status, "result": t.result,
                     "plan": app.plan_dict(plan)})

        runner = PlanRunner(app.h, plan, mode, ev, parallel=bool(body.get("parallel")) or None,
                            persistence=persistence, permissions=permissions)
        keep_going = bool(body.get("continue_on_failure"))
        runner.run(start, end, lambda t: keep_going)
        yield {"type": "done", "reason": "finished" if plan.finished else "stopped",
               "plan": app.plan_dict(plan), "state": app.state()}
    return app.start_job("run", pid, "running a plan", produce)


@route("POST", r"/api/code/([\w-]+)/expand")
def post_expand(app: WebApp, q, body, pid):
    """Expansion passes: each makes the chosen steps' details deeper (all steps when
    'steps' is empty), steps in parallel as RAM allows."""
    plan = app.load_plan(pid)
    steps = _numbers(body, "steps") or None
    times = max(1, min(int(body.get("times") or 1), 5))

    def produce(ev: WebEvents, job: Job) -> Iterator[dict]:
        app.ensure_loaded(ev)
        app.plan = job.subject = plan
        for i in range(times):
            ev.emit({"type": "status", "text": f"pass {i + 1} of {times}"})
            expand(app.h, plan, steps, parallel=body.get("parallel", True) is not False, checks=bool(body.get("checks")),
                   on_end=lambda t: ev.emit({"type": "step", "num": t.num, "depth": t.depth, "detail": t.detail}))
        yield {"type": "done", "plan": app.plan_dict(plan), "state": app.state()}
    return app.start_job("expand", pid, "expanding a plan", produce)


@route("GET", r"/api/jobs/(\w+)/events")
def get_job_events(app: WebApp, q, body, jid):
    """Follow a background run, replaying its events from number `since` (0: all)."""
    if not app.job or app.job.id != jid:
        raise ApiError(404, "that run is no longer kept")
    return app.job


# ------------------------------------------------------------------ chat
def _chat_busy(app: WebApp, cid: str) -> None:
    if app.live("chat", cid):
        raise ApiError(409, "the model is answering in this chat; wait for it or press Stop")


@route("GET", "/api/chats")
def get_chats(app: WebApp, q, body):
    from ..chat import COMMANDS
    return {"chats": app.h.chats.list(),
            "commands": [{"name": k, "usage": u, "about": a} for k, (u, a) in COMMANDS.items()],
            "tools": [t.name for t in app.h.tools]}


@route("POST", "/api/chats")
def post_chat(app: WebApp, q, body):
    return app.h.chats.new().to_dict()


@route("GET", r"/api/chats/([\w-]+)")
def get_chat(app: WebApp, q, body, cid):
    return app.h.chats.load(cid).to_dict()


@route("PUT", r"/api/chats/([\w-]+)")
def put_chat(app: WebApp, q, body, cid):
    chat = app.h.chats.load(cid)
    chat.title = _need(body, "title").strip()[:120]
    chat.save()
    return chat.to_dict()


@route("DELETE", r"/api/chats/([\w-]+)")
def delete_chat(app: WebApp, q, body, cid):
    _chat_busy(app, cid)
    app.h.chats.delete(cid)
    return {"deleted": cid}


@route("POST", r"/api/chats/([\w-]+)/send")
def post_chat_send(app: WebApp, q, body, cid):
    """A message (with any command lines) and the reply, as a background job: each new
    message is an event, and the reply streams as text."""
    chat = app.h.chats.load(cid)
    text = _need(body, "text")
    thread = body.get("thread") or 0
    if not isinstance(thread, int):
        raise ApiError(400, "'thread' is a message number")
    if thread:
        chat.get(thread)
    commands, question = chat.check(text)

    def produce(ev: WebEvents, job: Job) -> Iterator[dict]:
        job.subject = chat
        if question or any(n in ("agent", "ask", "plan", "runplan") for n, _ in commands):
            app.ensure_loaded(ev)
        chat.send(text, thread, ev, on_message=lambda m: ev.emit({"type": "message", "message": asdict(m)}))
        yield {"type": "done", "chat": chat.to_dict(), "state": app.state()}
    return app.start_job("chat", cid, "answering in chat", produce)


@route("PUT", r"/api/chats/([\w-]+)/messages/(\d+)/pin")
def put_chat_pin(app: WebApp, q, body, cid, num):
    _chat_busy(app, cid)
    chat = app.h.chats.load(cid)
    chat.pin(int(num), bool(body.get("pinned", True)))
    return chat.to_dict()


@route("POST", r"/api/chats/([\w-]+)/export")
def post_chat_export(app: WebApp, q, body, cid):
    """Save message 'num', or its code block 'block' (from 1), to 'path' (default under
    exports/), kept as a relic; 'force' overwrites an existing file."""
    chat = app.h.chats.load(cid)
    num, block = body.get("num"), body.get("block") or None
    if not isinstance(num, int) or (block is not None and not isinstance(block, int)):
        raise ApiError(400, "'num' (and 'block') are numbers")
    return chat.export(num, block, str(body.get("path") or ""), bool(body.get("force")))


@route("POST", r"/api/chats/([\w-]+)/rewind")
def post_chat_rewind(app: WebApp, q, body, cid):
    """Clear everything after message 'to' (0: all); with 'thread', only that thread's replies."""
    _chat_busy(app, cid)
    chat = app.h.chats.load(cid)
    to, thread = body.get("to"), body.get("thread") or 0
    if not isinstance(to, int) or not isinstance(thread, int):
        raise ApiError(400, "'to' (and 'thread') are message numbers")
    gone = chat.rewind(to, thread)
    return dict(chat.to_dict(), cleared=gone)


# ------------------------------------------------------------------ keys
@route("GET", "/api/keys")
def get_keys(app: WebApp, q, body):
    """Which API keys are set (never the keys themselves)."""
    from .. import keys
    return {"keys": keys.status(), "file": str(keys.keys_file())}


@route("PUT", r"/api/keys/(\w+)")
def put_key(app: WebApp, q, body, name):
    """Save a key in the keys file (outside every project); an empty value removes it."""
    from .. import keys
    keys.put(name, str(body.get("value") or ""))
    return {"keys": keys.status(), "file": str(keys.keys_file())}


# ---------------------------------------------------------------- relics
@route("GET", "/api/relics")
def get_relics(app: WebApp, q, body):
    return {"relics": app.h.relics.all()}


@route("GET", "/api/relics/file")
def get_relic(app: WebApp, q, body):
    path = q.get("path", "")
    return {"path": path, "text": app.h.relics.read(path)}


@route("PUT", "/api/relics/file")
def put_relic(app: WebApp, q, body):
    path = _need(body, "path")
    text = body.get("text")
    if not isinstance(text, str):
        raise ApiError(400, "'text' is required")
    app.h.relics.save(path, text)
    return {"path": path, "relics": app.h.relics.all()}


@route("DELETE", "/api/relics/file")
def delete_relic(app: WebApp, q, body):
    """Take a file off the list; the file itself stays."""
    app.h.relics.forget(q.get("path", ""))
    return {"relics": app.h.relics.all()}


# --------------------------------------------------------------- models
@route("GET", "/api/models")
def get_models(app: WebApp, q, body):
    eng = engine(app.h.cfg)
    lst = eng.listing()
    return {"engine": app.h.cfg["model"]["backend"], "where": lst.where, "error": lst.error,
            "local": [{"name": m.name, "size": m.size, "max_context": m.max_context, "source": m.about,
                       "active": m.active} for m in lst.models],
            "available": [{"name": a.name, "size_gb": a.size_gb, "note": a.note, "gated": a.gated}
                          for a in lst.available]}


@route("POST", "/api/model")
def post_model(app: WebApp, q, body):
    words = [_need(body, "target")]
    if body.get("ctx"):
        words.append(f"ctx={body['ctx']}")
    with app.model_work("switching models"):
        try:
            values, warnings = engine(app.h.cfg).switch_values(words)
            done = app.h.reconfigure(app.h.cfg.with_values(values))
        except (ValueError, KeyError, RuntimeError) as e:
            raise ApiError(400, str(e)) from None
    return {"done": warnings + done, "state": app.state()}


@route("POST", "/api/pull")
def post_pull(app: WebApp, q, body):
    name = _need(body, "name")
    eng = engine(app.h.cfg)
    app.check_free()

    def stream(ev: WebEvents) -> Iterator[dict]:
        last = [0.0]

        def progress(done, total):
            if time.time() - last[0] > 0.25 or done == total:
                last[0] = time.time()
                ev.emit({"type": "progress", "done": done, "total": total})
        with app.model_work(f"downloading {name}"):
            plan = eng.plan_pull(name)
            if plan.wont_run and not body.get("anyway"):
                raise ApiError(400, plan.warning)
            if plan.warning:
                ev.emit({"type": "notice", "text": plan.warning})
            got = eng.pull(plan, progress, force=bool(body.get("force")))
            yield {"type": "done", "name": got.name, "already": got.already, "detail": got.detail,
                   "warning": got.warning}
    return stream


@route("DELETE", r"/api/models/([^/]+)")
def delete_model(app: WebApp, q, body, name):
    from urllib.parse import unquote
    eng = engine(app.h.cfg)
    got = eng.delete(eng.plan_delete(unquote(name), in_use=True))
    return {"deleted": got.items, "freed": got.freed, "note": got.note}


# ---------------------------------------------------------------- speech
@route("GET", "/api/speech")
def get_speech(app: WebApp, q, body):
    from .. import speech
    return speech.status(app.h.cfg)


@route("POST", "/api/speech/transcribe")
def post_speech_transcribe(app: WebApp, q, body):
    """Speech to text: the body is the recording (WAV from the page)."""
    from .. import speech
    return speech.transcribe(app.h.cfg, body["data"], q.get("engine") or None, None, q.get("language") or None)


@route("POST", "/api/speech/speak")
def post_speech_speak(app: WebApp, q, body):
    """Text to speech: the WAV, base64-encoded."""
    import base64

    from .. import speech
    r = speech.speak(app.h.cfg, _need(body, "text"), body.get("engine") or None, body.get("voice") or None)
    return {"audio": base64.b64encode(r["audio"]).decode(), "type": "audio/wav", "engine": r["engine"],
            "voice": r["voice"], "seconds": r["seconds"]}


@route("POST", "/api/speech/pull")
def post_speech_pull(app: WebApp, q, body):
    from .. import speech
    name = _need(body, "name")
    if not speech.parse(name):
        raise ApiError(400, "a speech model: whisper:SIZE, vosk:NAME, piper:VOICE or bark")

    def stream(ev: WebEvents) -> Iterator[dict]:
        last = [0.0]

        def progress(done, total):
            if time.time() - last[0] > 0.25 or done == total:
                last[0] = time.time()
                ev.emit({"type": "progress", "done": done, "total": total})
        item = speech.pull(app.h.cfg, name, progress, force=bool(body.get("force")))
        yield {"type": "done", "name": item.name, "detail": f"{item.size_mb} MB in {item.path}",
               "speech": speech.status(app.h.cfg)}
    return stream


@route("DELETE", r"/api/speech/models/([^/]+)")
def delete_speech_model(app: WebApp, q, body, name):
    from urllib.parse import unquote

    from .. import speech
    item = speech.delete(app.h.cfg, unquote(name))
    return {"deleted": item.name, "speech": speech.status(app.h.cfg)}


# ------------------------------------------------------------- settings
@route("GET", "/api/tools")
def get_tools(app: WebApp, q, body):
    return {"tools": [{"name": t.name, "risky": t.needs_permission, "description": t.description,
                       "params": t.params} for t in app.h.tools], "warnings": app.h.tools.warnings}


@route("GET", "/api/config")
def get_config(app: WebApp, q, body):
    flat, defaults = flatten(app.h.cfg.data), flatten(DEFAULTS)
    rows = [{"key": k, "value": "…hidden…" if is_secret(k) and v else v, "default": defaults.get(k),
             "changed": v != defaults.get(k), "secret": is_secret(k)} for k, v in flat.items()]
    return {"files": [str(f) for f in app.h.cfg.files], "settings": rows}


@route("POST", "/api/config")
def post_config(app: WebApp, q, body):
    values = body.get("values")
    if not isinstance(values, dict) or not values:
        raise ApiError(400, "'values' must map setting names to values")
    parsed = {k: parse_value(v) if isinstance(v, str) else v for k, v in values.items()}
    with app.model_work("applying settings"):
        try:
            done = app.h.reconfigure(app.h.cfg.with_values(parsed))
        except (KeyError, RuntimeError) as e:
            raise ApiError(400, str(e.args[0] if e.args else e)) from None
    return {"done": done, "state": app.state()}
