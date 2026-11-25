"""Child process for the Capsule registry lock-order tests.

A compute hook asks for the Capsule registry inside a workspace write, so it
holds the Context lock, while another thread builds the registry facade
again after a reset. A deadlock between the two would hold the process-wide
registry lock forever, so each scenario runs in its own process. A thread
still blocked at the deadline dumps every stack and the process exits with
code 3.
"""

import faulthandler
import os
from pathlib import Path
import sys
import threading
import time

import heavenbase as hb
from heavenbase.capsule import capsule_registry, reset_capsule_registry
from heavenbase.utils.config import ConfigManager

DEADLINE = 60.0
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


def _label(name):
    """Compute hook: it runs inside the workspace write, under the Context lock."""
    context = _STATE["context"]
    if _STATE["ordered"]:
        _STATE["in_hook"].set()
        _STATE["rebuilding"].wait(10)
        time.sleep(0.3)  # the other thread now waits for the Context lock inside its registry build
    rows = capsule_registry(resolver=context.modules(), config=context.config).list()
    return f"{name}:{len(rows)}"


class LabelledNote(hb.Entity):
    identifier = "capsule-lock-order-note"

    name = hb.field(hb.ShortText)
    label = hb.field(hb.ShortText).compute(_label, inputs=["name"])


def main(root: Path, scenario: str) -> str:
    ordered = scenario == "ordered"
    rounds = 1 if ordered else 200
    context = _context(root / "ctx")
    _STATE.update(context=context, ordered=ordered, in_hook=threading.Event(), rebuilding=threading.Event())
    resolver, config = context.modules(), context.config
    capsule_registry(resolver=resolver, config=config).list()
    ws = hb.HeavenBase("capsule-lock-order", context=context, detached=True, backends={"main": {"type": "inmem"}})
    ws.register(LabelledNote)
    errors: list = []

    def write_notes():
        try:
            for i in range(rounds):
                ws.upsert(LabelledNote, {"object_id": f"note-{i}", "name": f"note {i}"})
        except BaseException as exc:  # reported below
            errors.append(exc)

    def rebuild_registry():
        try:
            for _ in range(rounds):
                if ordered:
                    _STATE["in_hook"].wait(10)
                reset_capsule_registry(resolver=resolver, config=config)
                _STATE["rebuilding"].set()
                capsule_registry(resolver=resolver, config=config)
        except BaseException as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=write_notes, daemon=True), threading.Thread(target=rebuild_registry, daemon=True)]
    for thread in threads:
        thread.start()
    end = time.monotonic() + DEADLINE
    for thread in threads:
        thread.join(max(0.0, end - time.monotonic()))
    if any(thread.is_alive() for thread in threads):
        print("deadlocked", flush=True)
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        sys.stderr.flush()
        os._exit(3)
    if errors:
        raise errors[0]
    count = ws.count(LabelledNote)
    context.close()
    return f"ok {count}"


if __name__ == "__main__":
    print(main(Path(sys.argv[1]), sys.argv[2]), flush=True)
