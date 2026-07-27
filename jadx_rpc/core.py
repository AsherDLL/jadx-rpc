"""Headless jadx sessions.

A session is a directory of files, not a running process. Opening a target runs
two cheap jadx passes, an index pass and a resource pass, and everything after
that is either a file read or an on demand decompile of a single class. The one
expensive operation, decompiling every class, is opt in and runs in the
background because it costs minutes on a real application.

Measured on a 12 MB APK with 10761 classes, jadx 1.5.6, so the split above is
not a guess:

    index pass     7.6 s   19705 classes, 117352 methods, raw and alias names
    resource pass  4.4 s   decoded AndroidManifest.xml and every resource
    single class   4.5 s   one decompiled class
    full export  175.0 s   223 MB of Java plus the call graph
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from . import mappings

# jadx returns 3 when some classes failed to decompile. The rest of the output
# is still written and still usable, so it is a success for our purposes.
JADX_OK = (0, 3)

ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
COMPONENT_TAGS = ("activity", "activity-alias", "service", "receiver", "provider")
STRING_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"')


class JadxRpcError(RuntimeError):
    pass


def _write_json(path: Path, payload: dict) -> None:
    """Write atomically so a concurrent status read never sees half a file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------
# locations
# --------------------------------------------------------------------------


def state_dir() -> Path:
    override = os.environ.get("JADX_RPC_STATE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "jadx-rpc"


def jadx_bin() -> str:
    found = os.environ.get("JADX_BIN") or shutil.which("jadx")
    if not found:
        raise JadxRpcError("jadx not found. Put it on PATH or set JADX_BIN to the jadx launcher.")
    return found


def _session_id(target: Path) -> str:
    return hashlib.sha256(str(target).encode()).hexdigest()[:12]


class Session:
    def __init__(self, directory: Path):
        self.dir = directory

    # layout
    @property
    def meta_path(self) -> Path:
        return self.dir / "session.json"

    @property
    def index_path(self) -> Path:
        return self.dir / "mapping.json"

    @property
    def callgraph_path(self) -> Path:
        return self.dir / "callgraph.json"

    @property
    def src(self) -> Path:
        return self.dir / "src"

    @property
    def res(self) -> Path:
        return self.dir / "res"

    @property
    def cache(self) -> Path:
        return self.dir / "cache"

    @property
    def renames_path(self) -> Path:
        return self.dir / "renames.mapping"

    @property
    def logs(self) -> Path:
        return self.dir / "logs"

    def read_meta(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise JadxRpcError(f"no session at {self.dir}") from None

    def write_meta(self, meta: dict) -> None:
        _write_json(self.meta_path, meta)

    def load_index(self) -> dict:
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise JadxRpcError("index missing, reopen the target") from None

    def load_renames(self):
        return mappings.load(self.renames_path)

    def pending_renames(self) -> int:
        meta = self.read_meta()
        return mappings.count(self.load_renames()) - meta.get("renames_applied", 0)


def _sessions() -> list[Session]:
    root = state_dir()
    if not root.is_dir():
        return []
    return [Session(d) for d in sorted(root.iterdir()) if (d / "session.json").is_file()]


def resolve(target: str | None = None) -> Session:
    """Pick the session to act on.

    An explicit path or id wins, then JADX_RPC_TARGET, then the only open
    session if there is exactly one.
    """
    wanted = target or os.environ.get("JADX_RPC_TARGET")
    if wanted:
        as_path = Path(wanted).expanduser()
        if as_path.exists():
            session = Session(state_dir() / _session_id(as_path.resolve()))
            if session.meta_path.is_file():
                return session
            raise JadxRpcError(f"{as_path} is not open, run: jadx-rpc open {as_path}")
        session = Session(state_dir() / wanted)
        if session.meta_path.is_file():
            return session
        raise JadxRpcError(f"no session for target {wanted!r}")

    found = _sessions()
    if len(found) == 1:
        return found[0]
    if not found:
        raise JadxRpcError("no open sessions, run: jadx-rpc open <apk>")
    ids = ", ".join(s.dir.name for s in found)
    raise JadxRpcError(f"{len(found)} sessions open, pass --target or set JADX_RPC_TARGET. Open: {ids}")


# --------------------------------------------------------------------------
# running jadx
# --------------------------------------------------------------------------


def _run(session: Session, name: str, args: list[str]) -> int:
    session.logs.mkdir(parents=True, exist_ok=True)
    log = session.logs / f"{name}.log"
    with log.open("w", encoding="utf-8") as handle:
        handle.write(" ".join(args) + "\n\n")
        handle.flush()
        rc = subprocess.call(args, stdout=handle, stderr=subprocess.STDOUT)
    if rc not in JADX_OK:
        tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
        raise JadxRpcError(f"jadx {name} pass failed with exit {rc}\n{tail}")
    return rc


def _common_args(session: Session, meta: dict) -> list[str]:
    args = []
    if meta.get("deobf"):
        args.append("--deobf")
    if meta.get("threads"):
        args += ["-j", str(meta["threads"])]
    if session.renames_path.exists() and session.renames_path.stat().st_size:
        args += ["--mappings-path", str(session.renames_path)]
    return args


def _index_pass(session: Session, meta: dict) -> None:
    """Build mapping.json: every class, method and field, raw name and alias.

    Fallback decompilation mode skips code restructuring, which is what makes
    this pass seconds rather than minutes. The per class JSON files it writes
    alongside are not useful here and are deleted, so the pass costs transient
    disk roughly the size of the decompiled application.
    """
    tmp = session.dir / "_index"
    shutil.rmtree(tmp, ignore_errors=True)
    args = [
        jadx_bin(), "-q",
        "--output-format", "json",
        "-m", "fallback",
        "--no-res",
        "-d", str(tmp), "-ds", str(tmp),
        *_common_args(session, meta),
        meta["input"],
    ]
    try:
        _run(session, "index", args)
        produced = tmp / "mapping.json"
        if not produced.is_file():
            raise JadxRpcError("jadx index pass produced no mapping.json")
        shutil.move(str(produced), str(session.index_path))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _apply_renames_to_index(session)


def _apply_renames_to_index(session: Session) -> None:
    """Fold recorded renames into the index by hand.

    Fallback mode honours --deobf but not --mappings-path, so the index comes
    back with the original aliases. The full export runs in normal mode and
    does apply them, so without this the index and the exported sources would
    disagree about every renamed name. Enigma renames are a direct raw to new
    name mapping, so applying them here is exact rather than a guess.
    """
    entries = session.load_renames()
    if not entries:
        return
    renamed_classes = {
        raw.replace("/", "."): entry.new_name.replace("/", ".")
        for raw, entry in entries.items()
        if entry.new_name
    }
    index = session.load_index()
    for cls in index["classes"]:
        raw = cls["name"]
        top = cls.get("top-class") or raw
        new_top = renamed_classes.get(top)
        if new_top:
            cls["alias"] = (new_top + raw[len(top):]).replace("$", ".")
            cls["json"] = new_top.replace(".", "/") + ".json"

        entry = entries.get(raw.replace(".", "/"))
        if entry is None:
            continue
        for method in cls.get("methods", ()):
            new = entry.methods.get((method["name"], method["signature"][len(method["name"]):]))
            if new:
                method["alias"] = new
        # The index carries no field descriptors, so fields match on name
        # alone. Two fields of one class can share a name in bytecode, which
        # only affects the label shown here, never the rename jadx applies.
        by_name = {name: new for (name, _desc), new in entry.fields.items()}
        for field in cls.get("fields", ()):
            if field["name"] in by_name:
                field["alias"] = by_name[field["name"]]
    _write_json(session.index_path, index)


def _resource_pass(session: Session, meta: dict) -> None:
    shutil.rmtree(session.res, ignore_errors=True)
    scratch = session.dir / "_res"
    args = [
        jadx_bin(), "-q", "-s",
        "-d", str(scratch), "-dr", str(session.res),
        *_common_args(session, meta),
        meta["input"],
    ]
    try:
        _run(session, "resources", args)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _export_pass(session: Session, meta: dict) -> None:
    """Decompile every class. Minutes on a real application."""
    shutil.rmtree(session.src, ignore_errors=True)
    args = [
        jadx_bin(), "-q",
        "--no-res",
        "--call-graph", "json",
        "-d", str(session.dir), "-ds", str(session.src),
        *_common_args(session, meta),
        meta["input"],
    ]
    _run(session, "export", args)


# --------------------------------------------------------------------------
# session lifecycle
# --------------------------------------------------------------------------


def _index_counts(session: Session) -> dict:
    index = session.load_index()
    classes = index["classes"]
    return {
        "classes": len(classes),
        "methods": sum(len(c.get("methods", ())) for c in classes),
        "fields": sum(len(c.get("fields", ())) for c in classes),
    }


def open_target(
    input_path: str,
    *,
    deobf: bool = False,
    threads: int | None = None,
    export: bool = False,
    force: bool = False,
) -> dict:
    """Open a target and build its index. Reopening an unchanged target is free."""
    source = Path(input_path).expanduser().resolve()
    if not source.is_file():
        raise JadxRpcError(f"input not found: {source}")

    session = Session(state_dir() / _session_id(source))
    stat = source.stat()
    fingerprint = {"size": stat.st_size, "mtime": int(stat.st_mtime)}

    if session.meta_path.is_file() and not force:
        meta = session.read_meta()
        if meta.get("fingerprint") == fingerprint and session.index_path.is_file():
            result = {"id": session.dir.name, "input": meta["input"], "reused": True}
            result.update(_index_counts(session))
            result["export"] = _export_state(session)["state"]
            return result

    shutil.rmtree(session.dir, ignore_errors=True)
    session.dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "input": str(source),
        "fingerprint": fingerprint,
        "deobf": deobf,
        "threads": threads,
        "opened_at": int(time.time()),
        "renames_applied": 0,
    }
    session.write_meta(meta)

    started = time.time()
    _index_pass(session, meta)
    _resource_pass(session, meta)
    elapsed = round(time.time() - started, 1)

    result = {"id": session.dir.name, "input": str(source), "reused": False, "elapsed_s": elapsed}
    result.update(_index_counts(session))
    result["export"] = "none"
    if export:
        result["export"] = start_export(session.dir.name)["state"]
    return result


def _export_state(session: Session) -> dict:
    path = session.dir / "export.json"
    state = _read_json(path)
    if state is None:
        return {"state": "none"}
    if state.get("state") == "running":
        pid = state.get("pid")
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if not alive:
            state = {"state": "failed", "error": "export process died, see logs/export.log"}
            _write_json(path, state)
        else:
            state["elapsed_s"] = int(time.time() - state.get("started_at", time.time()))
    return state


def start_export(target: str | None = None, *, background: bool = True) -> dict:
    """Decompile every class. This is the expensive pass, minutes on a real app."""
    session = resolve(target)
    current = _export_state(session)
    if current.get("state") == "running":
        return current

    if not background:
        return _export_worker_run(session)

    # The worker owns export.json from the moment it starts, so nothing here
    # can clobber a state it has already written. Wait briefly for it to
    # appear, which also means a short export reports "ready" straight away.
    (session.dir / "export.json").unlink(missing_ok=True)
    subprocess.Popen(
        [sys.executable, "-m", "jadx_rpc.core", "--export-worker", str(session.dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        state = _export_state(session)
        if state.get("state") != "none":
            return state
        time.sleep(0.05)
    return {"state": "unknown", "error": "export worker did not report in, see logs/export.log"}


def _disk_mb(directory: Path) -> float:
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue  # an export writing underneath us
    return round(total / 1e6, 1)


def status(target: str | None = None) -> dict:
    """Session state: index counts, export progress, pending renames and disk use."""
    session = resolve(target)
    meta = session.read_meta()
    source = Path(meta["input"])
    result = {
        "id": session.dir.name,
        "input": meta["input"],
        "input_present": source.is_file(),
        "deobf": meta.get("deobf", False),
        "export": _export_state(session),
        "pending_renames": session.pending_renames(),
        "state_dir": str(session.dir),
        "disk_mb": _disk_mb(session.dir),
    }
    result.update(_index_counts(session))
    result["sources_ready"] = session.src.is_dir()
    result["resources_ready"] = session.res.is_dir()
    result["callgraph_ready"] = session.callgraph_path.is_file()
    return result


def list_sessions() -> dict:
    """Every open session, with what it was opened from and its export state."""
    out = []
    for session in _sessions():
        try:
            meta = session.read_meta()
        except JadxRpcError:
            continue
        out.append({
            "id": session.dir.name,
            "input": meta["input"],
            "export": _export_state(session).get("state", "none"),
            "opened_at": meta.get("opened_at"),
        })
    return {"sessions": out, "count": len(out)}


def close(target: str | None = None, *, purge: bool = False) -> dict:
    """Free the decompiled sources and keep the index, or purge the session entirely."""
    session = resolve(target)
    meta = session.read_meta()
    if purge:
        shutil.rmtree(session.dir, ignore_errors=True)
        return {"closed": session.dir.name, "input": meta["input"], "purged": True}
    for path in (session.src, session.cache, session.dir / "export.json", session.callgraph_path):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            path.unlink()
    return {"closed": session.dir.name, "input": meta["input"], "purged": False}


# --------------------------------------------------------------------------
# index queries
# --------------------------------------------------------------------------


def _stale(session: Session) -> bool:
    return session.pending_renames() > 0


def _tag(session: Session, payload: dict) -> dict:
    if _stale(session):
        payload["stale"] = True
        payload["note"] = "renames are pending, run reload to apply them"
    return payload


def classes(pattern: str | None = None, *, limit: int = 200, target: str | None = None) -> dict:
    """List classes from the index. Pattern is a case insensitive regex, no export needed."""
    session = resolve(target)
    index = session.load_index()
    matcher = re.compile(pattern, re.IGNORECASE) if pattern else None
    out = []
    total = 0
    for entry in index["classes"]:
        if matcher and not (matcher.search(entry["name"]) or matcher.search(entry["alias"])):
            continue
        total += 1
        if len(out) < limit:
            item = {
                "name": entry["alias"],
                "raw_name": entry["name"],
                "inner": entry.get("inner", False),
                "methods": len(entry.get("methods", ())),
                "fields": len(entry.get("fields", ())),
            }
            if entry.get("top-class"):
                item["top_class"] = entry["top-class"]
            out.append(item)
    return _tag(session, {"classes": out, "returned": len(out), "matched": total, "truncated": total > len(out)})


def _normalize_symbol(symbol: str) -> str:
    """Accept com/foo/Bar as well as com.foo.Bar, without touching descriptors.

    Only the class and member part before a descriptor is separator agnostic.
    A JVM descriptor keeps its slashes, Ljava/lang/String; is not Ljava.lang.String;.
    """
    cut = len(symbol)
    for marker in ("(", ":"):
        found = symbol.find(marker)
        if found != -1:
            cut = min(cut, found)
    return symbol[:cut].replace("/", ".") + symbol[cut:]


def _find_class(index: dict, fqn: str) -> dict:
    wanted = fqn.replace("/", ".")
    for entry in index["classes"]:
        if entry["alias"] == wanted or entry["name"] == wanted:
            return entry
    raise JadxRpcError(f"class not found: {fqn}. Try: jadx-rpc classes --filter {fqn.split('.')[-1]}")


def members(fqn: str, target: str | None = None) -> dict:
    """Methods and fields of a class, with the exact strings rename expects."""
    session = resolve(target)
    entry = _find_class(session.load_index(), fqn)
    raw = entry["name"]
    # signature is already the raw method name followed by its JVM descriptor,
    # which is exactly what an Enigma METHOD line needs.
    methods = [
        {
            "name": m["alias"],
            "raw_name": m["name"],
            "signature": m["signature"],
            "offset": m.get("offset"),
            "rename_target": f"{raw}.{m['signature']}",
        }
        for m in entry.get("methods", ())
    ]
    fields = [{"name": f["alias"], "raw_name": f["name"]} for f in entry.get("fields", ())]
    return _tag(session, {
        "name": entry["alias"],
        "raw_name": raw,
        "inner": entry.get("inner", False),
        "rename_target": raw,
        "methods": methods,
        "fields": fields,
        "field_rename_note": "field renames need a JVM descriptor, pass "
                             "<raw class>.<field>:<descriptor>, for example com.foo.Bar.salt:Ljava/lang/String;",
    })


def symbols(pattern: str, *, kind: str | None = None, limit: int = 100, target: str | None = None) -> dict:
    """Search class, method and field names in the index. No export needed."""
    session = resolve(target)
    matcher = re.compile(pattern, re.IGNORECASE)
    kinds = {kind} if kind else {"class", "method", "field"}
    out: list[dict] = []
    total = 0
    for entry in session.load_index()["classes"]:
        if "class" in kinds and (matcher.search(entry["name"]) or matcher.search(entry["alias"])):
            total += 1
            if len(out) < limit:
                out.append({"kind": "class", "name": entry["alias"], "raw_name": entry["name"]})
        if "method" in kinds:
            for m in entry.get("methods", ()):
                if matcher.search(m["name"]) or matcher.search(m["alias"]):
                    total += 1
                    if len(out) < limit:
                        out.append({
                            "kind": "method",
                            "name": f"{entry['alias']}.{m['alias']}",
                            "raw_name": f"{entry['name']}.{m['name']}",
                            "signature": m["signature"],
                        })
        if "field" in kinds:
            for f in entry.get("fields", ()):
                if matcher.search(f["name"]) or matcher.search(f["alias"]):
                    total += 1
                    if len(out) < limit:
                        out.append({
                            "kind": "field",
                            "name": f"{entry['alias']}.{f['alias']}",
                            "raw_name": f"{entry['name']}.{f['name']}",
                        })
    return _tag(session, {"symbols": out, "returned": len(out), "matched": total, "truncated": total > len(out)})


# --------------------------------------------------------------------------
# source
# --------------------------------------------------------------------------


def _source_rel(entry: dict) -> Path:
    # The index records the output path each class was written to, which is
    # also where the Java export puts it. Inner classes point at the file of
    # their top level parent.
    written = entry.get("json")
    if not written:
        raise JadxRpcError(f"no source path recorded for {entry['name']}, it produces no code")
    return Path(written).with_suffix(".java")


def _decompile_one(session: Session, meta: dict, entry: dict) -> Path:
    out = session.cache / _source_rel(entry)
    out.parent.mkdir(parents=True, exist_ok=True)
    scratch = session.dir / "_single"
    target_class = entry.get("top-class") or entry["name"]
    args = [
        jadx_bin(), "-q", "--no-res",
        "-d", str(scratch),
        "--single-class", target_class,
        "--single-class-output", str(out),
        *_common_args(session, meta),
        meta["input"],
    ]
    try:
        _run(session, "single-class", args)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if not out.is_file():
        raise JadxRpcError(f"jadx did not produce source for {target_class}")
    return out


def decompile_class(fqn: str, *, lines: str | None = None, target: str | None = None) -> dict:
    """Decompiled source of one class.

    Served from the full export when it exists, otherwise decompiled on demand
    and cached, which costs a few seconds the first time and nothing after.
    """
    session = resolve(target)
    meta = session.read_meta()
    entry = _find_class(session.load_index(), fqn)
    rel = _source_rel(entry)

    path = session.src / rel
    origin = "export"
    if not path.is_file():
        path = session.cache / rel
        origin = "cache"
        if not path.is_file():
            path = _decompile_one(session, meta, entry)
            origin = "on-demand"

    text = path.read_text(encoding="utf-8", errors="replace")
    all_lines = text.splitlines()
    result = {
        "name": entry["alias"],
        "raw_name": entry["name"],
        "path": str(rel),
        "origin": origin,
        "line_count": len(all_lines),
    }
    if entry.get("inner"):
        result["note"] = f"inner class, source shown is its top level parent {entry.get('top-class')}"
    if lines:
        start, _, end = lines.partition(":")
        try:
            first = max(1, int(start or 1))
            last = int(end) if end else len(all_lines)
        except ValueError:
            raise JadxRpcError(f"bad line range {lines!r}, expected something like 40:120") from None
        last = min(max(last, first), len(all_lines))
        result["lines"] = f"{first}:{last}"
        result["code"] = "\n".join(all_lines[first - 1:last])
    else:
        result["code"] = text
    return _tag(session, result)


def _require_export(session: Session, what: str) -> bool:
    """Return True when the export is only partially written."""
    state = _export_state(session).get("state", "none")
    if session.src.is_dir():
        return state == "running"
    if state == "running":
        raise JadxRpcError(f"{what} needs the full export, which is still running. Check: jadx-rpc status")
    raise JadxRpcError(
        f"{what} needs every class decompiled. Start it with: jadx-rpc export --background "
        "(minutes on a real app). For name lookups that need no export use: jadx-rpc symbols"
    )


def _walk_sources(session: Session):
    for path in sorted(session.src.rglob("*.java")):
        try:
            yield path, path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # an export writing underneath us


def search(pattern: str, *, limit: int = 100, context: int = 0, target: str | None = None) -> dict:
    """Regex over every decompiled source file."""
    session = resolve(target)
    partial = _require_export(session, "search")
    matcher = re.compile(pattern)
    hits: list[dict] = []
    total = 0
    files = 0
    for path, text in _walk_sources(session):
        if not matcher.search(text):
            continue
        files += 1
        lines = text.splitlines()
        for number, line in enumerate(lines, 1):
            if not matcher.search(line):
                continue
            total += 1
            if len(hits) < limit:
                hit = {
                    "file": str(path.relative_to(session.src)),
                    "line": number,
                    "text": line.strip()[:400],
                }
                if context:
                    lo = max(0, number - 1 - context)
                    hi = min(len(lines), number + context)
                    hit["context"] = "\n".join(lines[lo:hi])
                hits.append(hit)
    return _tag(session, {
        "hits": hits, "returned": len(hits), "matched": total,
        "files_matched": files, "truncated": total > len(hits),
        "partial": partial,
    })


def strings(pattern: str | None = None, *, min_len: int = 4, limit: int = 200, target: str | None = None) -> dict:
    """String literals in the decompiled sources."""
    session = resolve(target)
    partial = _require_export(session, "strings")
    matcher = re.compile(pattern) if pattern else None
    seen: dict[str, dict] = {}
    total = 0
    for path, text in _walk_sources(session):
        for number, line in enumerate(text.splitlines(), 1):
            for literal in STRING_LITERAL.findall(line):
                if len(literal) < min_len or (matcher and not matcher.search(literal)):
                    continue
                total += 1
                if literal not in seen and len(seen) < limit:
                    seen[literal] = {
                        "value": literal,
                        "file": str(path.relative_to(session.src)),
                        "line": number,
                    }
    return _tag(session, {
        "strings": list(seen.values()), "returned": len(seen),
        "occurrences": total, "truncated": total > len(seen),
        "partial": partial,
    })


# --------------------------------------------------------------------------
# resources and manifest
# --------------------------------------------------------------------------


def _manifest_path(session: Session) -> Path:
    path = session.res / "AndroidManifest.xml"
    if not path.is_file():
        raise JadxRpcError("no AndroidManifest.xml, this target is not an Android application")
    return path


def manifest(target: str | None = None) -> dict:
    """The decoded AndroidManifest.xml as text."""
    session = resolve(target)
    path = _manifest_path(session)
    return {"path": "AndroidManifest.xml", "xml": path.read_text(encoding="utf-8", errors="replace")}


def entrypoints(target: str | None = None) -> dict:
    """Components an attacker or a caller can reach, from AndroidManifest.xml."""
    session = resolve(target)
    root = ET.parse(_manifest_path(session)).getroot()
    package = root.get("package", "")

    target_sdk = None
    sdk = root.find("uses-sdk")
    if sdk is not None:
        target_sdk = sdk.get(ANDROID_NS + "targetSdkVersion")

    def qualify(name: str | None) -> str:
        if not name:
            return ""
        if name.startswith("."):
            return package + name
        return name if "." in name else f"{package}.{name}"

    components: list[dict] = []
    application = root.find("application")
    if application is not None:
        for tag in COMPONENT_TAGS:
            for element in application.findall(tag):
                filters = []
                for filter_element in element.findall("intent-filter"):
                    filters.append({
                        "actions": [a.get(ANDROID_NS + "name") for a in filter_element.findall("action")],
                        "categories": [c.get(ANDROID_NS + "name") for c in filter_element.findall("category")],
                        "data": [
                            {k.split("}")[-1]: v for k, v in d.attrib.items()}
                            for d in filter_element.findall("data")
                        ],
                    })
                declared = element.get(ANDROID_NS + "exported")
                components.append({
                    "kind": tag,
                    "name": qualify(element.get(ANDROID_NS + "name")),
                    "exported": {"true": True, "false": False}.get(declared),
                    # Components with no explicit android:exported default to
                    # exported when they carry an intent filter. Applications
                    # targeting API 31 and up must declare it, so this only
                    # fills a gap on older targets.
                    "exported_effective": {"true": True, "false": False}.get(declared, bool(filters)),
                    "permission": element.get(ANDROID_NS + "permission"),
                    "intent_filters": filters,
                })

    return {
        "package": package,
        "target_sdk": target_sdk,
        "application": qualify(application.get(ANDROID_NS + "name")) if application is not None else None,
        "debuggable": application.get(ANDROID_NS + "debuggable") == "true" if application is not None else None,
        "uses_permissions": [p.get(ANDROID_NS + "name") for p in root.findall("uses-permission")],
        "declared_permissions": [p.get(ANDROID_NS + "name") for p in root.findall("permission")],
        "components": components,
        "exported_count": sum(1 for c in components if c["exported_effective"]),
    }


def resources(pattern: str | None = None, *, limit: int = 200, target: str | None = None) -> dict:
    """List decoded resource files. Pattern is a case insensitive regex over the path."""
    session = resolve(target)
    if not session.res.is_dir():
        raise JadxRpcError("no resources decoded for this target")
    matcher = re.compile(pattern, re.IGNORECASE) if pattern else None
    out = []
    total = 0
    for path in sorted(session.res.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(session.res))
        if matcher and not matcher.search(rel):
            continue
        total += 1
        if len(out) < limit:
            out.append({"path": rel, "size": path.stat().st_size})
    return {"resources": out, "returned": len(out), "matched": total, "truncated": total > len(out)}


def resource(path: str, target: str | None = None) -> dict:
    """Read one decoded resource, by its path relative to the resource root."""
    session = resolve(target)
    full = (session.res / path).resolve()
    if not str(full).startswith(str(session.res.resolve())):
        raise JadxRpcError("path escapes the resource directory")
    if not full.is_file():
        raise JadxRpcError(f"resource not found: {path}")
    data = full.read_bytes()
    try:
        return {"path": path, "size": len(data), "text": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"path": path, "size": len(data), "binary": True,
                "note": f"binary resource, read it from {full}"}


# --------------------------------------------------------------------------
# call graph
# --------------------------------------------------------------------------


def _callgraph(session: Session) -> tuple[dict, list]:
    if not session.callgraph_path.is_file():
        _require_export(session, "the call graph")
        raise JadxRpcError("call graph missing, rerun: jadx-rpc export")
    data = json.loads(session.callgraph_path.read_text(encoding="utf-8"))
    return {n["id"]: n["method"] for n in data["nodes"]}, data["edges"]


def _match_nodes(nodes: dict, query: str) -> list[int]:
    wanted = _normalize_symbol(query)
    exact = [i for i, m in nodes.items() if m == wanted]
    if exact:
        return exact
    prefix = [i for i, m in nodes.items() if m.startswith(wanted + "(")]
    if prefix:
        return prefix
    return [i for i, m in nodes.items() if wanted in m]


def _related(target: str | None, query: str, direction: str) -> dict:
    session = resolve(target)
    nodes, edges = _callgraph(session)
    matched = _match_nodes(nodes, query)
    if not matched:
        raise JadxRpcError(f"no method in the call graph matches {query!r}")
    wanted = set(matched)
    out = []
    for edge in edges:
        if direction == "callers" and edge["to"] in wanted:
            out.append({"method": nodes.get(edge["from"]), "of": nodes.get(edge["to"]), "resolved": edge["resolved"]})
        elif direction == "callees" and edge["from"] in wanted:
            out.append({"method": nodes.get(edge["to"]), "of": nodes.get(edge["from"]), "resolved": edge["resolved"]})
    return _tag(session, {
        "query": query,
        "matched_methods": [nodes[i] for i in matched],
        direction: out,
        "count": len(out),
    })


def callers(method: str, target: str | None = None) -> dict:
    """Methods that call this one, from the call graph. Needs the export."""
    return _related(target, method, "callers")


def callees(method: str, target: str | None = None) -> dict:
    """Methods this one calls, from the call graph. Needs the export."""
    return _related(target, method, "callees")


# --------------------------------------------------------------------------
# renames
# --------------------------------------------------------------------------


def rename(kind: str, symbol: str, new_name: str, target: str | None = None) -> dict:
    """Record a rename. Applied on the next reload, not immediately.

    One rename costs a full re-decompile, so they are batched. Targets use the
    raw names that members and callers report:

        class   com.foo.Bar                          -> new fully qualified name
        method  com.foo.Bar.baz(Ljava/lang/String;)V -> new simple name
        field   com.foo.Bar.salt:Ljava/lang/String;  -> new simple name
    """
    session = resolve(target)
    entries = session.load_renames()
    symbol = _normalize_symbol(symbol)

    if kind == "class":
        _find_class(session.load_index(), symbol)
        entry = entries.setdefault(symbol.replace(".", "/"), mappings.ClassEntry())
        entry.new_name = new_name.replace(".", "/")
    elif kind == "method":
        if "(" not in symbol:
            raise JadxRpcError(
                "method renames need a JVM descriptor, for example "
                "com.foo.Bar.baz(Ljava/lang/String;)V. Get it from: jadx-rpc members <class>"
            )
        head, _, descriptor = symbol.partition("(")
        owner, _, name = head.rpartition(".")
        if not owner or not name:
            raise JadxRpcError(f"cannot split class and method out of {symbol!r}")
        entries.setdefault(owner.replace(".", "/"), mappings.ClassEntry()).methods[(name, "(" + descriptor)] = new_name
    elif kind == "field":
        if ":" not in symbol:
            raise JadxRpcError(
                "field renames need a JVM descriptor, for example "
                "com.foo.Bar.salt:Ljava/lang/String;"
            )
        head, _, descriptor = symbol.partition(":")
        owner, _, name = head.rpartition(".")
        if not owner or not name:
            raise JadxRpcError(f"cannot split class and field out of {symbol!r}")
        entries.setdefault(owner.replace(".", "/"), mappings.ClassEntry()).fields[(name, descriptor)] = new_name
    else:
        raise JadxRpcError(f"unknown rename kind {kind!r}, expected class, method or field")

    mappings.dump(entries, session.renames_path)
    return {
        "kind": kind, "target": symbol, "new_name": new_name,
        "pending": session.pending_renames(),
        "note": "run reload to apply pending renames to the decompiled output",
    }


def list_renames(target: str | None = None) -> dict:
    """Every recorded rename and how many are waiting for a reload."""
    session = resolve(target)
    entries = session.load_renames()
    return {
        "renames": mappings.as_list(entries),
        "total": mappings.count(entries),
        "pending": session.pending_renames(),
        "mapping_file": str(session.renames_path),
    }


def reload(target: str | None = None, *, background: bool = True) -> dict:
    """Rebuild with the recorded renames applied."""
    session = resolve(target)
    meta = session.read_meta()
    had_export = session.src.is_dir()

    started = time.time()
    shutil.rmtree(session.cache, ignore_errors=True)
    _index_pass(session, meta)
    meta["renames_applied"] = mappings.count(session.load_renames())
    session.write_meta(meta)

    result = {
        "id": session.dir.name,
        "renames_applied": meta["renames_applied"],
        "index_elapsed_s": round(time.time() - started, 1),
        "export": "none",
    }
    if had_export:
        result["export"] = start_export(session.dir.name, background=background)["state"]
    else:
        result["note"] = "sources were not exported, only the index was rebuilt"
    return result


# --------------------------------------------------------------------------
# background export worker
# --------------------------------------------------------------------------


def _export_worker_run(session: Session) -> dict:
    marker = session.dir / "export.json"
    meta = session.read_meta()
    started = time.time()
    _write_json(marker, {"state": "running", "pid": os.getpid(), "started_at": int(started)})
    try:
        _export_pass(session, meta)
    except Exception as exc:  # noqa: BLE001 - recorded so status can report it
        _write_json(marker, {"state": "failed", "error": str(exc)})
        raise
    state = {"state": "ready", "elapsed_s": round(time.time() - started, 1)}
    _write_json(marker, state)
    meta["renames_applied"] = mappings.count(session.load_renames())
    session.write_meta(meta)
    return state


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--export-worker":
        try:
            _export_worker_run(Session(Path(sys.argv[2])))
        except Exception:  # noqa: BLE001 - already recorded in export.json
            raise SystemExit(1) from None
        raise SystemExit(0)
    raise SystemExit("jadx_rpc.core is not a command, use jadx-rpc")
