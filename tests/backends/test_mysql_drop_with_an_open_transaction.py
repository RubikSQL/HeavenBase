"""A MySQL ``drop_database()`` while another thread of the same Context holds a transaction on the database."""

import os
import threading
import time
from contextlib import closing
from uuid import uuid4

import pytest
import sqlalchemy as sa

import heavenbase as hb
from heavenbase.llm.cache import LLMCache
from heavenbase.utils.config import ConfigManager

_KEY = {"object_id": "text-row", "spec_key": "spec", "request_key": "request", "args": {}}
# Seconds. Only stop a deadlocked run; no passing run depends on them.
_DROP_SEEN = 60
_BOTH_END = 60


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


def _facade(context, server, database):
    return hb.Database(
        database=database,
        provider="mysql",
        host=server.host,
        port=server.port or 3306,
        username=server.username,
        password=server.password or "",
        resolver=context.modules(),
        config=context.config,
    )


def _waiting_drop(admin, database):
    """Return the connection id of the DROP DATABASE statement of ``database`` while it runs, or ``None``."""
    for row in admin.exec_driver_sql("SHOW FULL PROCESSLIST").fetchall():
        info = row[7] or ""
        if info.upper().startswith("DROP DATABASE") and database in info:
            return row[0]
    return None


@pytest.mark.full
@pytest.mark.parametrize("call", ["clone", "tables"])
def test_a_mysql_drop_and_a_transaction_of_the_same_context_on_the_database_both_end(tmp_path, call):
    url = os.environ.get("HVNB_MYSQL_URL")
    if not url:
        pytest.skip("requires a dedicated MySQL test database in HVNB_MYSQL_URL")
    server = sa.engine.make_url(url)
    database = f"hb_drop_tx_{uuid4().hex[:12]}"
    admin_engine = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as admin:
            admin.exec_driver_sql(f"CREATE DATABASE `{database}`")
            admin.exec_driver_sql(f"CREATE TABLE `{database}`.orders (id INT PRIMARY KEY, item VARCHAR(40))")
            admin.exec_driver_sql(f"INSERT INTO `{database}`.orders VALUES (1, 'desk')")
            with closing(_context(tmp_path / "ctx")) as context:
                # The Context's own cache use registers the SQL dialects that a Database facade resolves.
                cache = LLMCache.current(config_manager=context.config)
                cache.config["backend"] = {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "own.db")}}
                cache.get_text(_KEY)
                ordering, dropping = _facade(context, server, database), _facade(context, server, database)
                assert ordering.execute("SELECT COUNT(*) FROM orders", readonly=True).to_lists() == [[1]]
                in_transaction, go = threading.Event(), threading.Event()
                outcome: dict[str, object] = {}

                def update_then_call():
                    try:
                        with ordering.tx():
                            ordering.execute("UPDATE orders SET item = 'chair' WHERE id = 1")
                            in_transaction.set()
                            assert go.wait(_DROP_SEEN + _BOTH_END)
                            # Needs a new facade (clone) or a new schema index (tables) while the drop runs.
                            getattr(ordering, call)()
                        outcome["call"] = "committed"
                    except Exception as error:  # reported below
                        outcome["call"] = error

                def drop():
                    try:
                        dropping.drop_database()
                        outcome["drop"] = "dropped"
                    except Exception as error:  # reported below
                        outcome["drop"] = error

                user = threading.Thread(target=update_then_call, daemon=True)
                dropper = threading.Thread(target=drop, daemon=True)
                user.start()
                assert in_transaction.wait(_DROP_SEEN)
                dropper.start()
                # MySQL makes the DROP wait for the metadata lock of the open transaction.
                waiting = None
                for _ in range(_DROP_SEEN * 10):
                    waiting = _waiting_drop(admin, database)
                    if waiting is not None or not dropper.is_alive():
                        break
                    threading.Event().wait(0.1)
                go.set()
                user.join(_BOTH_END)
                dropper.join(_BOTH_END)
                stuck = user.is_alive() or dropper.is_alive()
                if stuck:
                    # The two threads wait for each other: end the DROP so the run can finish, then fail below.
                    killed = _waiting_drop(admin, database)
                    if killed is not None:
                        admin.exec_driver_sql(f"KILL QUERY {int(killed)}")
                    user.join(_BOTH_END)
                    dropper.join(_BOTH_END)
                assert not stuck, f"the drop and the transaction's thread waited for each other: {outcome}"
                # The call fails at once instead of waiting for the drop, so the transaction rolls back and the
                # DROP that waited for it ends.
                assert isinstance(outcome["call"], RuntimeError), outcome
                assert "Database is being dropped" in str(outcome["call"]) or "Engine has been disposed" in str(outcome["call"])
                assert outcome["drop"] == "dropped", outcome
            assert admin.exec_driver_sql(f"SHOW DATABASES LIKE '{database}'").fetchall() == []
    finally:
        with admin_engine.connect() as admin:
            admin.exec_driver_sql(f"DROP DATABASE IF EXISTS `{database}`")
        admin_engine.dispose()


@pytest.mark.full
@pytest.mark.parametrize("running", [False, True], ids=["alone", "during another drop"])
@pytest.mark.parametrize("drop", ["drop_database", "drop"])
def test_a_thread_with_an_open_mysql_transaction_cannot_drop_its_database(tmp_path, drop, running):
    url = os.environ.get("HVNB_MYSQL_URL")
    if not url:
        pytest.skip("requires a dedicated MySQL test database in HVNB_MYSQL_URL")
    server = sa.engine.make_url(url)
    database = f"hb_drop_own_tx_{uuid4().hex[:12]}"
    admin_engine = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as admin:
            admin.exec_driver_sql(f"CREATE DATABASE `{database}`")
            admin.exec_driver_sql(f"CREATE TABLE `{database}`.orders (id INT PRIMARY KEY, item VARCHAR(40))")
            admin.exec_driver_sql(f"INSERT INTO `{database}`.orders VALUES (1, 'desk')")
            with closing(_context(tmp_path / "ctx")) as context, closing(_context(tmp_path / "other")) as other:
                for ctx in (context, other):
                    cache = LLMCache.current(config_manager=ctx.config)
                    cache.config["backend"] = {
                        "main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / f"own-{id(ctx):x}.db")}
                    }
                    cache.get_text(_KEY)
                ordering, dropping = _facade(context, server, database), _facade(context, server, database)
                outcome: dict[str, object] = {}
                elsewhere = threading.Thread(target=lambda: outcome.setdefault("other", _facade(other, server, database).drop_database()), daemon=True)
                with ordering.tx():
                    ordering.execute("UPDATE orders SET item = 'chair' WHERE id = 1")
                    if running:
                        # Another Context's drop of the database, whose DROP waits for this transaction.
                        elsewhere.start()
                        for _ in range(_DROP_SEEN * 10):
                            if _waiting_drop(admin, database) is not None:
                                break
                            threading.Event().wait(0.1)
                        assert _waiting_drop(admin, database) is not None
                    began = time.monotonic()
                    # The drop's statement would wait for this thread's own transaction, so the drop fails at once.
                    with pytest.raises(RuntimeError, match="connection to it in use"):
                        getattr(dropping, drop)()
                    assert time.monotonic() - began < 10
                if running:
                    elsewhere.join(_BOTH_END)
                    assert not elsewhere.is_alive()
                    assert admin.exec_driver_sql(f"SHOW DATABASES LIKE '{database}'").fetchall() == []
                else:
                    assert ordering.execute("SELECT item FROM orders", readonly=True).to_lists() == [["chair"]]
    finally:
        with admin_engine.connect() as admin:
            admin.exec_driver_sql(f"DROP DATABASE IF EXISTS `{database}`")
        admin_engine.dispose()
