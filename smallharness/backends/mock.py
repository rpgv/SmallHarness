"""Deterministic backend for tests and for trying the harness without a model."""
from __future__ import annotations

import re
from collections import deque

from .base import Backend, apply_stop


class MockBackend(Backend):
    name = "mock"

    def __init__(self, script: list[str] | None = None):
        self.script = deque(script or [])
        self.prompts: list[str] = []

    def generate(self, prompt: str, max_tokens: int, stop: list[str] | None = None) -> str:
        self.prompts.append(prompt)
        if self.script:
            return apply_stop(self.script.popleft(), stop)
        if "Summarize the conversation" in prompt:
            return "- (mock summary of earlier conversation)"
        if "PLAN FORMAT" in prompt:
            return "1. [safe_list_files] List the workspace files\n2. [none] Report findings (after: 1)"
        if "EXPAND STEP" in prompt:
            text = "Use the suggested tool on the workspace. Report what it shows. It is done when the result is known."
            return text + ("\nCHECK: [safe_list_files] the workspace lists the expected files" if "CHECK: [tool]" in prompt else "")
        if "CHECK FOR TASK" in prompt:
            return "DONE: mock check passed"
        if "EXCERPTS:" in prompt:     # research: answer from the first excerpt, citing it
            cites = re.findall(r"^\[(\d+)\]", prompt, re.M)
            return f"From the excerpts: see [{cites[0]}]." if cites else "The excerpts do not answer this."
        if "CURRENT TASK" in prompt:
            return "DONE: mock completed the task"
        users = re.findall(r"(?:<start_of_turn>user|USER:)\s*(.*?)(?:<end_of_turn>|\n(?:ASSISTANT|TOOL RESULT):|$)", prompt, re.S)
        last = users[-1].strip().splitlines()[-1] if users else ""
        if last.startswith("TOOL RESULT"):
            return "I have the tool result above. (mock backend)"
        return f"[mock] You said: {last[:200]}"

