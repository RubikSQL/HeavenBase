"""Child process for the read-and-write lock test: threads read, write, and write inside reads of two workspaces.

Each thread repeats random calls for a few seconds: reads of either
workspace, a write of the workspace it is reading, a write of the other
workspace inside a read, writes whose compute hook reads the other
workspace, and plain writes. A deadlock would leave threads blocked, so the
process waits for them until a deadline, then dumps every stack and exits
with code 3.
"""

import faulthandler
import random
import sys
import threading
import time
from pathlib import Path

import heavenbase as hb
from heavenbase.utils.config import ConfigManager

DEADLINE = 90.0
RUN_SECONDS = 4.0
THREADS = 8
_STATE: dict = {}


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


class PlainNote(hb.Entity):
    identifier = "upgrade-child-plain-note"

    name = hb.field(hb.ShortText)


def _label(name):
    """Compute hook: it runs inside its workspace's write and reads the other workspace."""
    return f"{name}:{_STATE['first'].count(PlainNote)}"


class LabelledNote(hb.Entity):
    identifier = "upgrade-child-labelled-note"

    name = hb.field(hb.ShortText)
    label = hb.field(hb.ShortText).compute(_label, inputs=["name"])


def main(root: Path) -> str:
    context = _context(root / "ctx")
    first = hb.HeavenBase("upgrade-child-first", context=context, backends={"main": {"type": "inmem"}})
    first.register(PlainNote)
    second = hb.HeavenBase("upgrade-child-second", context=context, backends={"main": {"type": "inmem"}})
    second.register(PlainNote)
    second.register(LabelledNote)
    _STATE["first"] = first
    stop = time.monotonic() + RUN_SECONDS
    counts: dict[str, int] = {}
    errors: list[str] = []
    counts_lock = threading.Lock()

    def worker(index: int) -> None:
        pick = random.Random(index)
        done: dict[str, int] = {}
        try:
            step = 0
            while time.monotonic() < stop:
                step += 1
                key = f"t{index}-{step % 7}"
                op = pick.choice(("read", "read-write-own", "read-write-other", "write-hook", "write", "nested"))
                if op == "read":
                    first.count(PlainNote)
                elif op == "read-write-own":
                    with first._lock.read():
                        first.count(PlainNote)
                        first.upsert(PlainNote, {"object_id": key, "name": "own"})
                elif op == "read-write-other":
                    with first._lock.read():
                        second.upsert(PlainNote, {"object_id": key, "name": "other"})
                elif op == "write-hook":
                    second.upsert(LabelledNote, {"object_id": key, "name": "hooked"})
                elif op == "write":
                    first.upsert(PlainNote, {"object_id": key, "name": "plain"})
                else:
                    with second._lock.read():
                        second.upsert(PlainNote, {"object_id": key, "name": "nested"})
                        with first._lock.read():
                            first.upsert(PlainNote, {"object_id": key, "name": "inner"})
                done[op] = done.get(op, 0) + 1
        except BaseException as exc:  # reported below
            errors.append(f"thread {index}: {type(exc).__name__}: {exc}")
        with counts_lock:
            for op, number in done.items():
                counts[op] = counts.get(op, 0) + number

    threads = [threading.Thread(target=worker, args=(index,), name=f"worker-{index}", daemon=True) for index in range(THREADS)]
    for thread in threads:
        thread.start()
    end = time.monotonic() + DEADLINE
    for thread in threads:
        thread.join(max(0.0, end - time.monotonic()))
    stuck = [thread.name for thread in threads if thread.is_alive()]
    if stuck:
        print(f"stuck {stuck}", flush=True)
        faulthandler.dump_traceback(all_threads=True)
        sys.stdout.flush()
        import os

        os._exit(3)
    if errors:
        return "errors " + "; ".join(errors[:5])
    context.close()
    return f"ok {sum(counts.values())} " + " ".join(f"{op}={number}" for op, number in sorted(counts.items()))


if __name__ == "__main__":
    print(main(Path(sys.argv[1])))
