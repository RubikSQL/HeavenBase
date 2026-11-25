"""Workspace and Context lock protocol under concurrent, nested use.

Every scenario runs its threads as daemons and fails, instead of hanging the
suite, when a thread is still blocked after the deadline.
"""

from __future__ import annotations

from contextlib import contextmanager
import threading
import time

import pytest

import heavenbase as hb
from heavenbase.context import locks
from heavenbase.llm import base as llm_base
from heavenbase.llm.cache import LLMCache, LLMEmbeddingEntry

DEADLINE = 60.0
JOIN = 30.0  # a thread that must finish soon; far below DEADLINE, generous for a loaded host
BUILD_JOIN = 300.0  # a thread that constructs a workspace: over 30 s of CPU on a heavily loaded host

# Compute hooks are module-level functions that look up the live workspaces here.
_PEERS: dict[str, object] = {}


def _run_threads(*targets, deadline: float = DEADLINE) -> None:
    """Run each target on its own daemon thread and fail on a deadlock or error."""
    errors: list[BaseException] = []

    def guard(target):
        def run():
            try:
                target()
            except BaseException as error:  # pragma: no cover - reported below
                errors.append(error)

        return run

    threads = [threading.Thread(target=guard(target), daemon=True) for target in targets]
    for thread in threads:
        thread.start()
    end = time.monotonic() + deadline
    for thread in threads:
        thread.join(max(0.0, end - time.monotonic()))
    stuck = [thread.name for thread in threads if thread.is_alive()]
    assert not stuck, f"threads deadlocked: {stuck}"
    if errors:
        raise errors[0]


def _wait_for(predicate, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "condition not reached"
        time.sleep(0.005)


def _inmem(ws_id: str) -> hb.HeavenBase:
    return hb.HeavenBase(ws_id, detached=True, backends={"main": {"type": "inmem"}})


class _LockNote(hb.Entity):
    identifier = "lock-note"

    name = hb.field(hb.ShortText)


def _count_peer_notes(name):
    """Read the peer workspace from inside a write of this one."""
    return f"{name}:{_PEERS['reader-of-peer'].count(_LockNote)}"


def _write_peer_note(name):
    """Write the peer workspace from inside a write of this one."""
    _PEERS["entered"].set()
    _PEERS["writes-peer"].upsert(_LockNote, {"object_id": f"from-{name}", "name": name})
    return name


class _DescribedNote(hb.Entity):
    identifier = "lock-described-note"

    name = hb.field(hb.ShortText)


class _DescribedManyNote(hb.Entity):
    identifier = "lock-described-many-note"

    name = hb.field(hb.ShortText)


class _ReadsPeer(hb.Entity):
    identifier = "lock-reads-peer"

    name = hb.field(hb.ShortText)
    label = hb.field(hb.ShortText).compute(_count_peer_notes, inputs=["name"])


class _WritesPeer(hb.Entity):
    identifier = "lock-writes-peer"

    name = hb.field(hb.ShortText)
    label = hb.field(hb.ShortText).compute(_write_peer_note, inputs=["name"])


@pytest.fixture(autouse=True)
def _clear_peers():
    _PEERS.clear()
    yield
    _PEERS.clear()


def test_describe_holds_the_exclusive_lock_while_readers_run():
    ws = _inmem("lock-describe")
    ws.register(_DescribedNote)
    ws.upsert(_DescribedNote, {"object_id": "n1", "name": "one"})
    described = threading.Event()

    def describe() -> None:
        ws.describe(_DescribedNote, desc="described while a reader runs", tags=["probe"])
        described.set()

    with ws._lock.read():
        writer = threading.Thread(target=describe, daemon=True)
        writer.start()
        assert not described.wait(0.3)  # describe writes MetaSchema rows, so it waits for the reader
        assert ws.get("n1", entity=_DescribedNote)["name"] == "one"
    writer.join(timeout=10)

    assert described.is_set()
    assert _DescribedNote.schema().desc == "described while a reader runs"


def test_concurrent_describes_and_reads_finish_with_one_complete_description():
    ws = _inmem("lock-describe-many")
    ws.register(_DescribedManyNote)
    ws.upsert(_DescribedManyNote, {"object_id": "n1", "name": "one"})
    barrier = threading.Barrier(12)

    def describer(i: int):
        def run() -> None:
            barrier.wait()
            for round_ in range(10):
                ws.describe(_DescribedManyNote, desc=f"d-{i}-{round_}", tags=[f"t-{i}-{round_}"])

        return run

    def reader() -> None:
        barrier.wait()
        for _ in range(20):
            assert ws.get("n1", entity=_DescribedManyNote)["name"] == "one"
            assert ws.list_entities()

    _run_threads(*(describer(i) for i in range(6)), *(reader for _ in range(6)))

    schema = _DescribedManyNote.schema()
    assert schema.desc.startswith("d-")
    _, i, round_ = schema.desc.split("-")
    assert f"t-{i}-{round_}" in schema.tags  # desc and tags come from the same describe call


def test_write_hooks_that_read_the_other_workspace_never_deadlock():
    first = _inmem("lock-cross-a")
    second = _inmem("lock-cross-b")
    first.register(_ReadsPeer)
    first.register(_LockNote)
    second.register(_LockNote)
    rounds = 15
    barrier = threading.Barrier(6)

    def write_first() -> None:
        # Each write of `first` reads `second` from its compute hook.
        barrier.wait()
        for i in range(rounds):
            _PEERS["reader-of-peer"] = second
            first.upsert(_ReadsPeer, {"object_id": f"a-{i}", "name": f"a-{i}"})

    def write_second() -> None:
        barrier.wait()
        for i in range(rounds):
            second.upsert(_LockNote, {"object_id": f"b-{i}", "name": f"b-{i}"})

    def write_first_plain() -> None:
        barrier.wait()
        for i in range(rounds):
            first.upsert(_LockNote, {"object_id": f"c-{i}", "name": f"c-{i}"})

    def read(ws):
        def run() -> None:
            barrier.wait()
            for _ in range(rounds * 2):
                ws.count(_LockNote)

        return run

    _run_threads(write_first, write_second, write_first_plain, read(first), read(second), read(second))

    assert first.count(_ReadsPeer) == rounds
    assert second.count(_LockNote) == rounds
    assert first.count(_LockNote) == rounds


def test_write_hook_writing_a_workspace_whose_reader_waits_for_the_context_lock():
    # A writer's hook writes `peer` while a reader of `peer` asks for the Context
    # lock (for example to write elsewhere). The reader is parked behind the
    # writer, so the writer does not wait for it; the reader resumes afterwards.
    source = _inmem("lock-parked-source")
    peer = _inmem("lock-parked-peer")
    other = _inmem("lock-parked-other")
    source.register(_WritesPeer)
    peer.register(_LockNote)
    other.register(_LockNote)
    _PEERS["writes-peer"] = peer
    _PEERS["entered"] = threading.Event()
    seen = {}

    def write_source() -> None:
        source.upsert(_WritesPeer, {"object_id": "s1", "name": "s1"})

    def read_peer_then_write_other() -> None:
        with peer._lock.read():
            seen["before"] = peer.count(_LockNote)
            writer = threading.Thread(target=write_source, daemon=True)
            writer.start()
            assert _PEERS["entered"].wait(10)
            _wait_for(lambda: len(peer._lock._writers_waiting) == 1)
            other.upsert(_LockNote, {"object_id": "o1", "name": "o1"})  # parks on the Context lock
            seen["after"] = peer.count(_LockNote)
            writer.join(timeout=10)
            seen["writer_done"] = not writer.is_alive()

    _run_threads(read_peer_then_write_other)

    assert seen == {"before": 0, "after": 1, "writer_done": True}  # not a snapshot across the wait
    assert other.count(_LockNote) == 1


def test_a_read_that_writes_another_workspace_while_a_writer_waits_for_the_read():
    first = _inmem("lock-read-writes-a")
    second = _inmem("lock-read-writes-b")
    first.register(_LockNote)
    second.register(_LockNote)
    first.upsert(_LockNote, {"object_id": "a0", "name": "a0"})
    written = threading.Event()

    def write_first() -> None:
        first.upsert(_LockNote, {"object_id": "a1", "name": "a1"})
        written.set()

    def read_first_then_write_second() -> None:
        with first._lock.read():
            writer = threading.Thread(target=write_first, daemon=True)
            writer.start()
            _wait_for(lambda: len(first._lock._writers_waiting) == 1)  # the writer has announced itself and waits for this read
            second.upsert(_LockNote, {"object_id": "b1", "name": "b1"})
        writer.join(timeout=10)

    _run_threads(read_first_then_write_second)

    assert written.is_set()
    assert first.count(_LockNote) == 2
    assert second.count(_LockNote) == 1


def _embed_through_cache(name):
    """Embed through a cached LLM from inside a write of this workspace."""
    llm, cache = _PEERS["llm"], _PEERS["cache"]
    return str(llm.embed(name, cache=cache, reduce=True))


class _CachedEmbedding(hb.Entity):
    identifier = "lock-cached-embedding"

    name = hb.field(hb.ShortText)
    vector = hb.field(hb.ShortText).compute(_embed_through_cache, inputs=["name"])


def test_compute_hook_with_a_cached_llm_races_cache_misses_and_hits(monkeypatch):
    calls = []

    def fake_embed(spec, inputs, **kwargs):
        calls.append(len(inputs))
        time.sleep(0.001)
        return {"data": [{"index": index, "embedding": [float(len(text))]} for index, text in enumerate(inputs)], "usage": {}}

    monkeypatch.setattr(llm_base, "embed_complete", fake_embed)
    cache = LLMCache.from_dict(
        {
            "enabled": True,
            "workspace": "lock-llm-cache",
            "backend": {"main": {"type": "inmem"}},
            "namespaces": {"embedding": {"enabled": True, "touch_hits": True}},
        }
    )
    llm = hb.LLM(preset="embed")
    _PEERS.update(llm=llm, cache=cache)
    ws = _inmem("lock-cached-llm")
    ws.register(_CachedEmbedding)
    rounds = 20
    barrier = threading.Barrier(6)

    def write_rows(tag: str):
        def run() -> None:
            barrier.wait()
            for i in range(rounds):  # each row embeds a new text: a cache miss written from inside the write
                ws.upsert(_CachedEmbedding, {"object_id": f"{tag}-{i}", "name": f"{tag}-text-{i}"})

        return run

    def embed_outside(tag: str):
        def run() -> None:
            barrier.wait()
            for i in range(rounds):
                llm.embed(f"{tag}-miss-{i}", cache=cache, reduce=True)  # miss on another thread
                llm.embed("shared hit", cache=cache, reduce=True)  # hit that touches the cached row

        return run

    def read_rows() -> None:
        barrier.wait()
        for _ in range(rounds):
            ws.count(_CachedEmbedding)
            cache.workspace.count(LLMEmbeddingEntry)

    _run_threads(write_rows("w1"), write_rows("w2"), embed_outside("e1"), embed_outside("e2"), read_rows, read_rows)

    assert ws.count(_CachedEmbedding) == 2 * rounds
    assert cache.workspace.count(LLMEmbeddingEntry) == 4 * rounds + 1
    hb.DEFAULT_CONTEXT._retire_hidden_store(cache._store, drop=True)


def test_facades_of_one_workspace_share_one_lock(tmp_path):
    context = hb.Context.load(
        {
            "version": 1,
            "root": str(tmp_path / "root"),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=hb.ConfigManager(root=str(tmp_path / "root"), setup=True),
    )
    try:
        backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "shared.db")}}
        first = hb.HeavenBase("lock-shared", context=context, backends=backends)
        second = hb.HeavenBase("lock-shared", context=context, backends=backends)
        child = hb.HeavenBase("lock-shared", context=context.child("lock-shared"), backends=backends)
        other = hb.HeavenBase("lock-other", context=context, backends=backends)
        detached = hb.HeavenBase("lock-shared", context=context, detached=True, backends=backends)

        assert first is not second
        assert first._lock is second._lock is child._lock
        assert other._lock is not first._lock
        assert detached._lock is not first._lock
        first.register(_LockNote)
        second.register(_LockNote)
        written = threading.Event()

        def write_through_second() -> None:
            second.upsert(_LockNote, {"object_id": "n1", "name": "one"})
            written.set()

        with first._lock.read():  # a read through one facade excludes a write through the other
            writer = threading.Thread(target=write_through_second, daemon=True)
            writer.start()
            assert not written.wait(0.3)
        writer.join(timeout=10)
        assert written.is_set()
        assert first.get("n1", entity=_LockNote)["name"] == "one"
    finally:
        context.close()


def test_an_abandoned_write_wait_wakes_the_readers_it_held_back(monkeypatch):
    monkeypatch.setattr(locks, "_RECHECK", 60.0)  # only a wake-up ends a wait here
    ws = _inmem("lock-abandoned-wait")
    ws.register(_LockNote)
    lock = ws._lock
    calls = []
    original = type(lock)._drained

    def drained(self):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt("writer gave up")
        return original(self)

    writer_errors = []
    reader_done = threading.Event()

    def write() -> None:
        try:
            lock.acquire_write()
        except KeyboardInterrupt as error:
            writer_errors.append(error)

    def read() -> None:
        with lock.read():
            reader_done.set()

    with lock.read():
        lock._drained = drained.__get__(lock)
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        _wait_for(lambda: len(lock._writers_waiting) == 1 and len(lock._waiters) == 1)
        (gate,) = lock._waiters
        reader = threading.Thread(target=read, daemon=True)
        reader.start()  # a new reader queues behind the waiting writer
        _wait_for(lambda: len(lock._waiters) == 2)
        gate.release()  # wakes the writer only; it gives up
        writer.join(timeout=10)
        assert writer_errors
        assert reader_done.wait(10)  # the reader is woken although nothing else notifies it
    reader.join(timeout=10)
    del lock._drained
    assert not ws.context._lock.owned()
    ws.upsert(_LockNote, {"object_id": "n1", "name": "one"})
    assert ws.count(_LockNote) == 1


# ---------------------------------------------------------------- writer queue and drains


def _write_other_note(name):
    """Write the other workspace of the pair from inside a write of this one."""
    target = _PEERS["b"] if name.startswith("a") else _PEERS["a"]
    target.upsert(_LockNote, {"object_id": f"from-{name}", "name": name})
    return name


def _gated_count_peer(name):
    """Read the peer workspace from inside a write, once the test lets it."""
    _PEERS["in-hook"].set()
    assert _PEERS["hook-go"].wait(30)
    return f"{name}:{_PEERS['reader-of-peer'].count(_LockNote)}"


def _record_write_order(name):
    """Record the order in which writes of this workspace run."""
    _PEERS["order"].append(name)
    return name


def _open_current_llm_cache(name):
    """Reach the process LLM cache's hidden workspace from inside a write."""
    LLMCache.current().workspace
    return name


class _WritesOther(hb.Entity):
    identifier = "lock-writes-other"

    name = hb.field(hb.ShortText)
    label = hb.field(hb.ShortText).compute(_write_other_note, inputs=["name"])


class _GatedReadsPeer(hb.Entity):
    identifier = "lock-gated-reads-peer"

    name = hb.field(hb.ShortText)
    label = hb.field(hb.ShortText).compute(_gated_count_peer, inputs=["name"])


class _OrderedNote(hb.Entity):
    identifier = "lock-ordered-note"

    name = hb.field(hb.ShortText)
    label = hb.field(hb.ShortText).compute(_record_write_order, inputs=["name"])


class _CacheOpeningNote(hb.Entity):
    identifier = "lock-cache-opening-note"

    name = hb.field(hb.ShortText)
    label = hb.field(hb.ShortText).compute(_open_current_llm_cache, inputs=["name"])


def _start(target) -> threading.Thread:
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def _parked_on(thread: threading.Thread, lock: object) -> bool:
    return locks._PARKED.get(thread.ident) is lock


def _assert_idle(lock) -> None:
    assert lock._writer is None
    assert not lock._writers_waiting and not lock._nested_waiting and not lock._waiters
    assert lock._head is None and not lock._queue and not lock._pending
    assert lock not in lock._context_lock._watchers and lock._context_lock._drainer is not lock
    assert not lock._readers and not lock._outer_readers
    assert lock._held_back == 0 and lock._entitled == 0


def test_write_hooks_that_write_the_other_workspace_never_deadlock():
    first = _inmem("lock-cross-write-a")
    second = _inmem("lock-cross-write-b")
    for ws in (first, second):
        ws.register(_WritesOther)
        ws.register(_LockNote)
    _PEERS.update(a=first, b=second)
    rounds = 15
    barrier = threading.Barrier(6)

    def write(ws, tag):
        def run() -> None:
            barrier.wait()
            for i in range(rounds):  # each write of one workspace writes the other from its hook
                ws.upsert(_WritesOther, {"object_id": f"{tag}-{i}", "name": f"{tag}-{i}"})

        return run

    def write_plain(ws, tag):
        def run() -> None:
            barrier.wait()
            for i in range(rounds):
                ws.upsert(_LockNote, {"object_id": f"{tag}-{i}", "name": f"{tag}-{i}"})

        return run

    def read(ws):
        def run() -> None:
            barrier.wait()
            for _ in range(rounds * 2):
                ws.count(_LockNote)

        return run

    _run_threads(write(first, "a"), write(second, "b"), write_plain(first, "pa"), write_plain(second, "pb"), read(first), read(second))

    assert first.count(_WritesOther) == second.count(_WritesOther) == rounds
    assert first.count(_LockNote) == second.count(_LockNote) == 2 * rounds


def test_a_read_is_not_blocked_by_a_write_of_another_workspace():
    first = _inmem("lock-independent-read-a")
    second = _inmem("lock-independent-read-b")
    first.register(_LockNote)
    second.register(_LockNote)
    first.upsert(_LockNote, {"object_id": "a1", "name": "a1"})
    seen = {}

    with second._lock:  # an unfinished write of `second` holds the Context lock
        reader = _start(lambda: seen.update(count=first.count(_LockNote)))
        reader.join(JOIN)
        assert not reader.is_alive()
    assert seen == {"count": 1}


def test_a_read_is_not_blocked_by_a_write_of_another_workspace_while_writers_of_its_own_wait():
    # Writers of `first` are queued; their head has drained and waits for the
    # Context lock, which an unfinished write of `second` holds. A new outer
    # read of `first` does not wait for that write.
    first = _inmem("lock-independent-queued-a")
    second = _inmem("lock-independent-queued-b")
    first.register(_LockNote)
    second.register(_LockNote)
    first.upsert(_LockNote, {"object_id": "a1", "name": "a1"})
    context_lock = first.context._lock
    seen = {}

    with second._lock:
        writers = [_start(lambda i=i: first.upsert(_LockNote, {"object_id": f"w{i}", "name": "w"})) for i in range(2)]
        _wait_for(lambda: len(first._lock._queue) == 2 and any(_parked_on(w, context_lock) for w in writers))
        reader = _start(lambda: seen.update(count=first.count(_LockNote)))
        reader.join(JOIN)
        assert not reader.is_alive()
        assert seen == {"count": 1}
        assert first._lock._writer is None and len(first._lock._writers_waiting) == 2
    for thread in writers:
        thread.join(JOIN)
        assert not thread.is_alive()
    assert first.count(_LockNote) == 3
    _assert_idle(first._lock)


def test_a_writer_drains_the_readers_of_its_workspace_without_the_context_lock():
    # Writers of `first` and `third` both wait for long reads of their own
    # workspace; neither holds the Context lock meanwhile, so a write of
    # `second` (and a write of a workspace being read elsewhere) proceeds.
    first = _inmem("lock-drain-a")
    second = _inmem("lock-drain-b")
    third = _inmem("lock-drain-c")
    for ws in (first, second, third):
        ws.register(_LockNote)
    release = threading.Event()
    written = {"a": threading.Event(), "c": threading.Event()}

    def hold_read(ws):
        def run() -> None:
            with ws._lock.read():
                assert release.wait(30)

        return run

    def write(ws, key):
        def run() -> None:
            ws.upsert(_LockNote, {"object_id": key, "name": key})
            written[key].set()

        return run

    readers = [_start(hold_read(first)), _start(hold_read(third))]
    _wait_for(lambda: first._lock._readers and third._lock._readers)
    writers = [_start(write(first, "a")), _start(write(third, "c"))]
    try:
        _wait_for(lambda: len(first._lock._writers_waiting) == 1 and len(third._lock._writers_waiting) == 1)
        assert first.context._lock._owner is None  # announced writers hold no Context lock
        other = _start(lambda: second.upsert(_LockNote, {"object_id": "b", "name": "b"}))
        other.join(JOIN)
        assert not other.is_alive()
        assert second.count(_LockNote) == 1
        assert not written["a"].is_set() and not written["c"].is_set()
    finally:
        release.set()
    for thread in (*readers, *writers):
        thread.join(JOIN)
    assert written["a"].is_set() and written["c"].is_set()
    _assert_idle(first._lock)
    _assert_idle(third._lock)


def test_an_announced_writer_holds_back_new_outer_readers_for_one_write():
    ws = _inmem("lock-phase-fair")
    other = _inmem("lock-phase-fair-other")
    ws.register(_LockNote)
    other.register(_LockNote)
    release = threading.Event()
    seen = {}

    def hold_read() -> None:
        with ws._lock.read():
            assert release.wait(30)

    def outer_read() -> None:
        seen["outer"] = ws.count(_LockNote)

    def nested_read() -> None:
        with other._lock.read():  # holds a HeavenBase lock, so its read of `ws` is nested
            seen["nested"] = ws.count(_LockNote)

    holder = _start(hold_read)
    _wait_for(lambda: ws._lock._readers)
    writer = _start(lambda: ws.upsert(_LockNote, {"object_id": "w", "name": "w"}))
    try:
        _wait_for(lambda: len(ws._lock._writers_waiting) == 1)
        late = _start(outer_read)
        _wait_for(lambda: _parked_on(late, ws._lock))
        assert "outer" not in seen  # a new outer reader waits behind the announced writer
        nested = _start(nested_read)
        nested.join(JOIN)
        assert seen["nested"] == 0  # a nested reader does not wait for an announced writer
    finally:
        release.set()
    for thread in (holder, writer, late):
        thread.join(JOIN)
    assert seen["outer"] == 1  # the held-back reader entered after exactly that write
    _assert_idle(ws._lock)


def test_outer_writers_of_one_workspace_run_in_arrival_order():
    ws = _inmem("lock-fifo")
    ws.register(_OrderedNote)
    _PEERS["order"] = []
    release = threading.Event()

    def hold_read() -> None:
        with ws._lock.read():
            assert release.wait(30)

    holder = _start(hold_read)
    _wait_for(lambda: ws._lock._readers)
    writers = []
    for i in range(5):
        name = f"w{i}"
        writers.append(_start(lambda name=name: ws.upsert(_OrderedNote, {"object_id": name, "name": name})))
        _wait_for(lambda i=i: len(ws._lock._writers_waiting) == i + 1)
    release.set()
    for thread in (holder, *writers):
        thread.join(JOIN)
        assert not thread.is_alive()
    assert _PEERS["order"] == [f"w{i}" for i in range(5)]
    _assert_idle(ws._lock)


def test_outer_readers_and_writers_of_one_workspace_all_progress():
    ws = _inmem("lock-fair-mix")
    ws.register(_LockNote)
    stop = time.monotonic() + 2.0
    reads = [0] * 6
    writes = [0] * 6

    def reader(i):
        def run() -> None:
            while time.monotonic() < stop:
                ws.count(_LockNote)
                reads[i] += 1

        return run

    def writer(i):
        def run() -> None:
            n = 0
            while time.monotonic() < stop:
                ws.upsert(_LockNote, {"object_id": f"w{i}-{n % 10}", "name": "x"})
                writes[i] += 1
                n += 1

        return run

    _run_threads(*(reader(i) for i in range(6)), *(writer(i) for i in range(6)))

    assert min(reads) > 0, reads  # neither side starves the other
    assert min(writes) > 0, writes
    _assert_idle(ws._lock)


def test_a_queue_head_admits_readers_while_another_thread_owns_the_context_lock():
    # 1. The drained head of `ws` waits for the Context lock, owned by H1: a
    #    new outer reader R1 enters at once. 2. Once the head owns the lock
    #    with R1 running, it gives the lock back and drains R1 without it, so a
    #    write of `other` proceeds. 3. Meanwhile the Context lock is free, so a
    #    new reader R2 is held back; 4. when H2 takes the Context lock, R2 is
    #    woken and enters without any write of `ws`. 5. After its one extra
    #    drain the head waits for R2 under the Context lock and holds R3 back,
    #    which enters after that write.
    ws = _inmem("lock-redrain")
    other = _inmem("lock-redrain-other")
    ws.register(_LockNote)
    other.register(_LockNote)
    context_lock = ws.context._lock
    lock = ws._lock
    first_holding, first_go, second_holding, second_go = (threading.Event() for _ in range(4))
    release = {key: threading.Event() for key in ("r1", "r2", "r3")}
    seen = {}

    def hold_context(holding, go):
        def run() -> None:
            with context_lock:
                holding.set()
                assert go.wait(30)

        return run

    def read(key):
        def run() -> None:
            with lock.read():
                seen[key] = ws.count(_LockNote)
                assert release[key].wait(30)

        return run

    def write_other(name):
        thread = _start(lambda: other.upsert(_LockNote, {"object_id": name, "name": name}))
        thread.join(JOIN)
        assert not thread.is_alive()

    turns = lock._turns
    first_holder = _start(hold_context(first_holding, first_go))
    assert first_holding.wait(10)
    writer = _start(lambda: ws.upsert(_LockNote, {"object_id": "outer", "name": "outer"}))
    second_holder = readers = None
    try:
        _wait_for(lambda: _parked_on(writer, context_lock))
        readers = {"r1": _start(read("r1"))}
        _wait_for(lambda: "r1" in seen)  # 1
        assert seen["r1"] == 0 and first_holder.is_alive()
        first_go.set()
        first_holder.join(JOIN)
        _wait_for(lambda: lock._pending == {readers["r1"].ident} and context_lock._owner is None)  # 2
        write_other("o1")
        readers["r2"] = _start(read("r2"))
        _wait_for(lambda: _parked_on(readers["r2"], lock) and lock._held_back == 1)  # 3
        assert "r2" not in seen
        second_holder = _start(hold_context(second_holding, second_go))
        assert second_holding.wait(10)
        _wait_for(lambda: "r2" in seen)  # 4
        assert seen["r2"] == 0 and lock._turns == turns
        release["r1"].set()
        readers["r1"].join(JOIN)
        _wait_for(lambda: _parked_on(writer, context_lock) and not lock._pending)
        second_go.set()
        second_holder.join(JOIN)
        _wait_for(lambda: context_lock._owner == writer.ident)  # 5: waits for R2 under the lock
        readers["r3"] = _start(read("r3"))
        _wait_for(lambda: _parked_on(readers["r3"], lock))
        assert "r3" not in seen and lock._writer is None
        release["r2"].set()
        readers["r2"].join(JOIN)
        writer.join(JOIN)
        assert not writer.is_alive()
        _wait_for(lambda: "r3" in seen)
        assert seen["r3"] == 1  # entered after that write
    finally:
        for event in (first_go, second_go, *release.values()):
            event.set()
    for thread in (writer, *(readers or {}).values()):
        thread.join(JOIN)
        assert not thread.is_alive()
    assert ws.count(_LockNote) == 1
    _assert_idle(lock)


class _SlowMutex:
    """Mutex that the thread ``slow`` takes a while to get, as one that wakes late does."""

    def __init__(self, mutex) -> None:
        self._mutex = mutex
        self.slow: int | None = None

    def __enter__(self):
        if threading.get_ident() == self.slow:
            time.sleep(0.3)
        return self._mutex.__enter__()

    def __exit__(self, *exc):
        return self._mutex.__exit__(*exc)


def test_an_entitled_reader_enters_before_the_next_queued_writer_even_when_it_wakes_late():
    # W1 writes; W2 is queued; R arrives and is held back. When W1 finishes,
    # R is entitled to enter but wakes late; W2, the new head, waits for it
    # rather than taking the exclusive side first.
    lock = locks._WorkspaceLock(locks._ContextLock())
    lock._mutex = mutex = _SlowMutex(lock._mutex)
    writing, release = threading.Event(), threading.Event()
    order = []

    def first_write() -> None:
        with lock:
            writing.set()
            assert release.wait(30)

    def second_write() -> None:
        with lock:
            order.append("w2")

    def read() -> None:
        with lock.read():
            order.append("r")

    w1 = _start(first_write)
    assert writing.wait(10)
    w2 = _start(second_write)
    _wait_for(lambda: len(lock._queue) == 2)  # the first write's turn, until it ends, and the second
    reader = _start(read)
    _wait_for(lambda: _parked_on(reader, lock) and len(lock._waiters) == 1)  # held back, waiting
    mutex.slow = reader.ident
    release.set()
    for thread in (w1, w2, reader):
        thread.join(JOIN)
        assert not thread.is_alive()
    assert order == ["r", "w2"]
    _assert_idle(lock)


def test_a_held_back_outer_reader_waits_for_at_most_one_write():
    lock = locks._WorkspaceLock(locks._ContextLock())
    waits = []
    admits_reader = lock._admits_reader

    def spy(nested, arrival):
        result = admits_reader(nested, arrival)
        if result and arrival is not None:
            waits.append(lock._turns - arrival)
        return result

    lock._admits_reader = spy
    stop = time.monotonic() + 1.5
    writes = [0] * 6
    reads = [0] * 4

    def writer(i):
        def run() -> None:
            while time.monotonic() < stop:
                with lock:
                    time.sleep(0.0005)
                writes[i] += 1

        return run

    def reader(i):
        def run() -> None:
            while time.monotonic() < stop:
                with lock.read():
                    time.sleep(0.0002)
                reads[i] += 1

        return run

    _run_threads(*(writer(i) for i in range(6)), *(reader(i) for i in range(4)))

    assert waits and max(waits) <= 1, (len(waits), max(waits, default=0))
    assert min(writes) > 0 and min(reads) > 0
    _assert_idle(lock)


def test_a_hook_reads_a_workspace_whose_reader_waits_for_the_context_lock_behind_an_announced_writer():
    # AML-style measure scope: M holds the shared side of `ws` and asks for the
    # Context lock, which H owns; W, a writer of `ws`, waits for M; H's hook
    # then reads `ws`. The nested read does not wait for W, so all finish.
    source = _inmem("lock-measure-source")
    ws = _inmem("lock-measure-ws")
    source.register(_GatedReadsPeer)
    ws.register(_LockNote)
    ws.upsert(_LockNote, {"object_id": "n0", "name": "n0"})
    _PEERS.update({"reader-of-peer": ws, "in-hook": threading.Event(), "hook-go": threading.Event()})
    measuring, measure_go = threading.Event(), threading.Event()
    seen = {}

    def measure() -> None:
        with ws._lock.read():
            measuring.set()
            assert measure_go.wait(30)
            with ws.context._lock:
                seen["measured"] = ws.count(_LockNote)

    def write_source() -> None:
        source.upsert(_GatedReadsPeer, {"object_id": "s", "name": "s"})

    def write_ws() -> None:
        ws.upsert(_LockNote, {"object_id": "n1", "name": "n1"})

    def drive() -> None:
        m = _start(measure)
        assert measuring.wait(10)
        h = _start(write_source)
        assert _PEERS["in-hook"].wait(10)
        w = _start(write_ws)
        _wait_for(lambda: len(ws._lock._writers_waiting) == 1)
        measure_go.set()
        _wait_for(lambda: _parked_on(m, ws.context._lock))
        _PEERS["hook-go"].set()
        for thread in (m, h, w):
            thread.join(DEADLINE / 2)
            assert not thread.is_alive()

    _run_threads(drive)

    assert seen["measured"] == 1
    assert source.get("s", entity=_GatedReadsPeer)["label"] == "s:1"
    assert ws.count(_LockNote) == 2


def test_construction_drains_readers_of_its_id_without_the_context_lock(tmp_path):
    context = hb.Context.load(
        {
            "version": 1,
            "root": str(tmp_path / "root"),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=hb.ConfigManager(root=str(tmp_path / "root"), setup=True),
    )
    try:
        backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "shared.db")}}
        first = hb.HeavenBase("lock-construct", context=context, backends=backends)
        other = hb.HeavenBase("lock-construct-other", context=context, backends=backends)
        other.register(_LockNote)
        built = {}
        release = threading.Event()

        def hold_read() -> None:
            with first._lock.read():
                assert release.wait(30)

        def build() -> None:
            try:
                built["ws"] = hb.HeavenBase("lock-construct", context=context, backends=backends)
            except BaseException as error:  # pragma: no cover - reported below
                built["error"] = error

        holder = _start(hold_read)
        _wait_for(lambda: first._lock._readers)
        builder = _start(build)
        try:
            _wait_for(lambda: len(first._lock._writers_waiting) == 1)
            assert context._lock._owner is None  # construction waits for the reader without the Context lock
            elsewhere = _start(lambda: other.upsert(_LockNote, {"object_id": "o", "name": "o"}))
            elsewhere.join(JOIN)
            assert not elsewhere.is_alive()
            assert "ws" not in built
        finally:
            release.set()
        holder.join(JOIN)
        builder.join(BUILD_JOIN)
        assert not builder.is_alive()
        assert "error" not in built, built.get("error")
        assert built["ws"]._lock is first._lock
        _assert_idle(first._lock)
    finally:
        context.close()


def test_llm_cache_reset_races_write_hooks_that_open_the_current_cache():
    ws = _inmem("lock-cache-reset")
    ws.register(_CacheOpeningNote)
    rounds = 10
    barrier = threading.Barrier(3)

    def write(tag):
        def run() -> None:
            barrier.wait()
            for i in range(rounds):  # the hook takes the cache lock while this write holds the Context lock
                ws.upsert(_CacheOpeningNote, {"object_id": f"{tag}-{i}", "name": f"{tag}-{i}"})

        return run

    def reset() -> None:
        barrier.wait()
        for _ in range(rounds):  # reset takes the cache lock, then retires stores under the Context lock
            LLMCache.reset()

    try:
        _run_threads(write("a"), write("b"), reset)
    finally:
        LLMCache.reset(drop=True)
    assert ws.count(_CacheOpeningNote) == 2 * rounds


def test_llm_cache_reset_drop_lets_a_cache_built_during_the_drop_keep_working(monkeypatch):
    """A cache that ``current()`` builds while ``reset(drop=True)`` drops the old store opens its own store after the drop.

    Opened earlier, it would share the cache database's engine with the store being dropped; the drop then
    disposes that engine, and every later cached call through the new process cache fails until another reset.
    """
    from heavenbase.workspace.hidden_store import HiddenWorkspaceStore

    def reply(spec, messages, stream=False, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {"total_tokens": 1}}

    def make():
        return hb.LLM(preset="mock", mock_chat=reply, mock_pause=(0, 0), cache=True)

    original = HiddenWorkspaceStore.drop
    dropping, release = threading.Event(), threading.Event()
    first_done, go_on = threading.Event(), threading.Event()
    results: dict[str, object] = {}

    def held_drop(store) -> None:
        dropping.set()
        assert release.wait(JOIN)
        original(store)

    def chat_during_drop() -> None:
        try:
            llm = make()
            results["first"] = llm.chat("asked during the drop")
            first_done.set()
            assert go_on.wait(JOIN)
            results["second"] = llm.chat("asked during the drop")
        except BaseException as error:  # reported below
            results["error"] = error
        finally:
            first_done.set()

    try:
        assert make().chat("opens the process cache store") == "ok"
        monkeypatch.setattr(HiddenWorkspaceStore, "drop", held_drop)
        resetter = _start(lambda: LLMCache.reset(drop=True))
        assert dropping.wait(JOIN)
        chatter = _start(chat_during_drop)
        first_done.wait(1.0)  # the new cache must not open its store while the old one is being dropped
        release.set()
        resetter.join(JOIN)
        assert not resetter.is_alive()
        go_on.set()
        chatter.join(JOIN)
        assert not chatter.is_alive()
        monkeypatch.setattr(HiddenWorkspaceStore, "drop", original)
        assert "error" not in results, results.get("error")
        assert results == {"first": "ok", "second": "ok"}
        assert make().chat("asked after the drop") == "ok"
        assert make().chat("asked after the drop") == "ok"
    finally:
        release.set()
        go_on.set()
        monkeypatch.setattr(HiddenWorkspaceStore, "drop", original)
        LLMCache.reset(drop=True)


def _cached_mock_llm():
    def reply(spec, messages, stream=False, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {"total_tokens": 1}}

    return hb.LLM(preset="mock", mock_chat=reply, mock_pause=(0, 0), cache=True)


def test_llm_cache_reset_drop_closes_a_store_opened_before_the_drop_took_the_context_lock():
    """``reset(drop=True)`` unregisters the old cache, then waits for the Context lock to drop its store.

    A cache that ``current()`` builds in that gap may open its store on the database the drop disposes. The drop
    closes that store first, so the new process cache reopens it on a fresh engine instead of failing with
    "Engine has been disposed" on every later call.
    """
    from heavenbase.llm import cache as cache_module

    try:
        assert _cached_mock_llm().chat("opens the process cache store") == "ok"
        old = LLMCache.current()
        context_lock = old.context._lock
        with context_lock:
            resetter = _start(lambda: LLMCache.reset(drop=True))
            _wait_for(lambda: _parked_on(resetter, context_lock))
            reference = cache_module._LLM_CACHES.get(old.config_manager)
            assert reference is None or reference() is not old  # unregistered, store not yet dropped
            assert _cached_mock_llm().chat("asked in the gap") == "ok"  # opens the new cache's store now
            assert LLMCache.current()._store is not None
        resetter.join(JOIN)
        assert not resetter.is_alive()
        for i in range(3):
            assert _cached_mock_llm().chat(f"asked after the drop {i % 2}") == "ok"
    finally:
        LLMCache.reset(drop=True)


def test_llm_cache_reset_drop_keeps_an_llm_built_before_an_earlier_reset_working():
    """An LLM keeps the cache it was built with after a reset unregisters it, and that cache reopens its store.

    A later ``reset(drop=True)`` of the registered cache drops the same database; it closes the kept cache's store
    too, so the LLM's next cached call reopens it instead of failing with "Engine has been disposed".
    """
    try:
        kept = _cached_mock_llm()
        assert kept.chat("first") == "ok"
        LLMCache.reset(drop=True)
        assert kept.chat("reopens the kept cache") == "ok"
        assert _cached_mock_llm().chat("opens the registered cache") == "ok"
        LLMCache.reset(drop=True)
        assert kept.chat("after the second drop") == "ok"
        assert kept.chat("after the second drop") == "ok"
        assert _cached_mock_llm().chat("after the second drop") == "ok"
    finally:
        LLMCache.reset(drop=True)


# ---------------------------------------------------------------- the Context-lock owner waits for a new outer reader


def _fan_out_in_hook(name):
    """From inside a write, run ``_PEERS["work"]`` four times on worker threads and join them.

    The fan-out is unbounded (one thread per task) unless ``_PEERS["bounded"]``
    is set; then two threads take the four tasks in turn, as a fixed-size pool
    does, so each worker reads again after its first read.
    """
    if not _PEERS.get("armed"):
        return name
    _PEERS["in-hook"].set()
    _wait_for(_PEERS["peer-writer-waits"])
    work = _PEERS["work"]
    if _PEERS.get("bounded"):
        tasks = [work] * 4
        tasks_lock = threading.Lock()

        def take_tasks() -> None:
            while True:
                with tasks_lock:
                    if not tasks:
                        return
                    task = tasks.pop()
                task()

        workers = [threading.Thread(target=take_tasks, daemon=True) for _ in range(2)]
    else:
        workers = [threading.Thread(target=work, daemon=True) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()  # a pool's map / result: the write holds the Context lock until every worker is done
    return name


class _FanOutNote(hb.Entity):
    identifier = "lock-fan-out-note"

    name = hb.field(hb.ShortText)
    label = hb.field(hb.ShortText).compute(_fan_out_in_hook, inputs=["name"])


def _fan_out_while_a_peer_writer_waits(source, start_peer_writer, work, bounded=False) -> None:
    holder = {}
    _PEERS.update(
        {
            "armed": True,
            "bounded": bounded,
            "in-hook": threading.Event(),
            "peer-writer-waits": lambda: "x" in holder and _parked_on(holder["x"], source.context._lock),
            "work": work,
        }
    )

    def drive() -> None:
        w = _start(lambda: source.upsert(_FanOutNote, {"object_id": "w", "name": "w"}))
        assert _PEERS["in-hook"].wait(10)
        holder["x"] = start_peer_writer()
        for thread in (w, holder["x"]):
            thread.join(DEADLINE / 2)
            assert not thread.is_alive()

    _run_threads(drive)


@pytest.mark.parametrize("bounded", [False, True], ids=["unbounded", "bounded"])
def test_a_write_hook_joins_workers_that_read_a_workspace_whose_writer_waits_for_the_context_lock(bounded):
    # W writes `source`; its hook reads `peer` on worker threads and joins them.
    # X, an outer writer of `peer`, arrives meanwhile and waits for the Context
    # lock that W owns. The workers' outer reads must not wait for X.
    source = _inmem("lock-fan-out-source")
    peer = _inmem("lock-fan-out-peer")
    source.register(_FanOutNote)
    peer.register(_LockNote)
    peer.upsert(_LockNote, {"object_id": "seed", "name": "seed"})
    counts = []

    _fan_out_while_a_peer_writer_waits(
        source,
        lambda: _start(lambda: peer.upsert(_LockNote, {"object_id": "x", "name": "x"})),
        lambda: counts.append(peer.count(_LockNote)),
        bounded=bounded,
    )

    assert counts == [1, 1, 1, 1]
    assert peer.count(_LockNote) == 2
    _assert_idle(peer._lock)


@pytest.mark.parametrize("bounded", [False, True], ids=["unbounded", "bounded"])
def test_a_write_hook_joins_workers_that_read_a_workspace_whose_writer_drained_once_more(bounded):
    # As above, but X has already let a reader in during an earlier wait for
    # the Context lock and drained it once more without the lock; its next
    # wait for the lock, owned by W, still does not hold the workers back.
    source = _inmem("lock-fan-out-again-source")
    peer = _inmem("lock-fan-out-again-peer")
    source.register(_FanOutNote)
    peer.register(_LockNote)
    peer.upsert(_LockNote, {"object_id": "seed", "name": "seed"})
    context_lock = source.context._lock
    holding, go, release = threading.Event(), threading.Event(), threading.Event()
    counts = []

    def hold_context() -> None:
        with context_lock:
            holding.set()
            assert go.wait(30)

    def hold_read() -> None:
        with peer._lock.read():
            assert release.wait(30)

    def peer_writer_waits() -> bool:
        release.set()  # X's drain ends and X asks for the Context lock, which W owns
        return _parked_on(x, context_lock)

    first = _start(hold_context)
    assert first.is_alive() and holding.wait(10)
    x = _start(lambda: peer.upsert(_LockNote, {"object_id": "x", "name": "x"}))
    reader = None
    try:
        _wait_for(lambda: _parked_on(x, context_lock))
        reader = _start(hold_read)
        _wait_for(lambda: peer._lock._readers)  # entered while `first` owns the Context lock
        go.set()
        first.join(JOIN)
        # X had the lock with the reader running, gave it back and drains the reader.
        _wait_for(lambda: context_lock._owner is None and not _parked_on(x, context_lock) and peer._lock._writer is None)
        _PEERS.update(
            {
                "armed": True,
                "bounded": bounded,
                "in-hook": threading.Event(),
                "peer-writer-waits": peer_writer_waits,
                "work": lambda: counts.append(peer.count(_LockNote)),
            }
        )

        def drive() -> None:
            w = _start(lambda: source.upsert(_FanOutNote, {"object_id": "w", "name": "w"}))
            for thread in (w, x, reader):
                thread.join(DEADLINE / 2)
                assert not thread.is_alive()

        _run_threads(drive)
    finally:
        go.set()
        release.set()
    assert counts == [1, 1, 1, 1]
    assert peer.count(_LockNote) == 2
    _assert_idle(peer._lock)


def test_a_write_hook_joins_workers_that_hit_the_llm_cache_while_a_miss_waits_to_write_it(monkeypatch):
    # The same shape through the Context's hidden LLM-cache workspace: the
    # workers' cache hits are outer reads of it, and another thread's cache
    # miss is an outer writer of it waiting for the Context lock.
    def fake_embed(spec, inputs, **kwargs):
        return {"data": [{"index": index, "embedding": [float(len(text))]} for index, text in enumerate(inputs)], "usage": {}}

    monkeypatch.setattr(llm_base, "embed_complete", fake_embed)
    cache = LLMCache.from_dict(
        {
            "enabled": True,
            "workspace": "lock-fan-out-cache",
            "backend": {"main": {"type": "inmem"}},
            "namespaces": {"embedding": {"enabled": True, "touch_hits": False}},
        }
    )
    llm = hb.LLM(preset="embed")
    llm.embed("hit-text", cache=cache, reduce=True)
    source = _inmem("lock-fan-out-cache-source")
    source.register(_FanOutNote)
    hits = []
    try:
        _fan_out_while_a_peer_writer_waits(
            source,
            lambda: _start(lambda: llm.embed("miss-text", cache=cache, reduce=True)),
            lambda: hits.append(llm.embed("hit-text", cache=cache, reduce=True)),
        )
        assert len(hits) == 4
        assert cache.workspace.count(LLMEmbeddingEntry) == 2
        _assert_idle(cache.workspace._lock)
    finally:
        hb.DEFAULT_CONTEXT._retire_hidden_store(cache._store, drop=True)


def _foreign_lock_cycle(ws, hold_context, writer_waits) -> dict:
    """M holds the Context lock and needs F; R holds F and reads `ws`; W writes `ws`.

    Returns what M and R observed; M must get F, so R's read must not wait for W.
    """
    foreign = threading.Lock()
    has_f, holding, go = threading.Event(), threading.Event(), threading.Event()
    seen = {}
    holder = {}

    def reader() -> None:
        with foreign:
            has_f.set()
            assert go.wait(30)
            seen["count"] = ws.count(_LockNote)

    def measure() -> None:
        assert has_f.wait(10)
        with hold_context():
            holding.set()
            _wait_for(lambda: "w" in holder and writer_waits(holder["w"]))
            go.set()
            seen["m-got-f"] = foreign.acquire(timeout=JOIN)
            if seen["m-got-f"]:
                foreign.release()

    def drive() -> None:
        r, m = _start(reader), _start(measure)
        assert holding.wait(10)
        holder["w"] = _start(lambda: ws.upsert(_LockNote, {"object_id": "w", "name": "w"}))
        for thread in (m, r, holder["w"]):
            thread.join(DEADLINE / 2)
            assert not thread.is_alive()

    _run_threads(drive)
    return seen


def test_a_reader_holding_the_context_lock_waits_for_a_lock_whose_holder_reads_its_workspace():
    # AML measure-scope shape: M holds the shared side of `ws` and then the
    # Context lock, and needs F. W, an outer writer of `ws`, drains M.
    ws = _inmem("lock-foreign-measure")
    ws.register(_LockNote)
    ws.upsert(_LockNote, {"object_id": "seed", "name": "seed"})

    @contextmanager
    def measure_scope():
        with ws._lock.read(), ws.context._lock:
            yield

    seen = _foreign_lock_cycle(ws, measure_scope, lambda w: ws._lock._head == w.ident and ws._lock._pending)

    assert seen == {"count": 1, "m-got-f": True}
    assert ws.count(_LockNote) == 2
    _assert_idle(ws._lock)


def test_a_write_of_another_workspace_waits_for_a_lock_whose_holder_reads_a_workspace_with_a_waiting_writer():
    # M writes `other` (it owns the Context lock) and needs F. W, an outer
    # writer of `ws`, has drained and waits for the Context lock.
    ws = _inmem("lock-foreign-write")
    other = _inmem("lock-foreign-write-other")
    ws.register(_LockNote)
    other.register(_LockNote)
    ws.upsert(_LockNote, {"object_id": "seed", "name": "seed"})

    seen = _foreign_lock_cycle(ws, lambda: other._lock, lambda w: _parked_on(w, ws.context._lock))

    assert seen == {"count": 1, "m-got-f": True}
    assert ws.count(_LockNote) == 2
    _assert_idle(ws._lock)


# ---------------------------------------------------------------- randomised nesting


def _hook_read_some_peer(name):
    """Read one workspace of the random set from inside a write."""
    return f"{name}:{_PEERS['random'][hash(name) % 3].count(_LockNote)}"


def _hook_write_some_peer(name):
    """Write one workspace of the random set from inside a write."""
    _PEERS["random"][(hash(name) + 1) % 3].upsert(_LockNote, {"object_id": f"h-{name}", "name": name})
    return name


class _RandomReadsPeer(hb.Entity):
    identifier = "lock-random-reads-peer"

    name = hb.field(hb.ShortText)
    label = hb.field(hb.ShortText).compute(_hook_read_some_peer, inputs=["name"])


class _RandomWritesPeer(hb.Entity):
    identifier = "lock-random-writes-peer"

    name = hb.field(hb.ShortText)
    label = hb.field(hb.ShortText).compute(_hook_write_some_peer, inputs=["name"])


@pytest.mark.parametrize("seed", range(2))
def test_random_nested_reads_and_writes_across_workspaces_never_deadlock(seed):
    import random

    workspaces = [_inmem(f"lock-random-{seed}-{i}") for i in range(3)]
    for ws in workspaces:
        for entity in (_LockNote, _RandomReadsPeer, _RandomWritesPeer):
            ws.register(entity)
    _PEERS["random"] = workspaces
    stop = time.monotonic() + 3.0
    done = {"reads": 0, "writes": 0}

    def worker(i):
        def run() -> None:
            rnd = random.Random(seed * 100 + i)
            n = 0
            while time.monotonic() < stop:
                ws, other = rnd.choice(workspaces), rnd.choice(workspaces)
                op = rnd.random()
                n += 1
                key = f"t{i}-{n % 20}"
                row = {"object_id": key, "name": key}
                if op < 0.25:
                    ws.upsert(_LockNote, row)
                elif op < 0.35:
                    ws.upsert(_RandomReadsPeer, row)
                elif op < 0.45:
                    ws.upsert(_RandomWritesPeer, row)
                elif op < 0.60:
                    ws.count(_LockNote)
                elif op < 0.70:  # a read that writes another workspace
                    with ws._lock.read():
                        ws.count(_LockNote)
                        if other is not ws:
                            other.upsert(_LockNote, row)
                elif op < 0.80:  # a read that then takes the Context lock
                    with ws._lock.read(), ws.context._lock:
                        ws.count(_LockNote)
                        time.sleep(0.001)
                elif op < 0.88:  # the Context lock, then nested writes of two workspaces
                    with ws.context._lock:
                        ws.upsert(_LockNote, row)
                        other.upsert(_RandomWritesPeer, row)
                elif op < 0.94:  # a long read
                    with ws._lock.read():
                        ws.count(_LockNote)
                        time.sleep(0.003)
                else:  # a read nested in a read of another workspace
                    with ws._lock.read():
                        other.count(_LockNote)
                done["writes" if op < 0.45 else "reads"] += 1

        return run

    _run_threads(*(worker(i) for i in range(12)))

    assert done["reads"] and done["writes"]
    for ws in workspaces:
        _assert_idle(ws._lock)


def test_outer_readers_fan_out_reads_of_each_others_workspace_while_both_writers_drain():
    # P reads `first` and joins a worker that reads `second`; Q reads `second`
    # and joins a worker that reads `first`. Writers of both workspaces drain
    # meanwhile, holding no HeavenBase lock, so the Context lock is free. If
    # both heads held new readers back, each worker would wait for the other
    # head, which waits for the reader that joins it.
    first = _inmem("lock-cross-fan-a")
    second = _inmem("lock-cross-fan-b")
    for ws in (first, second):
        ws.register(_LockNote)
        ws.upsert(_LockNote, {"object_id": "n", "name": "n"})
    inside = threading.Barrier(2)
    go = threading.Event()
    seen = {}

    def read_and_fan_out(source, target, key):
        def run() -> None:
            with source._lock.read():
                inside.wait(10)
                assert go.wait(30)
                worker = _start(lambda: seen.setdefault(key, target.count(_LockNote)))
                worker.join()  # no limit, like a pool's map

        return run

    def drive() -> None:
        readers = [_start(read_and_fan_out(first, second, "p")), _start(read_and_fan_out(second, first, "q"))]
        _wait_for(lambda: first._lock._readers and second._lock._readers)
        writers = [
            _start(lambda: first.upsert(_LockNote, {"object_id": "a", "name": "a"})),
            _start(lambda: second.upsert(_LockNote, {"object_id": "b", "name": "b"})),
        ]
        _wait_for(lambda: first._lock._pending and second._lock._pending)
        assert first.context._lock._owner is None  # both heads drain without the Context lock
        go.set()
        for thread in (*readers, *writers):
            thread.join(DEADLINE / 2)
            assert not thread.is_alive()

    _run_threads(drive)
    assert set(seen) == {"p", "q"}
    assert first.count(_LockNote) == 2 and second.count(_LockNote) == 2
    for ws in (first, second):
        _assert_idle(ws._lock)


def test_only_one_draining_head_holds_new_readers_back_while_the_context_lock_is_free():
    # Writers of `first` and `second` both drain a long read, holding no
    # HeavenBase lock. The first head to drain is the drainer and holds a new
    # reader of its workspace back; the other head does not, so a new reader
    # of `second` enters at once. The held-back reader enters after that write.
    first = _inmem("lock-drainer-a")
    second = _inmem("lock-drainer-b")
    for ws in (first, second):
        ws.register(_LockNote)
    context_lock = first.context._lock
    release = {"a": threading.Event(), "b": threading.Event()}
    seen = {}

    def hold_read(ws, key):
        def run() -> None:
            with ws._lock.read():
                assert release[key].wait(30)

        return run

    def outer_read(ws, key):
        def run() -> None:
            seen[key] = ws.count(_LockNote)

        return run

    holders = [_start(hold_read(first, "a")), _start(hold_read(second, "b"))]
    _wait_for(lambda: first._lock._readers and second._lock._readers)
    writers = [_start(lambda: first.upsert(_LockNote, {"object_id": "a", "name": "a"}))]
    late = []
    try:
        _wait_for(lambda: context_lock._drainer is first._lock)
        writers.append(_start(lambda: second.upsert(_LockNote, {"object_id": "b", "name": "b"})))
        _wait_for(lambda: second._lock._head is not None and second._lock._pending)
        assert context_lock._owner is None and context_lock._drainer is first._lock
        late.append(_start(outer_read(second, "b")))
        late[0].join(JOIN)
        assert seen["b"] == 0  # the second head does not hold readers back
        late.append(_start(outer_read(first, "a")))
        _wait_for(lambda: _parked_on(late[1], first._lock) and first._lock._held_back == 1)
        assert "a" not in seen  # the drainer does
    finally:
        release["a"].set()  # first's write completes before second's head can own the Context lock
        writers[0].join(JOIN)
        release["b"].set()
    for thread in (*holders, *writers, *late):
        thread.join(JOIN)
        assert not thread.is_alive()
    assert seen["a"] == 1  # entered after that write
    assert first.count(_LockNote) == 1 and second.count(_LockNote) == 1
    for ws in (first, second):
        _assert_idle(ws._lock)


def _hook_fan_out_reads(name):
    """Count every other workspace of the fan-out set on worker threads and join them."""
    source = name.split(":")[0]
    counts = []
    workers = [threading.Thread(target=lambda ws=ws: counts.append(ws.count(_LockNote)), daemon=True) for key, ws in _PEERS["fan-out"].items() if key != source]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    return f"{name}:{sum(counts)}"


class _FanOutReadsNote(hb.Entity):
    identifier = "lock-fan-out-reads-note"

    name = hb.field(hb.ShortText)
    label = hb.field(hb.ShortText).compute(_hook_fan_out_reads, inputs=["name"])


@pytest.mark.parametrize("seed", range(3))
def test_random_read_fan_out_across_workspaces_never_deadlocks(seed):
    # Outer readers of one workspace, and write hooks of one workspace, read
    # the other workspaces on worker threads and join them, while outer
    # writers of every workspace queue and drain.
    import random

    workspaces = {f"lock-fan-{seed}-{i}": _inmem(f"lock-fan-{seed}-{i}") for i in range(3)}
    for ws in workspaces.values():
        ws.register(_LockNote)
        ws.register(_FanOutReadsNote)
        ws.upsert(_LockNote, {"object_id": "n", "name": "n"})
    _PEERS["fan-out"] = workspaces
    keys = list(workspaces)
    stop = time.monotonic() + 3.0
    done = {"reads": 0, "writes": 0}

    def reader(i):
        def run() -> None:
            rnd = random.Random(seed * 100 + i)
            while time.monotonic() < stop:
                key = rnd.choice(keys)
                others = rnd.sample([k for k in keys if k != key], rnd.randint(1, len(keys) - 1))
                with workspaces[key]._lock.read():
                    workers = [_start(lambda k=k: workspaces[k].count(_LockNote)) for k in others]
                    time.sleep(rnd.random() * 0.003)
                    for worker in workers:
                        worker.join()
                done["reads"] += 1

        return run

    def writer(i):
        def run() -> None:
            rnd = random.Random(seed * 100 + 50 + i)
            n = 0
            while time.monotonic() < stop:
                key = rnd.choice(keys)
                n += 1
                if rnd.random() < 0.3:
                    workspaces[key].upsert(_FanOutReadsNote, {"object_id": f"h{i}-{n % 20}", "name": f"{key}:{n}"})
                else:
                    workspaces[key].upsert(_LockNote, {"object_id": f"w{i}-{n % 20}", "name": "w"})
                done["writes"] += 1

        return run

    _run_threads(*(reader(i) for i in range(4)), *(writer(i) for i in range(3)))

    assert done["reads"] and done["writes"]
    for ws in workspaces.values():
        _assert_idle(ws._lock)


def test_construction_close_and_reads_of_one_id_race_without_deadlock(tmp_path):
    context = hb.Context.load(
        {
            "version": 1,
            "root": str(tmp_path / "root"),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=hb.ConfigManager(root=str(tmp_path / "root"), setup=True),
    )
    backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "shared.db")}}
    first = hb.HeavenBase("lock-race-construct", context=context, backends=backends)
    first.register(_LockNote)
    stop = time.monotonic() + 2.0
    closed = threading.Event()

    def until_closed(step):
        def run() -> None:
            i = 0
            while time.monotonic() < stop and not closed.is_set():
                try:
                    step(i)
                except RuntimeError:  # the Context closed under this thread
                    return
                i += 1

        return run

    def read(_):
        with first._lock.read():
            first.count(_LockNote)
            time.sleep(0.002)

    def construct(i):
        ws = hb.HeavenBase("lock-race-construct", context=context, backends=backends)
        ws.register(_LockNote)
        ws.upsert(_LockNote, {"object_id": f"c{i % 5}", "name": "c"})

    def write(i):
        first.upsert(_LockNote, {"object_id": f"w{i % 5}", "name": "w"})

    def close() -> None:
        time.sleep(1.5)
        while True:
            try:
                context.close()
            except RuntimeError as error:
                assert str(error) in {
                    "Cannot close Context while its Context lock is held by another thread",
                    "Cannot retire Context while resolver operations are active",
                }
                if time.monotonic() >= stop:
                    return
                time.sleep(0.002)
            else:
                closed.set()
                return

    _run_threads(*(until_closed(read) for _ in range(3)), until_closed(construct), until_closed(write), until_closed(write), close)

    context.close()
    with pytest.raises(RuntimeError, match="Context is closed"):
        first.count(_LockNote)
    _assert_idle(first._lock)
