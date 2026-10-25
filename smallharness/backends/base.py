"""Backend interface. Backends are stateless prompt -> text: the harness owns
all context and sends the fully assembled window on every call."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator


class Backend(ABC):
    name = "base"
    mode = ""
    # Where the model's RAM lives, which decides how the pool counts it:
    #   resident  - weights stay loaded in this process while the instance exists
    #   per_call  - every call starts a process that loads the model, then exits
    #   external  - another process (a server) owns the model; harness RAM math does not apply
    memory = "resident"
    max_concurrent: int | None = None  # external only: requests the server takes at once

    def load(self) -> None:
        """Load weights. Called once per instance, before the first generate."""

    @abstractmethod
    def generate(self, prompt: str, max_tokens: int, stop: list[str] | None = None) -> str: ...

    def stream(self, prompt: str, max_tokens: int, stop: list[str] | None = None) -> Iterator[str]:
        yield self.generate(prompt, max_tokens, stop)

    def count_tokens(self, text: str) -> int | None:
        """Exact token count if the backend exposes a tokenizer, else None."""
        return None

    def close(self) -> None:
        pass


def apply_stop(text: str, stop: list[str] | None) -> str:
    if not stop:
        return text
    cut = len(text)
    for s in stop:
        i = text.find(s)
        if i != -1:
            cut = min(cut, i)
    return text[:cut]
