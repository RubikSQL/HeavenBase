"""An LLM cache call inside a MySQL transaction while the same Context drops the cache's database."""

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
_ANSWER = {"text": "summary of the quarterly report"}
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


def _waiting_drop(admin, database):
    """Return the connection id of the DROP DATABASE statement of ``database`` while it runs, or ``None``."""
    for row in admin.exec_driver_sql("SHOW FULL PROCESSLIST").fetchall():
        info = row[7] or ""
        if info.upper().startswith("DROP DATABASE") and database in info:
            return row[0]
    return None


@pytest.mark.full
def test_a_cache_call_inside_a_transaction_on_the_dropped_database_fails_and_the_drop_ends(tmp_path):
    url = os.environ.get("HVNB_MYSQL_URL")
    if not url:
        pytest.skip("requires a dedicated MySQL test database in HVNB_MYSQL_URL")
    workspace = f"hb-cache-tx-{uuid4().hex[:12]}"
    database = None
    admin_engine = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as admin:
            with closing(_context(tmp_path / "ctx")) as context:
                cache = LLMCache.current(config_manager=context.config)
                # The cache's MySQL route names its database after the cache workspace.
                cache.config["workspace"] = workspace
                cache.config["backend"] = {"main": {"type": "mysql", "name": "main", "url": url}}
                cache.set_text(_KEY, [], _ANSWER, [], policy="deterministic")
                assert cache.get_text(_KEY)["response"] == _ANSWER
                store_db = next(backend.db for _, backend in cache._store.workspace.backends.items() if getattr(backend, "db", None) is not None)
                database = store_db.spec.database
                admin.exec_driver_sql(f"CREATE TABLE `{database}`.orders (id INT PRIMARY KEY, item VARCHAR(40))")
                admin.exec_driver_sql(f"INSERT INTO `{database}`.orders VALUES (1, 'desk')")
                ordering, dropping = store_db.clone(), store_db.clone()
                in_transaction, go = threading.Event(), threading.Event()
                outcome: dict[str, object] = {}

                def update_then_call():
                    try:
                        with ordering.tx():
                            ordering.execute("UPDATE orders SET item = 'chair' WHERE id = 1")
                            in_transaction.set()
                            assert go.wait(_DROP_SEEN + _BOTH_END)
                            # The store is detached by now, so this call opens it again while the drop runs.
                            cache.get_text(_KEY)
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
                assert in_transaction.wait(_DROP_SEEN), outcome
                dropper.start()
                # MySQL makes the DROP wait for the metadata lock of the open transaction.
                for _ in range(_DROP_SEEN * 10):
                    if _waiting_drop(admin, database) is not None or not dropper.is_alive():
                        break
                    time.sleep(0.1)
                go.set()
                user.join(_BOTH_END)
                dropper.join(_BOTH_END)
                stuck = user.is_alive() or dropper.is_alive()
                if stuck:
                    # The two threads wait for each other: end the DROP so the run can finish, then fail below.
                    waiting = _waiting_drop(admin, database)
                    if waiting is not None:
                        admin.exec_driver_sql(f"KILL QUERY {int(waiting)}")
                    user.join(_BOTH_END)
                    dropper.join(_BOTH_END)
                assert not stuck, f"the drop and the transaction's thread waited for each other: {outcome}"
                # The call fails instead of waiting for the drop, so the transaction rolls back and the DROP ends.
                assert isinstance(outcome["call"], RuntimeError), outcome
                assert outcome["drop"] == "dropped", outcome
            assert admin.exec_driver_sql(f"SHOW DATABASES LIKE '{database}'").fetchall() == []
    finally:
        if database is not None:
            with admin_engine.connect() as admin:
                admin.exec_driver_sql(f"DROP DATABASE IF EXISTS `{database}`")
        admin_engine.dispose()
