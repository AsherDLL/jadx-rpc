"""Read and write the Enigma mapping file that jadx consumes via --mappings-path.

Format, tab indented, obfuscated name always on the left:

    CLASS com/foo/Bar com/foo/Cipher
    	METHOD baz encrypt (Ljava/lang/String;)V
    	FIELD salt pepper Ljava/lang/String;

The class line may carry no new name, which is how a member gets renamed
without renaming its class. Descriptors are mandatory on METHOD and FIELD
lines, jadx silently ignores lines that lack one.
"""

from __future__ import annotations

from pathlib import Path


class ClassEntry:
    __slots__ = ("new_name", "methods", "fields")

    def __init__(self, new_name: str | None = None):
        self.new_name = new_name
        self.methods: dict[tuple[str, str], str] = {}
        self.fields: dict[tuple[str, str], str] = {}

    def is_empty(self) -> bool:
        return self.new_name is None and not self.methods and not self.fields


def load(path: Path) -> dict[str, ClassEntry]:
    entries: dict[str, ClassEntry] = {}
    if not path.exists():
        return entries
    current: ClassEntry | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        parts = raw.strip().split()
        kind = parts[0]
        if kind == "CLASS":
            current = entries.setdefault(parts[1], ClassEntry())
            if len(parts) > 2:
                current.new_name = parts[2]
        elif kind == "METHOD" and current is not None and len(parts) == 4:
            current.methods[(parts[1], parts[3])] = parts[2]
        elif kind == "FIELD" and current is not None and len(parts) == 4:
            current.fields[(parts[1], parts[3])] = parts[2]
    return entries


def dump(entries: dict[str, ClassEntry], path: Path) -> None:
    lines: list[str] = []
    for cls in sorted(entries):
        entry = entries[cls]
        if entry.is_empty():
            continue
        lines.append(f"CLASS {cls} {entry.new_name}" if entry.new_name else f"CLASS {cls}")
        for (name, desc), new in sorted(entry.methods.items()):
            lines.append(f"\tMETHOD {name} {new} {desc}")
        for (name, desc), new in sorted(entry.fields.items()):
            lines.append(f"\tFIELD {name} {new} {desc}")
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def count(entries: dict[str, ClassEntry]) -> int:
    total = 0
    for entry in entries.values():
        total += 1 if entry.new_name else 0
        total += len(entry.methods) + len(entry.fields)
    return total


def as_list(entries: dict[str, ClassEntry]) -> list[dict]:
    out: list[dict] = []
    for cls in sorted(entries):
        entry = entries[cls]
        dotted = cls.replace("/", ".")
        if entry.new_name:
            out.append({"kind": "class", "target": dotted, "new_name": entry.new_name.replace("/", ".")})
        for (name, desc), new in sorted(entry.methods.items()):
            out.append({"kind": "method", "target": f"{dotted}.{name}{desc}", "new_name": new})
        for (name, desc), new in sorted(entry.fields.items()):
            out.append({"kind": "field", "target": f"{dotted}.{name}:{desc}", "new_name": new})
    return out


def demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "renames.mapping"
        entries: dict[str, ClassEntry] = {}
        cls = entries.setdefault("com/foo/Bar", ClassEntry())
        cls.new_name = "com/foo/Cipher"
        cls.methods[("baz", "(Ljava/lang/String;)V")] = "encrypt"
        cls.fields[("salt", "Ljava/lang/String;")] = "pepper"
        entries.setdefault("com/foo/Only", ClassEntry()).methods[("a", "()V")] = "start"
        dump(entries, path)

        text = path.read_text()
        assert "CLASS com/foo/Bar com/foo/Cipher" in text
        assert "\tMETHOD baz encrypt (Ljava/lang/String;)V" in text
        assert "\tFIELD salt pepper Ljava/lang/String;" in text
        assert "CLASS com/foo/Only\n" in text, "member-only rename keeps a bare class line"

        back = load(path)
        assert back["com/foo/Bar"].new_name == "com/foo/Cipher"
        assert back["com/foo/Bar"].methods[("baz", "(Ljava/lang/String;)V")] == "encrypt"
        assert back["com/foo/Bar"].fields[("salt", "Ljava/lang/String;")] == "pepper"
        assert back["com/foo/Only"].new_name is None
        assert count(back) == 4
        assert as_list(back)[0] == {"kind": "class", "target": "com.foo.Bar", "new_name": "com.foo.Cipher"}
    print("mappings demo ok")


if __name__ == "__main__":
    demo()
