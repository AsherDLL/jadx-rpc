"""Enigma mapping serialisation. Needs no jadx."""

from __future__ import annotations

from jadx_rpc import mappings


def test_round_trip(tmp_path):
    mappings.demo()


def test_class_line_without_a_new_name(tmp_path):
    path = tmp_path / "renames.mapping"
    entries = {"com/foo/Bar": mappings.ClassEntry()}
    entries["com/foo/Bar"].methods[("a", "()V")] = "start"
    mappings.dump(entries, path)
    # jadx ignores member lines that are not under a CLASS line, and it
    # ignores METHOD lines with no descriptor, so both have to be written.
    assert path.read_text() == "CLASS com/foo/Bar\n\tMETHOD a start ()V\n"


def test_empty_entries_are_dropped(tmp_path):
    path = tmp_path / "renames.mapping"
    mappings.dump({"com/foo/Bar": mappings.ClassEntry()}, path)
    assert path.read_text() == ""
    assert mappings.load(path) == {}


def test_load_of_a_missing_file_is_empty(tmp_path):
    assert mappings.load(tmp_path / "absent.mapping") == {}


def test_rewriting_the_same_symbol_replaces_it(tmp_path):
    path = tmp_path / "renames.mapping"
    entries: dict[str, mappings.ClassEntry] = {}
    entries.setdefault("com/foo/Bar", mappings.ClassEntry()).methods[("a", "()V")] = "start"
    entries["com/foo/Bar"].methods[("a", "()V")] = "begin"
    mappings.dump(entries, path)
    assert mappings.count(mappings.load(path)) == 1
    assert "begin" in path.read_text()


def test_overloads_are_kept_apart(tmp_path):
    path = tmp_path / "renames.mapping"
    entries: dict[str, mappings.ClassEntry] = {}
    entry = entries.setdefault("com/foo/Bar", mappings.ClassEntry())
    entry.methods[("a", "()V")] = "start"
    entry.methods[("a", "(I)V")] = "startWith"
    mappings.dump(entries, path)
    assert mappings.count(mappings.load(path)) == 2
