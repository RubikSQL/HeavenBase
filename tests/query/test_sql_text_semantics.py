"""SQL text-parity evidence, routing, and compiler separation tests."""

from __future__ import annotations

import pytest

import heavenbase as hb
from heavenbase.schema import FieldSchema


def _db(tmp_path, name: str) -> str:
    return hb.utils.path_to_file_uri(tmp_path / name)


def _filter_step(query) -> dict:
    return next(step for step in query.explain()["steps"] if step.get("node") == "filter")


def test_sqlite_managed_text_contract_proves_exact_filter_corpus(tmp_path):
    """Managed SQLite text columns should prove exact filters over adversarial strings."""
    backend = hb.backends.SQLiteBackend("main", _db(tmp_path, "text-corpus.db"))
    ws = hb.HeavenBase("sqlite-text-corpus-ws", backends={"main": backend})

    class TextDoc(hb.Entity):
        identifier = "text-corpus-sql-doc"

        value = hb.field(hb.ShortText).optional()

    ws.register(TextDoc)
    values = ["A", "a", "é", "e\u0301", "🙂", "trail ", "", None, "a%b", "a_b", "a/b", "line\nbreak", "nul\0char", "42"]
    ws.upsert_many(TextDoc, [{"object_id": f"d{index:02d}", "value": value} for index, value in enumerate(values)])

    for index, value in enumerate(values):
        query = ws.query(TextDoc).where(TextDoc.value == value)
        assert query.execute().ids == [f"d{index:02d}"]
        assert _filter_step(query)["handler_mode"] == "native"
        assert _filter_step(query)["portable_proof"] is True

    membership = ws.query(TextDoc).where(TextDoc.value.in_(["A", "a", None]))
    assert membership.execute().ids == ["d00", "d01", "d07"]
    assert _filter_step(membership)["handler_mode"] == "native"
    assert _filter_step(membership)["portable_proof"] is True

    canonical = ws.query(TextDoc).where(TextDoc.value == 42)
    assert canonical.execute().ids == ["d13"]
    assert _filter_step(canonical)["handler_mode"] == "native"

    comparisons = (
        (TextDoc.value != "é", [f"d{index:02d}" for index, item in enumerate(values) if item != "é"]),
        (TextDoc.value < "é", [f"d{index:02d}" for index, item in enumerate(values) if item is not None and item < "é"]),
        (TextDoc.value <= "é", [f"d{index:02d}" for index, item in enumerate(values) if item is not None and item <= "é"]),
        (TextDoc.value > "é", [f"d{index:02d}" for index, item in enumerate(values) if item is not None and item > "é"]),
        (TextDoc.value >= "é", [f"d{index:02d}" for index, item in enumerate(values) if item is not None and item >= "é"]),
    )
    for expr, expected in comparisons:
        query = ws.query(TextDoc).where(expr)
        assert query.execute().ids == expected
        assert _filter_step(query)["portable_proof"] is True

    contains = ws.query(TextDoc).where(TextDoc.value.contains("%"))
    match = ws.query(TextDoc).where(TextDoc.value.match("A%"))
    assert contains.execute().ids == ["d08"]
    assert match.execute().ids == ["d08"]
    assert _filter_step(contains)["handler_mode"] == "native"
    assert _filter_step(contains)["portable_proof"] is True
    assert _filter_step(match)["fallback_reason"] == "text_semantics_not_proven"

    embedded = (
        ws.query(TextDoc).where(TextDoc.value.contains("\0")),
        ws.query(TextDoc).where(TextDoc.value.starts_with("nul\0")),
        ws.query(TextDoc).where(TextDoc.value.ends_with("\0char")),
    )
    assert [query.execute().ids for query in embedded] == [["d12"], ["d12"], ["d12"]]
    assert all(_filter_step(query)["portable_proof"] is True for query in embedded)
    assert ws.query(TextDoc).where(TextDoc.value.contains("")).execute().ids == [f"d{index:02d}" for index in range(len(values))]

    aggregate = ws.query(TextDoc).group_by(TextDoc.value).aggregate(rows=TextDoc.object_id.count())
    assert aggregate.explain()["steps"][-1]["handler_mode"] == "native"
    assert aggregate.explain()["steps"][-1]["portable_proof"] is True
    ordered = ws.query(TextDoc).aggregate(low=TextDoc.value.min(), high=TextDoc.value.max(), unique=TextDoc.value.count(distinct=True))
    assert ordered.execute().rows() == [{"low": "", "high": "🙂", "unique": len(values) - 1}]
    assert ordered.explain()["steps"][-1]["portable_proof"] is True


def test_sqlite_legacy_nocase_column_uses_portable_scan(tmp_path):
    """A legacy NOCASE column must not determine portable equality semantics."""
    backend = hb.backends.SQLiteBackend("main", _db(tmp_path, "legacy-nocase.db"))
    table_name = hb.backends.Backend._physical_name("legacy-nocase-sql-doc")
    backend.db.execute(
        f'CREATE TABLE "{table_name}" (object_id VARCHAR(63) PRIMARY KEY, value VARCHAR(255) COLLATE NOCASE)',
        readonly=False,
    )
    ws = hb.HeavenBase("sqlite-legacy-nocase-ws", backends={"main": backend})

    class LegacyDoc(hb.Entity):
        identifier = "legacy-nocase-sql-doc"

        value = hb.field(hb.ShortText)

    ws.register(LegacyDoc)
    ws.upsert_many(LegacyDoc, [{"object_id": "upper", "value": "A"}, {"object_id": "lower", "value": "a"}])
    query = ws.query(LegacyDoc).where(LegacyDoc.value == "A")

    assert query.execute().ids == ["upper"]
    assert _filter_step(query)["handler_mode"] == "py_scan"
    assert _filter_step(query)["fallback_reason"] == "text_semantics_not_proven"


def test_sqlite_legacy_column_without_enforced_contract_remains_unproved(tmp_path):
    """A mutable legacy table must remain on scan even before and after a BLOB write."""
    backend = hb.backends.SQLiteBackend("main", _db(tmp_path, "legacy-blob.db"))
    table_name = hb.backends.Backend._physical_name("legacy-blob-sql-doc")
    backend.db.execute(
        f'CREATE TABLE "{table_name}" (object_id VARCHAR(63) PRIMARY KEY, value VARCHAR(255) COLLATE BINARY)',
        readonly=False,
    )
    ws = hb.HeavenBase("sqlite-legacy-blob-ws", backends={"main": backend})

    class BlobDoc(hb.Entity):
        identifier = "legacy-blob-sql-doc"

        value = hb.field(hb.ShortText)

    ws.register(BlobDoc)
    first = ws.query(BlobDoc).where(BlobDoc.value == "missing")
    assert first.execute().ids == []
    assert _filter_step(first)["handler_mode"] == "py_scan"

    backend.db.execute(f"INSERT INTO \"{table_name}\" (object_id, value) VALUES ('blob', X'626c6f62')", readonly=False)
    query = ws.query(BlobDoc).where(BlobDoc.value == b"blob")

    assert query.execute().ids == ["blob"]
    assert _filter_step(query)["handler_mode"] == "py_scan"
    assert _filter_step(query)["fallback_reason"] == "text_semantics_not_proven"


def test_sqlite_constraint_proof_rejects_lexical_decoys(tmp_path):
    """A named comment containing the expected text must not impersonate a check."""
    from heavenbase.backends.sql.parity.sqlite import _constraint_name

    backend = hb.backends.SQLiteBackend("main", _db(tmp_path, "legacy-decoy.db"))
    table_name = hb.backends.Backend._physical_name("legacy-decoy-sql-doc")
    constraint_name = _constraint_name("value")
    backend.db.execute(
        f"""CREATE TABLE "{table_name}" (
            object_id VARCHAR(63) PRIMARY KEY,
            value VARCHAR(255) COLLATE BINARY
                CONSTRAINT "{constraint_name}" CHECK (1) /* typeof("value") IN ('text', 'null') */
        )""",
        readonly=False,
    )
    ws = hb.HeavenBase("sqlite-legacy-decoy-ws", backends={"main": backend})

    class DecoyDoc(hb.Entity):
        identifier = "legacy-decoy-sql-doc"

        value = hb.field(hb.ShortText)

    ws.register(DecoyDoc)
    backend.db.execute(f"INSERT INTO \"{table_name}\" (object_id, value) VALUES ('blob', X'626c6f62')", readonly=False)
    query = ws.query(DecoyDoc).where(DecoyDoc.value == b"blob")

    assert query.execute().ids == ["blob"]
    assert _filter_step(query)["handler_mode"] == "py_scan"
    assert _filter_step(query)["fallback_reason"] == "text_semantics_not_proven"


def test_sqlite_constraint_proof_requires_active_enforcement(tmp_path):
    """Disabling SQLite checks must immediately revoke native text permission."""
    backend = hb.backends.SQLiteBackend("main", _db(tmp_path, "disabled-contract.db"))
    ws = hb.HeavenBase("sqlite-disabled-contract-ws", backends={"main": backend})

    class DisabledDoc(hb.Entity):
        identifier = "disabled-contract-sql-doc"

        value = hb.field(hb.ShortText)

    ws.register(DisabledDoc)
    backend.db.execute("PRAGMA ignore_check_constraints = ON", readonly=False)
    table_name = backend.tables[DisabledDoc.identifier].name
    backend.db.execute(f"INSERT INTO \"{table_name}\" (object_id, value) VALUES ('blob', X'626c6f62')", readonly=False)
    query = ws.query(DisabledDoc).where(DisabledDoc.value == b"blob")

    assert query.execute().ids == ["blob"]
    assert _filter_step(query)["handler_mode"] == "py_scan"
    assert _filter_step(query)["fallback_reason"] == "text_semantics_not_proven"


def test_sqlite_managed_text_contract_rejects_non_text_storage(tmp_path):
    """Managed SQLite DDL should make its text-storage proof durable across writes."""
    backend = hb.backends.SQLiteBackend("main", _db(tmp_path, "managed-contract.db"))
    ws = hb.HeavenBase("sqlite-managed-contract-ws", backends={"main": backend})

    class ManagedDoc(hb.Entity):
        identifier = "managed-contract-sql-doc"

        value = hb.field(hb.ShortText)

    ws.register(ManagedDoc)
    table_name = backend.tables[ManagedDoc.identifier].name
    ddl = backend.db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :table_name",
        {"table_name": table_name},
        readonly=True,
    ).first()["sql"]
    response = backend.db.execute(
        f"INSERT INTO \"{table_name}\" (object_id, value) VALUES ('blob', X'626c6f62')",
        readonly=False,
        safe=True,
    )

    assert 'COLLATE "BINARY"' in ddl
    assert "typeof(\"value\")in('text','null')" in ddl.replace(" ", "").lower()
    assert response.ok is False
    assert response.error_type == "IntegrityError"
    assert "CHECK constraint failed" in response.error_message
    assert _filter_step(ws.query(ManagedDoc).where(ManagedDoc.value == "missing"))["portable_proof"] is True


def test_unsafe_is_explicit_and_may_follow_provider_text_behavior(tmp_path):
    """Unsafe policy should bypass only proof and report the applied relaxation."""
    backend = hb.backends.SQLiteBackend("main", _db(tmp_path, "unsafe.db"))
    ws = hb.HeavenBase("sqlite-unsafe-ws", backends={"main": backend})

    class NativeDoc(hb.Entity):
        identifier = "unsafe-sql-doc"

        value = hb.field(hb.ShortText)

    ws.register(NativeDoc)
    ws.upsert_many(NativeDoc, [{"object_id": "upper", "value": "É"}, {"object_id": "lower", "value": "é"}])
    portable = ws.query(NativeDoc).where(NativeDoc.value.match("é"))
    unsafe = ws.query(NativeDoc).where(NativeDoc.value.match("é")).unsafe()
    unsafe_like = ws.query(NativeDoc).where(NativeDoc.value.like("é")).unsafe()
    json_unsafe = ws.query_json(NativeDoc, {"filter": {"value": {"match": "é"}}, "unsafe": True})

    assert set(portable.execute().ids) == {"upper", "lower"}
    assert _filter_step(portable)["handler_mode"] == "py_scan"
    assert unsafe.execute().ids == ["lower"]
    assert _filter_step(unsafe)["handler_mode"] == "native"
    assert _filter_step(unsafe)["portable_proof"] is False
    assert _filter_step(unsafe)["unsafe"] is True
    assert _filter_step(unsafe)["unsafe_reasons"] == ["text_semantics_not_proven"]
    assert _filter_step(unsafe_like)["handler_mode"] == "native"
    assert _filter_step(unsafe_like)["portable_proof"] is False
    assert json_unsafe.spec.unsafe is True
    assert json_unsafe.execute().ids == ["lower"]


def test_all_sql_text_filters_fail_closed_and_unsafe_uses_only_real_compilers():
    """Every SQL provider should scan safely and expose its raw compiler only when unsafe."""
    from heavenbase.backends.sql.capabilities import registered_sql_dialects
    from heavenbase.execution.handlers.seed import seed_handlers
    from heavenbase.backends.sql.handler.base import sql_filter_handler
    from sqlalchemy import Column, MetaData, String, Table

    field = FieldSchema("value", hb.ShortText())
    table = Table("items", MetaData(), Column("value", String(255)))
    handlers = seed_handlers()
    operands = {
        "eq": "A",
        "ne": "A",
        "lt": "A",
        "lte": "A",
        "gt": "A",
        "gte": "A",
        "match": "A%_",
        "like": "A%_",
        "ilike": "A%_",
        "contains": "A%_",
        "starts_with": "A%_",
        "ends_with": "A%_",
        "in": ["A", "trail ", None],
        "regex": "^A.*$",
        "wildcard": "A*",
    }
    modules = hb.DEFAULT_CONTEXT.modules()
    for provider, dialect in registered_sql_dialects().items():
        if not modules.inspect("backend_type", provider):
            # A SQL dialect another test registered without a backend type of its name has no handler context.
            continue
        for op, operand in operands.items():
            safe = sql_filter_handler(op)(field, operand, hb.ext.HandlerContext("main", provider, hb.InlineColumn.identifier, handlers=handlers))
            assert safe.notes["mode"] == "py_scan", (provider, op)
            assert safe.notes["fallback_reason"] == "text_semantics_not_proven", (provider, op)

            unsafe_ctx = hb.ext.HandlerContext("main", provider, hb.InlineColumn.identifier, unsafe=True, handlers=handlers)
            unsafe = sql_filter_handler(op)(field, operand, unsafe_ctx)
            has_compiler = dialect.has_native_filter_compiler(op) or dialect.supports_native_text_filter(op)
            assert (unsafe.notes["mode"] == "native") is has_compiler, (provider, op)
            if has_compiler:
                assert unsafe.notes["portable_proof"] is False
                assert unsafe.notes["unsafe_reasons"] == ("text_semantics_not_proven",)
                assert unsafe.payload(table) is not None

    integer = sql_filter_handler("eq")(
        FieldSchema("value", hb.Integer()),
        1,
        hb.ext.HandlerContext("main", "sql", hb.InlineColumn.identifier, handlers=handlers),
    )
    assert integer.notes["mode"] == "native"


def test_mysql_text_evidence_is_live_narrow_and_engine_specific():
    """MySQL proof should require an exact UTF-8 column contract and reject lookalikes."""
    from heavenbase.backends.sql.capabilities import sql_dialect_spec
    from heavenbase.database.response import SQLResponse
    from sqlalchemy import Column, MetaData, String, Table

    table = Table("mysql_proof_doc", MetaData(), Column("value", String(17)))
    field = FieldSchema("value", hb.ShortText(max_chars=17))

    class FakeDB:
        def __init__(self, row):
            self.row = row

        def execute(self, *_args, **_kwargs):
            return SQLResponse.from_rows([self.row])

    class FakeBackend:
        def __init__(self, row):
            self.db = FakeDB(row)
            self.tables = {"mysql-proof-doc": table}

    valid = {
        "server_version": "8.4.1",
        "version_comment": "MySQL Community Server",
        "client_charset": "utf8mb4",
        "connection_charset": "utf8mb4",
        "results_charset": "utf8mb4",
        "data_type": "varchar",
        "column_charset": "utf8mb4",
        "max_chars": 17,
        "max_bytes": 68,
        "generation_expression": "",
        "table_type": "BASE TABLE",
        "proof_collation_charset": "utf8mb4",
        "proof_collation_pad": "PAD SPACE",
    }
    dialect = sql_dialect_spec("mysql")

    assert dialect.text_ops(FakeBackend(valid), "mysql-proof-doc", field, hb.InlineColumn.identifier) == ("eq", "ne", "in")
    for override in (
        {"server_version": "11.4.2-MariaDB"},
        {"version_comment": "OceanBase CE"},
        {"connection_charset": "latin1"},
        {"results_charset": "latin1"},
        {"column_charset": "utf8mb3"},
        {"data_type": "varbinary"},
        {"max_chars": 16},
        {"max_bytes": 67},
        {"generation_expression": "lower(value)"},
        {"table_type": "VIEW"},
        {"proof_collation_charset": "utf8mb3"},
        {"proof_collation_pad": "NO PAD"},
    ):
        row = {**valid, **override}
        assert dialect.text_ops(FakeBackend(row), "mysql-proof-doc", field, hb.InlineColumn.identifier) == ()


def test_sqlite_text_ddl_uses_descriptor_owned_length():
    """SQLite DDL and parity types should retain custom logical text limits."""
    from heavenbase.backends.sql.base import _sa_col
    from heavenbase.backends.sql.parity.sqlite import column_type
    from sqlalchemy import Column, MetaData, Table
    from sqlalchemy.dialects import sqlite
    from sqlalchemy.schema import CreateTable

    dtype = hb.ShortText(max_chars=17)
    table = Table("items", MetaData(), Column("title", _sa_col(dtype)))
    ddl = str(CreateTable(table).compile(dialect=sqlite.dialect())).upper()

    assert "TITLE VARCHAR(17)" in ddl
    assert column_type(dtype).length == 17


def test_mysql_long_text_storage_covers_the_logical_utf8mb4_limit():
    """MySQL LongText storage must budget four bytes per logical character."""
    from heavenbase.database.types import DatabaseTextType
    from sqlalchemy import Column, MetaData, Table
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateTable

    table = Table("items", MetaData(), Column("body", DatabaseTextType(length=hb.LongText().max_chars)))
    ddl = str(CreateTable(table).compile(dialect=mysql.dialect())).upper()

    assert "BODY MEDIUMTEXT" in ddl


def test_mysql_exact_text_compiler_guards_trailing_space_bytes():
    """MySQL exact predicates should repair PAD SPACE equality without casting the indexed key."""
    from heavenbase.backends.sql.capabilities import sql_dialect_spec
    from sqlalchemy import Column, MetaData, String, Table
    from sqlalchemy.dialects import mysql

    table = Table("items", MetaData(), Column("value", String(255)))
    dialect = sql_dialect_spec("mysql")
    equality = dialect.text_filter_expression(table.c.value, "eq", "trail ")
    membership = dialect.text_filter_expression(table.c.value, "in", ["A", "trail "])
    equality_sql = str(equality.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True})).lower()
    membership_sql = str(membership.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True})).lower()

    assert "collate utf8mb4_bin" in equality_sql
    assert "octet_length(items.value) = 6" in equality_sql
    assert membership_sql.count("octet_length(items.value)") == 2


def test_sql_dialect_text_candidates_validate_and_publish():
    """Text candidates should be immutable, validated, and metadata-visible."""
    spec = hb.ext.SQLDialectSpec("text-proof-spec", native_text_ops=("eq", "in"))
    key_spec = hb.ext.SQLDialectSpec(
        "text-key-proof-spec",
        native_text_key_ops=("group", "distinct"),
        text_key_evidence=lambda _backend, _entity_id, _field, _strategy: ("group", "distinct"),
        text_key_factory=lambda col, _purpose: col,
    )

    assert spec.native_text_ops == ("eq", "in")
    assert spec.to_dict()["native_text_ops"] == ["eq", "in"]
    assert spec.to_dict()["native_text_key_ops"] == []
    assert key_spec.to_dict()["native_text_key_ops"] == ["group", "distinct"]
    assert key_spec.text_key_ops(object(), "doc", FieldSchema("value", hb.ShortText()), hb.InlineColumn.identifier) == ("group", "distinct")
    assert hb.ext.SQLDialectSpec("text-proof-empty").native_text_ops == ()
    with pytest.raises(ValueError, match="subset"):
        hb.ext.SQLDialectSpec("text-proof-invalid", native_ops=("eq",), native_text_ops=("regex",))
    with pytest.raises(ValueError, match="native_text_key_ops"):
        hb.ext.SQLDialectSpec("text-key-proof-invalid", native_text_key_ops=("equality",), text_key_factory=lambda col, _purpose: col)
    with pytest.raises(ValueError, match="text_key_factory"):
        hb.ext.SQLDialectSpec("text-key-proof-missing-compiler", native_text_key_ops=("group",))
    with pytest.raises(TypeError, match="text_evidence"):
        hb.ext.SQLDialectSpec("text-proof-callback", text_evidence="not-callable")
    with pytest.raises(TypeError, match="text_key_evidence"):
        hb.ext.SQLDialectSpec("text-key-proof-callback", text_key_evidence="not-callable")


def test_documented_daily_text_fallback_model_matches_registry():
    """The durable fallback estimate should stay synchronized with dialect metadata."""
    from heavenbase.backends.sql.capabilities import sql_dialect_spec

    weights = {
        "eq": 28,
        "ne": 4,
        "in": 14,
        "lt": 1,
        "lte": 1,
        "gt": 1,
        "gte": 1,
        "contains": 15,
        "match": 12,
        "starts_with": 7,
        "ends_with": 3,
        "like": 3,
        "ilike": 2,
        "wildcard": 2,
        "regex": 2,
    }

    def coverage(identifier: str) -> tuple[int, int]:
        spec = sql_dialect_spec(identifier)
        safe = sum(weights.get(op, 0) for op in spec.native_text_ops) + (4 if spec.native_text_key_ops else 0)
        unsafe_coverage = sum(weight for op, weight in weights.items() if spec.has_native_filter_compiler(op) or spec.supports_native_text_filter(op))
        return safe, unsafe_coverage + (4 if spec.native_aggregate_ops else 0)

    assert coverage("sqlite") == (79, 98)
    assert coverage("mysql") == (46, 98)


def test_sqlite_affix_compiler_preserves_bytes_and_keeps_an_index_candidate():
    """SQLite prefix lowering should handle NUL while retaining a binary range candidate."""
    from heavenbase.backends.sql.parity.sqlite import text_filter
    from sqlalchemy import Column, MetaData, String, Table
    from sqlalchemy.dialects import sqlite

    table = Table("items", MetaData(), Column("value", String))
    prefix_sql = str(text_filter(table.c.value, "starts_with", "nul\0").compile(dialect=sqlite.dialect())).lower()
    suffix_sql = str(text_filter(table.c.value, "ends_with", "\0char").compile(dialect=sqlite.dialect())).lower()

    assert '(items.value collate "binary") >=' in prefix_sql
    assert "substr(cast(items.value as blob)" in prefix_sql
    assert "substr(cast(items.value as blob)" in suffix_sql


def test_sql_dialect_text_extensions_preserve_legacy_positional_signature():
    """New proof fields must not shift pre-existing positional arguments."""
    spec = hb.ext.SQLDialectSpec("legacy-positional-sql", ("eq",), ("count",), True)

    assert spec.native_ops == ("eq",)
    assert spec.native_aggregate_ops == ("count",)
    assert spec.case_insensitive_like is True
    assert spec.native_text_ops == ()
    assert spec.native_text_key_ops == ()


def test_sql_text_evidence_failure_withholds_native_permission():
    """Provider inspection failures should safely decline native execution."""

    def fail_evidence(_backend, _entity_id, _field, _strategy):
        raise RuntimeError("inspection failed")

    spec = hb.ext.SQLDialectSpec("text-proof-failure", native_text_ops=("eq",), text_evidence=fail_evidence)

    assert spec.text_ops(object(), "doc", FieldSchema("value", hb.ShortText()), hb.InlineColumn.identifier) == ()


def test_scalar_text_proof_does_not_authorize_aggregate_keys():
    """Scalar equality evidence must not authorize grouping or distinctness."""
    spec = hb.ext.SQLDialectSpec(
        "scalar-only-proof-sql",
        native_text_ops=("eq",),
        text_evidence=lambda _backend, _entity_id, _field, _strategy: ("eq",),
    )
    field = FieldSchema("value", hb.ShortText())

    assert spec.text_ops(object(), "scalar-only-proof-doc", field, hb.InlineColumn.identifier) == ("eq",)
    assert spec.text_key_ops(object(), "scalar-only-proof-doc", field, hb.InlineColumn.identifier) == ()


def test_literal_contains_and_match_have_independent_sql_compilers(monkeypatch):
    """Provider-proved literal compilers should escape metacharacters and differ by case mode."""
    from heavenbase.backends.sql.handler import base as sql_base
    from heavenbase.backends.sql.handler import text as sql_text
    from sqlalchemy import Column, MetaData, String, Table
    from sqlalchemy.dialects import sqlite

    spec = hb.ext.SQLDialectSpec(
        "literal-proof-sql",
        native_text_ops=("contains", "match"),
        text_evidence=lambda _backend, _entity_id, _field, _strategy: ("contains", "match"),
    )
    monkeypatch.setattr(sql_base, "sql_dialect_spec", lambda _identifier, *, resolver=None: spec)
    monkeypatch.setattr(sql_text, "sql_dialect_spec", lambda _identifier, *, resolver=None: spec)
    ctx = hb.ext.HandlerContext(
        "main",
        "sqlite",
        hb.InlineColumn.identifier,
        backend_instance=object(),
        entity_id="literal-proof-doc",
    )
    field = FieldSchema("value", hb.ShortText())
    table = Table("items", MetaData(), Column("value", String))
    contains = sql_base.sql_filter_handler("contains")(field, "A%_/", ctx)
    match = sql_base.sql_filter_handler("match")(field, "A%_/", ctx)
    contains_sql = str(contains.payload(table).compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
    match_sql = str(match.payload(table).compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))

    assert contains.notes["mode"] == match.notes["mode"] == "native"
    assert " ESCAPE '/'" in contains_sql and " ESCAPE '/'" in match_sql
    assert "lower(" not in contains_sql.lower()
    assert "lower(" in match_sql.lower()
