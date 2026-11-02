"""Gemini access: ask a model much larger than the local one, for what it knows. Only a
command runs it (/gemini), never the local model on its own.

- Only the question is sent: no files, no conversation, no notes.
- By default there is no Google Search: the answer is the model's own knowledge, as of
  its training. That works on a free API key. With [gemini].search = true the answer
  is grounded in Google Search and lists its pages, which needs billing on the key's
  project (free-tier projects refuse every grounded call with a quota error).
- If the model is overloaded or out of free quota, the next of [gemini].fallback is
  tried, so a busy model does not end the question.

The key comes from keys.py (GEMINI_API_KEY, or the keys file) and is sent in a header,
not in the URL."""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import keys
from .errors import HarnessError, Invalid

API = "https://generativelanguage.googleapis.com/v1beta"
SYSTEM = ("You are a knowledgeable expert consulted by a smaller assistant. Answer accurately and concisely, "
          "with the key facts first. Say plainly when you are unsure, when experts disagree, or when the answer "
          "may have changed since your training.")
SYSTEM_SEARCH = ("Answer the question from current web sources. Be concise and factual; say when sources "
                 "disagree or when you are unsure.")
_RETRY = (429, 500, 503, 504)          # the next model may answer


class GeminiError(HarnessError):
    """Gemini failed or refused."""
    status = 502


@dataclass
class GeminiAnswer:
    question: str
    text: str
    model: str = ""
    sources: list[dict] = field(default_factory=list)      # with search: {"title", "url"}
    queries: list[str] = field(default_factory=list)       # with search: what it searched for
    notes: list[str] = field(default_factory=list)         # models that did not answer, and why

    @property
    def searched(self) -> bool:
        return bool(self.sources or self.queries)

    def label(self) -> str:
        return f"{self.model}" + (" with Google Search" if self.searched else ", from its own knowledge")

    def markdown(self) -> str:
        """The answer as a document, for the knowledge base or a file."""
        out = [f"# {self.question}", "", self.text.strip(), ""]
        if self.sources:
            out += ["## Sources", ""] + [f"{i}. {s['title']} - {s['url']}" for i, s in enumerate(self.sources, 1)]
        out += ["", f"_Answered by {self.label()}._"]
        return "\n".join(out) + "\n"


def ask(question: str, cfg: dict, opener=None) -> GeminiAnswer:
    """Ask Gemini. `cfg` is the [gemini] settings."""
    question = question.strip()
    if not question:
        raise Invalid("what should Gemini be asked?")
    key = keys.get("gemini")
    if not key:
        raise HarnessError("no Gemini API key: set GEMINI_API_KEY, or save one with "
                           "`smallharness key gemini` (or Settings → API keys on the web)")
    search = bool(cfg.get("search", False))
    models = [m for m in [cfg.get("model") or "gemini-flash-latest", *cfg.get("fallback", [])] if m]
    models = list(dict.fromkeys(models))
    notes: list[str] = []
    for i, model in enumerate(models):
        last = i == len(models) - 1
        try:
            data = _call(model, question, search, key, float(cfg.get("timeout", 60)), opener)
        except _Retry as e:
            notes.append(f"{model}: {e.reason}")
            if last:
                raise GeminiError(e.message + (f" (also tried: {'; '.join(notes[:-1])})" if len(notes) > 1 else "")) from None
            continue
        answer = parse(question, data, model)
        answer.notes = notes
        return answer
    raise GeminiError("no Gemini model to ask: set [gemini].model")


class _Retry(Exception):
    def __init__(self, reason: str, message: str):
        self.reason, self.message = reason, message


def _call(model: str, question: str, search: bool, key: str, timeout: float, opener) -> dict:
    body = {"systemInstruction": {"parts": [{"text": SYSTEM_SEARCH if search else SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": question}]}]}
    if search:
        body["tools"] = [{"google_search": {}}]
    req = urllib.request.Request(f"{API}/models/{model}:generateContent", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key}, method="POST")
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        message = _http_message(e.code, e.read().decode("utf-8", "replace"), model, search)
        if e.code in _RETRY:
            raise _Retry({429: "out of quota", 503: "overloaded"}.get(e.code, f"error {e.code}"), message) from None
        raise GeminiError(message) from None
    except (TimeoutError, socket.timeout):
        raise _Retry("timed out", f"Gemini ({model}) did not answer within {timeout:.0f} s") from None
    except (urllib.error.URLError, OSError) as e:
        raise GeminiError(f"Gemini could not be reached: {getattr(e, 'reason', e)}") from None
    except ValueError:
        raise GeminiError("Gemini's reply is not JSON") from None


def parse(question: str, data: dict, model: str = "") -> GeminiAnswer:
    cands = data.get("candidates") or []
    if not cands:
        block = (data.get("promptFeedback") or {}).get("blockReason")
        raise GeminiError(f"Gemini gave no answer{f' (blocked: {block})' if block else ''}")
    c = cands[0]
    text = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts", []) if not p.get("thought")).strip()
    if not text:
        raise GeminiError(f"Gemini gave an empty answer ({c.get('finishReason', 'no reason given')})")
    meta = c.get("groundingMetadata") or {}
    sources, seen = [], set()
    for chunk in meta.get("groundingChunks", []):
        web = chunk.get("web") or {}
        if str(web.get("uri", "")).startswith(("https://", "http://")) and web["uri"] not in seen:
            seen.add(web["uri"])
            sources.append({"title": web.get("title") or web["uri"], "url": web["uri"]})
    return GeminiAnswer(question, text, data.get("modelVersion") or model, sources, list(meta.get("webSearchQueries", [])))


def _http_message(status: int, body: str, model: str, search: bool = False) -> str:
    try:
        err = json.loads(body)["error"]
        detail = err["message"].split("\n")[0][:300]
    except (ValueError, KeyError, TypeError, AttributeError):
        err, detail = {}, body[:300]
    hint = {400: " (check the key)", 403: " (the key is not allowed to use this API)",
            404: f" (no model {model!r}: set [gemini].model)", 503: " (the model is overloaded; try again soon)"}.get(status, "")
    if status == 429:
        quotas, retry = [], ""
        for d in err.get("details", []) if isinstance(err, dict) else []:
            for v in d.get("violations", []):
                q = v.get("quotaId") or v.get("quotaMetric", "")
                if q and q not in quotas:
                    quotas.append(q)
            retry = d.get("retryDelay", retry)
        hint = (" (Google Search has no free quota: set [gemini].search = false, or enable billing on the key's project)"
                if search else " (the free quota for this model is used up for now; try again later or another model)")
        if quotas:
            detail += f" · quota: {', '.join(quotas[:3])}"
        if retry:
            detail += f" · retry in {retry}"
    return f"Gemini error {status}{hint}: {detail}"
