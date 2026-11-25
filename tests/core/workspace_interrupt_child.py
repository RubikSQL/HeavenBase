"""Child process for the interrupt test: a timer's handler raises at a random moment of in-memory workspace calls.

After each interrupted call another thread writes to the workspace. At the end the Context resets and closes. A call
that a lock left held stops is reported instead of waited for.
"""

import faulthandler
import random
import signal
import sys
import threading
import time
from pathlib import Path

import heavenbase as hb
from heavenbase.context.reset import reset_default_local_state
from heavenbase.utils.config import ConfigManager

ROUNDS = 400


class Interrupted(BaseException):
    pass


class InterruptedNote(hb.Entity):
    identifier = "interrupted-note"

    name = hb.field(hb.ShortText)
    size = hb.field(hb.Integer)


def main(root: Path) -> str:
    backend = {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"}
    context = hb.Context.load(
        {"version": 1, "root": str(root), "workspace": "default", "backend": backend},
        config=ConfigManager(root=str(root), default={}),
    )
    ws = hb.HeavenBase("interrupted", context=context, detached=True, backends={"main": {"type": "inmem"}})
    ws.register(InterruptedNote)
    calls = {
        "get": lambda i: ws.get("n0", InterruptedNote),
        "upsert": lambda i: ws.upsert(InterruptedNote, {"object_id": "n0", "name": "note", "size": i}),
        "count": lambda i: ws.count(InterruptedNote),
    }
    armed = False

    def handler(signum, frame):
        nonlocal armed
        if armed:
            armed = False
            raise Interrupted

    signal.signal(signal.SIGALRM, handler)
    interrupted = 0
    for name, call in calls.items():
        times = []
        for i in range(30):
            began = time.perf_counter()
            call(i)
            times.append(time.perf_counter() - began)
        typical = sorted(times)[15]
        rng = random.Random(name)
        for i in range(ROUNDS):
            try:
                try:
                    armed = True
                    signal.setitimer(signal.ITIMER_REAL, max(1e-5, rng.uniform(0.02, 1.0) * typical))
                    call(i)
                finally:
                    armed = False
            except Interrupted:
                interrupted += 1
            except Exception:  # the interrupted call may report its own failure
                pass
            signal.setitimer(signal.ITIMER_REAL, 0)
            writer = threading.Thread(target=calls["upsert"], args=(i,), daemon=True)
            writer.start()
            writer.join(20)
            if writer.is_alive():
                faulthandler.dump_traceback(all_threads=True)
                return f"a write of another thread waits after {interrupted} interrupted calls, the last one a {name}"
    if interrupted < 100:
        return f"only {interrupted} calls were interrupted"
    reset_default_local_state(context)
    context.close()
    return f"ok {interrupted}"


if __name__ == "__main__":
    print(main(Path(sys.argv[1])))
