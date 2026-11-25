"""A DuckDB drop removes the database for the process also while another Context is using the file.

The cases that need only SQLite (a drop refused under the thread's own transaction or held response, thread identity)
run without the DuckDB driver too, as in the dev-only test environment.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from time import monotonic

import pytest
from sqlalchemy import event

import heavenbase as hb
from heavenbase.database import drop_database as module_drop_database
from heavenbase.database.registry import DatabaseEngineRegistry, DropRefusedError, _thread_token
from heavenbase.utils.config import ConfigManager

_HAS_DUCKDB = importlib.util.find_spec("duckdb") is not None and importlib.util.find_spec("duckdb_engine") is not None
if _HAS_DUCKDB:
    import duckdb

fast = pytest.mark.fast
needs_duckdb = pytest.mark.skipif(not _HAS_DUCKDB, reason="needs the DuckDB driver (duckdb and duckdb-engine)")

# Seconds. Only stops a deadlocked test; no passing run depends on it.
_HANG_GUARD = 600


class ShopOrder(hb.Entity):
    identifier = "duckdb-drop-shop-order"
    customer = hb.field(hb.ShortText)
    item = hb.field(hb.ShortText)


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


def _ready(context, root):
    # Indexing the packaged modules registers the SQL dialects that a Database facade resolves, as workspace creation does.
    context._setup_modules()


def _workspace(context, path, name="orders"):
    workspace = hb.HeavenBase(
        name,
        context=context,
        detached=True,
        backends={"main": {"type": "duckdb", "name": "main", "database": hb.utils.path_to_file_uri(path)}},
    )
    workspace.register(ShopOrder)
    return workspace


def _facade(context, path, provider="duckdb"):
    return hb.Database(database=hb.utils.path_to_file_uri(path), provider=provider, resolver=context.modules(), config=context.config)


@pytest.fixture(scope="module")
def contexts(tmp_path_factory):
    """A Context that drops the files and another that uses them, both ready for Database facades.

    The tests share them, since indexing the packaged modules takes seconds; each test uses its own files.
    """
    root = tmp_path_factory.mktemp("contexts")
    with closing(_context(root / "dropper")) as dropper, closing(_context(root / "user")) as user:
        _ready(dropper, root)
        _ready(user, root)
        yield dropper, user


def _reached(event_, future):
    """Wait until ``event_`` is set; fail at once if ``future`` ends first. The guard only stops a deadlocked test."""
    deadline = monotonic() + _HANG_GUARD
    while not event_.wait(0.05):
        if future.done():
            future.result()
            pytest.fail("the worker finished before it reached its pause point")
        assert monotonic() < deadline, "the worker did not reach its pause point"


def _reached_state(check, future):
    """Wait until ``check()`` is true; fail at once if ``future`` ends first. The guard only stops a deadlocked test."""
    deadline = monotonic() + _HANG_GUARD
    while not check():
        if future.done():
            future.result()
            pytest.fail("the worker finished before the expected state")
        assert monotonic() < deadline, "the expected state was not reached"
        threading.Event().wait(0.01)


def _tables_on_disk(path):
    """Read the file as a new DuckDB database; HeavenBase has closed every connection to it by then."""
    with closing(duckdb.connect(str(path))) as connection:
        names = [row[0] for row in connection.execute("SELECT table_name FROM information_schema.tables ORDER BY table_name").fetchall()]
        return {name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}


def _holding(facade, statement, held, finish):
    """Run ``statement`` in a transaction of ``facade`` and keep the transaction open until ``finish`` is set."""
    with facade.tx():
        facade.execute(statement)
        held.set()
        assert finish.wait(_HANG_GUARD)


def _deliveries(context, path):
    facade = _facade(context, path)
    facade.execute("CREATE TABLE deliveries (id INTEGER PRIMARY KEY, item TEXT)", readonly=False)
    facade.execute("INSERT INTO deliveries VALUES (1, 'desk')", readonly=False)
    return facade


@pytest.mark.slow
@pytest.mark.parametrize("statement", ["INSERT INTO deliveries VALUES (2, 'lamp')", "SELECT COUNT(*) FROM deliveries"], ids=["writer", "reader"])
@needs_duckdb
def test_a_drop_waits_for_a_call_of_another_context_on_the_file_and_leaves_no_database_behind(tmp_path, contexts, statement):
    dropper, user = contexts
    shared = tmp_path / "shop.duckdb"
    theirs = _deliveries(user, shared)
    held, finish = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        using = pool.submit(_holding, theirs, statement, held, finish)
        try:
            _reached(held, using)
            dropping = pool.submit(_facade(dropper, shared).drop_database)
            threading.Event().wait(1.0)
            # The drop removes nothing while the other Context's transaction keeps the file open.
            assert not dropping.done()
            assert shared.exists()
        finally:
            finish.set()
        using.result(timeout=_HANG_GUARD)
        dropping.result(timeout=_HANG_GUARD)
    assert not shared.exists()
    assert not (tmp_path / "shop.duckdb.wal").exists()
    # A facade created after the drop, in either Context, opens an empty database and keeps its writes.
    assert _facade(dropper, shared).tables() == []
    again = _facade(user, shared)
    assert again.tables() == []
    again.execute("CREATE TABLE returns (id INTEGER PRIMARY KEY)", readonly=False)
    again.execute("INSERT INTO returns VALUES (7)", readonly=False)
    DatabaseEngineRegistry.dispose_all()
    # Nothing of the dropped database is left in the process, and the write made after the drop is on disk.
    assert _tables_on_disk(shared) == {"returns": 1}


@pytest.mark.slow
@needs_duckdb
def test_a_checkout_that_waited_for_a_drop_does_not_hold_up_the_next_drop(tmp_path, contexts, monkeypatch):
    monkeypatch.setattr(DatabaseEngineRegistry, "_DUCKDB_DROP_WAIT", 5.0)
    dropper, user = contexts
    shared = tmp_path / "shop.duckdb"
    holder, late = _deliveries(user, shared), _facade(user, shared)
    assert late.tables() == ["deliveries"]
    path = DatabaseEngineRegistry._local_path(late.spec)
    held, finish, gated = threading.Event(), threading.Event(), threading.Event()
    creator = {}
    may_wait = DatabaseEngineRegistry._may_wait.__func__

    def watched(cls, spec):
        # Asked only by a checkout or connection opening that found the file closing for a drop.
        if threading.get_ident() == creator.get("thread"):
            gated.set()
        return may_wait(cls, spec)

    monkeypatch.setattr(DatabaseEngineRegistry, "_may_wait", classmethod(watched))

    def create():
        creator["thread"] = threading.get_ident()
        late.execute("CREATE TABLE returns (id INTEGER PRIMARY KEY)", readonly=False)

    with ThreadPoolExecutor(max_workers=3) as pool:
        using = pool.submit(_holding, holder, "INSERT INTO deliveries VALUES (2, 'lamp')", held, finish)
        try:
            _reached(held, using)
            dropping = pool.submit(_facade(dropper, shared).drop_database)
            # The drop has taken and disposed the engines on the file and marked it closing. It cannot end while the
            # transaction is open.
            _reached_state(lambda: path in DatabaseEngineRegistry._closing, dropping)
            # This statement needs a connection now: it reaches the closing file's gate and waits there.
            creating = pool.submit(create)
            _reached(gated, creating)
            # Neither can end before the transaction does.
            assert not creating.done()
            assert not dropping.done()
            assert shared.exists()
        finally:
            finish.set()
        using.result(timeout=_HANG_GUARD)
        dropping.result(timeout=_HANG_GUARD)
        creating.result(timeout=_HANG_GUARD)
    # The statement ran on the file the drop left, not on the removed database.
    assert _facade(user, shared).tables() == ["returns"]
    # The connection it opened after waiting is idle in a pool that the first drop's dispose replaced. The next drop
    # closes it there instead of waiting for it, and the file opens again without a stale database in the process.
    began = monotonic()
    _facade(dropper, shared).drop_database()
    assert monotonic() - began < 3.0
    assert not shared.exists()
    assert late.execute("SELECT COUNT(*) FROM information_schema.tables", readonly=True).to_lists() == [[0]]


@pytest.mark.slow
@needs_duckdb
def test_a_connection_set_up_while_a_drop_starts_is_waited_for(tmp_path, contexts):
    dropper, user = contexts
    shared = tmp_path / "shop.duckdb"
    _deliveries(user, shared)
    writer = _facade(user, shared)
    engine = writer.engine
    engine.dispose()
    opening, release = threading.Event(), threading.Event()
    writer_thread = {}

    def slow_setup(dbapi_connection, record):
        # Stands for connection setup that runs after DuckDB opened the file, such as configured pragmas.
        if threading.get_ident() == writer_thread.get("id") and not opening.is_set():
            opening.set()
            assert release.wait(_HANG_GUARD)

    event.listen(engine, "connect", slow_setup, insert=True)

    def write():
        writer_thread["id"] = threading.get_ident()
        writer.execute("CREATE TABLE returns (id INTEGER PRIMARY KEY)", readonly=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(write)
        try:
            _reached(opening, writing)
            dropping = pool.submit(_facade(dropper, shared).drop_database)
            threading.Event().wait(1.0)
            # The connection being set up already holds the file open, so the drop waits for it.
            assert not dropping.done()
        finally:
            release.set()
        writing.result(timeout=_HANG_GUARD)
        dropping.result(timeout=_HANG_GUARD)
    # The statement waited for the drop at its checkout and ran on the file the drop left: no facade reads the
    # dropped table afterwards, and what the statement created stays.
    assert _facade(user, shared).tables() == ["returns"]
    assert _facade(dropper, shared).tables() == ["returns"]
    DatabaseEngineRegistry.dispose_all()
    assert _tables_on_disk(shared) == {"returns": 0}


@pytest.mark.slow
@pytest.mark.skipif(not hasattr(os, "link"), reason="needs hard links")
@needs_duckdb
def test_a_drop_closes_connections_through_another_name_of_the_file(tmp_path, contexts):
    dropper, user = contexts
    shared = tmp_path / "shop.duckdb"
    _deliveries(user, shared)
    DatabaseEngineRegistry.dispose_all()
    alias = tmp_path / "shop-link.duckdb"
    os.link(shared, alias)
    # Another name of the file, as another letter case of its name on a case-insensitive file system is.
    holder = _facade(user, alias)
    held, finish = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        using = pool.submit(_holding, holder, "INSERT INTO deliveries VALUES (2, 'lamp')", held, finish)
        try:
            _reached(held, using)
            dropping = pool.submit(_facade(dropper, shared).drop_database)
            threading.Event().wait(1.0)
            # The drop waits for the transaction that holds the file open through its other name.
            assert not dropping.done()
        finally:
            finish.set()
        using.result(timeout=_HANG_GUARD)
        dropping.result(timeout=_HANG_GUARD)
    assert not shared.exists()


@pytest.mark.slow
@needs_duckdb
def test_a_drop_refuses_and_keeps_the_file_while_a_connection_stays_in_use(tmp_path, contexts, monkeypatch):
    monkeypatch.setattr(DatabaseEngineRegistry, "_DUCKDB_DROP_WAIT", 1.0)
    dropper, user = contexts
    shared = tmp_path / "shop.duckdb"
    holder = _deliveries(user, shared)
    held, finish = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        using = pool.submit(_holding, holder, "INSERT INTO deliveries VALUES (2, 'lamp')", held, finish)
        try:
            _reached(held, using)
            with pytest.raises(DropRefusedError, match="stayed in use"):
                _facade(dropper, shared).drop_database()
            assert shared.exists()
        finally:
            finish.set()
        using.result(timeout=_HANG_GUARD)
    # The transaction committed to the file the drop left in place.
    assert _facade(user, shared).execute("SELECT id FROM deliveries ORDER BY id", readonly=True).to_lists() == [[1], [2]]


# Builds DuckDB workspaces, which takes seconds, so it runs in the full tier.
@pytest.mark.full
@pytest.mark.slow
@needs_duckdb
def test_a_refused_workspace_drop_drops_no_table_and_leaves_the_context_writing(tmp_path, contexts, monkeypatch):
    monkeypatch.setattr(DatabaseEngineRegistry, "_DUCKDB_DROP_WAIT", 8.0)
    dropper, user = contexts
    shared = tmp_path / "shop.duckdb"
    orders = _workspace(dropper, shared)
    orders.upsert(ShopOrder, {"object_id": "seed", "customer": "Ada", "item": "desk"})
    notes = _workspace(dropper, tmp_path / "notes.duckdb", name="notes")
    notes.upsert(ShopOrder, {"object_id": "n0", "customer": "Grace", "item": "lamp"})
    holder = _deliveries(user, shared)
    held, finish = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=3) as pool:
        using = pool.submit(_holding, holder, "INSERT INTO deliveries VALUES (2, 'lamp')", held, finish)
        try:
            _reached(held, using)
            dropping = pool.submit(orders.drop)
            threading.Event().wait(0.5)
            # The drop waits for the other Context's transaction without holding its own Context lock: a write of the
            # same Context ends while the drop still waits.
            pool.submit(notes.upsert, ShopOrder, {"object_id": "n1", "customer": "Grace", "item": "lamp"}).result(timeout=_HANG_GUARD)
            assert not dropping.done()
            with pytest.raises(DropRefusedError, match="The file was not removed.*No route was dropped"):
                dropping.result(timeout=_HANG_GUARD)
        finally:
            finish.set()
        using.result(timeout=_HANG_GUARD)
    # Nothing was dropped: the other Context's transaction committed, and the workspace still reads its rows.
    assert holder.execute("SELECT id FROM deliveries ORDER BY id", readonly=True).to_lists() == [[1], [2]]
    assert orders.get("seed", ShopOrder)["item"] == "desk"
    orders.release()
    notes.release()


@pytest.mark.slow
@needs_duckdb
def test_a_drop_removes_the_duckdb_write_ahead_log(tmp_path, contexts):
    dropper, _ = contexts
    shared = tmp_path / "shop.duckdb"
    # A database whose last process stopped before DuckDB checkpointed it, so its write-ahead log is still there.
    with closing(duckdb.connect(str(shared))) as connection:
        connection.execute("PRAGMA disable_checkpoint_on_shutdown")
        connection.execute("SET checkpoint_threshold = '1GB'")
        connection.execute("CREATE TABLE deliveries (id INTEGER PRIMARY KEY, item TEXT)")
        connection.execute("INSERT INTO deliveries VALUES (1, 'desk')")
    wal = tmp_path / "shop.duckdb.wal"
    assert wal.exists()
    _facade(dropper, shared).drop_database()
    assert not shared.exists()
    assert not wal.exists()
    # The log is not replayed into the database created after the drop.
    assert _facade(dropper, shared).tables() == []


@pytest.mark.slow
@pytest.mark.parametrize("provider", ["sqlite", pytest.param("duckdb", marks=needs_duckdb)])
@pytest.mark.parametrize("drop", ["drop_database", "drop", "module-level drop_database"])
def test_a_thread_cannot_drop_a_database_it_holds_a_transaction_on(tmp_path, contexts, provider, drop):
    context, _ = contexts
    shared = tmp_path / f"shop.{provider}"
    working, dropping = _facade(context, shared, provider), _facade(context, shared, provider)
    working.execute("CREATE TABLE deliveries (id INTEGER PRIMARY KEY, item TEXT)", readonly=False)
    with working.tx():
        working.execute("INSERT INTO deliveries VALUES (1, 'desk')")
        began = monotonic()
        # The drop would wait for this thread's own transaction, or remove the file under it.
        with pytest.raises(DropRefusedError, match="connection to it in use"):
            if drop == "module-level drop_database":
                module_drop_database(dropping.spec)
            else:
                getattr(dropping, drop)()
        assert monotonic() - began < 1.0
    assert working.execute("SELECT item FROM deliveries", readonly=True).to_lists() == [["desk"]]
    dropping.drop_database()
    assert not shared.exists()


@pytest.mark.slow
@pytest.mark.parametrize("provider", ["sqlite", pytest.param("duckdb", marks=needs_duckdb)])
@pytest.mark.parametrize("read", ["available(check=True)", "unconsumed SELECT"])
def test_a_drop_right_after_a_read_whose_response_was_dropped(tmp_path, contexts, provider, read):
    context, _ = contexts
    shared = tmp_path / f"orders.{provider}"
    db = _facade(context, shared, provider)
    db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, item TEXT)", readonly=False)
    db.execute("INSERT INTO orders VALUES (1, 'desk')", readonly=False)
    if read == "available(check=True)":
        assert db.available(check=True)
    else:
        db.execute("SELECT id, item FROM orders", readonly=True)
    # The response is gone, so its connection is back in the pool: the thread holds no connection.
    assert not DatabaseEngineRegistry._holds_any_connection()
    db.drop_database()
    assert not shared.exists()


@pytest.mark.slow
def test_a_response_still_held_keeps_its_connection_in_use(tmp_path, contexts):
    context, _ = contexts
    shared = tmp_path / "orders.db"
    db = _facade(context, shared, "sqlite")
    db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, item TEXT)", readonly=False)
    response = db.execute("SELECT id FROM orders", readonly=True)
    assert DatabaseEngineRegistry._holds_any_connection()
    with pytest.raises(DropRefusedError):
        db.drop_database()
    response.close()
    assert not DatabaseEngineRegistry._holds_any_connection()
    db.drop_database()


@pytest.mark.slow
@fast
def test_a_connection_left_by_an_ended_thread_does_not_count_for_a_new_thread(tmp_path, contexts):
    context, _ = contexts
    shared = tmp_path / "orders.db"
    keeper = _facade(context, shared, "sqlite")
    keeper.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, item TEXT)", readonly=False)
    kept, tokens = [], []

    def keep():
        tokens.append(_thread_token())
        kept.append(keeper.engine.connect())

    first = threading.Thread(target=keep)
    first.start()
    first.join()
    seen = {}

    def check():
        seen["ident"], seen["token"] = threading.get_ident(), _thread_token()
        try:
            # The registry's refusal check of a drop by this thread.
            DatabaseEngineRegistry._need_no_connection(keeper.spec)
            seen["refused"] = False
        except DropRefusedError:
            seen["refused"] = True
        seen["holds"] = DatabaseEngineRegistry._holds_any_connection()

    try:
        # A new thread may get the ended thread's identifier, never its token, so it holds no connection and its drop
        # is not refused for the connection that the ended thread left checked out.
        second = threading.Thread(target=check)
        second.start()
        second.join()
        assert seen["token"] != tokens[0]
        assert seen["refused"] is False and seen["holds"] is False
    finally:
        for connection in kept:
            connection.close()
    # Once that connection is closed nothing holds the file, so the drop removes it on every platform.
    _facade(context, shared, "sqlite").drop_database()
    assert not shared.exists()


class RoutedNote(hb.Entity):
    identifier = "duckdb-drop-routed-note"
    title = hb.field(hb.ShortText)
    v = hb.field(hb.Vector[3]).store(to="vec", strategy=hb.VectorIndex.identifier).required()


def _routed_workspace(context, name, main, vec, main_type="sqlite", vec_type="sqlite"):
    workspace = hb.HeavenBase(
        name,
        context=context,
        detached=True,
        backends={
            "main": {"type": main_type, "name": "main", "database": hb.utils.path_to_file_uri(main)},
            "vec": {"type": vec_type, "name": "vec", "database": hb.utils.path_to_file_uri(vec)},
        },
    )
    workspace.register(RoutedNote)
    workspace.upsert(RoutedNote, {"object_id": "a", "title": "first", "v": [1.0, 0.0, 0.0]})
    return workspace


@pytest.mark.slow
@pytest.mark.parametrize("held", ["main", "vec"])
def test_a_refused_workspace_drop_drops_no_route(tmp_path, contexts, held):
    context, _ = contexts
    main, vec = tmp_path / "rows.db", tmp_path / "vectors.db"
    notes = _routed_workspace(context, f"routed-{held}", main, vec)
    # This thread keeps a read open on one route's database, as an unconsumed result it still holds does.
    facade = _facade(context, main if held == "main" else vec, "sqlite")
    open_read = facade.execute("SELECT name FROM sqlite_master", readonly=True)
    with pytest.raises(DropRefusedError, match="No route was dropped"):
        notes.drop()
    open_read.close()
    # Every route is checked before any is dropped: both files and the workspace's rows are still there.
    assert main.exists() and vec.exists()
    assert notes.get("a", RoutedNote)["title"] == "first"
    notes.upsert(RoutedNote, {"object_id": "b", "title": "second", "v": [0.0, 1.0, 0.0]})
    assert notes.get("b", RoutedNote)["title"] == "second"
    notes.drop()
    assert not main.exists() and not vec.exists()


@pytest.mark.full
@pytest.mark.slow
@needs_duckdb
@pytest.mark.parametrize("busy_route", ["main", "vec"])
def test_a_refused_duckdb_route_stops_the_workspace_drop_and_the_error_names_the_routes_already_dropped(tmp_path, contexts, monkeypatch, busy_route):
    monkeypatch.setattr(DatabaseEngineRegistry, "_DUCKDB_DROP_WAIT", 2.0)
    # The connection is lent after the wait that a workspace drop begins with, so the drop of the file itself refuses.
    monkeypatch.setattr(DatabaseEngineRegistry, "_await_duckdb_quiet", lambda spec: None)
    dropper, user = contexts
    # The routes are dropped in order: "main", then "vec". The DuckDB file is the route in use.
    busy, other = tmp_path / "busy.duckdb", tmp_path / "other.db"
    if busy_route == "main":
        notes = _routed_workspace(dropper, "stops-at-main", busy, other, main_type="duckdb")
    else:
        notes = _routed_workspace(dropper, "stops-at-vec", other, busy, vec_type="duckdb")
    holder = _deliveries(user, busy)
    held, finish = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        using = pool.submit(_holding, holder, "INSERT INTO deliveries VALUES (2, 'lamp')", held, finish)
        try:
            _reached(held, using)
            with pytest.raises(DropRefusedError) as refusal:
                notes.drop()
        finally:
            finish.set()
        using.result(timeout=_HANG_GUARD)
    assert busy.exists()
    if busy_route == "main":
        assert "No route was dropped." in str(refusal.value)
        assert other.exists()
        assert notes.get("a", RoutedNote)["title"] == "first"
    else:
        assert "Routes already dropped: main." in str(refusal.value)
        assert not other.exists()
    # The drop can be made again and drops what is left.
    notes.drop()
    assert not busy.exists() and not other.exists()


_LOOKUP = {}


def _priced(name):
    if _LOOKUP.get("pause"):
        _LOOKUP["inside"].set()
        assert _LOOKUP["go"].wait(_HANG_GUARD)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: _LOOKUP["facade"].execute("SELECT COUNT(*) FROM prices", readonly=True).to_lists(), range(2)))
    return f"{name}:{rows[0][0][0]}"


class PricedItem(hb.Entity):
    identifier = "duckdb-drop-priced-item"
    name = hb.field(hb.ShortText)
    label = hb.field(hb.ShortText).compute(_priced, inputs=["name"])


def _drop_during_a_write_whose_hook_reads_the_file(root):
    """Run the case of the next test in this process and end the process, also when the write and the drop hang."""
    root = Path(root)
    context = _context(root / "ctx")
    _ready(context, root)
    shared = root / "shop.duckdb"
    items = hb.HeavenBase(
        "priced-items", context=context, detached=True, backends={"main": {"type": "duckdb", "name": "main", "database": hb.utils.path_to_file_uri(shared)}}
    )
    prices = _facade(context, shared)
    prices.execute("CREATE TABLE prices (id INTEGER)", readonly=False)
    _LOOKUP.update(facade=prices, pause=False, inside=threading.Event(), go=threading.Event())
    items.register(PricedItem)
    _LOOKUP["pause"] = True
    outcomes = {"write": "stuck", "drop": "stuck"}

    def keep(name, call):
        try:
            call()
            outcomes[name] = "ok"
        except Exception as error:
            outcomes[name] = repr(error)

    write = threading.Thread(target=keep, args=("write", lambda: items.upsert(PricedItem, {"object_id": "a", "name": "desk"})), daemon=True)
    drop = threading.Thread(target=keep, args=("drop", items.drop), daemon=True)
    write.start()
    outcomes["reached"] = _LOOKUP["inside"].wait(60)
    drop.start()
    drop.join(2)
    # The drop waits for the write, outside the file: the write's own worker threads still read it.
    outcomes["drop_waited"] = drop.is_alive()
    _LOOKUP["go"].set()
    write.join(60)
    drop.join(60)
    print("OUTCOME " + json.dumps({**outcomes, "exists": shared.exists()}), flush=True)
    os._exit(0)  # A stuck write's worker threads would keep the interpreter from ending.


@pytest.mark.full
@pytest.mark.slow
@needs_duckdb
def test_a_workspace_drop_waits_for_a_write_whose_hook_reads_the_file_on_worker_threads(tmp_path, home_env):
    # In its own process: a write that hangs keeps its Context lock and its worker threads.
    path = Path(__file__)
    code = f"import sys; sys.path.insert(0, {str(path.parent)!r}); import {path.stem} as case; case._drop_during_a_write_whose_hook_reads_the_file(sys.argv[1])"
    home = tmp_path / "home"
    home.mkdir()
    command = [sys.executable, "-c", code, str(tmp_path)]
    child = subprocess.run(command, env=home_env(home), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=_HANG_GUARD)
    lines = [line for line in child.stdout.splitlines() if line.startswith("OUTCOME ")]
    assert lines, child.stdout + child.stderr
    assert json.loads(lines[0].removeprefix("OUTCOME ")) == {"write": "ok", "drop": "ok", "reached": True, "drop_waited": True, "exists": False}


@pytest.mark.full
@pytest.mark.slow
@needs_duckdb
def test_a_workspace_drop_is_not_held_up_by_steady_short_uses_of_the_file(tmp_path, contexts, monkeypatch):
    monkeypatch.setattr(DatabaseEngineRegistry, "_DUCKDB_DROP_WAIT", 5.0)
    dropper, user = contexts
    shared = tmp_path / "shop.duckdb"
    orders = _workspace(dropper, shared, name="steady")
    orders.upsert(ShopOrder, {"object_id": "seed", "customer": "Ada", "item": "desk"})
    holder = _deliveries(user, shared)
    stop, uses = threading.Event(), []

    def use():
        while not stop.is_set():
            try:
                with holder.tx():
                    holder.execute("SELECT COUNT(*) FROM deliveries")
                    threading.Event().wait(0.05)
                uses.append(1)
            except Exception:  # the drop removed the table
                threading.Event().wait(0.01)

    # Each use is short, but with this many threads one of them is always using the file.
    threads = [threading.Thread(target=use, daemon=True) for _ in range(8)]
    try:
        for thread in threads:
            thread.start()
        deadline = monotonic() + 60
        while len(uses) < 16 and monotonic() < deadline:
            threading.Event().wait(0.05)
        orders.drop()
    finally:
        stop.set()
        for thread in threads:
            thread.join(60)
    assert not any(thread.is_alive() for thread in threads) and len(uses) >= 16


@pytest.mark.full
@pytest.mark.slow
@needs_duckdb
def test_a_workspace_drop_of_a_duckdb_file_that_cannot_be_removed_drops_its_tables(tmp_path, contexts, monkeypatch):
    context, _ = contexts
    shared = tmp_path / "shop.duckdb"
    orders = _workspace(context, shared, name="unremovable")
    orders.upsert(ShopOrder, {"object_id": "seed", "customer": "Ada", "item": "desk"})
    unlink = type(shared).unlink

    def held_open(self, *args, **kwargs):
        # As Windows answers for a file that another program holds open, or Linux for a bind-mounted one.
        if os.path.realpath(self) == os.path.realpath(shared):
            raise PermissionError(13, "The process cannot access the file", str(self))
        return unlink(self, *args, **kwargs)

    monkeypatch.setattr(type(shared), "unlink", held_open)
    orders.drop()
    monkeypatch.undo()
    DatabaseEngineRegistry.dispose_all()
    assert shared.exists()
    assert _tables_on_disk(shared) == {}


@fast
def test_a_close_that_a_garbage_collection_starts_inside_an_open_does_not_wait_on_it():
    from heavenbase.database.registry import _OpenCloseGate

    gate = _OpenCloseGate()
    closed = []
    with gate.held():
        # A collection inside DuckDB's open frees a pooled connection whose return closes it, on this thread.
        gate.close(lambda: closed.append("nested"))
        assert closed == []
        with gate.held():
            gate.close(lambda: closed.append("deeper"))
        assert closed == []
    # The deferred closes ran once the open let the gate go.
    assert closed == ["nested", "deeper"]
    # A close started inside a close is deferred the same way.
    gate.close(lambda: gate.close(lambda: closed.append("inside a close")))
    assert closed[-1] == "inside a close"


@pytest.mark.slow
def test_a_failing_deferred_close_is_logged_and_changes_nothing_else():
    from heavenbase.database.registry import _OpenCloseGate

    gate = _OpenCloseGate()
    closed = []

    def failing():
        raise OSError("disk full")

    with gate.held():
        gate.close(failing)
        gate.close(lambda: closed.append("after the failing one"))
    assert closed == ["after the failing one"]
    # The open's own error is the one that is raised.
    with pytest.raises(ValueError, match="the open failed"):
        with gate.held():
            gate.close(failing)
            raise ValueError("the open failed")
    # A close that is not deferred still raises to its caller.
    with pytest.raises(OSError, match="disk full"):
        gate.close(failing)


@fast
@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_a_forked_child_does_not_wait_for_a_gate_that_a_thread_of_the_parent_holds():
    from heavenbase.database.registry import _OpenCloseGate

    gate = _OpenCloseGate()
    inside, release = threading.Event(), threading.Event()

    def open_slowly():
        with gate.held():
            inside.set()
            release.wait(_HANG_GUARD)

    opener = threading.Thread(target=open_slowly, daemon=True)
    opener.start()
    assert inside.wait(60)
    child = os.fork()
    if child == 0:
        # The close of an inherited connection passes the gate that its opening passed in the parent.
        gate.close(lambda: os._exit(0))
        os._exit(1)
    try:
        deadline = monotonic() + 30
        ended, status = os.waitpid(child, os.WNOHANG)
        while not ended and monotonic() < deadline:
            threading.Event().wait(0.05)
            ended, status = os.waitpid(child, os.WNOHANG)
        if not ended:
            os.kill(child, 9)
            os.waitpid(child, 0)
        assert ended and status == 0
    finally:
        release.set()
        opener.join(60)


@pytest.mark.slow
@fast
@needs_duckdb
def test_a_connection_closed_on_the_opening_thread_during_an_open_does_not_hang(tmp_path, contexts, monkeypatch):
    context, _ = contexts
    shared = tmp_path / "shop.duckdb"

    def facade():
        return hb.Database(
            database=hb.utils.path_to_file_uri(shared), provider="duckdb", pool={"pool_class": "null"}, resolver=context.modules(), config=context.config
        )

    facade().execute("CREATE TABLE items (id INTEGER)", readonly=False)
    pending = facade().engine.connect()
    dialect_type = type(facade().engine.dialect)
    connect = dialect_type.connect
    state = {"done": False}

    def connect_and_return_another(self, *cargs, **cparams):
        # Stands for a garbage collection inside DuckDB's open that frees another pooled connection to the file:
        # its return closes it (a NullPool closes every returned connection) on this thread, inside the open.
        if not state["done"]:
            state["done"] = True
            pending.close()
        return connect(self, *cargs, **cparams)

    monkeypatch.setattr(dialect_type, "connect", connect_and_return_another)
    worker = threading.Thread(target=lambda: facade().execute("SELECT COUNT(*) FROM items", readonly=True).to_lists(), daemon=True)
    worker.start()
    worker.join(30)
    assert not worker.is_alive(), "the open waited on a close that it started itself"
    assert state["done"]


@pytest.mark.full
@pytest.mark.slow
@needs_duckdb
def test_a_refused_workspace_drop_says_which_route_drops_failed_before(tmp_path, contexts, monkeypatch):
    monkeypatch.setattr(DatabaseEngineRegistry, "_DUCKDB_DROP_WAIT", 2.0)
    monkeypatch.setattr(DatabaseEngineRegistry, "_await_duckdb_quiet", lambda spec: None)
    dropper, user = contexts
    main, busy = tmp_path / "rows.db", tmp_path / "busy.duckdb"
    notes = _routed_workspace(dropper, "failed-before", main, busy, vec_type="duckdb")

    def failing():
        raise OSError("disk error")

    monkeypatch.setattr(notes.backends.get("main"), "drop", failing)
    holder = _deliveries(user, busy)
    held, finish = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        using = pool.submit(_holding, holder, "INSERT INTO deliveries VALUES (2, 'lamp')", held, finish)
        try:
            _reached(held, using)
            with pytest.raises(DropRefusedError) as refusal:
                notes.drop()
        finally:
            finish.set()
        using.result(timeout=_HANG_GUARD)
    assert str(refusal.value).endswith("Routes whose drop failed, so that they can be partly dropped: main.")
    assert "disk error" in str(refusal.value) and "No route was dropped" not in str(refusal.value)
    monkeypatch.undo()
    notes.drop()
