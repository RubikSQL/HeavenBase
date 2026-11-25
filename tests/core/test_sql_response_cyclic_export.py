"""Default SQL response export preserves cycles in row values."""

import pytest

import heavenbase as hb

pytestmark = [pytest.mark.fast, pytest.mark.unit]


def test_sql_response_dict_export_copies_a_cyclic_value():
    value = {}
    value["self"] = value
    exported = hb.SQLResponse.from_rows([{"value": value, "alias": value}]).to_list()[0]

    assert exported["value"] is not value
    assert exported["value"]["self"] is exported["value"]
    assert exported["alias"] is exported["value"]
