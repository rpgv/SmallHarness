"""What plan tasks and recipe sections share: a status that goes pending -> running ->
done or failed. When a run is stopped part-way, the step that was running goes back
to pending, so the next run starts it again."""
from __future__ import annotations

from typing import Iterable, Protocol


class Step(Protocol):
    status: str


def back_to_pending(steps: Iterable[Step]) -> None:
    for s in steps:
        if s.status == "running":
            s.status = "pending"
