"""Fixtures.

The test target is a jar built from tests/fixtures/src with javac, so the
suite needs no network and no Android SDK. jadx converts jars to dex with its
bundled java-convert plugin, which exercises the same code paths an APK does
apart from the manifest and resources.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).parent / "fixtures" / "src"

needs_jadx = pytest.mark.skipif(
    not (shutil.which("jadx") or Path("/opt/jadx/bin/jadx").exists()),
    reason="jadx is not installed",
)
needs_javac = pytest.mark.skipif(shutil.which("javac") is None, reason="javac is not installed")


@pytest.fixture(scope="session")
def fixture_jar(tmp_path_factory) -> Path:
    if shutil.which("javac") is None:
        pytest.skip("javac is not installed")
    build = tmp_path_factory.mktemp("jar")
    classes = build / "classes"
    classes.mkdir()
    sources = [str(p) for p in SRC.rglob("*.java")]
    subprocess.run([shutil.which("javac"), "-d", str(classes), *sources], check=True)
    jar = build / "fixture.jar"
    subprocess.run([shutil.which("jar"), "cf", str(jar), "-C", str(classes), "."], check=True)
    return jar


@pytest.fixture
def state(tmp_path, monkeypatch) -> Path:
    """Point every session at a throwaway directory, never the real one."""
    directory = tmp_path / "state"
    monkeypatch.setenv("JADX_RPC_STATE_DIR", str(directory))
    monkeypatch.delenv("JADX_RPC_TARGET", raising=False)
    return directory


@pytest.fixture
def opened(state, fixture_jar):
    import jadx_rpc

    jadx_rpc.open_target(str(fixture_jar))
    return fixture_jar


def plant_manifest(package: str = "com.example.app") -> None:
    """Drop a manifest into the open session so package scoping has something to read.

    A jar carries no AndroidManifest.xml, and building a real APK would need the
    Android SDK. Scoping only ever reads the package attribute, so writing the
    file the resource pass would have produced exercises the real code path.
    """
    import jadx_rpc
    from jadx_rpc import core

    session = core.resolve(None)
    session.res.mkdir(parents=True, exist_ok=True)
    (session.res / "AndroidManifest.xml").write_text(
        f'<?xml version="1.0" encoding="utf-8"?>\n<manifest package="{package}"/>\n',
        encoding="utf-8",
    )
    assert jadx_rpc.classes(limit=1)["scope"] == "app", "scoping did not pick the manifest up"


def run_cli(*args: str) -> tuple[int, dict]:
    """Drive the real entry point in a subprocess, the way an agent would."""
    proc = subprocess.run(
        [sys.executable, "-m", "jadx_rpc.cli", *args],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
    )
    return proc.returncode, __import__("json").loads(proc.stdout)
