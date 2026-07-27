"""Command line front end. One JSON object on stdout per command."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import core
from .core import JadxRpcError


def _add_scope(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scope",
        choices=["app", "all"],
        default="app",
        help="app restricts to the application's own package, the default, since most "
             "classes in an APK are bundled libraries. Results always report what was hidden.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jadx-rpc",
        description="Headless jadx sessions for LLM agents. Every command prints one JSON object.",
    )
    parser.add_argument("-t", "--target", help="session id or input path, defaults to JADX_RPC_TARGET or the only open session")
    parser.add_argument("--compact", action="store_true", help="print JSON on a single line")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("open", help="open a target and build its index")
    p.add_argument("input", help="apk, dex, jar, aar, aab, xapk or apkm")
    p.add_argument("--export", action="store_true", help="also start the full decompile in the background")
    p.add_argument("--deobf", action="store_true", help="let jadx generate names for obfuscated symbols")
    p.add_argument("--threads", type=int, help="jadx worker threads")
    p.add_argument("--force", action="store_true", help="rebuild even if the session is current")
    p.add_argument(
        "--index-tmp",
        metavar="DIR",
        help="scratch directory for the index pass, which transiently needs roughly "
             "25x the input size. Defaults to inside the session directory.",
    )

    sub.add_parser("status", help="session state, counts and export progress")
    sub.add_parser("list", help="every open session")

    p = sub.add_parser("export", help="decompile every class, minutes on a real app")
    p.add_argument("--wait", action="store_true", help="block until it finishes instead of backgrounding")

    p = sub.add_parser("close", help="drop the decompiled sources, keep the index")
    p.add_argument("--purge", action="store_true", help="delete the whole session")

    p = sub.add_parser("classes", help="list classes from the index")
    p.add_argument("pattern", nargs="?", help="case insensitive regex over class names")
    _add_scope(p)
    p.add_argument("--limit", type=int, default=200)

    p = sub.add_parser("members", help="methods and fields of a class, with rename targets")
    p.add_argument("fqn")

    p = sub.add_parser("class", help="decompiled source of one class")
    p.add_argument("fqn")
    p.add_argument("--lines", help="line range, for example 40:120")

    p = sub.add_parser("symbols", help="search class, method and field names, no export needed")
    p.add_argument("pattern")
    p.add_argument("--kind", choices=["class", "method", "field"])
    _add_scope(p)
    p.add_argument("--limit", type=int, default=100)

    p = sub.add_parser("search", help="regex over every decompiled source file")
    p.add_argument("pattern")
    _add_scope(p)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--context", type=int, default=0, help="lines of context around each hit")

    p = sub.add_parser("strings", help="string literals in the decompiled sources")
    p.add_argument("pattern", nargs="?")
    _add_scope(p)
    p.add_argument("--min-len", type=int, default=4)
    p.add_argument("--limit", type=int, default=200)

    sub.add_parser("manifest", help="decoded AndroidManifest.xml")
    sub.add_parser("entrypoints", help="components, exported flags and permissions")

    p = sub.add_parser("resources", help="list decoded resources")
    p.add_argument("pattern", nargs="?")
    p.add_argument("--limit", type=int, default=200)

    p = sub.add_parser("resource", help="read one decoded resource")
    p.add_argument("path")

    p = sub.add_parser("callers", help="methods that call this one")
    p.add_argument("method")

    p = sub.add_parser("callees", help="methods this one calls")
    p.add_argument("method")

    p = sub.add_parser("rename", help="record a rename, applied on the next reload")
    p.add_argument("kind", choices=["class", "method", "field"])
    p.add_argument("symbol", help="com.foo.Bar, com.foo.Bar.baz(Ljava/lang/String;)V or com.foo.Bar.salt:Ljava/lang/String;")
    p.add_argument("new_name", metavar="new-name")

    sub.add_parser("renames", help="list recorded renames")

    p = sub.add_parser("reload", help="rebuild with the recorded renames applied")
    p.add_argument("--wait", action="store_true")

    p = sub.add_parser("mcp", help="serve the same commands over MCP on stdio")
    p.add_argument("--list-tools", action="store_true", help="print the tool list and exit")

    return parser


def dispatch(args: argparse.Namespace) -> dict:
    target = args.target
    command = args.command

    if command == "open":
        return core.open_target(
            args.input, deobf=args.deobf, threads=args.threads, export=args.export,
            force=args.force, index_tmp=args.index_tmp,
        )
    if command == "status":
        return core.status(target)
    if command == "list":
        return core.list_sessions()
    if command == "export":
        return core.start_export(target, background=not args.wait)
    if command == "close":
        return core.close(target, purge=args.purge)
    if command == "classes":
        return core.classes(args.pattern, scope=args.scope, limit=args.limit, target=target)
    if command == "members":
        return core.members(args.fqn, target)
    if command == "class":
        return core.decompile_class(args.fqn, lines=args.lines, target=target)
    if command == "symbols":
        return core.symbols(
            args.pattern, kind=args.kind, scope=args.scope, limit=args.limit, target=target
        )
    if command == "search":
        return core.search(
            args.pattern, scope=args.scope, limit=args.limit, context=args.context, target=target
        )
    if command == "strings":
        return core.strings(
            args.pattern, scope=args.scope, min_len=args.min_len, limit=args.limit, target=target
        )
    if command == "manifest":
        return core.manifest(target)
    if command == "entrypoints":
        return core.entrypoints(target)
    if command == "resources":
        return core.resources(args.pattern, limit=args.limit, target=target)
    if command == "resource":
        return core.resource(args.path, target)
    if command == "callers":
        return core.callers(args.method, target)
    if command == "callees":
        return core.callees(args.method, target)
    if command == "rename":
        return core.rename(args.kind, args.symbol, args.new_name, target)
    if command == "renames":
        return core.list_renames(target)
    if command == "reload":
        return core.reload(target, background=not args.wait)
    raise JadxRpcError(f"unknown command {command!r}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    indent = None if args.compact else 2

    if args.command == "mcp":
        from . import mcp

        return mcp.main(list_tools=args.list_tools, indent=indent)

    try:
        payload = {"ok": True, "result": dispatch(args)}
    except JadxRpcError as exc:
        payload = {"ok": False, "error": str(exc)}
    except KeyboardInterrupt:
        payload = {"ok": False, "error": "interrupted"}
    except Exception as exc:  # noqa: BLE001
        # A caller parsing stdout gets valid JSON whatever happens, including
        # for a bad regex or an unreadable file, rather than a traceback.
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        json.dump(payload, sys.stdout, indent=indent)
        sys.stdout.write("\n")
        sys.stdout.flush()
    except BrokenPipeError:
        # Piped into head, jq -n or similar. Point stdout at devnull so the
        # interpreter does not complain again while shutting down.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
