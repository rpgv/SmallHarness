"""The JSON API, one function per route. Errors from the harness carry their own HTTP
status (errors.py); the server turns them into replies."""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Iterator

from ..config import DEFAULTS, flatten, is_secret, parse_value
from ..engines import engine
from ..planning import PlanRunner, create_plan, draft_plan, edit_plan, expand
from ..recipe import (
    RecipeRunner,
    list_recipes,
    load_recipe,
    load_saved,
    recipes_dir,
    save_recipe,
)
from .app import ApiError, WebApp, WebEvents

# ------------------------------------------------------------------ routes
Route = Callable[..., Any]
BINARY_ROUTES = {"/api/research/upload"}    # the body is a file, not JSON


ROUTES: list[tuple[str, re.Pattern, Route]] = []


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn))
        return fn
    return deco


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
@route("GET", "/api/plans")
def get_plans(app: WebApp, q, body):
    out = []
    for plan in app.h.plans.list(50):
        done = sum(1 for t in plan.tasks if t.status == "done")
        out.append({"id": plan.id, "goal": plan.goal, "tasks": len(plan.tasks), "done": done,
                    "finished": plan.finished})
    return {"plans": out, "current": app.plan.id if app.plan else None,
            "specify": app.h.cfg["plan"].get("specify", "fresh")}


@route("GET", r"/api/plans/([\w-]+)")
def get_plan(app: WebApp, q, body, pid):
    return app.plan_dict(app.load_plan(pid))


@route("POST", "/api/plans")
def post_plan(app: WebApp, q, body):
    if isinstance(body.get("text"), str):          # drafted by hand: no model
        app.plan, warnings = draft_plan(app.h, str(body.get("goal") or ""), body["text"])
        return dict(app.plan_dict(app.plan), warnings=warnings)
    goal = _need(body, "goal")
    with app.model_work("planning"):
        app.ensure_loaded()
        app.plan, warnings = create_plan(app.h, goal)
    return dict(app.plan_dict(app.plan), warnings=warnings)


@route("PUT", r"/api/plans/([\w-]+)")
def put_plan(app: WebApp, q, body, pid):
    text = _need(body, "text")
    if app.working and app.plan and app.plan.id == pid:
        raise ApiError(409, "this plan is running; edit it when the run ends")
    plan = app.load_plan(pid)
    warnings = edit_plan(app.h, plan, str(body.get("goal") or ""), text)
    app.plan = plan
    return dict(app.plan_dict(plan), warnings=warnings)


@route("POST", r"/api/plans/([\w-]+)/run")
def post_run(app: WebApp, q, body, pid):
    plan = app.load_plan(pid)
    mode = body.get("mode", "fresh")
    if mode not in ("fresh", "continuous"):
        raise ApiError(400, "mode must be fresh or continuous")
    app.check_free()

    def stream(ev: WebEvents) -> Iterator[dict]:
        with app.model_work("running a plan"):
            app.ensure_loaded(ev)
            app.plan = plan

            def start(t):
                ev.emit({"type": "task_start", "num": t.num, "task": t.task, "tool": t.tool, "detail": t.detail})

            def end(t):
                ev.emit({"type": "task_end", "num": t.num, "status": t.status, "result": t.result,
                         "plan": app.plan_dict(plan)})

            runner = PlanRunner(app.h, plan, mode, ev, parallel=bool(body.get("parallel")) or None)
            keep_going = bool(body.get("continue_on_failure"))
            runner.run(start, end, lambda t: keep_going)
            yield {"type": "done", "reason": "finished" if plan.finished else "stopped",
                   "plan": app.plan_dict(plan), "state": app.state()}
    return stream


@route("POST", r"/api/plans/([\w-]+)/expand")
def post_expand(app: WebApp, q, body, pid):
    """Expansion passes: each makes the chosen steps' details deeper (all steps when
    'steps' is empty), steps in parallel as RAM allows."""
    plan = app.load_plan(pid)
    steps = body.get("steps") or None
    if steps is not None and not (isinstance(steps, list) and all(isinstance(n, int) for n in steps)):
        raise ApiError(400, "'steps' is a list of step numbers")
    times = max(1, min(int(body.get("times") or 1), 5))
    app.check_free()

    def stream(ev: WebEvents) -> Iterator[dict]:
        with app.model_work("expanding a plan"):
            app.ensure_loaded(ev)
            app.plan = plan
            for i in range(times):
                ev.emit({"type": "status", "text": f"pass {i + 1} of {times}"})
                expand(app.h, plan, steps, parallel=body.get("parallel", True) is not False,
                       on_end=lambda t: ev.emit({"type": "step", "num": t.num, "depth": t.depth, "detail": t.detail}))
            yield {"type": "done", "plan": app.plan_dict(plan), "state": app.state()}
    return stream


# --------------------------------------------------------------- recipes
def _recipe_dict(recipe) -> dict:
    from ..snippets import language_key, locate
    found = locate(recipe.target) if recipe.mode == "code" else None
    return dict(recipe.to_dict(), text=recipe.path.read_text(encoding="utf-8"),
                library=language_key(found[0]) if found else None)


@route("GET", "/api/recipes")
def get_recipes(app: WebApp, q, body):
    out = []
    for name in list_recipes(app.h.cfg):
        try:
            r = load_recipe(app.h.cfg, recipes_dir(app.h.cfg) / f"{name}.md")
        except ValueError as e:
            out.append({"name": name, "target": "", "sections": 0, "done": 0, "error": str(e)})
            continue
        out.append({"name": name, "target": r.target, "mode": r.mode, "sections": len(r.sections),
                    "done": r.written()})
    return {"recipes": out}


@route("GET", r"/api/recipes/([\w-]+)")
def get_recipe(app: WebApp, q, body, name):
    return _recipe_dict(load_saved(app.h.cfg, name))


@route("PUT", r"/api/recipes/([\w-]+)")
def put_recipe(app: WebApp, q, body, name):
    text = _need(body, "text")
    save_recipe(app.h.cfg, name, text, str(body.get("target") or "").strip(), str(body.get("mode") or ""))
    return _recipe_dict(load_saved(app.h.cfg, name))


@route("DELETE", r"/api/recipes/([\w-]+)")
def delete_recipe(app: WebApp, q, body, name):
    r = load_saved(app.h.cfg, name)
    r.path.unlink()
    if r.cache:
        r.cache.unlink(missing_ok=True)
    return {"deleted": name}


@route("POST", r"/api/recipes/([\w-]+)/run")
def post_recipe_run(app: WebApp, q, body, name):
    recipe = load_saved(app.h.cfg, name)
    only = body.get("only") or None
    if only is not None and not (isinstance(only, list) and all(isinstance(n, int) for n in only)):
        raise ApiError(400, "'only' is a list of section numbers")
    runner = RecipeRunner(app.h, recipe, only=only, everything=bool(body.get("all")))
    app.check_free()

    def stream(ev: WebEvents) -> Iterator[dict]:
        with app.model_work("writing a recipe"):
            app.ensure_loaded(ev)
            runner.events = ev
            res = runner.run(on_start=lambda s: ev.emit({"type": "section_start", "num": s.num, "text": s.text}),
                             on_end=lambda s: ev.emit({"type": "section_end", "num": s.num, "status": s.status,
                                                       "note": s.note, "code": s.code}),
                             parallel=bool(body.get("parallel")))
            yield {"type": "done", "reason": res.reason, "written": res.written, "failed": res.failed,
                   "recipe": _recipe_dict(recipe), "state": app.state()}
    return stream


# ---------------------------------------------------------- code library
def _library(app: WebApp, lang: str):
    from ..snippets import open_library
    lib = open_library(app.h.cfg, lang, app.h.embedder)
    if lib is None:
        raise ApiError(404, f"no code library for {lang!r}")
    return lib


@route("GET", "/api/snippets")
def get_libraries(app: WebApp, q, body):
    from ..snippets import libraries
    return {"languages": [{"key": k, "snippets": len(_library(app, k).all())} for k in libraries(app.h.cfg)]}


@route("GET", r"/api/snippets/(\w+)")
def get_snippets(app: WebApp, q, body, lang):
    lib = _library(app, lang)
    query = q.get("q", "").strip()
    found = lib.search(query, 20) if query else lib.all()
    return {"language": lib.language, "snippets": [s.to_dict() for s in found]}


@route("GET", r"/api/snippets/(\w+)/([\w-]+)")
def get_snippet(app: WebApp, q, body, lang, sid):
    lib = _library(app, lang)
    return lib.get(sid).to_dict(lib.code(sid))


@route("DELETE", r"/api/snippets/(\w+)/([\w-]+)")
def delete_snippet(app: WebApp, q, body, lang, sid):
    _library(app, lang).remove(sid)
    return {"deleted": sid}


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
