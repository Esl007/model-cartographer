"""Text tool-call protocol for models that write calls as text instead of using
the provider's native function calling.

Gemma 3 on Nebius, for example, answers a tool-enabled request with

    ```tool_code
    provision_service(service_id='svc_0')
    ```

and the provider does not turn that into a `tool_calls` entry, so the native
loop sees "no call" and would score the episode as an early stop -- a
protocol artifact, not a capability result. `parse_text_tool_calls` recovers
the calls from the text; `format_tool_results` writes the results back in the
matching `tool_output` convention.

Deliberately permissive about the CALL SYNTAX, deliberately literal about the
NAME: a misspelled or look-alike tool name is passed through unchanged, so it
still fails in the environment as `unknown_tool` exactly as it would natively.
Tool ids may contain '-' (look-alike ids like `set_dep-th_flag_9`), which is
not valid Python, so names are matched with a regex, not `ast`.
"""

from __future__ import annotations

import ast
import json
import re

_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z_]*)[ \t]*\n(.*?)```", re.DOTALL)
_CALL_RE = re.compile(r"(?m)^[ \t]*(?:print\()?[ \t]*([A-Za-z_][\w\-]*)[ \t]*\((.*)\)[ \t]*\)?[ \t]*;?[ \t]*$")
_CODE_LANGS = {"tool_code", "tool_call", "python", "py", "", "json", "tool"}


def _parse_args(argstr: str, param_order: list[str]) -> dict:
    """`service_id='svc_0', ticket="tkt-1"` -> dict. Positional arguments map
    onto the tool's declared parameter order. Unparseable -> {} (the
    environment then reports the missing arguments)."""
    try:
        node = ast.parse(f"_f({argstr})", mode="eval").body
    except SyntaxError:
        return {}
    if not isinstance(node, ast.Call):
        return {}
    out: dict = {}
    try:
        for i, a in enumerate(node.args):
            if i < len(param_order):
                out[param_order[i]] = ast.literal_eval(a)
        for kw in node.keywords:
            if kw.arg is not None:
                out[kw.arg] = ast.literal_eval(kw.value)
    except (ValueError, SyntaxError, TypeError):
        return {}
    return out


def _json_calls(blob: str) -> list[tuple[str, dict]]:
    try:
        data = json.loads(blob)
    except ValueError:
        return []
    items = data if isinstance(data, list) else [data]
    calls = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or it.get("tool") or (it.get("function") or {}).get("name")
        args = it.get("arguments", it.get("args", it.get("parameters", {})))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if isinstance(name, str) and isinstance(args, dict):
            calls.append((name, args))
    return calls


def parse_text_tool_calls(text: str | None, param_orders: dict[str, list[str]]) -> list[tuple[str, dict]]:
    """Every tool call written in `text`, in order, as (name, args).

    Looks in fenced blocks first (```tool_code, ```python, ```json, bare
    ```), then, if there are none, at bare lines of the form `name(...)`
    whose name is a known tool (so prose like "first(...)" is not mistaken
    for a call outside a code block). `param_orders` maps each known tool
    id to its parameter names, for positional arguments."""
    if not text:
        return []
    calls: list[tuple[str, dict]] = []
    fences = [(lang.lower(), body) for lang, body in _FENCE_RE.findall(text)]
    for lang, body in fences:
        if lang not in _CODE_LANGS:
            continue
        found = _json_calls(body.strip())
        if not found:
            for name, argstr in _CALL_RE.findall(body):
                found.append((name, _parse_args(argstr, param_orders.get(name, []))))
        calls.extend(found)
    if calls or fences:
        return calls
    for name, argstr in _CALL_RE.findall(text):
        if name in param_orders:
            calls.append((name, _parse_args(argstr, param_orders[name])))
    return calls


def format_tool_results(results: list[tuple[str, dict]]) -> str:
    """The user-turn message carrying results back, in the `tool_output`
    convention text-protocol models use."""
    lines = [f"{name}: {json.dumps(result)}" for name, result in results]
    return "```tool_output\n" + "\n".join(lines) + "\n```"


def catalog_prompt(tools: list[dict]) -> str:
    """The tool catalog written into the prompt, for providers that drop the
    `tools` request field for a model (Nebius does for Gemma 3: asked to list
    its tools, the model answers "NO TOOLS"). Same definitions the native
    path sends, as JSON, plus the calling convention."""
    defs = [
        {"name": t["function"]["name"], "description": t["function"].get("description", ""),
         "parameters": t["function"]["parameters"]}
        for t in tools
    ]
    return (
        "\n\nYou have access to the following functions:\n"
        + json.dumps(defs, indent=1)
        + "\n\nTo call a function, write exactly one call in a ```tool_code block, e.g.\n"
        "```tool_code\nfunction_name(param='value')\n```\n"
        "Use the function names exactly as listed. The result comes back in a ```tool_output block."
    )
