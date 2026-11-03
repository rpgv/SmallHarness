"""The terminal interface: a mode menu, and one shell per mode (chat, research,
code) on a shared base (shell.py); how events are shown (events.py), model
commands (model_cmds.py), and the command line (main.py)."""
from .events import TerminalEvents
from .main import choose_mode, confirm_trust, main, run_modes, run_notes, serve_web
from .model_cmds import delete_model, print_models, pull_model
from .code_mode import CodeShell
from .research_mode import ResearchShell
from .shell import Shell

__all__ = ["CodeShell", "ResearchShell", "Shell", "TerminalEvents", "choose_mode",
           "confirm_trust", "delete_model", "main", "print_models", "pull_model", "run_modes", "run_notes",
           "serve_web"]
