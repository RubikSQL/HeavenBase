"""Integration regressions for related schema and SQL registration changes."""

import os
from uuid import uuid4

import heavenbase as hb
import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


def _casefold_words(text, _config):
    return text.casefold().split()


@pytest.mark.slow
def test_normalizing_tokenizer_matches_embedded_keywords(tmp_path):
    class Listing(hb.Entity):
        identifier = "gram-casefold-listing"
        keywords = hb.field(hb.Array[hb.ShortText]).store(
            to="main",
            strategy=hb.SparseGramIndex(normalizer="none", gram_unit="token", tokenizer="r9gram-casefold-words"),
        )

    hb.ext.register_gram_tokenizer("r9gram-casefold-words", _casefold_words)
    backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "store.db")}}
    workspace = hb.HeavenBase("gram-casefold", detached=True, backends=backends)
    try:
        workspace.register(Listing)
        workspace.upsert(Listing, {"object_id": "tea", "keywords": ["Green Tea"]})
        assert workspace.query(Listing).where(Listing.keywords.contained_in("Green Tea")).execute().ids == ["tea"]
        assert workspace.query(Listing).where(Listing.keywords.contained_in("Buy Green Tea today")).execute().ids == ["tea"]
    finally:
        workspace.release()


@pytest.mark.slow
def test_rejected_catalog_change_preserves_stored_schema(tmp_path):
    class Listing(hb.Entity):
        identifier = "gram-catalog-reopen"
        object_id = hb.field(hb.Identifier(max_chars=300)).pk()
        name = hb.field(hb.ShortText)
        keywords = hb.field(hb.Array[hb.ShortText]).default([])

    class ListingWithoutKeywords(hb.Entity):
        identifier = Listing.identifier
        object_id = hb.field(hb.Identifier(max_chars=300)).pk()
        name = hb.field(hb.ShortText)

    backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "store.db")}}
    first = hb.HeavenBase("gram-catalog-reopen", detached=True, backends=backends)
    entity_row = f"entity:{Listing.identifier}"
    try:
        first.register(Listing, catalog="none")
        first.upsert_many(
            Listing,
            [
                {"object_id": "tea", "name": "Green Tea", "keywords": ["green tea"]},
                {"object_id": "local-stock-" + "seasonal-green-tea-" * 14, "name": "Seasonal Green Tea"},
            ],
        )
        before = first.get(entity_row, entity=hb.MetaSchema)
    finally:
        first.release()

    second = hb.HeavenBase("gram-catalog-reopen", detached=True, backends=backends)
    try:
        with pytest.raises(ValueError, match="Catalog row"):
            second.register(ListingWithoutKeywords, catalog="full", allow_replace=True)
        assert second.get(entity_row, entity=hb.MetaSchema)["meta"] == before["meta"]
    finally:
        second.release()


@pytest.mark.external
def test_postgres_registration_with_two_gram_routes_on_one_database():
    pytest.importorskip("psycopg2")
    endpoint = os.environ.get("HVNB_POSTGRES_URL")
    if not endpoint:
        pytest.skip("HVNB_POSTGRES_URL must point to a PostgreSQL test service")

    class Listing(hb.Entity):
        identifier = "gram-postgres-alias"
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex())
        categories = hb.field(hb.Array[hb.ShortText]).store(to="labels", strategy=hb.SparseGramIndex())

    database = f"hb_gram_alias_{uuid4().hex}"
    root = make_url(endpoint).set(database="postgres")
    admin = create_engine(root, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3})
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f"CREATE DATABASE {database}")
        try:
            url = root.set(database=database).update_query_dict({"options": "-c statement_timeout=3000"})
            route = {"type": "postgres", "url": url.render_as_string(hide_password=False)}
            workspace = hb.HeavenBase(
                database.replace("_", "-"),
                detached=True,
                backends={"main": dict(route, name="main"), "labels": dict(route, name="labels")},
            )
            try:
                for route_name in ("main", "labels"):
                    assert workspace.backends.get(route_name).db.execute("SELECT current_database()", readonly=True).scalar() == database
                assert workspace.register(Listing) is Listing
            finally:
                workspace.release()
        finally:
            with admin.connect() as connection:
                connection.exec_driver_sql(f"DROP DATABASE {database} WITH (FORCE)")
    finally:
        admin.dispose()
