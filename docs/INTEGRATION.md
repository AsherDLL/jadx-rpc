# Integrating jadx-rpc

For engineers wiring the engine into an agent harness. If you are teaching a
model to drive it directly, `AGENTS.md` is the document you want.

## What it needs

| Requirement | Detail |
|---|---|
| jadx on `PATH`, or `JADX_BIN` | 1.5.1 or newer. `callers` and `callees` need 1.5.6, everything else works on 1.5.1 |
| Python 3.10+ | no runtime dependencies beyond the standard library |
| A writable state directory | `JADX_RPC_STATE_DIR`, or `$XDG_STATE_HOME`, or `~/.local/state` |
| Scratch space for `open` | roughly 25x the input size, transiently. See "Disk" below |
| Heap | jadx peaked at 5.9 GB on a 12 MB APK unconstrained. Set `-Xmx` if you cap memory |

Nothing listens on a port and nothing runs between commands. A session is a
directory of files, so several processes can read one concurrently and a session
survives a restart.

## What it guarantees

These are tested, not aspirational. Build against them.

**Argv only.** No command needs a shell, a pipe or a here-doc. Every argument is
a single argv token, including regex patterns. You can exec it directly.

**One JSON object on stdout, always.** Success is
`{"ok": true, "result": {...}}` with exit 0. Failure is
`{"ok": false, "error": "..."}` with exit 1. There is no third shape: an
unexpected exception is still reported inside the envelope. A closed pipe exits
quietly rather than printing a traceback.

**Addressable per call.** `--target <path-or-session-id>` on any command selects
the session, so a caller never depends on ambient state. `JADX_RPC_TARGET` and
"the only open session" exist for interactive use; do not rely on either from a
harness.

**Bounded output.** Every command that can return a list takes `--limit` and
reports `matched`, `returned` and `truncated`. Nothing streams unboundedly, which
matters when the caller has no way to truncate before the result reaches a
context window.

**Nothing hidden silently.** When `--scope app` filters library code, the result
carries `matched_all_scopes` and `hidden_by_scope`. When renames are pending,
reads carry `stale: true`. When a result would be incomplete for a reason the
caller cannot see, such as a half-finished export, the command fails instead of
answering.

## The call shape

```
jadx-rpc [--compact] [--target <path|id>] <subcommand> [args] [--limit N]
```

`--compact` prints one line instead of indented JSON. Use it; it is easier to
parse and cheaper to pass around.

```bash
jadx-rpc --compact --target /samples/app.apk open
jadx-rpc --compact --target /samples/app.apk entrypoints
jadx-rpc --compact --target /samples/app.apk class com.example.Foo --lines 1:200
jadx-rpc --compact --target /samples/app.apk callers 'com.example.Foo.decrypt'
```

Mapping that onto a harness tool table is one line per tool. A subcommand, the
parameters the model supplies, and whatever output bound you enforce:

```python
TOOLS = [
    ("app_manifest",  "entrypoints", ()),
    ("app_classes",   "classes",     ("pattern",)),
    ("app_class",     "class",       ("class_name",)),
    ("app_search",    "search",      ("pattern",)),
    ("app_callers",   "callers",     ("method",)),
]

argv = ["jadx-rpc", "--compact", "--target", target, subcommand, *args]
```

Note what is absent: no path construction. You pass a class name and the engine
resolves it. A harness that builds `{out}/sources/{name}.java` from model output
has created a path traversal surface; this is the reason not to.

## Cost model

Measured on a 12 MB APK containing 10,761 classes, jadx 1.5.6. Decide from this
what to run eagerly.

| Command | Cost | Needs |
|---|---|---|
| `open` | 12 s | nothing |
| `classes`, `symbols`, `members` | 0.25 s | `open` |
| `entrypoints`, `manifest`, `resources` | 0.25 s | `open` |
| `class` | 5 s first time, then instant | `open` |
| `export` | 175 s, 223 MB | `open` |
| `search`, `strings`, `callers`, `callees` | under 1 s | `export` |

`open` runs two cheap jadx passes: resources, which decodes the manifest, and an
index of every class, method and field. `export` decompiles everything and is the
only expensive operation. It is opt-in (`open --export`, or `export` later) and
runs in the background, so the cheap commands stay available while it works.

Commands needing the export fail with a clear message until it reports `ready`,
and that includes while it is running or after it failed. A half-written export
would answer with a count that reads like a total but is a floor, which under
`--scope app` can look like a clean negative finding. Withholding is deliberate.
`symbols` answers name lookups without any export.

## Scope

93 percent of the classes in a real APK are bundled libraries. Measured on the
same sample: `symbols 'crypt|token|secret'` returns 2,030 hits unscoped, and
**zero** of them are under the application's own package.

`classes`, `symbols`, `search` and `strings` therefore default to `--scope app`,
derived from the manifest package cut to two segments, so `org.fdroid.fdroid`
scopes to `org.fdroid` and catches sibling packages the vendor owns. Pass
`--scope all` to widen. A target with no manifest, meaning every JAR and bare
DEX, falls back to `all` and says so in `scope_note`.

If your harness prefers to make that decision itself, pass `--scope all`
explicitly everywhere and filter on `raw_name`.

## Disk

`open`'s index pass writes one JSON file per class beside the `mapping.json` it
keeps, and there is no jadx flag to suppress them. Measured at 285 MB transient
for a 12 MB APK, so budget roughly 25x the input. They are deleted immediately
afterwards.

`open` refuses to start when the filesystem cannot hold that, naming the numbers
rather than filling the disk. `--index-tmp DIR` puts the scratch somewhere with
room, which is the flag to reach for when the state directory is on a small
volume.

The export itself is separate and persistent: 223 MB of sources for the same
sample. `close` frees it and keeps the index; `close --purge` removes everything.

## jadx versions

| Feature | Minimum |
|---|---|
| everything except the call graph | 1.5.1 |
| `callers`, `callees` | 1.5.6 |

The version is read once at `open` and recorded in the session. `status` reports
it as `jadx_version` and `callgraph_supported`. On an older jadx, `callers` and
`callees` fail naming the version required; nothing else is affected.

Two details worth knowing if you pin a version. jadx changed the exit code for a
partial decompile, where some classes fail but the rest are written: 1 up to
1.5.1, 3 from 1.5.6. jadx-rpc accepts both and verifies the expected artifact
exists, so a genuine argument error still fails loudly. And `--call-graph` does
not exist before 1.5.6, so it is only passed when the detected version supports
it; sending it to an older jadx would make it reject its arguments and do
nothing, costing the whole export rather than just the call graph.

## Concurrency and lifecycle

`open` on an unchanged target returns immediately with `"reused": true`, so it is
safe to call before every operation rather than tracking whether a target is
open.

The session directory is keyed by a hash of the absolute input path. Two
harnesses pointing at the same file share a session and its cached work. There is
no locking: two `open` calls racing on the same new target will duplicate work,
so serialise that if it matters to you.

`export` runs in a detached child. `status` reports `running`, `ready` or
`failed`, and detects a worker that died. Poll `status` rather than assuming a
duration.

## If you only need to read source

Be honest about whether you need this at all. jadx already writes decompiled
Java as ordinary files in a package tree, and `grep` and a file reader handle
that well. If reading and searching source is the whole job, run
`jadx -d out app.apk` and use your existing tools.

The engine earns its place for what those cannot do: the index and the call graph
arrive as a 35 MB JSON file and a node-and-edge id list, which no model can
consume directly, and which answer the questions grep cannot ("who calls this",
"what are this class's methods", "which of these names are obfuscated").
