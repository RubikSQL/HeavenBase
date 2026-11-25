"""Database execution classifies SQLAlchemy constructs without compiling them for diagnostics."""

from __future__ import annotations

import copy
import pickle
import sys
import threading
from typing import Optional

import pytest
import sqlalchemy as sa
import sqlalchemy.dialects.postgresql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase, Mapped, column_property, mapped_column, query_expression, with_expression
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.elements import ClauseElement, quoted_name
from sqlalchemy.sql.functions import Function, FunctionElement
from sqlalchemy.types import UserDefinedType

import heavenbase as hb
from heavenbase.database import DatabaseError, is_sql_readonly
from heavenbase.utils.config import engine as config_engine

pytestmark = [pytest.mark.fast, pytest.mark.unit]


@pytest.fixture
def counters(monkeypatch):
    """Count SQL compiler constructions and construct stringifications."""
    counts = {"compile": 0, "str": 0}
    compiler_init = SQLCompiler.__init__
    clause_str = ClauseElement.__str__

    def count_compile(self, *args, **kwargs):
        counts["compile"] += 1
        return compiler_init(self, *args, **kwargs)

    def count_str(self):
        counts["str"] += 1
        return clause_str(self)

    monkeypatch.setattr(SQLCompiler, "__init__", count_compile)
    monkeypatch.setattr(ClauseElement, "__str__", count_str)
    return counts


def _items_db():
    db = hb.Database(provider="sqlite", database=":memory:")
    items = sa.Table("items", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True), sa.Column("name", sa.String))
    db.create_table(items)
    db.orm_execute(sa.insert(items), [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}], readonly=False)
    return db, items


def test_orm_execute_reads_compile_once_and_render_query_text_lazily(counters):
    db, items = _items_db()
    stmt = sa.select(items.c.id).order_by(items.c.id)
    assert db.orm_execute(stmt, readonly=True).column(0) == [1, 2]
    counters.update(compile=0, str=0)

    responses = [db.orm_execute(stmt, readonly=True) for _ in range(10)]

    assert [resp.column(0) for resp in responses] == [[1, 2]] * 10
    # the engine serves the compiled cache; nothing stringifies the construct on a successful read
    assert counters == {"compile": 0, "str": 0}
    assert responses[0].query == str(stmt)
    assert counters["str"] == 2 and responses[0].query == str(stmt) and counters["str"] == 3
    assert db.execute("SELECT 1 AS x", readonly=True).query == "SELECT 1 AS x"


def test_readonly_guard_classifies_constructs_without_compiling(counters):
    db, items = _items_db()
    other = sa.Table("other", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True))
    modifying_cte = sa.insert(items).values(id=3, name="gamma").returning(items.c.id).cte("added")
    counters.update(compile=0, str=0)

    assert is_sql_readonly(sa.select(items.c.id)) is True
    assert is_sql_readonly(sa.select(items).where(sa.exists(sa.select(1).where(items.c.id == 1)))) is True
    assert is_sql_readonly(sa.select(items.c.id).union(sa.select(other.c.id))) is True
    assert is_sql_readonly(sa.select(modifying_cte.c.id)) is False
    assert is_sql_readonly(sa.select(items.c.id).add_cte(modifying_cte)) is False
    for construct in (sa.insert(items).values(id=3), sa.update(items).values(name="x"), sa.delete(items), sa.schema.CreateTable(other)):
        assert is_sql_readonly(construct) is False
    assert counters == {"compile": 0, "str": 0}

    assert is_sql_readonly(sa.text("SELECT count(*) FROM items")) is True
    assert is_sql_readonly(sa.text("DELETE FROM items")) is False
    assert is_sql_readonly(sa.select(items.c.id), query_text="DELETE FROM items") is False
    assert counters["str"] == 0

    assert db.orm_execute(sa.select(items.c.name).where(items.c.id == 2), readonly=True).scalar() == "beta"
    with pytest.raises(DatabaseError, match="readonly=True"):
        db.orm_execute(sa.insert(items).values(id=9, name="nine"), readonly=True)
    with pytest.raises(DatabaseError, match="readonly=True"):
        db.orm_execute(sa.text("DELETE FROM items"), readonly=True)
    assert db.orm_execute(sa.select(sa.func.count()).select_from(items), readonly=True).scalar() == 2


class _ExpressionBase(DeclarativeBase):
    pass


class _PropertyBase(DeclarativeBase):
    pass


def _removed_cte():
    return sa.text("DELETE FROM items RETURNING id").columns(id=sa.Integer).cte("removed")


class _ExpressionItem(_ExpressionBase):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    expr: Mapped[Optional[int]] = query_expression()


class _PropertyItem(_PropertyBase):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    gone = column_property(sa.select(_removed_cte().c.id).limit(1).scalar_subquery())


class _WritingColumnSpec(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw):
        return "INTEGER) AS x FROM items; DELETE FROM items; SELECT CAST(1 AS INTEGER"


class _writing_function(FunctionElement):
    inherit_cache = True


@compiles(_writing_function)
def _compile_writing_function(element, compiler, **kw):
    return "1; DELETE FROM items; SELECT 1"


def _writes_nested_as_text(items):
    """SELECT constructs whose write is SQL the construct structure cannot see."""
    removed = _removed_cte()
    return {
        "text cte": sa.select(removed.c.id),
        "added text cte": sa.select(items.c.id).add_cte(removed),
        "suffix": sa.select(items.c.id).suffix_with("; DELETE FROM items"),
        "literal column": sa.select(sa.literal_column("1; DELETE FROM items")),
        "custom operator": sa.select(items.c.id).where(items.c.id.op("; DELETE FROM items; SELECT 1 WHERE 1 =")(1)),
        "unquoted name": sa.select(sa.Table(quoted_name("items; DELETE FROM items; --", quote=False), sa.MetaData(), sa.Column("id", sa.Integer))),
        "extract field": sa.select(sa.extract("epoch FROM items.id) AS x FROM items; DELETE FROM items; SELECT extract(epoch", items.c.id)),
        "cast to a user type": sa.select(sa.cast(items.c.id, _WritingColumnSpec())),
        "unquoted package name": sa.select(Function("now", packagenames=(quoted_name("pg_catalog; DELETE FROM items; SELECT pg_catalog", quote=False),))),
        "element compiled by user code": sa.select(_writing_function()),
        "orm with_expression": sa.select(_ExpressionItem).options(with_expression(_ExpressionItem.expr, sa.select(removed.c.id).limit(1).scalar_subquery())),
        "orm column_property": sa.select(_PropertyItem),
    }


def test_a_select_nesting_a_write_as_text_is_not_a_read():
    from heavenbase.database.readonly import _is_sql_read

    db, items = _items_db()
    statements = _writes_nested_as_text(items)

    for name, statement in statements.items():
        assert "delete from items" in str(statement).lower(), name
        assert _is_sql_read(statement) is False, name
    # a writable CTE written with text(), reached directly or through an ORM entity or loader option,
    # is mutating SQL, as in 0.1.2.2: readonly=True rejects it and readonly=None commits it instead of
    # rolling its write back
    names = ("text cte", "added text cte", "orm with_expression", "orm column_property")
    for dialect in (None, "postgresql", "sqlite"):
        for name in names:
            assert is_sql_readonly(statements[name], dialect=dialect) is False, (name, dialect)
    for name in names:
        with pytest.raises(DatabaseError, match="readonly=True rejects mutating SQL"):
            db.orm_execute(statements[name], readonly=True)
        assert db._readonly(statements[name], None, None) is False


def test_a_select_nesting_a_write_as_text_is_classified_by_its_compiled_text(monkeypatch):
    from heavenbase.database import readonly

    _db, items = _items_db()
    parsed = []

    def parser(text, *_args):
        parsed.append(text)
        return "delete" not in text.lower()

    monkeypatch.setattr(readonly, "_sqlglot_readonly", parser)

    postgresql = sa.dialects.postgresql.dialect(paramstyle="named")
    words = {"unquoted name", "extract field", "unquoted package name"}
    for name, statement in _writes_nested_as_text(items).items():
        calls = len(parsed)
        assert is_sql_readonly(statement) is False, name
        if name in words:
            # where SQL expects one word, other text renders differently by dialect: unknown, it is not a read
            assert len(parsed) == calls, name
        else:
            assert len(parsed) == calls + 1 and parsed[-1] == str(statement).strip(), name
        assert is_sql_readonly(statement, dialect="postgresql") is False, name
        assert parsed[-1] == str(statement.compile(dialect=postgresql)).strip(), name
    # a type whose own SQL wraps the columns it reads is rendered too; this one only reads
    lowered = sa.select(sa.Table("items", sa.MetaData(), sa.Column("name", _Lowered())).c.name)
    assert is_sql_readonly(lowered) is True
    assert parsed[-1] == str(lowered).strip() and "lower(" in parsed[-1]


def test_a_select_is_classified_by_its_text_rendered_for_the_dialect(monkeypatch, counters):
    from heavenbase.database import readonly
    from heavenbase.database.readonly import _is_sql_read

    db, items = _items_db()
    parsed = []

    def parser(text, *_args):
        parsed.append(text)
        return "delete" not in text.lower()

    monkeypatch.setattr(readonly, "_sqlglot_readonly", parser)
    sequence = sa.Sequence(quoted_name("s'); DELETE FROM items; SELECT ('x", quote=False))
    scoped = [
        ("postgresql", sa.select(items.c.id).suffix_with("; DELETE FROM items", dialect="postgresql")),
        ("postgresql", sa.select(items.c.id).prefix_with("1; DELETE FROM items; SELECT", dialect="postgresql")),
        ("mysql", sa.select(items.c.id).with_statement_hint("; DELETE FROM items", dialect_name="mysql")),
        ("mssql", sa.select(items.c.id).with_hint(items, "WITH (NOLOCK); DELETE FROM items; --", dialect_name="mssql")),
    ]
    rendered_by_dialect = [
        ("mysql", sa.select(sa.collate(items.c.name, "utf8mb4_bin; DELETE FROM items; --"))),
        ("postgresql", sa.select(sequence.next_value())),
    ]

    for dialect, statement in scoped + rendered_by_dialect:
        assert is_sql_readonly(statement, dialect=dialect) is False, dialect
        assert "delete from items" in parsed[-1].lower(), dialect
    counters.update(compile=0, str=0)
    for dialect, statement in scoped + rendered_by_dialect:
        # without a dialect, str() would not render the text as the dialect does, so it may write
        assert is_sql_readonly(statement) is False and _is_sql_read(statement) is False, dialect
    for dialect, statement in scoped:
        # scoped to another dialect, the text does not render at all
        assert is_sql_readonly(statement, dialect="sqlite") is True, dialect
    assert counters == {"compile": 0, "str": 0}

    # the database renders a construct with its own dialect
    on_sqlite = sa.select(items.c.id).suffix_with("; DELETE FROM items", dialect="sqlite")
    with pytest.raises(DatabaseError, match="readonly=True rejects mutating SQL"):
        db.orm_execute(on_sqlite, readonly=True)
    assert "delete from items" in parsed[-1].lower()
    assert db.orm_execute(scoped[0][1], readonly=True).column(0) == [1, 2]


class _Label(sa.TypeDecorator):
    """A type defined outside SQLAlchemy that renders binds and columns as SQLAlchemy does."""

    impl = sa.String
    cache_ok = True


class _Lowered(sa.TypeDecorator):
    """A type defined outside SQLAlchemy that wraps every column it reads in its own SQL."""

    impl = sa.String
    cache_ok = True

    def column_expression(self, column):
        return sa.func.lower(column)


def test_a_select_nesting_read_only_text_is_classified_without_compiling(counters):
    from heavenbase.database.readonly import _is_sql_read

    db, items = _items_db()
    statements = [
        sa.select(items.c.id).where(sa.text("id > 1 AND name <> 'delete'")),
        sa.select(sa.literal_column("count(*)")).select_from(items),
        sa.select(items.c.id).prefix_with("DISTINCT"),
        sa.select(items.c.id).with_hint(items, "INDEX(items)").with_statement_hint("NOCACHE"),
        sa.select(sa.text("SELECT id FROM items WHERE id > 1").columns(id=sa.Integer).cte("kept").c.id),
        sa.select(items.c.id).where(items.c.name.op("||")("x") == "betax"),
        sa.select(sa.Table(quoted_name("items", quote=False), sa.MetaData(), sa.Column("id", sa.Integer)).c.id),
        sa.select(sa.extract("year", sa.column("day", sa.Date))).select_from(items),
        sa.select(sa.Table("items", sa.MetaData(), sa.Column("name", _Label())).c.name),
        sa.select(items.c.id).suffix_with("; DELETE FROM items", dialect="mysql"),
    ]
    counters.update(compile=0, str=0)

    for statement in statements[:-1]:
        assert is_sql_readonly(statement) is True
        assert _is_sql_read(statement) is True
    # a suffix scoped to another dialect does not render on this one
    assert is_sql_readonly(statements[-1], dialect="sqlite") is True
    assert counters == {"compile": 0, "str": 0}
    assert db.orm_execute(statements[0], readonly=True).column(0) == [2]
    assert db.orm_execute(statements[-1], readonly=True).column(0) == [1, 2]


def test_a_select_from_a_table_declared_with_prefixes_is_a_read(counters):
    from heavenbase.database.readonly import _is_sql_read

    db, _items = _items_db()
    # Table prefixes belong to the table's CREATE statement, as plain strings, and are not SQL the SELECT carries
    temporary = sa.Table("items", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True), sa.Column("name", sa.String), prefixes=["TEMPORARY"])

    class Base(DeclarativeBase):
        pass

    class Item(Base):
        __tablename__ = "items"
        __table_args__ = {"prefixes": ["TEMPORARY"]}
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]

    statements = [sa.select(temporary.c.id), sa.select(temporary).where(temporary.c.id == 2), sa.select(Item.id), sa.select(Item).where(Item.id == 2)]
    counters.update(compile=0, str=0)

    for statement in statements[:2]:
        assert is_sql_readonly(statement) is True
        assert _is_sql_read(statement) is True
    assert counters == {"compile": 0, "str": 0}
    # an ORM statement expands its entities only when compiled, so it is classified by its text
    for statement in statements[2:]:
        assert is_sql_readonly(statement) is True
        assert _is_sql_read(statement) is True
    for readonly in (False, True, None):
        assert db.orm_execute(statements[0], readonly=readonly).column(0) == [1, 2]
        assert db.orm_execute(statements[3], readonly=readonly).column(0) == [2]
    with db.tx() as tx:
        assert tx.orm_execute(statements[2], readonly=True).column(0) == [1, 2]


_DIALECTS = (None, "mssql", "postgresql", "mysql", "duckdb", "sqlite")


@pytest.fixture(params=["sqlglot absent", "sqlglot present"])
def sqlglot_mode(request, monkeypatch):
    """Classify with SQLGlot hidden from the import system, then with it installed."""
    if request.param == "sqlglot absent":
        monkeypatch.setitem(sys.modules, "sqlglot", None)
    else:
        pytest.importorskip("sqlglot")
    return request.param


def test_text_holding_a_second_statement_is_not_read_only(sqlglot_mode):
    db, items = _items_db()
    statements = _writes_nested_as_text(items)
    second_statements = [
        "SELECT 1; DELETE FROM items",
        "select 1;delete from items;",
        "SELECT 1 -- note\n; DELETE FROM items",
        "SELECT 'a'; DROP TABLE items",
        "WITH kept AS (SELECT 1 AS id) SELECT id FROM kept; DELETE FROM items",
        statements["suffix"],
        statements["literal column"],
    ]
    for statement in second_statements:
        assert is_sql_readonly(statement) is False, statement
        assert is_sql_readonly(statement, dialect="postgresql") is False, statement
    for statement in ("SELECT 1; DELETE FROM items", statements["suffix"]):
        # readonly=True refuses the statement before the driver sees it; readonly=None commits it
        with pytest.raises(DatabaseError, match="readonly=True rejects mutating SQL"):
            db.execute(statement, readonly=True) if isinstance(statement, str) else db.orm_execute(statement, readonly=True)
        assert db._readonly(sa.text(statement) if isinstance(statement, str) else statement, None, None) is False
    assert db.execute("SELECT count(*) FROM items", readonly=True).scalar() == 2

    single_reads = [
        "SELECT 1",
        "SELECT 1;",
        "  select count(*) from items ;  ",
        "SELECT ';' AS semi, 'delete' AS word",
        "SELECT 1 -- ; DELETE FROM items\n",
        "SELECT 1 /* ; DELETE FROM items */",
        "WITH kept AS (SELECT 1 AS id) SELECT id FROM kept",
        "VALUES (1)",
        "SELECT 1; -- trailing note",
        "SELECT 1; /* trailing note */",
        sa.select(items.c.id).suffix_with("-- ; trailing note\n"),
    ]
    for statement in single_reads:
        assert is_sql_readonly(statement) is True, statement
    for dialect in _DIALECTS:
        assert is_sql_readonly("SELECT 1; -- trailing note", dialect=dialect) is True, dialect
    assert db.execute("SELECT count(*) AS n FROM items; -- trailing note", readonly=True).scalar() == 2
    assert db.execute("SELECT name FROM items WHERE name <> ';' ORDER BY id;", readonly=True).column(0) == ["alpha", "beta"]


def test_text_without_a_statement_is_not_read_only(sqlglot_mode):
    db, _items = _items_db()
    for text in ("", "  ;  ", "-- only a comment", "/* only a comment */ ;"):
        assert is_sql_readonly(text) is False, text
        assert is_sql_readonly(text, dialect="sqlite") is False, text
    # an empty statement next to a read leaves it a read
    assert is_sql_readonly("SELECT 1; ;") is True
    assert db._readonly(sa.text("SELECT count(*) FROM items; ;"), None, True) is True
    with pytest.raises(DatabaseError, match="readonly=True rejects mutating SQL"):
        db._readonly(sa.text(" ; "), None, True)


def test_select_into_is_not_read_only(sqlglot_mode):
    db, _items = _items_db()
    for dialect in (None, "postgresql", "mssql", "duckdb"):
        assert is_sql_readonly("SELECT * INTO items_copy FROM items", dialect=dialect) is False, dialect
    assert is_sql_readonly("SELECT id, name FROM items", dialect="postgresql") is True
    # readonly=None commits the new table instead of rolling it back
    assert db._readonly(sa.text("SELECT * INTO items_copy FROM items"), None, None) is False


def test_sql_server_batch_without_semicolon_is_not_read_only(sqlglot_mode):
    db, _items = _items_db()
    # SQLGlot reads the second statement of these batches as a column or table alias
    for text in ("SELECT 1 DELETE FROM items", "SELECT (SELECT 1) DELETE FROM items", "SELECT * FROM items DELETE", "SELECT 1 SHUTDOWN"):
        for dialect in _DIALECTS:
            assert is_sql_readonly(text, dialect=dialect) is False, (text, dialect)
    # readonly=None commits the DELETE instead of rolling it back
    assert db._readonly(sa.text("SELECT 1 DELETE FROM items"), None, None) is False
    for text in ('SELECT 1 AS "delete"', "SELECT id AS ident FROM items AS kept"):
        assert is_sql_readonly(text) is True, text


def test_explain_analyze_and_pragma_settings_are_not_read_only(sqlglot_mode, tmp_path):
    not_read_only = [
        # EXPLAIN ANALYZE runs the statement it explains
        "EXPLAIN ANALYZE DELETE FROM items",
        "EXPLAIN (ANALYZE) DELETE FROM items",
        "PRAGMA user_version = 5",
        "PRAGMA main.user_version = 5",
        "PRAGMA user_version(5)",
        "PRAGMA optimize",
        "PRAGMA wal_checkpoint(TRUNCATE)",
        "PRAGMA incremental_vacuum",
    ]
    read_only = [
        "EXPLAIN SELECT 1",
        "EXPLAIN ANALYZE SELECT 1",
        "PRAGMA user_version",
        "PRAGMA database_list",
        "PRAGMA ignore_check_constraints",
        "PRAGMA table_info(items)",
        "PRAGMA table_info('items')",
        "PRAGMA main.table_info(items)",
        "SHOW TABLES",
        "DESCRIBE items",
    ]
    for dialect in _DIALECTS:
        for text in not_read_only:
            assert is_sql_readonly(text, dialect=dialect) is False, (text, dialect)
        for text in read_only:
            assert is_sql_readonly(text, dialect=dialect) is True, (text, dialect)

    db = hb.Database(provider="sqlite", database=(tmp_path / "pragma.db").as_uri())
    db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
    with pytest.raises(DatabaseError, match="readonly=True rejects mutating SQL"):
        with db.tx() as tx:
            tx.execute("PRAGMA user_version = 9", readonly=True)
    assert db.execute("PRAGMA user_version", readonly=True).scalar() == 0


def test_text_whose_quoting_dialects_read_differently_is_not_read_only(sqlglot_mode):
    db, _items = _items_db()
    not_read_only = [
        # MySQL runs an executable comment that SQLGlot drops
        "SELECT 1 /*! INTO OUTFILE '/tmp/out.txt' */",
        "/*! INSERT INTO items (id) */ SELECT 3",
        "SELECT * FROM items /*!50000 FOR UPDATE */",
        "SELECT 1 /*! ; DELETE FROM items */",
        # MySQL reads --note as two minus signs
        "SELECT 1 --note",
        "SELECT '\\'' ; DELETE FROM items; -- '",
        "SELECT 2 # '\n; DROP TABLE items -- '",
        "SELECT $$'$$; DELETE FROM items; -- '",
        "SELECT 1 /* /* */ ; DELETE FROM items; -- */",
        "SELECT [it's] FROM items",
        "SELECT 'unterminated; DELETE FROM items",
    ]
    for text in not_read_only:
        for dialect in _DIALECTS:
            assert is_sql_readonly(text, dialect=dialect) is False, (text, dialect)
    # readonly=True refuses the text; readonly=None commits it instead of rolling it back
    with pytest.raises(DatabaseError, match="readonly=True rejects mutating SQL"):
        db.execute("SELECT 1 /*! INTO OUTFILE '/tmp/out.txt' */", readonly=True)
    assert db._readonly(sa.text("/*! INSERT INTO items (id) */ SELECT 5"), None, None) is False


def test_explain_and_describe_of_a_write_stay_read_only(sqlglot_mode):
    # EXPLAIN and DESCRIBE without ANALYZE plan the statement without running it
    read_only = [
        "EXPLAIN DELETE FROM items",
        "EXPLAIN UPDATE items SET id = 1",
        "EXPLAIN INSERT INTO items VALUES (1)",
        "EXPLAIN FORMAT=JSON DELETE FROM items",
        "DESCRIBE DELETE FROM items",
        "SHOW DATABASES",
        "SHOW CREATE TABLE items",
    ]
    for dialect in _DIALECTS:
        for text in read_only:
            assert is_sql_readonly(text, dialect=dialect) is True, (text, dialect)
        # a read next to a statement SQLGlot does not parse as a read follows the lexical rule
        assert is_sql_readonly("SELECT 1; SHOW TABLES", dialect=dialect) is False, dialect


def test_cte_named_like_a_write_word_is_not_read_only(sqlglot_mode):
    for dialect in _DIALECTS:
        for name in ("backup", "restore", "copy"):
            assert is_sql_readonly(f"WITH {name} AS (SELECT 1 AS x) SELECT x FROM {name}", dialect=dialect) is False, (name, dialect)
        assert is_sql_readonly('WITH "backup" AS (SELECT 1 AS x) SELECT x FROM "backup"', dialect=dialect) is True, dialect


def test_sqlglot_keeps_parsed_reads_read_only():
    pytest.importorskip("sqlglot")
    # text SQLGlot parses as reads only, with no write nested and no alias spelled like a write word
    for dialect in _DIALECTS:
        for text in ("SELECT 1; SELECT 2", "SELECT backup FROM items", "SELECT [update] FROM items"):
            assert is_sql_readonly(text, dialect=dialect) is True, (text, dialect)
        assert is_sql_readonly("SELECT 1 AS copy", dialect=dialect) is False, dialect
    for dialect in ("postgresql", "mysql", "duckdb", "sqlite"):
        assert is_sql_readonly("SELECT * FROM items FOR UPDATE", dialect=dialect) is True, dialect


def test_lexical_fallback_reports_only_one_certain_read_statement(monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlglot", None)
    not_read_only = [
        # SQL Server runs a batch of statements with no semicolon between them
        "SELECT 1 DELETE FROM items",
        # a locking read is refused by read-only transactions
        "SELECT * FROM items FOR UPDATE",
        # quoting that one dialect reads as a literal and another as code can hide a second statement
        "SELECT '\\'' ; DELETE FROM items; -- '",
        "SELECT 2 # '\n; DROP TABLE items -- '",
        "SELECT $$'$$; DELETE FROM items; -- '",
        "SELECT 'unterminated; DELETE FROM items",
        # a second read, and a write word as a column (SQLGlot parses all three as reads)
        "SELECT 1; SELECT 2",
        "SELECT backup FROM items",
        "SELECT [update] FROM items",
    ]
    for text in not_read_only:
        assert is_sql_readonly(text) is False, text
    read_only = [
        "SHOW TABLES",
        "SHOW CREATE TABLE items",
        "DESCRIBE items",
        "EXPLAIN SELECT 1",
        "PRAGMA table_info(items)",
        "SELECT replace(name, 'a', 'b'), truncate(id, 2) FROM items",
        "/* lead */ SELECT name FROM items WHERE name IN ('insert', 'delete')",
    ]
    for text in read_only:
        assert is_sql_readonly(text) is True, text


def test_transaction_autodetect_commits_dml_constructs():
    db, items = _items_db()

    with db.tx(readonly=None) as tx:
        assert tx.orm_execute(sa.select(sa.func.count()).select_from(items)).scalar() == 2
        tx.orm_execute(sa.insert(items).values(id=3, name="gamma"))
    assert db.orm_execute(sa.select(sa.func.count()).select_from(items), readonly=True).scalar() == 3

    with db.tx(readonly=None) as tx:
        tx.orm_execute(sa.select(items.c.id))
    assert db.in_transaction() is False


def test_schema_index_invalidation_follows_ddl_constructs_only(monkeypatch, counters):
    db, items = _items_db()
    other = sa.Table("other", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True))
    calls = []
    monkeypatch.setattr(db, "invalidate_schema_index", lambda: calls.append("invalidate"))

    db.orm_execute(sa.select(items.c.id), readonly=True)
    db.orm_execute(sa.insert(items).values(id=3, name="gamma"), readonly=False)
    db.orm_execute(sa.delete(items).where(items.c.id == 3), readonly=False)
    assert calls == [] and counters["str"] == 0

    db.orm_execute(sa.schema.CreateTable(other), readonly=False)
    db.orm_execute(sa.text("ALTER TABLE other ADD COLUMN note TEXT"), readonly=False)
    db.execute("DROP TABLE other")
    assert calls == ["invalidate"] * 3
    # a DDL construct is judged by the SQL the driver ran, not rendered again
    assert counters["str"] == 0


def test_ddl_written_as_text_columns_invalidates_the_schema_index(monkeypatch):
    db, _items = _items_db()
    calls = []
    monkeypatch.setattr(db, "invalidate_schema_index", lambda: calls.append("invalidate"))

    # text().columns() makes a SELECT construct, but its SQL is whatever the text says
    db.orm_execute(sa.text("CREATE TABLE extra (id INTEGER)").columns(), readonly=False)
    db.orm_execute(sa.text("SELECT id FROM items").columns(id=sa.Integer), readonly=True)
    assert calls == ["invalidate"]


def test_a_construct_classified_by_its_text_is_not_rendered_again_after_it_runs(counters):
    db, _items = _items_db()
    textual = sa.text("SELECT id FROM items ORDER BY id").columns(id=sa.Integer)
    entity = sa.select(_ExpressionItem.id).order_by(_ExpressionItem.id)
    db.orm_execute(textual).close()
    db.orm_execute(entity).close()
    counters.update(compile=0, str=0)

    # a text().columns() construct is classified by its own text
    for readonly in (False, None, True):
        assert db.orm_execute(textual, readonly=readonly).column(0) == [1, 2]
    # an ORM statement is classified after it runs by the SQL the driver ran
    assert db.orm_execute(entity).column(0) == [1, 2]
    assert counters == {"compile": 0, "str": 0}


def test_safe_failure_response_renders_statement_on_demand():
    db, _ = _items_db()
    stmt = sa.select(sa.column("x")).select_from(sa.table("missing"))

    resp = db.orm_execute(stmt, readonly=True, safe=True)

    assert resp.ok is False and resp.error_type
    assert resp.query == str(stmt)
    with pytest.raises(DatabaseError, match="missing"):
        db.orm_execute(stmt, readonly=True)


def test_engine_resolution_reuses_the_spec_identity(monkeypatch):
    db, items = _items_db()
    seen = []
    fingerprint = config_engine._fingerprint

    def counting(value):
        if isinstance(value, tuple) and len(value) == 3 and value[0] == db.spec.domain:
            seen.append(value)
        return fingerprint(value)

    monkeypatch.setattr(config_engine, "_fingerprint", counting)
    for _ in range(5):
        assert db.orm_execute(sa.select(sa.func.count()).select_from(items), readonly=True).scalar() == 2
    # the immutable spec fingerprints itself once; every later engine lookup keys on the memoized identity
    assert seen == []


def test_sqlite_connect_pragmas_wait_for_a_database_another_connection_is_converting():
    # ``PRAGMA journal_mode = WAL`` answers "database is locked" at once, without
    # the busy handler, while another process creates the same file, so a
    # first connection of many processes on a fresh home failed.
    import sqlite3

    from heavenbase.database import engine as engine_module

    pragmas = ["PRAGMA foreign_keys = ON;", "PRAGMA journal_mode = WAL;", "PRAGMA busy_timeout = 6000;"]
    ran = []

    class Cursor:
        busy = 3

        def execute(self, pragma):
            if "journal_mode" in pragma and self.busy:
                self.busy -= 1
                raise sqlite3.OperationalError("database is locked")
            ran.append(pragma)

    for pragma in engine_module._ordered_pragmas(pragmas):
        engine_module._execute_pragma(Cursor(), pragma)

    assert ran == ["PRAGMA busy_timeout = 6000;", "PRAGMA foreign_keys = ON;", "PRAGMA journal_mode = WAL;"]

    class Broken:
        def execute(self, pragma):
            raise sqlite3.OperationalError("no such pragma")

    with pytest.raises(sqlite3.OperationalError, match="no such pragma"):
        engine_module._execute_pragma(Broken(), "PRAGMA bogus;")


class _LockedType(sa.types.TypeDecorator):
    """Column type holding an unpicklable object, as connection-aware custom types do."""

    impl = sa.String
    cache_ok = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock = threading.Lock()


@pytest.mark.parametrize("copier", ["clone", "deepcopy", "pickle"])
def test_response_copies_keep_query_text_not_the_construct(copier):
    db = hb.Database(provider="sqlite", database=":memory:")
    metadata = sa.MetaData()
    items = sa.Table(
        "items",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tag", _LockedType(), default=lambda: "x"),
    )
    db.create_table(items)
    db.orm_execute(sa.insert(items), [{"id": 1, "tag": "a"}], readonly=False)
    stmt = sa.select(items.c.id).where(items.c.id == 1)
    copy_of = {
        "clone": lambda resp: resp.clone(),
        "deepcopy": lambda resp: copy.deepcopy(resp.cache()),
        "pickle": lambda resp: pickle.loads(pickle.dumps(resp.cache())),
    }[copier]

    resp = db.orm_execute(stmt, readonly=True)
    copied = copy_of(resp)
    failed = db.orm_execute(sa.select(sa.column("x")).select_from(sa.table("missing")), readonly=True, safe=True)
    failed_copy = copy.deepcopy(failed) if copier != "pickle" else pickle.loads(pickle.dumps(failed))

    # the copy owns rendered SQL text only; the original construct, its Table and MetaData stay with the caller
    assert copied.to_dicts() == [{"id": 1}] and copied.query == str(stmt) and copied._statement is None
    assert failed_copy.ok is False and failed_copy.query == failed.query and failed_copy._statement is None
    assert resp._statement is stmt and resp.query == str(stmt)


@pytest.mark.fast
def test_a_sqlite_database_connect_runs_its_pragmas_busy_timeout_first_and_retries_a_locked_one(tmp_path, monkeypatch):
    # Through a real engine connect: the configured pragmas run busy_timeout
    # first, and a pragma answered with "database is locked" is retried.
    import sqlite3

    from heavenbase.database import engine as engine_module
    from heavenbase.utils import path_to_file_uri

    execute = engine_module._execute_pragma
    ran = []
    busy = [2]

    class LockedOnce:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, pragma):
            if "journal_mode" in pragma.lower() and busy[0]:
                busy[0] -= 1
                raise sqlite3.OperationalError("database is locked")
            return self.cursor.execute(pragma)

    def recording(cursor, pragma, deadline=None):
        ran.append(pragma)
        execute(LockedOnce(cursor), pragma, deadline)

    monkeypatch.setattr(engine_module, "_execute_pragma", recording)
    path = tmp_path / "pragmas.db"
    db = hb.Database(provider="sqlite", database=path_to_file_uri(path))
    assert db.orm_execute(sa.text("select 1"), readonly=True).scalar() == 1

    assert ran and "busy_timeout" in ran[0].lower()
    assert any("journal_mode" in pragma.lower() for pragma in ran)
    assert busy == [0]
    with sqlite3.connect(path) as check:
        assert check.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.fast
def test_a_sqlite_connect_to_a_database_another_connection_holds_locked_gives_up_after_its_busy_timeout(tmp_path):
    # Retrying a locked initialization statement must not outlast the connection's
    # busy timeout: 0.1.2.2 gave up after one busy timeout, not after 30 seconds.
    import sqlite3
    import time

    from heavenbase.utils import path_to_file_uri

    path = tmp_path / "held.db"
    holder = sqlite3.connect(path, isolation_level=None)
    try:
        holder.execute("PRAGMA journal_mode = DELETE")
        holder.execute("CREATE TABLE items (id INTEGER)")
        holder.execute("BEGIN EXCLUSIVE")
        db = hb.Database(
            provider="sqlite",
            database=path_to_file_uri(path),
            pragmas=["PRAGMA journal_mode = WAL;", "PRAGMA busy_timeout = 400;"],
        )
        try:
            started = time.monotonic()
            with pytest.raises(Exception, match="locked"):
                db.execute("SELECT count(*) FROM items")
            assert time.monotonic() - started < 4.0
        finally:
            db.dispose()
    finally:
        holder.execute("ROLLBACK")
        holder.close()
