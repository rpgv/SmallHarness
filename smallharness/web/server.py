"""The visual interface: a local web server with a JSON API over the harness, and a
single-page app (static/) that uses it.

Security, with no key to type: it listens on 127.0.0.1 by default. Requests whose Host
is not this server are refused (blocks DNS-rebinding pages); a request that carries an
Origin must come from this server's own page; and every request with a body must say it
is JSON (or, for an upload, binary), which a page on another site can only send after a
CORS preflight that this server never approves. So other sites cannot drive the API.

Model work (research answers, plans, loading, switching, pulls) runs one
request at a time; a second one gets 409. Plan runs and expansions work in
the background: their reply follows the run, and if the page goes away the run goes on,
and any page can follow it again (GET /api/jobs/ID/events?since=N). The knowledge base,
notes and settings work at any time, before a model is loaded.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterator
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..agent import Harness
from ..errors import HarnessError
from .app import STATIC, TYPES, ApiError, Job, WebApp, WebEvents
from .routes import BINARY_ROUTES, ROUTES


FOLLOW_PING = 15   # seconds a follower waits for an event before checking the page is there


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    app: WebApp
    allowed_hosts: set[str]
    server_version = f"SmallHarness/{__version__}"

    def log_message(self, fmt, *args):  # quiet: the terminal shows the URL only
        pass

    def _headers(self, status: int, ctype: str, length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; "
                                                    "base-uri 'none'; form-action 'none'")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _json(self, status: int, obj) -> None:
        data = json.dumps(obj, default=str).encode()
        self._headers(status, "application/json", len(data))
        self.wfile.write(data)

    def _refuse(self, method: str, path: str) -> str:
        """Why a request that could change something is refused, or '' (see the module)."""
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
            return "requests from other sites are refused"
        if method != "GET" and int(self.headers.get("Content-Length") or 0):
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            want = "application/octet-stream" if path in BINARY_ROUTES else "application/json"
            if ctype != want:
                return f"send {want}"
        return ""

    def _host_ok(self) -> bool:
        if not self.allowed_hosts:   # listening on every interface: any name for this machine
            return True
        return (self.headers.get("Host") or "").lower() in self.allowed_hosts

    def _static(self) -> None:
        name = urlparse(self.path).path.lstrip("/") or "index.html"
        path = (STATIC / name).resolve()
        if not path.is_relative_to(STATIC.resolve()) or not path.is_file() or path.suffix not in TYPES:
            self._json(404, {"error": "not found"})
            return
        data = path.read_bytes()
        self._headers(200, TYPES[path.suffix], len(data))
        self.wfile.write(data)

    def _handle(self, method: str) -> None:
        if not self._host_ok():
            self._json(403, {"error": "wrong host"})
            return
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            if method == "GET":
                self._static()
            else:
                self._json(404, {"error": "not found"})
            return
        refusal = self._refuse(method, url.path)
        if refusal:
            self._json(403, {"error": refusal})
            return
        for m, pattern, fn in ROUTES:
            match = pattern.match(url.path)
            if m == method and match:
                break
        else:
            self._json(404, {"error": f"no {method} {url.path}"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if url.path in BINARY_ROUTES:
                body = {"data": raw}
            else:
                body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ApiError(400, "the body must be a JSON object")
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            result = fn(self.app, query, body, *match.groups())
        except HarnessError as e:
            self._json(e.status, {"error": str(e)})
            return
        except json.JSONDecodeError:
            self._json(400, {"error": "the body is not JSON"})
            return
        except Exception as e:  # report, do not kill the server
            self._json(500, {"error": f"{type(e).__name__}: {e}"})
            return
        if isinstance(result, Job):
            since = query.get("since", "0")
            self._follow(result, int(since) if since.isdigit() else 0)
        elif callable(result):
            self._stream(result)
        else:
            self._json(200, result)

    def _stream(self, produce: Callable[[WebEvents], Iterator[dict]]) -> None:
        """NDJSON: one event per line, the last one {"type": "done" | "error" | "stopped"}."""
        self._headers(200, "application/x-ndjson")
        self.close_connection = True
        lock = threading.Lock()

        def write(obj: dict) -> None:
            with lock:
                self.wfile.write(json.dumps(obj, default=str).encode() + b"\n")
                self.wfile.flush()
        ev = WebEvents(self.app, write)
        try:
            for item in produce(ev):
                write(item)
        except HarnessError as e:
            self._try(write, {"type": "error", "error": str(e), "status": e.status})
        except KeyboardInterrupt:
            self._try(write, {"type": "stopped", "state": self.app.state()})
        except Exception as e:
            self._try(write, {"type": "error", "error": f"{type(e).__name__}: {e}"})

    def _follow(self, job: Job, since: int) -> None:
        """NDJSON: the job, then its events from number `since`, each with its number
        ('seq'), until it ends. A page that goes away stops following, not the job."""
        self._headers(200, "application/x-ndjson")
        self.close_connection = True

        def line(obj: dict) -> None:
            self.wfile.write(json.dumps(obj, default=str).encode() + b"\n")
            self.wfile.flush()
        try:
            line({"type": "job", **job.info()})
            while True:
                events, finished = job.wait(since, FOLLOW_PING)
                for seq, ev in enumerate(events, since):
                    if ev["type"] == "permit" and ev["id"] not in self.app.pending:
                        continue                  # answered already: do not ask again
                    line({**ev, "seq": seq})
                since += len(events)
                if finished:
                    break
                if not events:
                    line({"type": "ping"})         # finds a page that went away
        except OSError:
            pass

    @staticmethod
    def _try(write, obj) -> None:
        try:
            write(obj)
        except OSError:
            pass

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PUT(self): self._handle("PUT")
    def do_DELETE(self): self._handle("DELETE")


def make_server(h: Harness, host: str = "127.0.0.1", port: int = 8765) -> tuple[ThreadingHTTPServer, str]:
    """The server (not yet serving) and the URL to open."""
    app = WebApp(h)
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    real_port = server.server_address[1]
    names = {host, "localhost", "127.0.0.1"} if host in ("127.0.0.1", "localhost") else {host}
    handler.allowed_hosts = set() if host in ("0.0.0.0", "::", "") else \
        {f"{n}:{real_port}".lower() for n in names}
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    return server, f"http://{shown}:{real_port}/"
