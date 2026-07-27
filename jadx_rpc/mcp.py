"""MCP front end over the same functions the CLI calls.

Needs the optional dependency: pip install "jadx-rpc[mcp]"
"""

from __future__ import annotations

import inspect
import json
import sys

from . import core

TOOLS = {
    "jadx_open": core.open_target,
    "jadx_status": core.status,
    "jadx_list_sessions": core.list_sessions,
    "jadx_export": core.start_export,
    "jadx_close": core.close,
    "jadx_classes": core.classes,
    "jadx_members": core.members,
    "jadx_decompile_class": core.decompile_class,
    "jadx_symbols": core.symbols,
    "jadx_search": core.search,
    "jadx_strings": core.strings,
    "jadx_manifest": core.manifest,
    "jadx_entrypoints": core.entrypoints,
    "jadx_resources": core.resources,
    "jadx_resource": core.resource,
    "jadx_callers": core.callers,
    "jadx_callees": core.callees,
    "jadx_rename": core.rename,
    "jadx_renames": core.list_renames,
    "jadx_reload": core.reload,
}


def describe() -> list[dict]:
    out = []
    for name, function in TOOLS.items():
        doc = inspect.getdoc(function) or ""
        out.append({
            "name": name,
            "summary": doc.splitlines()[0] if doc else "",
            "parameters": [p for p in inspect.signature(function).parameters],
        })
    return out


def main(*, list_tools: bool = False, indent: int | None = 2) -> int:
    if list_tools:
        json.dump({"ok": True, "result": {"tools": describe(), "count": len(TOOLS)}}, sys.stdout, indent=indent)
        sys.stdout.write("\n")
        return 0

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        json.dump(
            {"ok": False, "error": 'the MCP server needs the optional extra: pip install "jadx-rpc[mcp]"'},
            sys.stdout,
            indent=indent,
        )
        sys.stdout.write("\n")
        return 1

    server = FastMCP("jadx-rpc")
    for name, function in TOOLS.items():
        server.add_tool(function, name=name)
    server.run()
    return 0
