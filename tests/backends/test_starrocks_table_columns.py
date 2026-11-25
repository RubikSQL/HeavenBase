"""A StarRocks table gains the columns a registration adds, and the backend caches only the columns the table holds.

No StarRocks server runs here: the stub records each statement the backend sends
and keeps the stored column names the way the server would after that DDL.
"""

import re
import warnings

import pytest
from sqlalchemy.exc import OperationalError

import heavenbase as hb
from heavenbase.execution.contracts import RowOp
from heavenbase.placement.binding import StorageBinding


class Note(hb.Entity):
    identifier = "starrocks-columns-note"
    title = hb.field(hb.ShortText)
    body = hb.field(hb.ShortText)


class NoteWithTopic(hb.Entity):
    identifier = "starrocks-columns-note"
    title = hb.field(hb.ShortText)
    body = hb.field(hb.ShortText)
    topic = hb.field(hb.ShortText)


class TaggedNote(hb.Entity):
    identifier = "starrocks-columns-tagged-note"
    title = hb.field(hb.ShortText).required()
    tags = hb.field(hb.Array[hb.ShortText])


class StoredTables:
    """Stand-in for the StarRocks server: the stored tables' column names, changed by the DDL the backend sends."""

    def __init__(self, tables=None, *, fail=()):
        self.tables = {name: list(columns) for name, columns in (tables or {}).items()}
        self.statements = []
        self.fail = set(fail)

    def execute(self, sql, *args, **kwargs):
        sql = str(sql)
        self.statements.append(sql)
        if len(self.statements) in self.fail:
            raise OperationalError(sql, {}, ConnectionError("Lost connection to the server during query"))
        created = re.match(r"CREATE TABLE IF NOT EXISTS `([^`]+)` \((.*)\) ENGINE=OLAP", sql)
        if created is not None:
            self.tables.setdefault(created.group(1), re.findall(r"`([^`]+)` [A-Z]", created.group(2)))
        added = re.match(r"ALTER TABLE `([^`]+)` ADD COLUMN `([^`]+)` ", sql)
        if added is not None:
            self.tables[added.group(1)].append(added.group(2))

    def columns(self, table, schema=None, full=False):
        return list(self.tables.get(table, []))

    def ddl(self):
        return [statement.split(" (")[0] for statement in self.statements]


def _starrocks(monkeypatch, stored):
    monkeypatch.setattr(hb.backends.StarRocksBackend, "_ensure_database", lambda *args: None)
    backend = hb.backends.StarRocksBackend("starrocks")
    monkeypatch.setattr(backend.db, "execute", stored.execute)
    monkeypatch.setattr(backend.db, "columns", stored.columns)
    return backend


def _bindings(entity):
    schema = entity.schema()
    return [StorageBinding(schema.entity_id, name, "starrocks", "inline", "test") for name in schema.fields if name != "object_id"]


def _cached(backend, entity):
    return sorted(backend.tables[entity.identifier].c.keys())


TABLE = "starrocks_columns_note"


@pytest.mark.fast
def test_a_replacement_on_the_same_backend_adds_the_new_column_and_writes_its_values(monkeypatch):
    stored = StoredTables()
    backend = _starrocks(monkeypatch, stored)
    backend.ensure(Note.schema(), _bindings(Note))
    backend.ensure(NoteWithTopic.schema(), _bindings(NoteWithTopic))
    assert stored.ddl() == [f"CREATE TABLE IF NOT EXISTS `{TABLE}`", f"ALTER TABLE `{TABLE}` ADD COLUMN `topic` VARCHAR(255) NULL"]
    assert _cached(backend, NoteWithTopic) == sorted(stored.tables[TABLE]) == ["body", "object_id", "title", "topic"]
    # what SQLBackend._apply_row_ops inserts for one upsert
    row = RowOp(Note.identifier, "n1", {"title": "Budget", "body": "Q3 budget.", "topic": "finance"})
    assert row.to_row(fields=backend.tables[Note.identifier].c.keys())["topic"] == "finance"


@pytest.mark.fast
def test_a_new_backend_adds_the_columns_a_stored_table_lacks(monkeypatch):
    stored = StoredTables({TABLE: ["object_id", "title", "body"]})
    backend = _starrocks(monkeypatch, stored)
    backend.ensure(NoteWithTopic.schema(), _bindings(NoteWithTopic))
    assert stored.ddl() == [f"CREATE TABLE IF NOT EXISTS `{TABLE}`", f"ALTER TABLE `{TABLE}` ADD COLUMN `topic` VARCHAR(255) NULL"]
    assert _cached(backend, NoteWithTopic) == sorted(stored.tables[TABLE])


@pytest.mark.fast
def test_a_column_the_server_rejects_is_not_cached_and_is_added_by_the_next_registration(monkeypatch):
    stored = StoredTables(fail={2})  # the ALTER TABLE
    backend = _starrocks(monkeypatch, stored)
    backend.ensure(Note.schema(), _bindings(Note))
    with pytest.raises(OperationalError):
        backend.ensure(NoteWithTopic.schema(), _bindings(NoteWithTopic))
    assert _cached(backend, Note) == sorted(stored.tables[TABLE]) == ["body", "object_id", "title"]
    backend.ensure(NoteWithTopic.schema(), _bindings(NoteWithTopic))
    assert _cached(backend, NoteWithTopic) == sorted(stored.tables[TABLE]) == ["body", "object_id", "title", "topic"]


@pytest.mark.fast
def test_a_new_table_declares_its_starrocks_types_and_keeps_array_fields_inline(monkeypatch):
    stored = StoredTables()
    backend = _starrocks(monkeypatch, stored)
    schema = TaggedNote.schema()
    bindings = [StorageBinding(schema.entity_id, "title", "starrocks", "inline", "test"), StorageBinding(schema.entity_id, "tags", "starrocks", "side", "test")]
    backend.ensure(schema, bindings)
    assert stored.statements == [
        "CREATE TABLE IF NOT EXISTS `starrocks_columns_tagged_note` "
        f"(`object_id` VARCHAR({hb.Identifier().max_chars}) NOT NULL, `title` VARCHAR(255) NOT NULL, `tags` VARCHAR(65533) NULL) "
        'ENGINE=OLAP PRIMARY KEY(`object_id`) DISTRIBUTED BY HASH(`object_id`) BUCKETS 1 PROPERTIES ("replication_num" = "1")'
    ]
    assert backend._side_specs.get(schema.entity_id, {}) == {}
    assert _cached(backend, TaggedNote) == ["object_id", "tags", "title"]


# What StarRocks prints for SHOW CREATE TABLE of a primary-key OLAP table (its columns carry COMMENT "").
SHOW_CREATE_TABLE = """CREATE TABLE `starrocks_columns_note` (
  `object_id` varchar(255) NOT NULL COMMENT "",
  `title` varchar(255) NULL COMMENT "",
  `body` varchar(255) NULL COMMENT ""
) ENGINE=OLAP
PRIMARY KEY(`object_id`)
DISTRIBUTED BY HASH(`object_id`) BUCKETS 1
PROPERTIES (
"replication_num" = "1",
"in_memory" = "false",
"enable_persistent_index" = "true",
"replicated_storage" = "true",
"compression" = "LZ4"
);"""


def _reflected_columns(table, schema=None, full=False):
    """Read the column names from StarRocks' SHOW CREATE TABLE text as SQLAlchemy's MySQL reflection does."""
    from sqlalchemy.dialects.mysql import base as mysql_base
    from sqlalchemy.dialects.mysql.reflection import MySQLTableDefinitionParser

    dialect = mysql_base.MySQLDialect()
    return [column["name"] for column in MySQLTableDefinitionParser(dialect, dialect.identifier_preparer).parse(SHOW_CREATE_TABLE, "utf8mb4").columns]


@pytest.mark.fast
def test_reflecting_an_olap_table_raises_no_warning_about_its_starrocks_clauses(monkeypatch):
    from sqlalchemy.exc import SAWarning

    with pytest.warns(SAWarning, match="Unknown schema content"):
        assert _reflected_columns(TABLE) == ["object_id", "title", "body"]
    stored = StoredTables()
    backend = _starrocks(monkeypatch, stored)
    monkeypatch.setattr(backend.db, "columns", _reflected_columns)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        backend.ensure(Note.schema(), _bindings(Note))
        backend.ensure(NoteWithTopic.schema(), _bindings(NoteWithTopic))
    assert [str(item.message) for item in caught if issubclass(item.category, SAWarning)] == []
    assert stored.ddl() == [f"CREATE TABLE IF NOT EXISTS `{TABLE}`", f"ALTER TABLE `{TABLE}` ADD COLUMN `topic` VARCHAR(255) NULL"]
    assert _cached(backend, NoteWithTopic) == ["body", "object_id", "title", "topic"]
    with pytest.warns(SAWarning, match="Unknown schema content"):
        _reflected_columns(TABLE)
