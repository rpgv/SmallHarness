import re

NAME = "safe_search_files"
DESCRIPTION = "Search file contents in the workspace for a word or regex; returns file:line matches."
PARAMS = {
    "query": {"type": "str", "desc": "text or regular expression"},
    "glob": {"type": "str", "desc": "which files, e.g. **/*.py", "default": "**/*"},
    "path": {"type": "str", "desc": "directory to search from", "default": "."},
}
SKIP = {".git", ".smallharness", "node_modules", "__pycache__", ".venv", "venv"}
LIMIT = 50


def run(args, ctx):
    try:
        rx = re.compile(args["query"], re.I)
    except re.error:
        rx = re.compile(re.escape(args["query"]), re.I)
    base = ctx.resolve(args["path"])
    hits = []
    for p in sorted(ctx.glob(base, args["glob"])):
        rel = p.relative_to(ctx.workspace)
        if not p.is_file() or any(part in SKIP for part in rel.parts) or p.stat().st_size > 1_000_000:
            continue
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(lines, 1):
            if rx.search(line):
                hits.append(f"{rel}:{i}: {line.strip()[:160]}")
                if len(hits) >= LIMIT:
                    return "\n".join(hits) + f"\n…[stopped at {LIMIT} matches]"
    return "\n".join(hits) or "no matches"
