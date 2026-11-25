"""The DuckDB write gate of the keyword-index check: no hang, no stale index, and the gate released on every path."""

import threading
import time

import pytest
from sqlalchemy import text

import heavenbase as hb
from heavenbase.backends.sql.strategies import sparse_gram
from heavenbase.backends.sql.strategies.sparse_gram import SparseGramIndexConflictError
from heavenbase.execution.contracts import RowOp

pytest.importorskip("duckdb_engine")

pytestmark = pytest.mark.full


class Doc(hb.Entity):
    identifier = "duck-gate-doc"
    labels = hb.field(hb.Array[hb.ShortText]).store(to="notes")


class IndexedDoc(hb.Entity):
    identifier = "duck-gate-doc"
    labels = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2))


class WiderDoc(hb.Entity):
    identifier = "duck-gate-doc"
    labels = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=3))


def _ws(tmp_path, name, shared):
    return hb.HeavenBase(
        name,
        detached=True,
        backends={
            "main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / f"{name}-system.db")},
            "notes": {"type": "duckdb", "database": shared},
        },
    )


@pytest.fixture
def shared(tmp_path):
    return hb.utils.path_to_file_uri(tmp_path / "shared.duckdb")


def _in_thread(fn, timeout=30):
    """Run ``fn`` in a thread; return (finished, failures, result)."""
    out, failures = [], []

    def run():
        try:
            out.append(fn())
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    return (not thread.is_alive()), failures, out


def _gates_held():
    return {key: (dict(gate[0]), gate[1]) for key, gate in sparse_gram._DUCKDB_GATES._gates.items() if gate[0] or gate[1] is not None}


def test_a_change_after_an_idle_writer_committed_does_not_wait(tmp_path, shared):
    writer, registrar = _ws(tmp_path, "duck-gate-w", shared), _ws(tmp_path, "duck-gate-r", shared)
    try:
        writer.register(IndexedDoc)
        writer.upsert(IndexedDoc, {"object_id": "d1", "labels": ["alpha"]})
        registrar.register(IndexedDoc)
        done, failures, _ = _in_thread(lambda: registrar.register(WiderDoc, allow_replace=True))
        assert done and failures == [], (done, failures, _gates_held())
        assert sorted(registrar.query(WiderDoc).where(WiderDoc.labels.contained_in("alpha")).execute().ids) == ["d1"]
    finally:
        registrar.release()
        writer.release()


def test_a_commit_inside_an_open_context_releases_the_gate(tmp_path, shared):
    writer, registrar = _ws(tmp_path, "duck-gate-w2", shared), _ws(tmp_path, "duck-gate-r2", shared)
    try:
        writer.register(IndexedDoc)
        registrar.register(IndexedDoc)
        backend = writer.backends.get("notes")
        with backend.db.tx():
            backend.upsert([RowOp(IndexedDoc.identifier, "d1", {"labels": ["alpha"]})])
            backend.db.commit()  # the context stays open in a fresh transaction
            done, failures, _ = _in_thread(lambda: registrar.register(WiderDoc, allow_replace=True), timeout=20)
            held = _gates_held()
        assert done and failures == [], (done, failures, held)
    finally:
        registrar.release()
        writer.release()


def test_a_rolled_back_or_refused_write_leaves_no_gate_held(tmp_path, shared):
    writer, registrar = _ws(tmp_path, "duck-gate-w3", shared), _ws(tmp_path, "duck-gate-r3", shared)
    try:
        writer.register(IndexedDoc)
        registrar.register(IndexedDoc)
        backend = writer.backends.get("notes")
        with pytest.raises(RuntimeError, match="roll back"):
            with backend.db.tx():
                backend.upsert([RowOp(IndexedDoc.identifier, "d1", {"labels": ["alpha"]})])
                raise RuntimeError("roll back")
        assert _gates_held() == {}
        registrar.register(WiderDoc, allow_replace=True)
        with pytest.raises(SparseGramIndexConflictError):
            writer.upsert(IndexedDoc, {"object_id": "d2", "labels": ["bravo"]})
        assert _gates_held() == {}
        done, failures, _ = _in_thread(lambda: registrar.register(IndexedDoc, allow_replace=True), timeout=20)
        assert done and failures == [], (done, failures, _gates_held())
    finally:
        registrar.release()
        writer.release()


def test_the_same_thread_changing_an_index_it_writes_in_an_open_transaction_is_refused_not_deadlocked(tmp_path, shared):
    writer, registrar = _ws(tmp_path, "duck-gate-w4", shared), _ws(tmp_path, "duck-gate-r4", shared)
    try:
        writer.register(IndexedDoc)
        registrar.register(IndexedDoc)
        backend = writer.backends.get("notes")

        def same_thread():
            with backend.db.tx():
                backend.upsert([RowOp(IndexedDoc.identifier, "d1", {"labels": ["alpha"]})])
                registrar.register(WiderDoc, allow_replace=True)

        done, failures, _ = _in_thread(same_thread, timeout=20)
        assert done, _gates_held()
        assert len(failures) == 1 and isinstance(failures[0], SparseGramIndexConflictError), failures
        assert _gates_held() == {}
    finally:
        registrar.release()
        writer.release()


def _writer_loop(ws, worker, count, results, errors, definition_box):
    current = [IndexedDoc]
    for i in range(count):
        object_id = f"w{worker}-{i}"
        for _attempt in range(20):
            try:
                ws.upsert(current[0], {"object_id": object_id, "labels": ["alpha"]})
                results.append(object_id)
                break
            except SparseGramIndexConflictError as exc:
                if "other SparseGramIndex options" in str(exc) or "rebuilt" in str(exc):
                    current[0] = WiderDoc
                    ws.register(WiderDoc, allow_replace=True)
                continue
            except ValueError as exc:
                if "registered with a different structure" not in str(exc):
                    errors.append(repr(exc))
                    break
                # another thread registered the wider definition in this shared workspace
                current[0] = WiderDoc
                continue
            except Exception as exc:  # reported
                errors.append(repr(exc))
                break


def test_concurrent_writers_and_a_rebuild_neither_hang_nor_leave_the_index_stale(tmp_path, shared):
    writers = [_ws(tmp_path, f"duck-gate-cw{n}", shared) for n in range(3)]
    registrar = _ws(tmp_path, "duck-gate-cr", shared)
    try:
        for ws in writers:
            ws.register(IndexedDoc)
        registrar.register(IndexedDoc)
        box = [IndexedDoc]
        results, errors = [], []
        threads = [threading.Thread(target=_writer_loop, args=(ws, n, 40, results, errors, box), daemon=True) for n, ws in enumerate(writers)]
        for thread in threads:
            thread.start()
        time.sleep(0.3)
        box[0] = WiderDoc
        done, failures, _ = _in_thread(lambda: registrar.register(WiderDoc, allow_replace=True), timeout=90)
        for thread in threads:
            thread.join(120)
        assert done and failures == [], (done, failures, _gates_held())
        assert not any(thread.is_alive() for thread in threads), _gates_held()
        assert errors == []
        stored = sorted(row["object_id"] for row in registrar.backends.get("notes").scan(WiderDoc.identifier))
        assert sorted(results) == stored
        assert sorted(registrar.query(WiderDoc).where(WiderDoc.labels.contained_in("alpha")).execute().ids) == stored
    finally:
        registrar.release()
        for ws in writers:
            ws.release()


def test_one_shared_workspace_written_by_threads_while_one_rebuilds_does_not_deadlock(tmp_path, shared):
    ws = _ws(tmp_path, "duck-gate-one", shared)
    try:
        ws.register(IndexedDoc)
        box = [IndexedDoc]
        results, errors = [], []
        threads = [threading.Thread(target=_writer_loop, args=(ws, n, 30, results, errors, box), daemon=True) for n in range(4)]
        for thread in threads:
            thread.start()
        time.sleep(0.2)
        box[0] = WiderDoc
        done, failures, _ = _in_thread(lambda: ws.register(WiderDoc, allow_replace=True), timeout=90)
        for thread in threads:
            thread.join(120)
        assert done and failures == [], (done, failures, _gates_held())
        assert not any(thread.is_alive() for thread in threads), _gates_held()
        assert errors == []
        stored = sorted(row["object_id"] for row in ws.backends.get("notes").scan(WiderDoc.identifier))
        assert sorted(results) == stored
        assert sorted(ws.query(WiderDoc).where(WiderDoc.labels.contained_in("alpha")).execute().ids) == stored
    finally:
        ws.release()


def test_a_write_transaction_that_only_read_another_table_first_is_refused_after_a_change(tmp_path, shared):
    """Any statement the transaction ran before it took the gate counts, even one that reads no table (see gram-backend.md)."""
    writer, registrar = _ws(tmp_path, "duck-gate-w5", shared), _ws(tmp_path, "duck-gate-r5", shared)
    try:
        writer.register(IndexedDoc)
        registrar.register(IndexedDoc)
        backend = writer.backends.get("notes")
        with pytest.raises(SparseGramIndexConflictError, match="snapshot"):
            with backend.db.tx():
                backend.db.orm_execute(text("SELECT 1"))
                registrar.register(WiderDoc, allow_replace=True)
                backend.upsert([RowOp(IndexedDoc.identifier, "d1", {"labels": ["alpha"]})])
        assert _gates_held() == {}
    finally:
        registrar.release()
        writer.release()
