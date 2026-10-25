"""Token counting: the backend's tokenizer when it has one, else a conservative estimate."""
from __future__ import annotations

import math
import re

_WORD = re.compile(r"\w+|[^\w\s]")


def estimate_tokens(text: str) -> int:
    """SentencePiece vocabularies (Gemma) average ~3.5-4 chars per token on English
    and code; we take the larger of a char-based and a word-based estimate so we
    overcount rather than overflow the window."""
    if not text:
        return 0
    return max(math.ceil(len(text) / 3.5), math.ceil(len(_WORD.findall(text)) * 1.1))


class TokenCounter:
    """Counts with the backend's tokenizer if it has one. Otherwise it estimates, and
    corrects the estimate with real counts when the server reports them (observe())."""
    MARGIN = 1.05           # stay a little above the observed ratio
    MIN_SAMPLE = 200        # estimated tokens; shorter prompts say little about the ratio

    def __init__(self, backend=None):
        self.backend = backend
        self.scale = 1.0     # real tokens / estimated tokens, learned per model
        self.samples = 0

    def __call__(self, text: str) -> int:
        if self.backend is not None:
            n = self.backend.count_tokens(text)
            if n is not None:
                return n
        est = estimate_tokens(text)
        return math.ceil(est * self.scale * self.MARGIN) if self.samples else est

    def observe(self, text: str, real_tokens: int) -> None:
        """A prompt and the token count the runtime reported for it."""
        est = estimate_tokens(text)
        if est < self.MIN_SAMPLE or real_tokens <= 0:
            return
        ratio = min(1.5, max(0.5, real_tokens / est))
        self.scale = ratio if not self.samples else 0.7 * self.scale + 0.3 * ratio
        self.samples += 1

    def reset(self) -> None:
        """A different model has a different tokenizer."""
        self.scale, self.samples = 1.0, 0

    def describe(self) -> str:
        if self.backend is not None and self.backend.count_tokens("probe") is not None:
            return "exact (model tokenizer)"
        if self.samples:
            return f"estimate × {self.scale:.2f}, calibrated from {self.samples} server count(s)"
        return "estimate (~3.5 characters per token, on the safe side)"

    def truncate(self, text: str, max_tokens: int, marker: str = "\n…[truncated]") -> str:
        if self(text) <= max_tokens:
            return text
        lo, hi = 0, len(text)
        while lo < hi:  # binary search on character length
            mid = (lo + hi + 1) // 2
            if self(text[:mid]) + 4 <= max_tokens:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + marker
