"""Regression tests for Context shutdown and composition."""

from concurrent import futures
from concurrent.futures import Future
from contextlib import contextmanager
import inspect
import sys
import threading

import pytest
from sqlalchemy import event

import heavenbase as hb
from heavenbase.context import load_bootstrap, runtime
from heavenbase.context.reset import reset_default_local_state
from heavenbase.utils.config import ConfigManager

pytestmark = pytest.mark.fast


def _context(tmp_path, *, sqlite=False, config=None):
    if config is None:
        config = ConfigManager(root=str(tmp_path), default={"application": {"marker": "default"}}, refresh_interval=3600)
    backend = (
        load_bootstrap(home_path=tmp_path / "missing.yaml").backend
        if sqlite
        else {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"}
    )
    return hb.Context.load({"version": 1, "root": str(tmp_path), "workspace": "default", "backend": backend}, config=config)


def _on_thread(call):
    result = Future()

    def run():
        try:
            result.set_result(call())
        except BaseException as error:
            result.set_exception(error)

    threading.Thread(target=run, daemon=True).start()
    return result


def test_config_setup_releases_manager_lock_during_deferred_initialization(tmp_path, monkeypatch):
    config = ConfigManager(root=str(tmp_path), default={"application": {"marker": "default"}}, setup=True)
    context = _context(tmp_path, sqlite=True, config=config)
    assert config.get("application.marker") == "default"
    reset_default_local_state(context)
    entered, resume = threading.Event(), threading.Event()
    initialize = config._init_scope

    def pause_setup(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert resume.wait(10)
        return initialize(*args, **kwargs)

    monkeypatch.setattr(config, "_init_scope", pause_setup)
    reader = _on_thread(lambda: config.snapshot(refresh=True).get("application.marker"))
    try:
        assert entered.wait(10)
        assert _on_thread(context.deactivate_workspace).result(5) is None
    finally:
        resume.set()
        assert reader.result(10) == "default"
        context.close()


def test_config_snapshot_retries_refresh_after_context_reset(tmp_path, monkeypatch):
    context = _context(tmp_path, sqlite=True)
    config = context.config
    config.set("application.marker", "saved")
    registry = config._scope_registry
    entered, resume = threading.Event(), threading.Event()
    refresh = registry.refresh

    def pause_refresh():
        entered.set()
        assert resume.wait(10)
        return refresh()

    monkeypatch.setattr(registry, "refresh", pause_refresh)
    reader = _on_thread(lambda: config.snapshot(refresh=True).get("application.marker"))
    try:
        assert entered.wait(10)
        reset_default_local_state(context)
    finally:
        resume.set()
    try:
        assert reader.result(10) == "default"
    finally:
        context.close()


@pytest.mark.parametrize("shutdown", ["close", "reset"])
def test_sqlite_disposal_can_close_the_context(tmp_path, shutdown):
    context = _context(tmp_path, sqlite=True)
    backend = context.backend()
    engine = backend.db.engine
    disposed = []

    @event.listens_for(engine, "engine_disposed")
    def close_again(disposed_engine):
        context.close()
        disposed.append(disposed_engine)

    _on_thread(context.close if shutdown == "close" else lambda: reset_default_local_state(context)).result(10)

    assert disposed == [engine]
    if shutdown == "reset":
        assert context.backend() is not backend
    else:
        with pytest.raises(RuntimeError, match="Context is closed"):
            context.backend()
    context.close()


class _Note(hb.Entity):
    identifier = "context-lifecycle-note"
    name = hb.field(hb.ShortText)


def _workspace(context, name):
    workspace = hb.HeavenBase(name, context=context, detached=True, backends={"main": {"type": "inmem"}})
    workspace.register(_Note)
    workspace.upsert(_Note, {"object_id": "n1", "name": "one"})
    return workspace


@pytest.mark.slow
@pytest.mark.parametrize("sibling_reading", [False, True])
def test_reader_can_close_only_an_idle_sibling(tmp_path, monkeypatch, sibling_reading):
    context = _context(tmp_path)
    reader = context.child("reader")
    sibling = context.child("sibling")
    workspace = _workspace(reader, "reader")
    sibling_workspace = _workspace(sibling, "sibling")
    backend = workspace.backends.get("main")
    sibling_backend = sibling_workspace.backends.get("main")
    get_many = backend.get_many
    sibling_get_many = sibling_backend.get_many
    sibling_entered = threading.Event()
    release = threading.Event()

    def read_and_close(*args, **kwargs):
        if sibling_reading:
            with pytest.raises(RuntimeError, match="active workspace read"):
                sibling.close()
        else:
            sibling.close()
        return get_many(*args, **kwargs)

    def hold_sibling_read(*args, **kwargs):
        sibling_entered.set()
        assert release.wait(10)
        return sibling_get_many(*args, **kwargs)

    monkeypatch.setattr(backend, "get_many", read_and_close)
    monkeypatch.setattr(sibling_backend, "get_many", hold_sibling_read)
    sibling_read = _on_thread(lambda: sibling_workspace.get("n1", entity=_Note)) if sibling_reading else None
    try:
        if sibling_read is not None:
            assert sibling_entered.wait(10)
        assert _on_thread(lambda: workspace.get("n1", entity=_Note)).result(10)["name"] == "one"
        if sibling_read is None:
            with pytest.raises(RuntimeError, match="Context is closed"):
                sibling.backend()
        assert reader.backend() is context.backend()
    finally:
        release.set()

    if sibling_read is not None:
        assert sibling_read.result(10)["name"] == "one"
    context.close()


def test_failed_default_installation_leaves_room_for_a_healthy_context(tmp_path, monkeypatch):
    closed = _context(tmp_path / "closed")
    healthy = _context(tmp_path / "healthy")
    closed.close()
    monkeypatch.setattr(runtime, "_DEFAULT_CONTEXT", None)

    try:
        with pytest.raises(RuntimeError, match="Context is closed"):
            runtime.set_default_context(closed)
        assert runtime.set_default_context(healthy) is healthy
        assert runtime.default_context() is healthy
        workspace = hb.HeavenBase("explicit-default", detached=True, backends={"main": {"type": "inmem"}})
        assert workspace.context is healthy
    finally:
        healthy.close()


def test_first_default_assignment_refuses_a_closed_context(tmp_path, monkeypatch):
    closed = _context(tmp_path)
    closed.close()
    monkeypatch.setattr(runtime, "_DEFAULT_CONTEXT", None)
    monkeypatch.setattr(runtime, "_DEFAULT_BEFORE_ASSIGNMENT", None)
    monkeypatch.setitem(vars(runtime), "DEFAULT_CONTEXT", runtime._UNASSIGNED)

    try:
        with pytest.raises(RuntimeError, match="Context is closed"):
            runtime.DEFAULT_CONTEXT = closed
        assert runtime._DEFAULT_CONTEXT is None
    finally:
        del runtime.DEFAULT_CONTEXT


def test_reset_invalidates_cached_config_reads(tmp_path):
    context = _context(tmp_path, sqlite=True)
    config = context.config
    config.set("application.marker", "saved")
    assert config.get("application.marker") == "saved"

    reset_default_local_state(context)

    assert config.get("application.marker") == "default"
    config.set("application.marker", "new")
    assert config.get("application.marker") == "new"
    context.close()


def _interleave_backend_lookup(context, method, prefixes, retire):
    """Pause one lookup at the indicated line while the caller retires resources."""
    source, first = inspect.getsourcelines(method)
    target = next(first + i for i, line in enumerate(source) if line.strip().startswith(prefixes))
    paused = threading.Event()
    resume = threading.Event()

    def trace(frame, kind, arg):
        if kind == "line" and frame.f_code is method.__code__ and frame.f_lineno == target:
            paused.set()
            assert resume.wait(15)
        return trace

    def lookup():
        sys.settrace(trace)
        try:
            return context.backend()
        finally:
            sys.settrace(None)

    result = _on_thread(lookup)
    try:
        assert paused.wait(10)
        _on_thread(retire).result(10)
    finally:
        resume.set()
    return result.result(10)


def test_backend_lookup_racing_with_close_keeps_its_cached_reference(tmp_path):
    context = _context(tmp_path)
    cached = context.backend()

    assert (
        _interleave_backend_lookup(
            context,
            hb.Context._control_backend,
            ("return self._backend", "return backend"),
            context.close,
        )
        is cached
    )


def test_backend_lookup_racing_with_reset_finishes_the_current_backend(tmp_path):
    context = _context(tmp_path, sqlite=True)
    assert context._control_backend()._module_resolver is None

    backend = _interleave_backend_lookup(
        context,
        hb.Context.backend,
        "if backend._module_resolver is",
        lambda: reset_default_local_state(context),
    )

    assert backend is context.backend()
    assert backend.module_resolver is context.modules()
    context.close()


@pytest.mark.slow
def test_close_waits_for_an_active_writer_and_reads_can_still_start(tmp_path, monkeypatch):
    context = _context(tmp_path)
    writer = _workspace(context, "writer")
    reader = _workspace(context, "reader")
    backend = writer.backends.get("main")
    upsert = backend.upsert
    writing = threading.Event()
    release = threading.Event()

    def pause_upsert(*args, **kwargs):
        writing.set()
        assert release.wait(15)
        return upsert(*args, **kwargs)

    monkeypatch.setattr(backend, "upsert", pause_upsert)
    write = _on_thread(lambda: writer.upsert(_Note, {"object_id": "n2", "name": "two"}))
    try:
        assert writing.wait(10)
        closing = _on_thread(context.close)
        assert not _ended(closing, 0.3)  # the close waits for the write
        assert _on_thread(lambda: reader.get("n1", entity=_Note)).result(10)["name"] == "one"
    finally:
        release.set()
    write.result(10)
    closing.result(10)
    assert context._closed


@pytest.mark.slow
def test_close_gives_up_after_its_wait_and_leaves_the_context_open(tmp_path, monkeypatch):
    context = _context(tmp_path)
    writer = _workspace(context, "writer")
    backend = writer.backends.get("main")
    upsert = backend.upsert
    writing = threading.Event()
    release = threading.Event()

    def pause_upsert(*args, **kwargs):
        writing.set()
        assert release.wait(15)
        return upsert(*args, **kwargs)

    monkeypatch.setattr(runtime, "_SHUTDOWN_WAIT", 0.3, raising=False)
    monkeypatch.setattr(backend, "upsert", pause_upsert)
    write = _on_thread(lambda: writer.upsert(_Note, {"object_id": "n2", "name": "two"}))
    try:
        assert writing.wait(10)
        with pytest.raises(RuntimeError, match="Context lock is held by another thread"):
            _on_thread(context.close).result(10)
        with pytest.raises(RuntimeError, match="Context lock is held by another thread"):
            _on_thread(lambda: reset_default_local_state(context)).result(10)
    finally:
        release.set()
    write.result(10)
    monkeypatch.setattr(backend, "upsert", upsert)
    writer.upsert(_Note, {"object_id": "n3", "name": "three"})  # the Context stays open
    assert writer.count(_Note) == 3
    context.close()


def _ended(future, seconds):
    """Return whether ``future`` ends within ``seconds``."""
    try:
        future.exception(seconds)
    except futures.TimeoutError:
        return False
    return True


@pytest.fixture
def unused_default(monkeypatch):
    monkeypatch.setattr(runtime, "_DEFAULT_CONTEXT", None)
    monkeypatch.setattr(runtime, "_DEFAULT_BEFORE_ASSIGNMENT", None)
    monkeypatch.setitem(vars(runtime), "DEFAULT_CONTEXT", runtime._UNASSIGNED)


@pytest.mark.parametrize("by_assignment", [False, True])
def test_default_publication_serializes_with_close(tmp_path, monkeypatch, unused_default, by_assignment):
    candidate = _context(tmp_path)
    checked_open = threading.Event()
    publish = threading.Event()
    close_called = threading.Event()
    close_marked = threading.Event()
    need_open = candidate._need_open
    shutdown_lock = candidate._shutdown_lock
    service = candidate.modules()
    retire_service = service._retire_authority

    def wait_after_check():
        need_open()
        checked_open.set()
        assert publish.wait(10)

    @contextmanager
    def start_close(*args):
        close_called.set()
        with shutdown_lock(*args):
            yield

    def record_close_mark():
        close_marked.set()
        assert runtime._default_context_instance() is candidate
        retire_service()

    def install():
        if by_assignment:
            runtime.DEFAULT_CONTEXT = candidate
        else:
            runtime.set_default_context(candidate)

    monkeypatch.setattr(candidate, "_need_open", wait_after_check)
    monkeypatch.setattr(candidate, "_shutdown_lock", start_close)
    monkeypatch.setattr(service, "_retire_authority", record_close_mark)
    install_result = _on_thread(install)
    try:
        assert checked_open.wait(10)
        close_result = _on_thread(candidate.close)
        assert close_called.wait(10)
        assert not close_marked.wait(0.25)
    finally:
        publish.set()
    install_result.result(10)
    close_result.result(10)
    assert close_marked.is_set()


@pytest.mark.parametrize("shutdown", ["close", "reset"])
def test_first_implicit_default_refuses_an_owner_in_disposal(tmp_path, monkeypatch, unused_default, shutdown):
    config = ConfigManager(root=str(tmp_path / "sqlite"), default={})
    monkeypatch.setattr(runtime, "CM_HVNB", config)
    context = _context(tmp_path / "sqlite", sqlite=True, config=config)
    candidate = _context(tmp_path / "candidate")
    failures = []

    @event.listens_for(context.backend().db.engine, "engine_disposed")
    def select_default(engine):
        with pytest.raises(RuntimeError, match=f"Context {shutdown} is in progress") as error:
            runtime.default_context()
        failures.append(error.value)

    try:
        if shutdown == "reset":
            reset_default_local_state(context)
        else:
            context.close()
        assert len(failures) == 1
        assert runtime._default_context_instance() is None
        assert runtime.set_default_context(candidate) is candidate
        assert runtime.set_default_context(candidate) is candidate
    finally:
        context.close()
        candidate.close()
