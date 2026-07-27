"""Headless jadx sessions for LLM agents.

Every function here returns a plain dict and raises JadxRpcError on failure,
so they can be handed straight to an agent framework as callable tools:

    from google.adk.agents import LlmAgent
    import jadx_rpc

    agent = LlmAgent(
        model="gemini-2.0-flash",
        tools=[jadx_rpc.classes, jadx_rpc.decompile_class, jadx_rpc.search],
    )

The same functions back the jadx-rpc command line and the MCP server.
"""

from .core import (
    JadxRpcError,
    callees,
    callers,
    classes,
    close,
    decompile_class,
    entrypoints,
    list_renames,
    list_sessions,
    manifest,
    members,
    open_target,
    reload,
    rename,
    resource,
    resources,
    search,
    start_export,
    status,
    strings,
    symbols,
)

__all__ = [
    "JadxRpcError",
    "callees",
    "callers",
    "classes",
    "close",
    "decompile_class",
    "entrypoints",
    "list_renames",
    "list_sessions",
    "manifest",
    "members",
    "open_target",
    "reload",
    "rename",
    "resource",
    "resources",
    "search",
    "start_export",
    "status",
    "strings",
    "symbols",
]

__version__ = "0.1.0"
