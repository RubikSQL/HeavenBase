"""Regressions for dashboard shutdown and CLI manifest import options."""

import signal
import subprocess
import sys
import threading

import pytest
import yaml

import heavenbase as hb
from heavenbase.workspace import WorkspaceManifest

pytestmark = pytest.mark.fast


# A shell starts background jobs with SIGINT ignored, and a child inherits that
# disposition, so Uvicorn would re-raise the captured Ctrl+C into an ignored
# signal. The launcher installs Python's own Ctrl+C handler, as a terminal user
# has it, before running the CLI.
_DASHBOARD_LAUNCHER = """
import signal
import sys

from heavenbase.cli.app import main

signal.signal(signal.SIGINT, signal.default_int_handler)
main(sys.argv[1:])
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGINT delivery")
def test_ctrl_c_stops_the_dashboard_command_cleanly(tmp_path, home_env):
    env = home_env(tmp_path / "home")
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-c", _DASHBOARD_LAUNCHER, "dashboard", "--port", "0", "--no-open"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines = []
    ready = threading.Event()

    def read_stdout():
        for line in process.stdout:
            lines.append(line)
            if "dashboard at" in line:
                ready.set()

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    try:
        assert ready.wait(120), lines
        process.send_signal(signal.SIGINT)
        returncode = process.wait(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
    reader.join(timeout=10)
    stderr = process.stderr.read()

    assert returncode == 0, (returncode, stderr)
    assert "interrupted" not in stderr


_NOTE = {"entity_id": "r10extcli-cli-note", "fields": {"title": {"type": "short-text"}}}


def _hb(args, env, cwd):
    return subprocess.run([sys.executable, "-m", "heavenbase.cli.app", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=300)


@pytest.mark.slow
def test_ws_import_allow_replace_replaces_an_edited_entity_for_later_processes(tmp_path, home_env):
    identifier = "r10extcli-cli-import"
    database = hb.utils.path_to_file_uri(tmp_path / "notes.db")
    source = hb.HeavenBase(identifier, detached=True, backends={"main": {"type": "sqlite", "database": database}})
    try:
        source.register(dict(_NOTE))
        source.upsert("r10extcli-cli-note", {"object_id": "n1", "title": "Quarterly plan"})
        manifest = source.to_manifest()
    finally:
        source.release()
    entity = next(entity for entity in manifest["entities"] if entity["entity_id"] == "r10extcli-cli-note")
    assert entity["catalog"] == "full"
    entity["catalog"] = "identity"
    entity["fields"]["owner"] = {"type": "short-text", "required": False}
    path = tmp_path / "workspace.yaml"
    WorkspaceManifest.from_dict(manifest).save(str(path))
    env = home_env(tmp_path / "home")
    env["PYTHON_DOTENV_DISABLED"] = "1"

    help_text = _hb(["ws", "import", "--help"], env, tmp_path)
    assert help_text.returncode == 0 and "--allow-replace" in help_text.stdout, help_text.stderr
    refused = _hb(["ws", "import", str(path)], env, tmp_path)
    assert refused.returncode != 0
    assert "allow_replace" in refused.stdout + refused.stderr
    imported = _hb(["ws", "import", "--allow-replace", str(path)], env, tmp_path)
    assert imported.returncode == 0, (imported.stdout[-2000:], imported.stderr[-3000:])
    assert identifier in imported.stdout

    opened = _hb(["ws", "open", identifier], env, tmp_path)
    assert opened.returncode == 0, (opened.stdout[-2000:], opened.stderr[-3000:])
    assert "r10extcli-cli-note" in opened.stdout
    exported = _hb(["ws", "manifest", identifier], env, tmp_path)
    assert exported.returncode == 0, (exported.stdout[-2000:], exported.stderr[-3000:])
    saved = WorkspaceManifest.from_dict(yaml.safe_load(exported.stdout)).to_dict()
    note = next(entity for entity in saved["entities"] if entity["entity_id"] == "r10extcli-cli-note")
    assert note["catalog"] == "identity" and "owner" in note["fields"]
    listed = _hb(["ws", "list"], env, tmp_path)
    assert listed.returncode == 0 and identifier in listed.stdout, (listed.stdout[-2000:], listed.stderr[-3000:])
