"""What the web API shares between requests: the harness, whether the model is busy,
Stop, permission questions waiting for the page, how events reach the page, and the
run (plan or recipe) working in the background."""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Iterator

from .. import __version__
from ..agent import Events, Harness
from ..errors import HarnessError
from ..planning import Plan, plan_text

STATIC = Path(__file__).parent / "static"


TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
         ".woff2": "font/woff2"}


PERMIT_TIMEOUT = 600   # seconds a risky tool call waits for the user before it is denied


class ApiError(HarnessError):
    """A refusal that belongs to the web API itself (missing field, busy, bad mode).
    Errors from the harness carry their own status (errors.py)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Stopped(KeyboardInterrupt):
    """Raised inside model work when the user pressed Stop or closed the page."""


class Job:
    """A plan run, expansion or recipe run, working in the background so it outlives the
    request that started it. Its events are kept: a page that went away (reloaded, or a
    phone that put the browser to sleep) comes back and follows it from where it was."""

    def __init__(self, kind: str, target: str, label: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind, self.target, self.label = kind, target, label
        self.subject = None                   # the plan or recipe being worked on, live
        self.events: list[dict] = []
        self.finished = False
        self._cond = threading.Condition()

    def push(self, obj: dict) -> None:
        with self._cond:
            self.events.append(obj)
            self._cond.notify_all()

    def end(self) -> None:
        with self._cond:
            self.finished = True
            self._cond.notify_all()

    def wait(self, since: int, timeout: float) -> tuple[list[dict], bool]:
        """The events from number `since` on (waiting up to `timeout` for one), and
        whether the job has finished."""
        with self._cond:
            if since >= len(self.events) and not self.finished:
                self._cond.wait(timeout)
            return self.events[since:], self.finished

    def info(self) -> dict:
        return {"id": self.id, "kind": self.kind, "target": self.target, "label": self.label,
                "finished": self.finished}


class WebEvents(Events):
    """Harness events as NDJSON lines on the open response, or kept in a job. A risky
    tool call is sent to the page and waits for the user's answer (POST /api/permit)."""

    def __init__(self, app: "WebApp", write: Callable[[dict], None]):
        self.app = app
        self._write = write

    def emit(self, obj: dict) -> None:
        if self.app.stop.is_set():
            raise Stopped()
        try:
            self._write(obj)
        except OSError:  # the page went away: stop the work
            self.app.stop.set()
            raise Stopped() from None

    def on_text(self, chunk): self.emit({"type": "text", "text": chunk})
    def on_text_end(self): self.emit({"type": "text_end"})
    def on_stats(self, stats): self.emit({"type": "stats", **stats})
    def on_tool_call(self, name, args): self.emit({"type": "tool_call", "name": name, "args": args})

    def on_tool_result(self, o):
        self.emit({"type": "tool_result", "name": o.name, "ok": o.ok, "denied": o.denied, "text": o.text})

    def on_refresh(self, moved, before):
        self.emit({"type": "notice", "text": f"context at {before.usage:.0%}: moved {moved} turns to the summary"})

    def on_notice(self, msg): self.emit({"type": "notice", "text": msg})

    def permit(self, tool, args) -> bool:
        pid = uuid.uuid4().hex
        waiter = self.app.pending[pid] = [threading.Event(), False]
        try:
            self.emit({"type": "permit", "id": pid, "tool": tool.name, "description": tool.description,
                       "args": args})
            deadline = time.time() + PERMIT_TIMEOUT
            while not waiter[0].wait(0.25):
                if self.app.stop.is_set() or time.time() > deadline:
                    return False
            return bool(waiter[1])
        finally:
            self.app.pending.pop(pid, None)
            try:                              # other pages showing the question close it
                self._write({"type": "permit_end", "id": pid})
            except OSError:
                pass


class WebApp:
    def __init__(self, h: Harness):
        self.h = h
        self._research = None                 # the page's research session (one at a time)
        self.busy = threading.Lock()          # model work, one at a time
        self.stop = threading.Event()
        self.pending: dict[str, list] = {}    # permit id -> [answered, allowed]
        self.plan: Plan | None = None
        self.load_s: float | None = None    # how long the last model load took
        self.working = ""                     # what the model is doing, for /api/state
        self.job: Job | None = None           # the latest background run

    @property
    def research(self):
        from ..research import Research
        if self._research is None:
            self._research = Research(self.h)
        return self._research

    def new_research(self):
        from ..research import Research
        self._research = Research(self.h)
        return self._research

    # ------------------------------------------------------------ helpers
    def model_work(self, label: str):
        """A context manager for model work: one at a time, and Stop can end it."""
        app = self

        class _Work:
            def __enter__(self):
                if not app.busy.acquire(blocking=False):
                    raise ApiError(409, f"the model is busy ({app.working or 'working'}); wait or press Stop")
                app.stop.clear()
                app.working = label
                return self

            def __exit__(self, *exc):
                app.working = ""
                app.busy.release()
                return False
        return _Work()

    def start_job(self, kind: str, target: str, label: str,
                  produce: Callable[[WebEvents, Job], Iterator[dict]]) -> Job:
        """Run `produce` in the background as model work. Busy is refused here, so the
        page gets a real 409; Stop ends the job, but a page going away does not."""
        work = self.model_work(label)
        work.__enter__()
        job = self.job = Job(kind, target, label)
        ev = WebEvents(self, job.push)

        def run() -> None:
            try:
                for item in produce(ev, job):
                    job.push(item)
            except HarnessError as e:
                job.push({"type": "error", "error": str(e), "status": e.status})
            except KeyboardInterrupt:
                job.push({"type": "stopped", "state": dict(self.state(), busy="")})
            except Exception as e:  # report, do not kill the server
                job.push({"type": "error", "error": f"{type(e).__name__}: {e}"})
            finally:
                work.__exit__(None, None, None)
                job.end()
        threading.Thread(target=run, name=f"job-{kind}", daemon=True).start()
        return job

    def live(self, kind: str, target: str):
        """The plan or recipe a running job is working on, or None: it shows steps
        as they run, which the saved copy does not."""
        job = self.job
        if job and not job.finished and job.kind in kind.split("|") and job.target == target:
            return job.subject
        return None

    def check_free(self) -> None:
        """Refuse before a streamed reply starts, so the page gets a real 409. (model_work
        still decides, in case another request starts in between.)"""
        if self.busy.locked():
            raise ApiError(409, f"the model is busy ({self.working or 'working'}); wait or press Stop")

    def ensure_loaded(self, events: WebEvents | None = None) -> None:
        if self.h.pool.instances:
            return
        rep = self.h.pool.report()
        if not rep.fits:
            raise ApiError(400, "the model does not fit in free RAM:\n" + rep.describe())
        if events:
            events.emit({"type": "status", "text": f"loading {self.h.cfg.model_name}"})
        start = time.perf_counter()
        try:
            self.h.start()
        except RuntimeError as e:
            raise ApiError(400, f"cannot start the model: {e}") from None
        self.load_s = round(time.perf_counter() - start, 2)
        if events:
            events.emit({"type": "loaded", "seconds": self.load_s})

    def plan_dict(self, plan: Plan) -> dict:
        tasks = [{"num": t.num, "task": t.task, "tool": t.tool, "after": t.after, "status": t.status,
                  "result": t.result, "detail": t.detail, "depth": t.depth} for t in plan.tasks]
        return {"id": plan.id, "goal": plan.goal, "mode": plan.mode, "tasks": tasks,
                "finished": plan.finished, "text": plan_text(plan)}

    def load_plan(self, plan_id: str) -> Plan:
        return self.h.plans.load(plan_id)

    # -------------------------------------------------------------- state
    def state(self) -> dict:
        h, cfg = self.h, self.h.cfg
        be = h.pool.instances[0] if h.pool.instances else None
        try:
            capacity = h.pool.capacity
        except Exception:
            capacity = 1
        return {
            "version": __version__, "workspace": str(cfg.workspace),
            "model": {"name": cfg.model_name, "backend": cfg["model"]["backend"], "loaded": be is not None,
                      "mode": getattr(be, "mode", "") if be else "", "context_tokens": cfg.context_tokens,
                      "context_note": cfg.context.note},
            "instances": len(h.pool.instances), "capacity": capacity, "ram_managed": h.pool.ram_managed,
            "search": h.search_mode(), "embeddings": bool(h.embedder),
            "plan": self.plan.id if self.plan else None, "notes": len(h.notes.all()),
            "knowledge": {"sources": len(h.project_rag.sources), "chunks": len(h.project_rag)},
            "busy": self.working, "job": self.job.info() if self.job else None,
            "untrusted": cfg.untrusted.describe() if cfg.untrusted else [],
        }
