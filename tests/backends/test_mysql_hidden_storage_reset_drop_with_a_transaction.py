"""A Capsule registry or LLM cache drop on MySQL while another thread of the Context holds a transaction on its database.

``reset_capsule_registry(drop=True)`` and ``LLMCache.reset(drop=True)`` hold the Context lock while their
``DROP DATABASE`` runs, and MySQL makes that statement wait for the metadata lock of the open transaction. A call of
the transaction's thread that needs the Context lock (a workspace write, a registry call or a cache call) would then
wait for the drop's thread, so it raises ``RuntimeError``: the transaction rolls back and the drop ends.
"""

import os
import threading
import time
from contextlib import closing
from uuid import uuid4

import pytest
import sqlalchemy as sa

import heavenbase as hb
from heavenbase.capsule import registry as capsule_mod
from heavenbase.llm.cache import LLMCache
from heavenbase.utils.config import ConfigManager

_KEY = {"object_id": "text-row", "spec_key": "spec", "request_key": "request", "args": {}}
_ANSWER = {"text": "summary of the quarterly report"}
# Seconds. Only stop a deadlocked run; no passing run depends on them.
_SEEN = 60
_BOTH_END = 60


def add(left, right):
    return left + right


class ReadingNote(hb.Entity):
    identifier = "mysql-reset-reading-note"

    title = hb.field(hb.ShortText)


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


def _drop_statements(admin, database):
    """Return the connection ids of the DROP statements on ``database`` while they run."""
    rows = admin.exec_driver_sql("SHOW FULL PROCESSLIST").fetchall()
    return [row[0] for row in rows if (row[7] or "").upper().startswith("DROP") and database in (row[7] or "")]


def _store_database(workspace):
    return next(backend.db for _, backend in workspace.backends.items() if getattr(backend, "db", None) is not None)


@pytest.mark.full
@pytest.mark.parametrize(
    ("drop", "call"),
    [("registry", "workspace-write"), ("registry", "registry-call"), ("cache", "workspace-write"), ("cache", "cache-call")],
)
def test_a_reset_drop_and_a_transaction_of_the_same_context_on_its_database_both_end(tmp_path, drop, call):
    url = os.environ.get("HVNB_MYSQL_URL")
    if not url:
        pytest.skip("requires a dedicated MySQL test database in HVNB_MYSQL_URL")
    name = f"hb-reset-tx-{uuid4().hex[:10]}"
    admin_engine = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    database = None
    try:
        with admin_engine.connect() as admin, closing(_context(tmp_path / "ctx")) as context:
            resolver, config = context.modules(), context.config
            if drop == "registry":
                config.set("heavenbase.capsule.registry.workspace", name)
                config.set("heavenbase.capsule.registry.backend", {"main": {"type": "mysql", "name": "main", "url": url}})
                registry = capsule_mod.capsule_registry(resolver=resolver, config=config)
                registry.register(hb.Capsule(add, name="sum_two", namespace="demo", version="1", include_cloudpickle=False))
                store_db = _store_database(registry.store.owner.workspace)
            else:
                cache = LLMCache.current(config_manager=config)
                cache.config["workspace"] = name
                cache.config["backend"] = {"main": {"type": "mysql", "name": "main", "url": url}}
                cache.set_text(_KEY, [], _ANSWER, [], policy="deterministic")
                store_db = _store_database(cache._store.workspace)
            database = store_db.spec.database
            admin.exec_driver_sql(f"CREATE TABLE `{database}`.orders (id INT PRIMARY KEY, item VARCHAR(40))")
            admin.exec_driver_sql(f"INSERT INTO `{database}`.orders VALUES (1, 'desk')")
            notes = hb.HeavenBase("mysql-reset-notes", context=context, detached=True, backends={"main": {"type": "inmem"}})
            notes.register(ReadingNote)
            ordering = store_db.clone()
            in_transaction, go = threading.Event(), threading.Event()
            outcome: dict[str, object] = {}

            def update_then_call():
                try:
                    with ordering.tx():
                        ordering.execute("UPDATE orders SET item = 'chair' WHERE id = 1")
                        outcome["connection"] = ordering.execute("SELECT CONNECTION_ID()").to_lists()[0][0]
                        in_transaction.set()
                        assert go.wait(_SEEN + _BOTH_END)
                        if call == "workspace-write":
                            notes.upsert(ReadingNote, {"object_id": "n1", "title": "first"})
                        elif call == "registry-call":
                            capsule_mod.capsule_registry(resolver=resolver, config=config).list()
                        else:
                            LLMCache.current(config_manager=config).get_text(_KEY)
                    outcome["call"] = "committed"
                except Exception as error:  # reported below
                    outcome["call"] = error

            def run_drop():
                try:
                    if drop == "registry":
                        capsule_mod.reset_capsule_registry(drop=True, resolver=resolver, config=config)
                    else:
                        LLMCache.reset(drop=True, config_manager=config)
                    outcome["drop"] = "dropped"
                except Exception as error:  # reported below
                    outcome["drop"] = error

            user = threading.Thread(target=update_then_call, daemon=True)
            dropper = threading.Thread(target=run_drop, daemon=True)
            user.start()
            assert in_transaction.wait(_SEEN), outcome
            dropper.start()
            # MySQL makes the DROP wait for the metadata lock of the open transaction.
            for _ in range(_SEEN * 10):
                if _drop_statements(admin, database) or not dropper.is_alive():
                    break
                time.sleep(0.1)
            go.set()
            user.join(_BOTH_END)
            dropper.join(_BOTH_END)
            stuck = user.is_alive() or dropper.is_alive()
            if stuck:
                # The two threads wait for each other: end the transaction's connection so the run can finish.
                admin.exec_driver_sql(f"KILL {int(outcome['connection'])}")
                user.join(_BOTH_END)
                dropper.join(_BOTH_END)
            assert not stuck, f"the drop and the transaction's thread waited for each other: {outcome}"
            assert isinstance(outcome["call"], RuntimeError), outcome
            assert "on a database that a running drop removes" in str(outcome["call"])
            assert outcome["drop"] == "dropped", outcome
            assert admin.exec_driver_sql(f"SHOW DATABASES LIKE '{database}'").fetchall() == []
    finally:
        if database is not None:
            with admin_engine.connect() as admin:
                admin.exec_driver_sql(f"DROP DATABASE IF EXISTS `{database}`")
        admin_engine.dispose()
