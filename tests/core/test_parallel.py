from __future__ import annotations

import asyncio
import threading
import time

import pytest

from heavenbase.utils.parallel import TaskResult, abatch, abatch_stream, batch, batch_stream, pforeach, pmap

pytestmark = pytest.mark.full


def _double(x: int) -> int:
    return x * 2


def _kw(*values):
    return [{"x": value} for value in values]


def _values(results):
    return [result.value for result in results]


def _maybe_fail(x: int) -> int:
    if x == 2:
        raise ValueError(f"bad {x}")
    return x * 2


async def _async_double(x: int) -> int:
    await asyncio.sleep(0.01)
    return x * 2


async def _async_maybe_fail(x: int) -> int:
    await asyncio.sleep(0.01)
    if x == 2:
        raise ValueError(f"async bad {x}")
    return x * 2


def test_batch_ordering_kwargs_and_empty_input():
    def combine(x: int, y: int = 0, scale: int = 1) -> int:
        return (x + y) * scale

    ordered = batch(_double, _kw(3, 1, 2), max_workers=2, ordered=True)
    unordered = batch(_double, _kw(3, 1, 2), max_workers=2, ordered=False)

    assert _values(ordered) == [6, 2, 4]
    assert [result.index for result in ordered] == [0, 1, 2]
    assert all(result.ok for result in ordered)
    assert sorted(_values(unordered)) == [2, 4, 6]
    assert {result.index for result in unordered} == {0, 1, 2}
    assert _values(batch(_double, _kw(1, 2, 3), max_workers=1, ordered=True)) == [2, 4, 6]
    assert _values(batch(combine, [{"x": 1, "y": 2}, {"x": 3, "scale": 2}], max_workers=2, ordered=True)) == [3, 6]
    assert batch(_double, [], max_workers=2) == []


def test_batch_unordered_uses_completion_order():
    release = {value: threading.Event() for value in (1, 2, 3)}

    def staggered(x: int) -> int:
        assert release[x].wait(timeout=2.0)
        return x

    def release_next(done: int, total: int) -> None:
        assert total == 3
        if done == 1:
            release[2].set()
        elif done == 2:
            release[1].set()

    release[3].set()

    assert _values(batch(staggered, _kw(1, 2, 3), max_workers=3, ordered=False, on_progress=release_next)) == [3, 2, 1]


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("capture", [2, ValueError, 6]),
        ("skip", [2, 6]),
    ],
)
def test_batch_error_modes(mode, expected):
    results = batch(_maybe_fail, _kw(1, 2, 3), max_workers=2, ordered=True, on_error=mode)

    if mode == "capture":
        assert [results[0].value, type(results[1].error), results[2].value] == expected
        assert [result.ok for result in results] == [True, False, True]
    else:
        assert _values(results) == expected

    with pytest.raises(ValueError, match="bad 2"):
        batch(_maybe_fail, _kw(1, 2, 3), max_workers=2, ordered=True, on_error="raise")
    with pytest.raises(ValueError, match="on_error must be"):
        batch(_double, _kw(1), on_error="ignore")
    with pytest.raises(TypeError, match="must be a mapping"):
        batch(_double, [1], on_error="raise")


def test_batch_raise_stops_starting_pending_work():
    lock = threading.Lock()
    started = []

    def fail_first(x: int) -> int:
        with lock:
            started.append(x)
        if x == 0:
            raise ValueError("boom")
        time.sleep(0.10)
        return x

    with pytest.raises(ValueError, match="boom"):
        batch(fail_first, _kw(*range(20)), max_workers=2, ordered=False, on_error="raise")
    assert len(started) <= 2


def test_batch_progress_elapsed_and_task_result_error():
    calls = []

    def hook(done: int, total: int) -> None:
        calls.append((done, total))

    def slow_double(x: float) -> float:
        time.sleep(0.05)
        return x * 2

    batch(_double, _kw(1, 2, 3), max_workers=2, on_progress=hook)
    timed = batch(slow_double, _kw(1.0), max_workers=1)
    failed = batch(_maybe_fail, _kw(2), max_workers=1, on_error="capture")[0]

    assert calls == [(1, 3), (2, 3), (3, 3)]
    assert timed[0].elapsed_ms is not None and timed[0].elapsed_ms >= 40
    with pytest.raises(ValueError, match="bad 2"):
        failed.raise_if_error()


def test_batch_stream_ordering_skip_and_empty():
    started = {1: threading.Event(), 2: threading.Event()}
    release = {1: threading.Event(), 2: threading.Event()}

    def gated(x: int) -> int:
        if x in release:
            started[x].set()
            release[x].wait(timeout=2)
        return x

    stream = batch_stream(gated, _kw(1, 2, 3), max_workers=3, ordered=False)
    try:
        assert next(stream).value == 3
        assert started[1].wait(timeout=2)
        assert started[2].wait(timeout=2)
        release[2].set()
        assert next(stream).value == 2
        release[1].set()
        assert _values(stream) == [1]
    finally:
        release[1].set()
        release[2].set()

    assert _values(batch_stream(_double, _kw(3, 1, 2), max_workers=2, ordered=True)) == [6, 2, 4]
    assert _values(batch_stream(_maybe_fail, _kw(1, 2, 3), max_workers=2, ordered=True, on_error="skip")) == [2, 6]
    assert list(batch_stream(_double, [])) == []


def test_batch_stream_ordered_still_runs_concurrently():
    started = {1: threading.Event(), 2: threading.Event(), 3: threading.Event()}
    release = threading.Event()
    results = []
    errors = []

    def gated(x: int) -> int:
        started[x].set()
        if not release.wait(timeout=2):
            raise TimeoutError(f"task {x} was not released")
        return x

    def collect() -> None:
        try:
            results.extend(batch_stream(gated, _kw(1, 2, 3), max_workers=3, ordered=True, on_error="raise"))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=collect)
    thread.start()
    try:
        assert all(started[item].wait(timeout=2) for item in started)
        release.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        if errors:
            raise errors[0]
        assert _values(results) == [1, 2, 3]
    finally:
        release.set()
        thread.join(timeout=2)


@pytest.mark.anyio
async def test_abatch_basic_modes():
    ordered = await abatch(_async_double, _kw(3, 1, 2), max_workers=2, ordered=True)
    unordered = await abatch(_async_double, _kw(3, 1, 2), max_workers=2, ordered=False)
    sync_offloaded = await abatch(_double, _kw(3, 1, 2), max_workers=2, ordered=True)

    assert _values(ordered) == [6, 2, 4]
    assert all(result.ok for result in ordered)
    assert sorted(_values(unordered)) == [2, 4, 6]
    assert _values(sync_offloaded) == [6, 2, 4]
    assert _values(await abatch(_async_double, _kw(1, 2, 3), max_workers=1, ordered=True)) == [2, 4, 6]
    assert await abatch(_async_double, []) == []


@pytest.mark.anyio
async def test_abatch_error_modes_and_progress():
    calls = []

    def hook(done: int, total: int) -> None:
        calls.append((done, total))

    captured = await abatch(_async_maybe_fail, _kw(1, 2, 3), max_workers=2, ordered=True, on_error="capture")
    skipped = await abatch(_async_maybe_fail, _kw(1, 2, 3), max_workers=2, ordered=True, on_error="skip")
    await abatch(_async_double, _kw(1, 2, 3), max_workers=2, on_progress=hook)

    assert [captured[0].value, type(captured[1].error), captured[2].value] == [2, ValueError, 6]
    assert [result.ok for result in captured] == [True, False, True]
    assert _values(skipped) == [2, 6]
    assert calls == [(1, 3), (2, 3), (3, 3)]
    with pytest.raises(ValueError, match="async bad 2"):
        await abatch(_async_maybe_fail, _kw(1, 2, 3), max_workers=2, ordered=True, on_error="raise")


@pytest.mark.anyio
async def test_abatch_raise_stops_starting_pending_work():
    started = []

    async def fail_first(x: int) -> int:
        started.append(x)
        if x == 0:
            raise ValueError("async boom")
        await asyncio.sleep(0.10)
        return x

    with pytest.raises(ValueError, match="async boom"):
        await abatch(fail_first, _kw(*range(20)), max_workers=2, ordered=False, on_error="raise")
    assert len(started) <= 2


@pytest.mark.anyio
async def test_abatch_stream_ordering_skip_and_empty():
    started = {1: asyncio.Event(), 2: asyncio.Event()}
    release = {1: asyncio.Event(), 2: asyncio.Event()}

    async def gated(x: int) -> int:
        if x in release:
            started[x].set()
            await release[x].wait()
        return x

    stream = abatch_stream(gated, _kw(1, 2, 3), max_workers=3, ordered=False)
    try:
        assert (await anext(stream)).value == 3
        await asyncio.wait_for(started[1].wait(), timeout=2)
        await asyncio.wait_for(started[2].wait(), timeout=2)
        release[2].set()
        assert (await anext(stream)).value == 2
        release[1].set()
        assert _values([result async for result in stream]) == [1]
    finally:
        release[1].set()
        release[2].set()

    ordered = [result async for result in abatch_stream(_async_double, _kw(3, 1, 2), max_workers=2, ordered=True)]
    skipped = [result async for result in abatch_stream(_async_maybe_fail, _kw(1, 2, 3), max_workers=2, ordered=True, on_error="skip")]

    assert _values(ordered) == [6, 2, 4]
    assert _values(skipped) == [2, 6]
    assert [result async for result in abatch_stream(_async_double, [])] == []


@pytest.mark.anyio
async def test_abatch_stream_ordered_still_runs_concurrently():
    async def same_delay(x: int) -> int:
        await asyncio.sleep(0.08)
        return x

    start = time.perf_counter()
    results = [result async for result in abatch_stream(same_delay, _kw(1, 2, 3), max_workers=3, ordered=True)]

    assert _values(results) == [1, 2, 3]
    assert time.perf_counter() - start < 0.20


@pytest.mark.anyio
@pytest.mark.parametrize("streaming", [False, True])
async def test_abatch_cancellation_starts_and_cancels_pending_tasks(streaming):
    started = []

    async def track(x: int) -> int:
        started.append(x)
        await asyncio.sleep(0.5)
        return x

    async def consume_stream() -> list:
        return [result async for result in abatch_stream(track, _kw(1, 2, 3), max_workers=3, ordered=False)]

    task = asyncio.create_task(consume_stream() if streaming else abatch(track, _kw(1, 2, 3), max_workers=3, ordered=True))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(started) >= 1


def test_pmap_pforeach_and_task_result_helpers():
    out = []

    assert pmap(_double, _kw(3, 1, 2), max_workers=2) == [6, 2, 4]
    with pytest.raises(ValueError, match="bad 2"):
        pmap(_maybe_fail, _kw(1, 2, 3), max_workers=2)

    pforeach(lambda x: out.append(x), _kw(1, 2, 3), max_workers=1)
    assert sorted(out) == [1, 2, 3]

    def fail(x: int) -> None:
        if x == 2:
            raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        pforeach(fail, _kw(1, 2, 3), max_workers=2)

    error_result = TaskResult(index=0, item="a", ok=False, error=ValueError("x"))
    ok_result = TaskResult(index=1, item="b", ok=True, value=42)

    assert error_result.to_dict() == {"index": 0, "item": "a", "ok": False, "value": None, "error": "x", "elapsed_ms": None}
    ok_result.raise_if_error()
