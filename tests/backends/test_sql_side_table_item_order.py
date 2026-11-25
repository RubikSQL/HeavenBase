"""SideTable aggregates return items in their stored order on every SQL dialect."""

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, select
from sqlalchemy.dialects import mysql, sqlite

import heavenbase as hb

pytestmark = [pytest.mark.fast, pytest.mark.unit]

_SIDE = Table("catalog_tags", MetaData(), Column("idx", Integer), Column("item", String))


@pytest.mark.parametrize(("identifier", "dialect", "function"), [("mysql", mysql.dialect(), "json_arrayagg"), ("sqlite", sqlite.dialect(), "json_group_array")])
def test_unordered_aggregates_carry_each_item_position(identifier, dialect, function):
    spec = hb.ext.registered_sql_dialects()[identifier]
    sql = str(select(spec.side_table_json_agg_expression(_SIDE.c.item, _SIDE.c.idx)).compile(dialect=dialect)).lower()
    assert f"{function}(json_array(catalog_tags.idx, catalog_tags.item))" in sql
    # The engine may aggregate the rows in any order, for example in index order of the items.
    assert spec.side_table_json_agg_items([[2, "batch-7"], [0, "lighting"], [3, "home"], [1, "green"]]) == ["lighting", "green", "batch-7", "home"]


def test_postgres_aggregate_is_already_ordered():
    spec = hb.ext.registered_sql_dialects()["postgresql"]
    assert spec.side_table_json_agg_items(["lighting", "green"]) == ["lighting", "green"]
