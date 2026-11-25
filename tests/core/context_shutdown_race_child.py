"""Child process for the shutdown-wait race: resets and a close wait for reads, writes and reads that write.

Worker threads read two workspaces, write them, write the workspace they
read, and write with a compute hook that reads the other workspace. Another
thread resets the Context again and again and finally closes it; both wait
for the workers' work instead of refusing it. A worker whose facade a reset
retired builds a new one. A deadlock would leave threads blocked, so the
process waits for them until a deadline, then dumps every stack and exits
with code 3.
"""

import faulthandler
import os
import random
import sys
import threading
import time
from pathlib import Path

import heavenbase as hb
from heavenbase.context.reset import reset_default_local_state
from heavenbase.utils.config import ConfigManager

DEADLINE = 150.0
RUN_SECONDS = 4.0
THREADS = 6
# Errors a call may raise because a reset or the close overlapped it.
EXPECTED = ("reset is in progress", "close is in progress", "Context is closed", "retired")
_STATE: dict = {}


class RaceNote(hb.Entity):
    identifier = "shutdown-race-note"

    name = hb.field(hb.ShortText)


def _label(name):
    """Compute hook: it runs inside its workspace's write and reads the other workspace."""
    first = _STATE.get("first")
    return f"{name}:{0 if first is None else first.count(RaceNote)}"


class RaceLabel(hb.Entity):
    identifier = "shutdown-race-label"

    name = hb.field(hb.ShortText)
    label = hb.field(hb.ShortText).compute(_label, inputs=["name"])


def main(root: Path) -> str:
    context = hb.Context.load(
        {
            "version": 1,
            "root": str(root),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=ConfigManager(root=str(root), setup=False),
    )
    facades: dict[str, object] = {}
    building: set[str] = set()
    facades_lock = threading.Lock()

    def facade(name):
        """Return the current facade of ``name``; one worker builds a missing one while the others go on without it."""
        with facades_lock:
            ws = facades.get(name)
            if ws is not None or name in building:
                return ws
            building.add(name)
        try:
            # Built outside facades_lock: a build takes the Context lock.
            ws = hb.HeavenBase(f"race-{name}", context=context, detached=True, backends={"main": {"type": "inmem"}})
            ws.register(RaceNote)
            ws.register(RaceLabel)
            with facades_lock:
                facades[name] = ws
            _STATE[name] = ws
            return ws
        finally:
            with facades_lock:
                building.discard(name)

    def retire(name, ws):
        with facades_lock:
            if facades.get(name) is ws:
                facades.pop(name)

    closed = threading.Event()
    counts = {"done": 0, "expected-errors": 0, "resets": 0, "reset-errors": 0}
    errors: list[str] = []
    counts_lock = threading.Lock()

    def count(key):
        with counts_lock:
            counts[key] += 1

    def worker(index: int) -> None:
        pick = random.Random(index)
        while not closed.is_set():
            name = pick.choice(("first", "second"))
            op = pick.choice(("read", "write", "read-write", "hook"))
            try:
                ws = facade(name)
            except RuntimeError as exc:
                if not any(text in str(exc) for text in EXPECTED):
                    errors.append(f"worker {index} build: {exc}")
                    return
                count("expected-errors")
                continue
            if ws is None:  # another worker builds it
                time.sleep(0.005)
                continue
            try:
                if op == "read":
                    ws.count(RaceNote)
                elif op == "write":
                    ws.upsert(RaceNote, {"object_id": f"w{index}", "name": "plain"})
                elif op == "read-write":
                    with ws._lock.read():
                        ws.count(RaceNote)
                        ws.upsert(RaceNote, {"object_id": f"r{index}", "name": "inside"})
                else:
                    ws.upsert(RaceLabel, {"object_id": f"h{index}", "name": "hooked"})
                count("done")
            except RuntimeError as exc:
                if not any(text in str(exc) for text in EXPECTED):
                    errors.append(f"worker {index} {op}: {type(exc).__name__}: {exc}")
                    return
                retire(name, ws)
                count("expected-errors")
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(f"worker {index} {op}: {type(exc).__name__}: {exc}")
                return

    def shutdown() -> None:
        try:
            while time.monotonic() < stop:
                time.sleep(0.2)
                try:
                    reset_default_local_state(context)
                    count("resets")
                except RuntimeError as exc:
                    errors.append(f"reset: {exc}")
                    count("reset-errors")
            context.close()
        except BaseException as exc:  # reported below
            errors.append(f"shutdown: {type(exc).__name__}: {exc}")
        finally:
            closed.set()

    facade("first")
    facade("second")
    stop = time.monotonic() + RUN_SECONDS
    threads = [threading.Thread(target=worker, args=(index,), name=f"worker-{index}", daemon=True) for index in range(THREADS)]
    threads.append(threading.Thread(target=shutdown, name="shutdown", daemon=True))
    for thread in threads:
        thread.start()
    end = time.monotonic() + DEADLINE
    for thread in threads:
        thread.join(max(0.0, end - time.monotonic()))
    stuck = [thread.name for thread in threads if thread.is_alive()]
    if stuck:
        print(f"stuck {stuck} {counts}", flush=True)
        faulthandler.dump_traceback(all_threads=True)
        sys.stdout.flush()
        os._exit(3)
    if errors or not context._closed or not counts["resets"]:
        return f"errors closed={context._closed} {counts} " + "; ".join(errors[:5])
    return f"ok {counts}"


if __name__ == "__main__":
    print(main(Path(sys.argv[1])))
