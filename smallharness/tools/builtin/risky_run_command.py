import subprocess

NAME = "risky_run_command"
DESCRIPTION = "Run a shell command in the workspace. Always asks the user first."
PARAMS = {
    "command": {"type": "str", "desc": "the shell command"},
    "timeout": {"type": "int", "desc": "seconds before it is stopped", "default": 60},
}


def run(args, ctx):
    try:
        proc = subprocess.run(args["command"], shell=True, cwd=ctx.workspace, capture_output=True,
                              text=True, timeout=max(1, args["timeout"]))
    except subprocess.TimeoutExpired:
        return f"ERROR: timed out after {args['timeout']}s"
    out = (proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr.strip() else "")).strip()
    return f"exit code {proc.returncode}\n{out or '(no output)'}"
