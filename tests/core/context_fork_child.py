"""Child process for the fork test: a thread that holds the Context lock forks while another thread's close waits for it.

In the forked child only the forking thread exists. It lets the lock go and then writes and counts through the workspace;
the parent waits for it, lets the lock go and lets the close end. A child that cannot go on is ended by its own alarm.
"""

import os
import signal
import sys
import threading
import time
import warnings
from pathlib import Path

import heavenbase as hb
from heavenbase.utils.config import ConfigManager


class ForkNote(hb.Entity):
    identifier = "context-fork-note"

    name = hb.field(hb.ShortText)


def main(root: Path, closing: bool) -> str:
    backend = {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"}
    context = hb.Context.load(
        {"version": 1, "root": str(root), "workspace": "default", "backend": backend},
        config=ConfigManager(root=str(root), default={}),
    )
    ws = hb.HeavenBase("fork", context=context, detached=True, backends={"main": {"type": "inmem"}})
    ws.register(ForkNote)
    ws.upsert(ForkNote, {"object_id": "parent", "name": "parent"})
    context._lock.acquire()
    closer = None
    if closing:
        closer = threading.Thread(target=context.close, daemon=True)
        closer.start()
        end = time.monotonic() + 20
        while not context._lock._reserved:  # the close waits for the Context lock
            if time.monotonic() > end:
                return "the close did not start to wait"
            time.sleep(0.005)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # Python 3.12+ warns about a fork in a process with threads
        pid = os.fork()
    if pid == 0:
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.alarm(20)
        try:
            context._lock.release()
            ws.upsert(ForkNote, {"object_id": "child", "name": "child"})
            os._exit(0 if ws.count(ForkNote) == 2 else 3)
        except BaseException:  # reported by the exit code
            os._exit(4)
    _, status = os.waitpid(pid, 0)
    context._lock.release()
    if closer is not None:
        closer.join(60)
        if closer.is_alive() or not context._closed:
            return "the close did not end after the lock was free"
    if os.WIFSIGNALED(status):
        return f"the child was ended by signal {os.WTERMSIG(status)}"
    return "ok" if os.WEXITSTATUS(status) == 0 else f"the child exited with {os.WEXITSTATUS(status)}"


if __name__ == "__main__":
    print(main(Path(sys.argv[1]), sys.argv[2] == "close-waits"))
