"""End to end against a real jadx run on the fixture jar."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import jadx_rpc
from jadx_rpc import core
from jadx_rpc.core import JadxRpcError

from .conftest import needs_jadx, needs_javac, plant_manifest, run_cli

REPO = Path(__file__).parent.parent
VENDOR = "net.thirdparty.lib.Helper"

pytestmark = [needs_jadx, needs_javac]

CRYPTO = "com.example.app.Crypto"
ENCODE = "com.example.app.Crypto.encode(Ljava/lang/String;)Ljava/lang/String;"


def test_open_builds_an_index(state, fixture_jar):
    result = jadx_rpc.open_target(str(fixture_jar))
    assert result["reused"] is False
    assert result["classes"] >= 4
    assert result["methods"] >= 8
    assert result["export"] == "none", "the expensive pass must not run unless asked"

    again = jadx_rpc.open_target(str(fixture_jar))
    assert again["reused"] is True, "reopening an unchanged target must be free"


def test_open_rejects_a_missing_input(state):
    with pytest.raises(JadxRpcError, match="input not found"):
        jadx_rpc.open_target("/nonexistent/app.apk")


def test_classes_filters(opened):
    every = jadx_rpc.classes()
    names = {c["name"] for c in every["classes"]}
    assert CRYPTO in names
    assert "com.example.app.util.Hex" in names

    filtered = jadx_rpc.classes("hex")
    assert [c["name"] for c in filtered["classes"]] == ["com.example.app.util.Hex"]
    assert filtered["matched"] == 1


class TestScope:
    """Scoping hides bundled library code, and always says how much."""

    def test_without_a_manifest_nothing_is_scoped_away(self, opened):
        result = jadx_rpc.classes()
        assert result["scope"] == "all", "a jar has no manifest to scope against"
        assert "no AndroidManifest.xml" in result["scope_note"]
        assert "hidden_by_scope" not in result
        assert VENDOR in {c["name"] for c in result["classes"]}

    def test_classes_hides_another_vendor_and_reports_it(self, opened):
        plant_manifest()
        result = jadx_rpc.classes()
        names = {c["name"] for c in result["classes"]}
        assert VENDOR not in names
        assert "com.example.app.Crypto" in names
        assert result["app_package_prefix"] == "com.example"
        assert result["hidden_by_scope"] == 1
        assert result["matched_all_scopes"] == result["matched"] + 1
        assert "--scope all" in result["scope_note"]

    def test_scope_all_puts_it_back(self, opened):
        plant_manifest()
        result = jadx_rpc.classes(scope="all")
        assert VENDOR in {c["name"] for c in result["classes"]}
        assert "hidden_by_scope" not in result

    def test_a_prefix_must_end_on_a_package_boundary(self, opened):
        # com.exampleX is a different vendor from com.example and must not match.
        plant_manifest("com.exam.app")
        assert jadx_rpc.classes()["matched"] == 0
        assert jadx_rpc.classes()["hidden_by_scope"] == 5

    def test_symbols_scope_covers_members_of_a_hidden_class(self, opened):
        plant_manifest()
        result = jadx_rpc.symbols("describe")
        assert result["matched"] == 0, "a method of a hidden class is hidden with it"
        assert result["hidden_by_scope"] == 1
        assert jadx_rpc.symbols("describe", scope="all")["matched"] == 1

    def test_unknown_scope_is_rejected(self, opened):
        with pytest.raises(JadxRpcError, match="unknown scope"):
            jadx_rpc.classes(scope="everything")


class TestJadxVersion:
    """One engine has to work against whichever jadx a consumer pinned."""

    def test_the_version_is_read_and_recorded(self, state, fixture_jar):
        result = jadx_rpc.open_target(str(fixture_jar))
        assert re.match(r"\d+\.\d+\.\d+", result["jadx_version"]), result["jadx_version"]
        assert jadx_rpc.status()["jadx_version"] == result["jadx_version"]

    def test_partial_decompiles_are_accepted_on_either_jadx(self):
        # 1 up to jadx 1.5.1, 3 from 1.5.6, both meaning "finished with errors".
        assert 1 in core.JADX_OK and 3 in core.JADX_OK

    @pytest.mark.parametrize(
        ("version", "supported"),
        [("1.5.6", True), ("1.5.7", True), ("1.6.0", True), ("2.0.0", True),
         ("1.5.1", False), ("1.4.9", False), ("unknown", False), ("", False)],
    )
    def test_callgraph_support_is_decided_by_version(self, version, supported):
        assert core._supports_callgraph({"jadx_version": version}) is supported

    def test_an_old_jadx_names_the_version_callers_needs(self, opened):
        session = core.resolve(None)
        meta = session.read_meta()
        meta["jadx_version"] = "1.5.1"
        session.write_meta(meta)
        session.callgraph_path.unlink(missing_ok=True)
        with pytest.raises(JadxRpcError, match="need jadx 1.5.6 or newer"):
            jadx_rpc.callers("com.example.app.Crypto.encode")

    def test_a_pass_that_produced_nothing_fails_even_on_an_accepted_exit_code(self, opened):
        session = core.resolve(None)
        with pytest.raises(JadxRpcError, match="produced no"):
            core._run(session, "probe", ["true"], produces=session.dir / "never-written")


class TestIndexDiskGuard:
    def test_it_passes_when_there_is_room(self, tmp_path):
        core._check_index_space(tmp_path, 1024)  # 25 KB needed, no exception

    def test_it_refuses_and_names_the_numbers(self, tmp_path):
        huge = shutil.disk_usage(tmp_path).free  # 25x free space cannot fit
        with pytest.raises(JadxRpcError, match="not enough space"):
            core._check_index_space(tmp_path, huge)

    def test_index_tmp_redirects_the_scratch_directory(self, tmp_path):
        session = core.Session(tmp_path / "session")
        assert core._index_tmp(session, {}) == session.dir / "_index"
        redirected = core._index_tmp(session, {"index_tmp": str(tmp_path / "big")})
        assert redirected.parent == tmp_path / "big"
        assert session.dir.name in redirected.name, "sessions must not collide in a shared dir"


def test_classes_reports_truncation(opened):
    result = jadx_rpc.classes(limit=1)
    assert result["returned"] == 1
    assert result["truncated"] is True
    assert result["matched"] > 1


def test_members_carries_usable_rename_targets(opened):
    result = jadx_rpc.members(CRYPTO)
    assert result["rename_target"] == CRYPTO
    by_name = {m["name"]: m for m in result["methods"]}
    assert by_name["encode"]["rename_target"] == ENCODE
    assert by_name["encode"]["signature"] == "encode(Ljava/lang/String;)Ljava/lang/String;"
    assert {f["name"] for f in result["fields"]} == {"SECRET_KEY", "salt"}


def test_members_rejects_an_unknown_class(opened):
    with pytest.raises(JadxRpcError, match="class not found"):
        jadx_rpc.members("com.example.app.Nope")


def test_class_decompiles_on_demand_then_caches(opened):
    first = jadx_rpc.decompile_class(CRYPTO)
    assert first["origin"] == "on-demand"
    assert "class Crypto" in first["code"]
    assert first["line_count"] > 10

    second = jadx_rpc.decompile_class(CRYPTO)
    assert second["origin"] == "cache"


def test_class_slices_lines(opened):
    result = jadx_rpc.decompile_class(CRYPTO, lines="1:3")
    assert result["lines"] == "1:3"
    assert len(result["code"].splitlines()) == 3


def test_class_line_range_is_clamped_not_silently_empty(opened):
    total = jadx_rpc.decompile_class(CRYPTO)["line_count"]
    assert jadx_rpc.decompile_class(CRYPTO, lines="1:99999")["lines"] == f"1:{total}"
    assert jadx_rpc.decompile_class(CRYPTO, lines="5:2")["lines"] == "5:5"
    with pytest.raises(JadxRpcError, match="bad line range"):
        jadx_rpc.decompile_class(CRYPTO, lines="abc")


def test_inner_class_resolves_to_its_parent_file(opened):
    result = jadx_rpc.decompile_class("com.example.app.Main.Result")
    assert result["raw_name"] == "com.example.app.Main$Result"
    assert "Main.java" in result["path"]
    assert "note" in result


def test_symbols_needs_no_export(opened):
    result = jadx_rpc.symbols("hex", kind="method")
    names = {s["name"] for s in result["symbols"]}
    assert "com.example.app.util.Hex.toHex" in names
    assert "com.example.app.util.Hex.fromHex" in names


def test_search_and_strings_refuse_before_the_export(opened):
    with pytest.raises(JadxRpcError, match="every class decompiled"):
        jadx_rpc.search("SECRET")
    with pytest.raises(JadxRpcError, match="every class decompiled"):
        jadx_rpc.strings()


def test_callgraph_refuses_before_the_export(opened):
    with pytest.raises(JadxRpcError, match="every class decompiled"):
        jadx_rpc.callers(ENCODE)


class TestAfterExport:
    @pytest.fixture(autouse=True)
    def exported(self, opened):
        state = jadx_rpc.start_export(background=False)
        assert state["state"] == "ready"

    def test_status_reports_ready(self):
        result = jadx_rpc.status()
        assert result["export"]["state"] == "ready"
        assert result["sources_ready"] is True
        assert result["callgraph_ready"] is True
        assert result["pending_renames"] == 0

    def test_class_now_comes_from_the_export(self):
        assert jadx_rpc.decompile_class(CRYPTO)["origin"] == "export"

    def test_search_finds_source_text(self):
        result = jadx_rpc.search("hunter2", context=1)
        assert result["matched"] >= 1
        assert result["partial"] is False
        hit = result["hits"][0]
        assert hit["file"].endswith("Crypto.java")
        assert "hunter2" in hit["context"]

    def test_strings_extracts_literals(self):
        values = {s["value"] for s in jadx_rpc.strings()["strings"]}
        assert "hunter2-not-a-real-key" in values
        assert "https://example.invalid/api/v1/report" in values

    def test_strings_filters(self):
        result = jadx_rpc.strings("^https://")
        assert [s["value"] for s in result["strings"]] == ["https://example.invalid/api/v1/report"]

    def test_callers_resolves_real_edges(self):
        result = jadx_rpc.callers(ENCODE)
        callers = {c["method"] for c in result["callers"]}
        assert "com.example.app.Main.run(Lcom/example/app/Crypto;Ljava/lang/String;)Lcom/example/app/Main$Result;" in callers

    def test_callees_resolves_real_edges(self):
        result = jadx_rpc.callees("com.example.app.Crypto.encode")
        callees = {c["method"] for c in result["callees"]}
        assert any("Hex.toHex" in c for c in callees)
        assert any("Crypto.mix" in c for c in callees)

    def test_search_scope_hides_another_vendor_and_reports_it(self):
        plant_manifest()
        scoped = jadx_rpc.search("thirdparty-vendor-banner")
        assert scoped["matched"] == 0
        assert scoped["hidden_by_scope"] >= 1
        assert scoped["files_matched"] == 0

        everything = jadx_rpc.search("thirdparty-vendor-banner", scope="all")
        assert everything["matched"] >= 1
        assert everything["hits"][0]["file"].startswith("net/thirdparty/")

    def test_search_still_finds_app_code_while_scoped(self):
        plant_manifest()
        result = jadx_rpc.search("hunter2")
        assert result["matched"] >= 1
        assert result["hits"][0]["file"].startswith("com/example/")
        assert result["app_package_prefix"] == "com.example"

    def test_strings_scope_hides_another_vendor(self):
        plant_manifest()
        assert "thirdparty-vendor-banner" not in {
            s["value"] for s in jadx_rpc.strings()["strings"]
        }
        assert "thirdparty-vendor-banner" in {
            s["value"] for s in jadx_rpc.strings(scope="all")["strings"]
        }

    def test_callers_rejects_an_unknown_method(self):
        with pytest.raises(JadxRpcError, match="no method in the call graph"):
            jadx_rpc.callers("com.example.Nope.gone")

    def test_rename_is_pending_until_reload(self):
        jadx_rpc.rename("class", CRYPTO, "com.example.app.CipherBox")
        listed = jadx_rpc.list_renames()
        assert listed["pending"] == 1
        assert listed["renames"][0] == {
            "kind": "class", "target": CRYPTO, "new_name": "com.example.app.CipherBox",
        }
        assert jadx_rpc.classes("Crypto")["stale"] is True

        result = jadx_rpc.reload(background=False)
        assert result["renames_applied"] == 1
        assert result["export"] == "ready"

        renamed = jadx_rpc.decompile_class("com.example.app.CipherBox")
        assert renamed["raw_name"] == CRYPTO
        assert "class CipherBox" in renamed["code"]
        assert "stale" not in renamed
        assert jadx_rpc.decompile_class(CRYPTO)["name"] == "com.example.app.CipherBox"

    def test_rename_method_and_field(self):
        jadx_rpc.rename("method", ENCODE, "encrypt")
        jadx_rpc.rename("field", "com.example.app.Crypto.salt:Ljava/lang/String;", "pepper")
        assert jadx_rpc.list_renames()["pending"] == 2

        jadx_rpc.reload(background=False)
        code = jadx_rpc.decompile_class(CRYPTO)["code"]
        assert "String encrypt(" in code
        assert "this.pepper" in code

    def test_rename_method_without_a_descriptor_is_rejected(self):
        with pytest.raises(JadxRpcError, match="JVM descriptor"):
            jadx_rpc.rename("method", "com.example.app.Crypto.encode", "encrypt")

    def test_rename_field_without_a_descriptor_is_rejected(self):
        with pytest.raises(JadxRpcError, match="JVM descriptor"):
            jadx_rpc.rename("field", "com.example.app.Crypto.salt", "pepper")

    def test_close_frees_the_sources_and_keeps_the_index(self):
        jadx_rpc.close()
        assert jadx_rpc.status()["sources_ready"] is False
        assert jadx_rpc.classes("Crypto")["matched"] == 1


def test_manifest_and_entrypoints_need_an_android_target(opened):
    with pytest.raises(JadxRpcError, match="not an Android application"):
        jadx_rpc.manifest()
    with pytest.raises(JadxRpcError, match="not an Android application"):
        jadx_rpc.entrypoints()


def test_resource_lookup_cannot_escape_the_session(opened):
    with pytest.raises(JadxRpcError, match="escapes the resource directory"):
        jadx_rpc.resource("../../../etc/passwd")


def test_resources_lists_the_jar_payload(opened):
    result = jadx_rpc.resources()
    assert any(r["path"].endswith("Crypto.class") for r in result["resources"])


def test_sessions_are_listed_and_purged(state, fixture_jar):
    jadx_rpc.open_target(str(fixture_jar))
    assert jadx_rpc.list_sessions()["count"] == 1
    jadx_rpc.close(purge=True)
    assert jadx_rpc.list_sessions()["count"] == 0


def test_resolve_reports_when_nothing_is_open(state):
    with pytest.raises(JadxRpcError, match="no open sessions"):
        jadx_rpc.status()


class TestCli:
    def test_success_envelope(self, state, fixture_jar):
        code, payload = run_cli("open", str(fixture_jar))
        assert code == 0
        assert payload["ok"] is True
        assert payload["result"]["classes"] >= 4

    def test_error_envelope(self, state):
        code, payload = run_cli("status")
        assert code == 1
        assert payload["ok"] is False
        assert "no open sessions" in payload["error"]

    def test_commands_take_a_target(self, state, fixture_jar):
        run_cli("open", str(fixture_jar))
        code, payload = run_cli("--target", str(fixture_jar), "classes", "hex")
        assert code == 0
        assert payload["result"]["matched"] == 1

    def test_a_closed_pipe_does_not_traceback(self, state, fixture_jar):
        run_cli("open", str(fixture_jar))
        piped = subprocess.run(
            f"{sys.executable} -m jadx_rpc.cli classes | head -c 20",
            shell=True, capture_output=True, text=True, cwd=REPO,
        )
        assert "BrokenPipeError" not in piped.stderr
        assert "Traceback" not in piped.stderr

    def test_unexpected_failures_still_print_json(self, state, fixture_jar):
        run_cli("open", str(fixture_jar))
        code, payload = run_cli("symbols", "(unclosed")
        assert code == 1
        assert payload["ok"] is False
        assert "error" in payload

    def test_mcp_tool_list_needs_no_optional_extra(self, state):
        code, payload = run_cli("mcp", "--list-tools")
        assert code == 0
        names = {t["name"] for t in payload["result"]["tools"]}
        assert {"jadx_open", "jadx_search", "jadx_rename"} <= names
