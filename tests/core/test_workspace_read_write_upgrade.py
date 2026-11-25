"""A thread that reads a workspace can also write it.

0.1.2.2 ran every call of a workspace under the reentrant Context lock, so a
write made inside a read of the same workspace worked, also from a read
callback or while building another facade of that workspace. The
reader-writer lock now takes the exclusive side for such a write as a nested
write: it takes the Context lock and waits for the workspace's other running
readers, not for its own read, and two readers that both write wait for each
other's write only.
"""

from concurrent.futures import Future
from pathlib import Path
import subprocess
import sys
import threading

import pytest

import heavenbase as hb
from heavenbase.utils.config import ConfigManager

_CHILD = Path(__file__).with_name("workspace_upgrade_child.py")


class UpgradeNote(hb.Entity):
    identifier = "upgrade-note"

    name = hb.field(hb.ShortText)


def _context(root):
    return hb.Context.load(
        {
            "version": 1,
            "root": str(root),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=ConfigManager(root=str(root), setup=False),
    )


@pytest.fixture(scope="module")
def context(tmp_path_factory):
    """One Context for the module's tests, each on its own workspaces, so module setup runs once."""
    context = _context(tmp_path_factory.mktemp("upgrade") / "root")
    yield context
    context.close()


def _workspace(context, name):
    ws = hb.HeavenBase(name, context=context, detached=True, backends={"main": {"type": "inmem"}})
    ws.register(UpgradeNote)
    ws.upsert(UpgradeNote, {"object_id": "n1", "name": "one"})
    return ws


def _on_thread(call):
    result = Future()

    def run():
        try:
            result.set_result(call())
        except BaseException as error:
            result.set_exception(error)

    threading.Thread(target=run, daemon=True).start()
    return result


@pytest.mark.slow
def test_a_read_callback_writes_the_workspace_it_reads(context, monkeypatch):
    ws = _workspace(context, "upgrade-callback")
    backend = ws.backends.get("main")
    get_many = backend.get_many
    wrote = []

    def write_while_reading(*args, **kwargs):
        if not wrote:
            wrote.append(True)
            ws.upsert(UpgradeNote, {"object_id": "n2", "name": "two"})
        return get_many(*args, **kwargs)

    monkeypatch.setattr(backend, "get_many", write_while_reading)
    assert ws.get("n1", entity=UpgradeNote)["name"] == "one"
    assert wrote == [True]
    assert ws.count(UpgradeNote) == 2
    assert ws.get("n2", entity=UpgradeNote)["name"] == "two"


@pytest.mark.full
@pytest.mark.slow
def test_a_facade_of_a_workspace_being_read_can_be_built(context, tmp_path):
    backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "shared.db")}}
    first = hb.HeavenBase("upgrade-built", context=context, backends=backends)
    first.register(UpgradeNote)
    first.upsert(UpgradeNote, {"object_id": "n1", "name": "one"})
    with first._lock.read():
        second = hb.HeavenBase("upgrade-built", context=context, backends=backends)
        second.register(UpgradeNote)
        second.upsert(UpgradeNote, {"object_id": "n2", "name": "two"})
    assert second._lock is first._lock
    assert first.count(UpgradeNote) == 2


@pytest.mark.slow
def test_two_readers_that_both_write_their_workspace_finish(context):
    ws = _workspace(context, "upgrade-two")
    inside = threading.Barrier(2, timeout=10)

    def read_then_write(index):
        with ws._lock.read():
            inside.wait()  # both threads hold the read before either writes
            ws.upsert(UpgradeNote, {"object_id": f"w{index}", "name": str(index)})
            return ws.get(f"w{index}", entity=UpgradeNote)["name"]

    results = [_on_thread(lambda index=index: read_then_write(index)) for index in range(2)]
    assert sorted(result.result(20) for result in results) == ["0", "1"]
    assert ws.count(UpgradeNote) == 3


@pytest.mark.fast
def test_a_reader_that_writes_waits_for_the_other_running_readers(context):
    ws = _workspace(context, "upgrade-wait")
    reading, release, wrote = threading.Event(), threading.Event(), threading.Event()

    def hold_a_read():
        with ws._lock.read():
            reading.set()
            assert release.wait(10)
            return ws.count(UpgradeNote)

    def read_then_write():
        with ws._lock.read():
            ws.upsert(UpgradeNote, {"object_id": "n2", "name": "two"})
            wrote.set()

    try:
        holder = _on_thread(hold_a_read)
        assert reading.wait(10)
        writer = _on_thread(read_then_write)
        assert not wrote.wait(0.3)  # the write waits for the other thread's running read
        release.set()
        assert holder.result(10) == 1  # the other read saw no write while it ran
        writer.result(10)
        assert ws.count(UpgradeNote) == 2
    finally:
        release.set()


@pytest.mark.full
@pytest.mark.slow
def test_reads_that_write_their_own_or_another_workspace_do_not_deadlock(tmp_path):
    done = subprocess.run([sys.executable, str(_CHILD), str(tmp_path)], capture_output=True, text=True, timeout=300)

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert done.stdout.strip().splitlines()[-1].startswith("ok ")
