from importlib.util import find_spec

import pytest

import heavenbase as hb

WORKSPACES = hb.DEFAULT_CONTEXT._workspace_registry


class CustomInteropType(hb.LogicalType):
    """Logical type used by the registered custom inferer test."""

    identifier = "custom-interop-type"


class FirstPriorityType(hb.LogicalType):
    """Logical type used by the first-priority inferer test."""

    identifier = "first-priority-type"


class ResolverOnlyType(hb.LogicalType):
    """Logical type visible only through an explicitly supplied resolver."""

    identifier = "resolver-only-type"


def _infer_custom(source, values, hint, report):
    """Infer `CustomInteropType` for the test category domain."""
    del hint, report
    return CustomInteropType() if source == "category" and set(values) <= {"a", "b"} else None


def _infer_first(source, values, hint, report):
    """Infer `FirstPriorityType` for the priority probe."""
    del values, hint, report
    return FirstPriorityType() if source == "priority_probe" else None


def _infer_resolver_only(source, values, hint, report, *, resolver):
    """Infer the resolver-only type while exercising resolver injection."""
    del values, hint, report
    assert resolver is not None
    return ResolverOnlyType() if source == "private_column" else None


@pytest.fixture(autouse=True)
def _isolate_workspaces():
    before = set(WORKSPACES.keys())
    yield
    for wid in set(WORKSPACES.keys()) - before:
        WORKSPACES.pop(wid, None)


class FakeSQLResponse:
    def __init__(self, rows):
        self.ok = True
        self._rows = rows

    def to_list(self, row_fmt="dict"):
        assert row_fmt == "dict"
        return list(self._rows)


def test_sql_result_accepts_naive_tables_and_long_text_default():
    iset = hb.from_sql_result([{"sku": "p1", "name": "Oak Desk", "price": 10.0}], entity_id="interop-naive", id="sku")

    assert iset.entity.schema().get("name").dtype.identifier == "long-text"
    assert any(issue.code == "assumed_long_text" and issue.path == "name" for issue in iset.report.issues)

    ws = hb.HeavenBase("test-interop-naive")
    with pytest.raises(KeyError, match="has no registered Entity class"):
        iset.upsert(ws)
    assert iset.entity.schema().entity_id not in ws.entities
    assert iset.register(ws) is iset.entity
    assert iset.upsert(ws) == ["p1"]
    assert ws.get("p1", entity=iset.entity)["name"] == "Oak Desk"

    list_rows = hb.from_sql_result([["p2", "Lamp", 25.0]], columns=["sku", "name", "price"], entity_id="interop-list", id="sku")
    assert list_rows.rows == [{"object_id": "p2", "name": "Lamp", "price": 25.0}]


def test_empty_interop_upsert_still_requires_explicit_class_registration():
    """An empty staging set must preserve the separate register/upsert lifecycle."""

    class EmptyInteropThing(hb.Entity):
        identifier = "empty-interop-thing"

        name = hb.field(hb.ShortText)

    workspace = hb.HeavenBase("test-empty-interop-upsert")
    staged = hb.InteropSet.from_entity(EmptyInteropThing)

    with pytest.raises(KeyError, match="has no registered Entity class"):
        staged.upsert(workspace)

    assert EmptyInteropThing.identifier not in workspace.entities


def test_entity_owned_sql_import_helper_returns_interop_set():
    iset = hb.Entity.from_sql_result([{"sku": "p1", "name": "Oak Desk", "price": 10.0}], entity_id="interop-entity-sql", id="sku")

    assert isinstance(iset, hb.InteropSet)
    assert iset.entity.schema().entity_id == "interop-entity-sql"
    assert iset.rows == [{"object_id": "p1", "name": "Oak Desk", "price": 10.0}]


def test_sql_result_accepts_sqlresponse_like_objects():
    iset = hb.from_sql_result(FakeSQLResponse([{"sku": "p1", "name": "Desk"}]), entity_id="interop-sqlresponse", id="sku")

    assert iset.rows == [{"object_id": "p1", "name": "Desk"}]
    assert iset.report.source == "sql"


def test_null_only_sql_columns_are_reported_without_row_noise():
    iset = hb.from_sql_result([{"sku": "p1", "name": "Desk", "note": None}], entity_id="interop-null-only", id="sku")

    assert "note" in iset.entity.schema().fields
    assert iset.rows == [{"object_id": "p1", "name": "Desk"}]
    assert any(issue.code == "empty_column" and issue.path == "note" for issue in iset.report.issues)


def test_renamed_target_overrides_do_not_create_duplicate_fields():
    iset = hb.from_sql_result(
        [{"sku": "p1", "display_name": "Desk"}],
        entity_id="interop-renamed-override",
        id="sku",
        rename={"display_name": "name"},
        overrides={"name": hb.field(hb.ShortText).required()},
    )

    assert iset.report.ok
    assert iset.entity.schema().get("name").dtype.identifier == "short-text"
    assert iset.entity.schema().get("name").required is True
    assert iset.rows == [{"object_id": "p1", "name": "Desk"}]


def test_optional_dependency_error_message_is_actionable():
    from heavenbase.interop.base import need

    with pytest.raises(ImportError, match="install heavenbase\\[interop\\]"):
        need("__heavenbase_missing_optional__", "test feature")


def test_register_interop_type_inferer_extends_mapping_without_core_edits():
    registered = hb.register_interop_type_inferer(_infer_custom, first=True)
    iset = hb.from_sql_result([{"sku": "p1", "category": "a"}], entity_id="interop-custom-inferer", id="sku")

    assert isinstance(registered, hb.InteropTypeInferer)
    assert registered.identifier in hb.registered_interop_type_inferers()
    assert iset.entity.schema().get("category").dtype.identifier == "custom-interop-type"


@pytest.mark.fast
def test_sql_interop_threads_explicit_resolver_to_registered_inferers():
    from heavenbase.modules import ModuleService
    from heavenbase.utils.registry import Registry

    resolver = ModuleService(Registry("system-modules"))
    hb.register_interop_type_inferer(_infer_resolver_only, resolver=resolver)

    iset = hb.from_sql_result(
        [{"sku": "p1", "private_column": "value"}],
        entity_id="interop-explicit-resolver",
        id="sku",
        resolver=resolver,
    )

    assert iset.entity.schema().get("private_column").dtype.identifier == "resolver-only-type"


def test_interop_type_inferer_protocol_wraps_callable_and_metadata():
    def infer_short_code(source, values, hint, report):
        if source == "code" and all(isinstance(value, str) and len(value) <= 4 for value in values):
            return hb.ShortText()
        return None

    inferer = hb.InteropTypeInferer(infer_short_code, identifier="test-short-code-inferer", priority=-100)
    report = hb.InteropReport("unit")

    assert inferer.infer("code", ["A1"], None, report).identifier == "short-text"
    assert inferer("other", ["A1"], None, report) is None
    assert inferer.to_dict() == {
        "identifier": "test-short-code-inferer",
        "fn": f"{infer_short_code.__module__}.test_interop_type_inferer_protocol_wraps_callable_and_metadata.<locals>.infer_short_code",
        "priority": -100,
    }


def test_interop_type_inferer_priority_runs_first():
    inferer = hb.InteropTypeInferer(_infer_first, identifier="test-first-priority-inferer")
    registered = hb.register_interop_type_inferer(inferer, first=True, replace=True)
    iset = hb.from_sql_result([{"sku": "p1", "priority_probe": "text"}], entity_id="interop-priority-inferer", id="sku")

    ordered = list(hb.registered_interop_type_inferers().values())
    assert ordered[0].identifier == registered.identifier
    assert iset.entity.schema().get("priority_probe").dtype.identifier == "first-priority-type"


def test_override_requiredness_is_preserved():
    iset = hb.from_sql_result(
        [{"sku": "p1", "name": "Desk"}],
        entity_id="interop-required",
        id="sku",
        overrides={"name": hb.field(hb.LongText).required()},
    )

    assert iset.entity.schema().get("name").required is True


def test_interop_set_instances_and_direct_workspace_record_upsert():
    class RecordThing(hb.Entity):
        name = hb.field(hb.LongText)

    class ExternalRecord:
        def model_dump(self):
            return {"object_id": "r1", "name": "Desk"}

    ws = hb.HeavenBase("test-interop-record")
    ws.register(RecordThing)
    assert ws.upsert(RecordThing, ExternalRecord()) == "r1"
    assert ws.get("r1", entity=RecordThing)["name"] == "Desk"

    iset = hb.InteropSet.from_entity(RecordThing, [ExternalRecord()])
    assert iset.one().to_dict() == {"object_id": "r1", "name": "Desk"}


def test_pydantic_v1_shaped_records_are_coerced_by_duck_type():
    class RecordThing(hb.Entity):
        name = hb.field(hb.LongText)

    class PydanticV1Like:
        __fields__ = {"object_id": object(), "name": object()}

        def dict(self):
            return {"object_id": "r1", "name": "Desk"}

    ws = hb.HeavenBase("test-interop-pydantic-v1-like")
    ws.register(RecordThing)

    assert ws.upsert(RecordThing, PydanticV1Like()) == "r1"
    assert ws.get("r1", entity=RecordThing)["name"] == "Desk"


def test_interop_rows_module_owns_record_normalization():
    from heavenbase.interop import row_from_any as facade_row_from_any
    from heavenbase.interop.rows import row_from_any, rows_from_naive_table, source_row

    class ExternalRecord:
        def model_dump(self):
            return {"object_id": "r1", "name": "Desk", "note": None}

    assert facade_row_from_any is row_from_any
    assert source_row({"name": None}) == {"name": None}
    assert row_from_any(ExternalRecord()) == {"object_id": "r1", "name": "Desk"}
    assert rows_from_naive_table([["r2", "Lamp"]], columns=["object_id", "name"]) == [{"object_id": "r2", "name": "Lamp"}]


def test_register_refreshes_metadata_but_rejects_structure_change():
    ws = hb.HeavenBase("test-interop-metadata")

    class MetadataThing(hb.Entity):
        """Original description."""

        name = hb.field(hb.LongText, desc="Original name")

    ws.register(MetadataThing)

    class MetadataThingV2(hb.Entity):
        """Updated description."""

        identifier = "metadata-thing"
        name = hb.field(hb.LongText, desc="Updated name")

    registered = ws.register(MetadataThingV2)
    assert registered is MetadataThing
    assert MetadataThing.schema().desc == "Updated description."
    assert MetadataThing.schema().get("name").desc == "Updated name"

    fields = ws.query(hb.MetaSchema).where(hb.MetaSchema.subject_id == "metadata-thing").where(hb.MetaSchema.field == "name").execute()
    assert fields.rows()[0]["desc"] == "Updated name"

    class MetadataThingBroken(hb.Entity):
        identifier = "metadata-thing"
        name = hb.field(hb.Integer)

    with pytest.raises(ValueError, match="different structure"):
        ws.register(MetadataThingBroken)


def test_workspace_describe_updates_metadata_only():
    ws = hb.HeavenBase("test-interop-describe")

    class DescribedThing(hb.Entity):
        name = hb.field(hb.LongText)

    ws.register(DescribedThing)
    ws.describe(DescribedThing, desc="Catalog row", fields={"name": {"desc": "Display name", "tags": ["display"], "examples": ["Desk"]}})

    assert DescribedThing.schema().desc == "Catalog row"
    field_schema = DescribedThing.schema().get("name")
    assert field_schema.desc == "Display name"
    assert field_schema.tags == ["display"]
    assert field_schema.examples == ["Desk"]


@pytest.mark.skipif(find_spec("pydantic") is None, reason="pydantic is not installed")
def test_pydantic_import_upsert_and_export():
    from pydantic import BaseModel, Field

    class ProductModel(BaseModel):
        sku: str = Field(description="Stable SKU", max_length=16)
        name: str
        price: float

    iset = hb.Entity.from_pydantic(ProductModel, rows=[ProductModel(sku="p1", name="Desk", price=10.0)], entity_id="interop-pydantic", id="sku")
    ws = hb.HeavenBase("test-interop-pydantic")
    iset.register(ws)
    iset.upsert(ws)

    frame = ws.query(iset.entity).execute()
    models = frame.to_pydantic(ProductModel, rename={"object_id": "sku"})

    assert iset.entity.schema().get("object_id").dtype.identifier == "identifier"
    assert models[0].sku == "p1"
    assert models[0].name == "Desk"


@pytest.mark.skipif(find_spec("pandas") is None, reason="pandas is not installed")
def test_pandas_import_and_export():
    import pandas as pd

    df = pd.DataFrame([{"sku": "p1", "name": "Desk", "price": 10.0}])
    iset = hb.Entity.from_pandas(df, entity_id="interop-pandas", id="sku")
    ws = hb.HeavenBase("test-interop-pandas")
    iset.register(ws)
    iset.upsert(ws)

    out = ws.query(iset.entity).execute().to_pandas()

    assert list(out["name"]) == ["Desk"]
    assert iset.entity.schema().get("name").dtype.identifier == "long-text"


@pytest.mark.skipif(find_spec("pandas") is None, reason="pandas is not installed")
def test_pandas_object_column_infers_from_samples():
    import pandas as pd

    df = pd.DataFrame([{"sku": "p1", "payload": {"source": "manual"}}])
    iset = hb.from_pandas(df, entity_id="interop-pandas-object", id="sku")

    assert iset.entity.schema().get("payload").dtype.identifier == "json"
    assert any(issue.code == "assumed_json" and issue.path == "payload" for issue in iset.report.issues)


@pytest.mark.skipif(find_spec("pandas") is None, reason="pandas is not installed")
def test_pandas_duplicate_columns_report_error():
    import pandas as pd

    df = pd.DataFrame([["p1", "Desk"]], columns=["sku", "sku"])
    iset = hb.from_pandas(df, entity_id="interop-pandas-dupes", id="sku")

    assert not iset.report.ok
    assert any(issue.code == "duplicate_source_column" for issue in iset.report.issues)
    with pytest.raises(ValueError, match="Interop conversion failed"):
        iset.upsert(hb.HeavenBase("test-interop-pandas-dupes"))


@pytest.mark.skipif(find_spec("pyarrow") is None, reason="pyarrow is not installed")
def test_pyarrow_import_and_export():
    import pyarrow as pa

    table = pa.table({"sku": ["p1"], "name": ["Desk"], "price": [10.0]})
    iset = hb.from_pyarrow(table, entity_id="interop-arrow", id="sku")
    ws = hb.HeavenBase("test-interop-arrow")
    iset.register(ws)
    iset.upsert(ws)

    out = ws.query(iset.entity).execute().to_pyarrow()

    assert out.num_rows == 1
    assert out.column("name").to_pylist() == ["Desk"]


@pytest.mark.skipif(find_spec("numpy") is None, reason="numpy is not installed")
def test_numpy_import_and_export():
    import numpy as np

    arr = np.asarray([["p1", "Desk", 10.0]], dtype=object)
    iset = hb.from_numpy(arr, columns=["sku", "name", "price"], entity_id="interop-numpy", id="sku", overrides={"price": hb.Float})
    ws = hb.HeavenBase("test-interop-numpy")
    iset.register(ws)
    iset.upsert(ws)

    out = ws.query(iset.entity).select("name", "price").execute().to_numpy("name", "price")

    assert out.shape == (1, 2)
    assert out[0, 0] == "Desk"


@pytest.mark.skipif(find_spec("sqlalchemy") is None, reason="sqlalchemy is not installed")
def test_sqlalchemy_result_import():
    from sqlalchemy import Column, Float, MetaData, String, Table, create_engine, insert, select

    engine = create_engine("sqlite:///:memory:")
    meta = MetaData()
    products = Table("products", meta, Column("sku", String, primary_key=True), Column("name", String), Column("price", Float))
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(products), [{"sku": "p1", "name": "Desk", "price": 10.0}])
        iset = hb.from_sql_result(conn.execute(select(products)), entity_id="interop-sqlalchemy", id="sku")

    assert iset.rows == [{"object_id": "p1", "name": "Desk", "price": 10.0}]
