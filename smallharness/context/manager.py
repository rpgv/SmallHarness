"""Builds the model context window from the session and keeps it within budget.

Window layout (in order, each section budgeted):
  system   short role + tool list                 (always)
  summary  rolling summary of refreshed turns     (<= summary_budget)
  context  user/model-added context + auto RAG    (<= retrieve_budget each)
  turns    recent conversation                    (the rest)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .. import prompts
from ..tokens import TokenCounter
from .rag import Chunk, RagStore, search, terms
from .session import Session, transcript


@dataclass
class Built:
    prompt: str
    sections: dict[str, int]
    total: int
    budget: int
    retrieved: list[Chunk] = field(default_factory=list)
    trimmed_turns: int = 0

    @property
    def usage(self) -> float:
        return self.total / self.budget if self.budget else 0.0


class ContextManager:
    def __init__(self, cfg, counter: TokenCounter, session: Session, project_rag: RagStore,
                 session_rag: RagStore, system_prompt: Callable[[bool], str], retrieve: bool = True):
        """`retrieve`: add search results to prompts ([context].auto_retrieve). Off for the
        modes' own contexts, which start clean."""
        self.cfg = cfg
        self.count = counter
        self.session = session
        self.project_rag = project_rag
        self.session_rag = session_rag
        self.system_prompt = system_prompt
        self.retrieve_on = retrieve
        self.last: Built | None = None

    @property
    def budget(self) -> int:
        m = self.cfg["model"]
        return self.cfg.context_tokens - m["max_output_tokens"]

    @property
    def template(self) -> str:
        return self.cfg["model"]["template"]

    # ------------------------------------------------------------- retrieval
    FOLLOW_UP_TERMS = 6   # a message with fewer searchable words is read as a follow-up

    def retrieval_query(self, message: str) -> str:
        """What to search for: the message, and for a short follow-up ("and the second
        one?") the user's previous message too, which holds the subject."""
        if len(terms(message)) >= self.FOLLOW_UP_TERMS:
            return message
        users = [t.content for t in self.session.history if t.role == "user"]
        if users and users[-1] == message:
            users = users[:-1]          # the message itself was already recorded
        return f"{message}\n{users[-1]}" if users else message

    def retrieve(self, query: str, k: int | None = None) -> list[Chunk]:
        """This session's archive and the project index, ranked together."""
        return search([self.session_rag, self.project_rag], query, k or self.cfg["context"]["retrieve_k"])

    # ----------------------------------------------------------------- build
    def build(self, query: str | None = None, extra_system: str = "", tools: bool = True) -> Built:
        c = self.cfg["context"]
        budget = self.budget
        sections: dict[str, int] = {}

        system = self.system_prompt(tools) + ("\n\n" + extra_system if extra_system else "")
        sections["system"] = self.count(system)


        summary = ""
        if self.session.summary:
            summary = self.count.truncate(self.session.summary, int(budget * c["summary_budget"]))
            sections["summary"] = self.count(summary)

        ctx_cap = int(budget * c["retrieve_budget"])
        added = self._fit([f"[{i.label}]\n{i.text}" for i in reversed(self.session.injected)], ctx_cap)
        sections["added"] = self.count("\n\n".join(added)) if added else 0

        retrieved: list[Chunk] = []
        rag_parts: list[str] = []
        if query and c["auto_retrieve"] and self.retrieve_on:
            seen = {i.label for i in self.session.injected}
            for ch in self.retrieve(self.retrieval_query(query)):
                if ch.source.startswith("session:") and self._in_window(ch):
                    continue
                if f"file:{ch.source}" in seen:
                    continue
                retrieved.append(ch)
            rag_parts = self._fit([f"[{ch.cite()}]\n{ch.text}" for ch in retrieved], ctx_cap)
            retrieved = retrieved[:len(rag_parts)]
            sections["retrieved"] = self.count("\n\n".join(rag_parts)) if rag_parts else 0

        blocks = [system]
        if summary:
            blocks.append("SUMMARY OF EARLIER CONVERSATION:\n" + summary)
        if added or rag_parts:
            blocks.append("CONTEXT:\n" + "\n\n".join(list(reversed(added)) + rag_parts))
        head = "\n\n".join(blocks)

        # Turns: newest first until the budget is used; the rest are trimmed (they
        # remain in session history, and a refresh archives them to the session RAG).
        room = budget - self.count(head) - 8
        turns, used, trimmed = [], 0, 0
        for t in reversed(self.session.window):
            cost = self.count(t.content) + 4
            if used + cost > room and turns:
                trimmed = len(self.session.window) - len(turns)
                break
            if used + cost > room:  # a single huge turn: truncate it
                turns.append((t.role, self.count.truncate(t.content, max(room - 4, 16))))
                used = room
                continue
            turns.append((t.role, t.content))
            used += cost
        turns.reverse()
        sections["turns"] = used

        prompt = prompts.render(self.template, head, turns)
        built = Built(prompt, sections, self.count(prompt), budget, retrieved, trimmed)
        self.last = built
        return built

    def _fit(self, parts: list[str], cap: int) -> list[str]:
        out, used = [], 0
        for p in parts:
            n = self.count(p)
            if used + n > cap:
                if not out:
                    out.append(self.count.truncate(p, cap))
                break
            out.append(p)
            used += n
        return out

    def _in_window(self, ch: Chunk) -> bool:
        lo = min((t.n for t in self.session.window), default=10**9)
        try:
            start = int(ch.source.rsplit("#", 1)[1].split("-")[0])
        except (IndexError, ValueError):
            return False
        return start >= lo

    # --------------------------------------------------------------- refresh
    def needs_refresh(self, built: Built | None = None) -> bool:
        built = built or self.last
        c = self.cfg["context"]
        if not built or not c["auto_refresh"]:
            return False
        return built.usage >= c["refresh_threshold"] or built.trimmed_turns > 0

    def refresh(self, summarize: Callable[[str, str], str] | None = None, keep: int | None = None) -> int:
        """Move all but the last `keep` window turns out of the model context:
        archive them to the session RAG and fold them into the rolling summary.
        Returns the number of turns moved."""
        keep = self.cfg["context"]["keep_recent_turns"] if keep is None else keep
        win = self.session.window
        old = win[:-keep] if keep else list(win)
        if not old:
            return 0
        text = transcript(old)
        self.session_rag.add_text(f"session:{self.session.id}#{old[0].n}-{old[-1].n}", text)
        if summarize:
            summary = summarize(self.session.summary, text)
        else:
            summary = extractive_summary(self.session.summary, old)
        keep_from = win[-keep].n if keep else old[-1].n + 1
        self.session.compact(summary.strip(), keep_from)
        return len(old)


def extractive_summary(previous: str, turns) -> str:
    """Fallback when no model is used for summarizing: the first line of each turn."""
    lines = [previous] if previous else []
    for t in turns:
        first = next((ln.strip() for ln in t.content.splitlines() if ln.strip()), "")
        if first:
            lines.append(f"- {t.role}: {first[:160]}")
    return "\n".join(lines[-20:])
