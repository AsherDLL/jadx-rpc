# jadx-rpc for agents

Command reference and a working method for reverse engineering an Android
application with jadx-rpc. Every command prints one JSON object to stdout,
`{"ok": true, "result": ...}` or `{"ok": false, "error": ...}`, and exits 0 or 1.

## Cost model

Read this before choosing commands. Two of the passes are cheap and one is not.

| Operation | Cost on a 12 MB APK | Notes |
|---|---|---|
| `open` | 12 s | index plus decoded resources, runs once |
| `classes`, `symbols`, `members`, `entrypoints`, `manifest`, `resources` | under 0.3 s | served from the index and the decoded resources |
| `class` before the export | about 5 s the first time, then instant | decompiles that one class and caches it |
| `export` | about 3 minutes, 223 MB | decompiles all 10761 classes, needed by `search`, `strings`, `callers`, `callees` |
| `class` after the export | instant | read from the export |

The practical consequence: triage with `entrypoints`, `classes`, `symbols` and
`members` costs nothing, and reading a handful of classes costs seconds. Start
the export early with `open --export` if you expect to search, because it runs
in the background while you work.

## Triage order that works

1. `jadx-rpc open app.apk --export`

   Builds the index and decodes resources, then starts the full decompile in
   the background. You can work immediately.

2. `jadx-rpc entrypoints`

   Every activity, service, receiver and provider, each with its `exported`
   flag, required permission and intent filters, plus the declared and
   requested permissions. Exported components with no permission are the
   reachable attack surface. Start there.

3. `jadx-rpc symbols 'crypt|token|secret|password|admin'`

   Searches class, method and field names in the index. Needs no export, so it
   works within a second of opening the target. Use this before `search`.
   Scoped to the app's own package by default; read `hidden_by_scope` before
   deciding whether the answer is "the app does not do this" or "I need to look
   at the libraries too".

4. `jadx-rpc members <class>`

   Methods and fields of one class, with the exact strings `rename` expects.

5. `jadx-rpc class <class> --lines 40:180`

   Decompiled source. Read a range rather than the whole file when the class is
   large, the result reports `line_count` so you know what you are slicing.

6. `jadx-rpc status`

   Check whether the export finished. Once `export.state` is `ready`, the
   commands below are available.

7. `jadx-rpc search 'javax\.crypto|Runtime\.getRuntime' --context 2`

   Regex over every decompiled source file.

8. `jadx-rpc callers <method>` and `jadx-rpc callees <method>`

   Resolved call edges from jadx's own call graph, not a text match. Pass the
   method as `com.foo.Bar.baz` or with its descriptor when overloads exist.

## Commands

### Session

    jadx-rpc open <input> [--export] [--deobf] [--threads N] [--force]
    jadx-rpc status
    jadx-rpc list
    jadx-rpc export [--wait]
    jadx-rpc close [--purge]

`open` accepts apk, dex, jar, aar, aab, xapk and apkm. Reopening an unchanged
target returns immediately with `"reused": true`. `--deobf` lets jadx invent
names for obfuscated symbols. `close` frees the decompiled sources and keeps the
index, `close --purge` deletes the session.

Which session a command acts on is decided in this order: the `--target` flag,
the `JADX_RPC_TARGET` environment variable, then the only open session if there
is exactly one. With several sessions open and no target given, the command
fails and lists the candidates.

### Code

    jadx-rpc classes [pattern] [--scope app|all] [--limit N]
    jadx-rpc members <class>
    jadx-rpc class <class> [--lines A:B]
    jadx-rpc symbols <pattern> [--kind class|method|field] [--scope app|all] [--limit N]
    jadx-rpc search <pattern> [--scope app|all] [--limit N] [--context N]
    jadx-rpc strings [pattern] [--scope app|all] [--min-len N] [--limit N]

`classes` and `symbols` read the index and need no export. `search` and
`strings` read the decompiled sources and require it. Patterns are Python
regular expressions, case insensitive for `classes` and `symbols`, case
sensitive for `search` and `strings`.

**Scope, and why it defaults to the app.** 93 percent of the classes in a real
APK are bundled libraries. On one measured sample `symbols 'crypt|token|secret'`
returns 2030 hits and not one of them is in the application's own code. So these
four commands default to `--scope app`, meaning the package declared in the
manifest.

Nothing is hidden without being counted. A scoped result carries
`matched_all_scopes` and `hidden_by_scope`, so you always know what you did not
see and can widen with `--scope all`. Two cases worth recognising:

- `matched: 0` with a large `hidden_by_scope` is a real answer. It means the
  behaviour you searched for is in a bundled library and not in the app's own
  code. Report that, do not immediately rerun with `--scope all` and report the
  library hits as if they were the app's.
- `scope: all` with a `scope_note` means there was no manifest to scope against,
  which is every JAR and bare DEX. Nothing was filtered.

Results that hit their limit report `"truncated": true` along with `matched`,
the number of results that existed. Never read a truncated result as a complete
answer, either raise `--limit` or narrow the pattern.

### Android

    jadx-rpc manifest
    jadx-rpc entrypoints
    jadx-rpc resources [pattern] [--limit N]
    jadx-rpc resource <path>

`entrypoints` reports `exported` as the manifest declares it, which is null when
the attribute is absent, and `exported_effective`, which fills that gap with the
platform default: a component with an intent filter and no explicit attribute is
exported. Applications targeting API 31 and up must declare the attribute, so
check `target_sdk` before relying on the derived value.

### Call graph

    jadx-rpc callers <method>
    jadx-rpc callees <method>

Both require the export, and both require jadx 1.5.6 or newer. On an older jadx
they fail saying so, and every other command still works. `jadx-rpc status`
reports `jadx_version` and `callgraph_supported` if you want to check first.

The `resolved` field on each edge is jadx's own confidence that the call target
was determined statically.

### Renames

    jadx-rpc rename class  com.foo.Bar                          com.foo.Cipher
    jadx-rpc rename method 'com.foo.Bar.baz(Ljava/lang/String;)V' decrypt
    jadx-rpc rename field  'com.foo.Bar.salt:Ljava/lang/String;'  key
    jadx-rpc renames
    jadx-rpc reload [--wait]

Renames are recorded in an Enigma mapping file and applied on the next `reload`,
which re-runs the decompile. That is deliberate. Applying one rename costs a
full re-decompile, so a loop that reloads after every rename would spend all its
time in jadx. Record everything you learned about a class, then reload once.

While renames are pending, every read command returns `"stale": true`, which
means the source you are reading predates your own renames.

Targets always use the original obfuscated names, on both sides of a rename and
before and after a reload. `members` prints the exact string to pass as
`rename_target` for the class and for each method. Method and field renames need
the JVM descriptor, `members` supplies it for methods, and for fields you build
it from the declaration in the decompiled source.

## Things that will bite you

- `search`, `strings`, `callers` and `callees` fail with a clear error until the
  export is finished, including while it is part way through. That is not a bug
  in your command. A count taken mid-export looks like an answer but is only a
  floor, and scoped it can read as zero app hits purely because those files are
  not decompiled yet, so the result is withheld rather than shown with a caveat
  you might miss. Poll `status`, or use `symbols`, which needs no export.
- Inner classes live in their parent's source file. Asking for
  `com.foo.Outer.Inner` returns the source of `com.foo.Outer` with a note
  saying so.
- A class name can be given by its display name or its original obfuscated
  name, both resolve to the same class.
- jadx fails to decompile a small fraction of classes in most real
  applications. That is normal, the rest of the output is unaffected.
