"""JSON over HTTP, for the model servers and the embedding server: one place that
builds requests, adds the API key, and turns every failure into an HttpError. The
callers only word the error for their own server."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlparse


class HttpError(RuntimeError):
    """`status` is the HTTP status, or None when the server could not be reached;
    `detail` is the server's reply body, or the reason it could not be reached."""

    def __init__(self, status: int | None, detail: str):
        super().__init__(f"{status}: {detail}" if status else detail)
        self.status, self.detail = status, detail


def normalize_base_url(url: str) -> str:
    """OpenAI-style servers: accept http://host:port as well as http://host:port/v1."""
    url = url.rstrip("/")
    return url + "/v1" if urlparse(url).path in ("", "/") else url


def open_json(base: str, path: str, payload: dict | None = None, *, method: str = "",
              api_key: str = "", timeout: float = 60):
    """Send `payload` as JSON (POST, else GET) and return the open response, for reading
    or streaming. Raises HttpError."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers,
                                 method=method or ("POST" if data is not None else "GET"))
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode("utf-8", "replace")) from None
    except (urllib.error.URLError, OSError) as e:   # refused, unknown host, timed out
        raise HttpError(None, str(getattr(e, "reason", e))) from None


def call_json(base: str, path: str, payload: dict | None = None, **kw) -> dict:
    """open_json, and the reply parsed. A reply that is not JSON is an HttpError too."""
    with open_json(base, path, payload, **kw) as r:
        try:
            return json.load(r)
        except ValueError:
            raise HttpError(r.status, "the reply is not JSON") from None
