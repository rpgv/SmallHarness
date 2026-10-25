"""Lenient parser for tool calls in small-model output.

Canonical form:  <tool>{"name": "safe_read_file", "args": {"path": "a.py"}}</tool>

Also accepted, because small models drift into them:
  - Python call syntax:   <tool>safe_read_file(path="a.py")</tool>, print(...) wrappers,
                          default_api.safe_read_file(...), positional arguments
  - <tool_call> / <function_call> tags, a missing closing tag
  - code fences inside the tag, ```tool_code / ```python fences holding a call
  - JSON quirks: single quotes, trailing commas, Python literals, truncation
  - `name {json-args}`, args flattened next to "name"
  - a reply that is nothing but a call to a known tool

Text that only *mentions* a tool tag (prose, or the model echoing an error
message) is not a call: without a readable call or a known tool name inside
the tag, the parser returns None instead of raising.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass

_TAG = re.compile(r"<(tool|tool_call|function_call)>\s*(.*?)\s*(?:</\1>|$)", re.S)
_FENCE = re.compile(r"```[ \t]*([\w-]*)[ \t]*\n?(.*?)```", re.S)
_NAMED = re.compile(r"^([A-Za-z_][\w.]*)\s*(\{.*\})\s*$", re.S)
_IDENT = re.compile(r"\b(?:safe|risky)_\w+")
_JSON_WORDS = {"true": True, "false": False, "null": None, "none": None}


@dataclass
class ToolCall:
    name: str
    args: dict
    raw: str


class ToolCallError(ValueError):
    def __init__(self, message: str, name: str | None = None):
        super().__init__(message)
        self.name = name  # the tool the model seemed to want, if recognisable


def _loads(s: str):
    s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    fixed = re.sub(r",\s*([}\]])", r"\1", s)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass
    try:
        return ast.literal_eval(re.sub(r"\b(true|false|null)\b",
                                       lambda m: {"true": "True", "false": "False", "null": "None"}[m.group(1)],
                                       fixed))
    except (ValueError, SyntaxError):
        pass
    opened = fixed.count("{") - fixed.count("}")
    if opened > 0:  # truncated output: close open braces
        return _loads(fixed + "}" * opened)
    raise ToolCallError("the JSON is not valid; write double quotes inside text values as \\\"")


def _from_obj(obj, raw: str) -> ToolCall:
    if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
        raise ToolCallError('the JSON needs a "name" string')
    args = obj.get("args", obj.get("arguments", obj.get("parameters")))
    if args is None:
        args = {k: v for k, v in obj.items() if k != "name"}
    if isinstance(args, str):
        args = _loads(args) if args.strip().startswith("{") else {}
    if not isinstance(args, dict):
        raise ToolCallError('"args" must be an object', obj["name"])
    return ToolCall(obj["name"].strip(), args, raw)


def _literal(node: ast.AST):
    if isinstance(node, ast.Name) and node.id.lower() in _JSON_WORDS:
        return _JSON_WORDS[node.id.lower()]
    return ast.literal_eval(node)


def _from_call_syntax(body: str, raw: str, known: dict[str, list[str]]) -> ToolCall | None:
    """`name(a=1, b="x")`, optionally wrapped in print() or prefixed by a module."""
    try:
        node = ast.parse(body.strip().rstrip(";"), mode="eval").body
    except SyntaxError:
        return None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print" \
            and len(node.args) == 1 and isinstance(node.args[0], ast.Call):
        node = node.args[0]
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    if not name:
        return None
    try:
        args = {kw.arg: _literal(kw.value) for kw in node.keywords if kw.arg}
        positional = [_literal(a) for a in node.args]
    except (ValueError, SyntaxError):
        raise ToolCallError("argument values must be plain strings, numbers or booleans", name)
    if positional:
        order = [p for p in known.get(name, []) if p not in args]
        if len(positional) > len(order):
            raise ToolCallError("use named arguments, e.g. path=\"README.md\"", name)
        args.update(zip(order, positional))
    return ToolCall(name, args, raw)


def _strip_fences(body: str) -> str:
    m = _FENCE.fullmatch(body.strip())
    return m.group(2).strip() if m else body.strip()


def _decode_value(seg: str):
    """One salvaged argument value. Strings may hold unescaped quotes and raw newlines."""
    seg = seg.strip().rstrip(",").strip()
    if len(seg) >= 2 and seg[0] == '"' and seg[-1] == '"':
        inner = re.sub(r'(?<!\\)"', r'\\"', seg[1:-1])
        try:
            return json.JSONDecoder(strict=False).decode(f'"{inner}"')
        except json.JSONDecodeError:
            return seg[1:-1]
    try:
        return _JSON_WORDS[seg.lower()] if seg.lower() in _JSON_WORDS else json.loads(seg)
    except json.JSONDecodeError:
        return seg


def _salvage(body: str, raw: str, known: dict[str, list[str]]) -> ToolCall | None:
    """Recover a call whose JSON is broken, typically by code with unescaped quotes
    inside a string. The tool's parameter names are known, so the text is split at
    `"param":` markers instead of being parsed as JSON."""
    name = None
    m = re.match(r"\s*([A-Za-z_][\w.]*)\s*(?=\{)", body)
    if m and m.group(1).rsplit(".", 1)[-1] in known:          # name{...}
        name, obj = m.group(1).rsplit(".", 1)[-1], body[m.end():]
    else:
        for nm in re.finditer(r'"name"\s*:\s*"(\w+)"\s*,?', body):
            if nm.group(1) in known:
                name = nm.group(1)
                break
        if not name:
            return None
        am = re.search(r'"(?:args|arguments|parameters)"\s*:\s*(?=\{)', body)
        # wrapped: take the args object; flattened: drop the tool-name pair
        obj = body[am.end():] if am else body[:nm.start()] + body[nm.end():]
    obj = obj.strip()
    if obj.startswith("{"):
        obj = obj[1:]
    obj = obj.rstrip()
    while obj.endswith("}"):  # closing braces of the args object and any wrapper
        obj = obj[:-1].rstrip()
    params, seen, marks = known[name], set(), []
    for km in re.finditer(r'"(\w+)"\s*:', obj):
        key = km.group(1)
        if key in params and key not in seen:
            seen.add(key)
            marks.append((key, km.start(), km.end()))
    if not marks:
        return None
    args = {}
    for i, (key, _, vstart) in enumerate(marks):
        vend = marks[i + 1][1] if i + 1 < len(marks) else len(obj)
        args[key] = _decode_value(obj[vstart:vend])
    return ToolCall(name, args, raw)


def _parse_body(body: str, raw: str, known: dict[str, list[str]]) -> ToolCall | None:
    body = _strip_fences(body)
    try:
        return _parse_strict(body, raw, known)
    except ToolCallError:
        call = _salvage(body, raw, known)
        if call is None:
            raise
        return call


def _parse_strict(body: str, raw: str, known: dict[str, list[str]]) -> ToolCall | None:
    if "{" in body and body.lstrip().startswith("{"):
        return _from_obj(_loads(body[body.index("{"):body.rindex("}") + 1] if "}" in body else body), raw)
    named = _NAMED.match(body)
    if named:
        args = _loads(named.group(2))
        return ToolCall(named.group(1).rsplit(".", 1)[-1], args if isinstance(args, dict) else {}, raw)
    call = _from_call_syntax(body, raw, known)
    if call:
        return call
    if "{" in body and "}" in body:  # JSON after some leading words
        try:
            return _from_obj(_loads(body[body.index("{"):body.rindex("}") + 1]), raw)
        except ToolCallError:
            pass
    return None


def _mentioned_tool(text: str, known: dict[str, list[str]]) -> str | None:
    for name in _IDENT.findall(text):
        if not known or name in known:
            return name
    return None


def parse_tool_call(text: str, known: dict[str, list[str]] | None = None) -> ToolCall | None:
    """Return the first tool call in `text`, or None if there is none.

    `known` maps tool names to their parameter order; it is used to map
    positional arguments and to tell a broken call from a passing mention.
    Raises ToolCallError only when the model clearly tried to call a tool
    (the tag holds a tool name or JSON) but the call cannot be read."""
    known = known or {}
    for m in _TAG.finditer(text):
        body = m.group(2)
        try:
            call = _parse_body(body, m.group(0), known)
        except ToolCallError as e:
            raise ToolCallError(str(e), e.name or _mentioned_tool(body, known)) from None
        if call:
            return call
        name = _mentioned_tool(body, known)
        if name or body.strip().startswith("{"):
            raise ToolCallError("the call inside the tool tags could not be read", name)
        # otherwise: prose or an echoed message that merely contains a tag
    for m in _FENCE.finditer(text):
        lang, body = m.group(1).lower(), m.group(2).strip()
        if lang in ("tool", "tool_code", "tool_call", "json", "python", "py", ""):
            try:
                call = _parse_body(body, m.group(0), known)
            except ToolCallError:
                continue
            # A fenced block counts as a call only if it is clearly one.
            if call and (lang.startswith("tool") or call.name in known or "args" in body or "arguments" in body):
                return call
    stripped = text.strip()
    if known and stripped and "\n" not in stripped:  # a bare call to a known tool, nothing else
        call = _from_call_syntax(stripped, stripped, known)
        if call and call.name in known:
            return call
    return None
