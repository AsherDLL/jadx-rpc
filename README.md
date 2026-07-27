# jadx-rpc

Headless [jadx](https://github.com/skylot/jadx) sessions for LLM agents.

jadx has two shapes today and neither suits an autonomous agent. The GUI is
built for a human reading code on a screen, and the plugins that expose it to a
model need that GUI running. The command line tool is a batch decompiler with no
memory between runs, so an agent that asks twenty questions pays the full
parsing cost twenty times.

jadx-rpc adds the missing piece, a session. Opening a target does the expensive
work once and writes it to disk. Everything after that is a file read, a regex
scan or a single class decompile, and it answers in milliseconds. It drives the
real jadx command line, so it works anywhere jadx works and never needs a
display.

## How it works

There is no daemon and no socket. Ghidra needs one because its analysis only
exists inside a live JVM and dies with it. jadx does not have that constraint,
its output is plain files, so the session is a directory:

    ~/.local/state/jadx-rpc/<hash>/
      session.json      what was opened and how
      mapping.json      every class, method and field, original and displayed names
      res/              decoded resources, including AndroidManifest.xml
      src/              decompiled Java, written by the optional full export
      callgraph.json    resolved call edges
      cache/            classes decompiled on demand
      renames.mapping   recorded renames in Enigma format

Nothing runs between commands. Two agents can query the same session at once
because they are only reading files, and a session survives a reboot.

## What it costs

Measured on a 12 MB APK containing 10761 classes, with jadx 1.5.6 on an eight
core desktop. Your numbers will scale with the size of the application.

| Command | Time | What it does |
|---|---|---|
| `open` | 12 s | indexes 19705 classes and 117352 methods, decodes every resource |
| `classes`, `symbols`, `members` | 0.25 s | reads the index |
| `entrypoints`, `manifest`, `resources` | 0.25 s | reads the decoded resources |
| `class` | 5 s first time, then instant | decompiles one class and caches it |
| `export` | 175 s, 223 MB | decompiles all 10761 classes, plus the call graph |
| `search`, `strings`, `callers`, `callees` | seconds | needs the export |

The split is the point. Triage, symbol lookup and reading individual classes are
all cheap, and the one expensive operation is opt in and runs in the background.

## Install

### jadx

jadx-rpc calls the `jadx` launcher, so install jadx first.

    # any platform, from the release zip
    curl -LO https://github.com/skylot/jadx/releases/download/v1.5.6/jadx-1.5.6.zip
    mkdir -p /opt/jadx && unzip jadx-1.5.6.zip -d /opt/jadx
    ln -s /opt/jadx/bin/jadx /opt/jadx/bin/jadx-gui ~/.local/bin/

    # or from a package manager
    brew install jadx                                  # macOS
    sudo pacman -S jadx                                # Arch
    flatpak install flathub com.github.skylot.jadx     # Flathub

jadx needs a 64 bit Java 11 or later. The zip ships both `jadx` and `jadx-gui`.
jadx-rpc only uses `jadx`, the GUI is there when you want to look at the same
target yourself.

    jadx --version    # expect 1.5.6 or newer

If `jadx` is not on PATH, point `JADX_BIN` at the launcher.

### jadx-rpc

    uv tool install git+https://github.com/AsherDLL/jadx-rpc

    # or
    pipx install git+https://github.com/AsherDLL/jadx-rpc

    # or with the MCP server included
    uv tool install "jadx-rpc[mcp] @ git+https://github.com/AsherDLL/jadx-rpc"

Python 3.10 or later. The base install has no dependencies at all, everything it
needs is in the standard library.

    jadx-rpc list     # expect {"ok": true, "result": {"sessions": [], "count": 0}}

Output is indented by default. The examples below use `--compact`, which prints
one line, because that is easier to read in a page and easier to pipe.

## Use

    $ jadx-rpc open app.apk --export
    {"ok": true, "result": {"id": "1781c62d0f3b", "classes": 19705, "methods": 117352,
                            "elapsed_s": 12.6, "export": "running"}}

`--export` starts the full decompile in the background. You do not have to wait
for it, the commands below work immediately.

    $ jadx-rpc entrypoints
    {"ok": true, "result": {"package": "org.fdroid.fdroid", "target_sdk": "30",
                            "components": [...], "exported_count": 10, ...}}

    $ jadx-rpc symbols 'crypt|token|secret'
    $ jadx-rpc members org.fdroid.fdroid.FDroidApp
    $ jadx-rpc class org.fdroid.fdroid.FDroidApp --lines 1:80

Once `jadx-rpc status` reports the export ready:

    $ jadx-rpc search 'javax\.crypto' --context 2
    $ jadx-rpc strings '^https://'
    $ jadx-rpc callers 'org.fdroid.fdroid.FDroidApp.onCreate'

Renaming obfuscated symbols, recorded now and applied in one pass later:

    $ jadx-rpc rename class com.a.b.c com.example.PaymentHandler
    $ jadx-rpc rename method 'com.a.b.c.d(Ljava/lang/String;)V' verifySignature
    $ jadx-rpc renames
    $ jadx-rpc reload

`AGENTS.md` has the full command reference, the cost of each command and a
triage order that works. It is written to be read by a model, so point your
agent at it.

## Wiring it to an LLM

Three ways into the same functions. Pick whichever fits your stack.

### Shell agents

Claude Code, Codex, Cursor, pi and anything else that can run a command. There
is nothing to configure, the tool prints JSON. Give the agent the reference:

    Read AGENTS.md in jadx-rpc, then triage app.apk and report every exported
    component that reaches a crypto call.

### MCP

    claude mcp add jadx-rpc -- jadx-rpc mcp

or in a client configuration file:

    {
      "mcpServers": {
        "jadx-rpc": {
          "command": "jadx-rpc",
          "args": ["mcp"],
          "env": {"JADX_RPC_TARGET": "/abs/path/to/app.apk"}
        }
      }
    }

Twenty tools, one per command. `jadx-rpc mcp --list-tools` prints them without
starting a server, and works on the base install. Serving needs the extra,
`pip install "jadx-rpc[mcp]"`.

### Python, for ADK, LangChain and friends

Every command is an importable function that returns a dict and raises
`JadxRpcError` on failure, so a framework can register it directly with no
subprocess and no MCP hop.

    from google.adk.agents import LlmAgent
    import jadx_rpc

    jadx_rpc.open_target("/abs/path/to/app.apk", export=True)

    agent = LlmAgent(
        name="apk_triage",
        model="gemini-2.0-flash",
        instruction=open("AGENTS.md").read(),
        tools=[
            jadx_rpc.entrypoints,
            jadx_rpc.classes,
            jadx_rpc.symbols,
            jadx_rpc.members,
            jadx_rpc.decompile_class,
            jadx_rpc.search,
            jadx_rpc.callers,
        ],
    )

The docstrings and type hints are the tool schemas, so nothing needs describing
twice.

## Environment

| Variable | Effect |
|---|---|
| `JADX_BIN` | path to the jadx launcher, when it is not on PATH |
| `JADX_RPC_TARGET` | session every command acts on, a path or a session id |
| `JADX_RPC_STATE_DIR` | where sessions live, for sandboxes and CI |

With exactly one session open, `JADX_RPC_TARGET` is optional. With several open
and none named, commands fail and list the candidates rather than guessing.

## Limits worth knowing

- The index pass writes and then deletes a set of intermediate files roughly the
  size of the decompiled application. Opening a very large APK needs transient
  disk to match.
- Field renames need a JVM descriptor, which the index does not carry. Build it
  from the declaration in the decompiled source. Class and method renames need
  nothing extra, `members` prints the exact string to pass back.
- jadx fails to decompile a small fraction of classes in most real applications.
  That is jadx behaving normally and the rest of the output is unaffected.
- Only local files are read. jadx-rpc does not fetch, upload or phone anywhere.

## Development

    git clone https://github.com/AsherDLL/jadx-rpc && cd jadx-rpc
    uv venv && uv pip install -e . pytest
    uv run pytest

The suite builds its own test jar with `javac` and runs real jadx against it, so
it needs no network and no Android SDK. Tests skip cleanly when `jadx` or
`javac` is missing.

## License

GPL-3.0-or-later. See `LICENSE`.

jadx itself is a separate project under Apache-2.0. jadx-rpc runs it as an
external program and does not include or link any of its code.
