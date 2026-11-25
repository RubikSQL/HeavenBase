"""An extension can still pass name warning options positionally."""

import inspect

import pytest

from heavenbase.database.dialect import DatabaseDialectSpec

pytestmark = [pytest.mark.fast, pytest.mark.unit]


def test_database_dialect_spec_preserves_positional_name_warning_options():
    defaults = []
    for parameter in list(inspect.signature(DatabaseDialectSpec).parameters.values())[1:]:
        defaults.append({} if parameter.name in {"tags", "memory_connect_args"} else parameter.default)
        if parameter.name == "vector_native_type":
            break

    factory = ("sqlalchemy.dialects.sqlite", "dialect")
    spec = DatabaseDialectSpec("extension", *defaults, factory, ("reserved",), 42)

    assert spec.name_warning_dialect_factory == factory
    assert spec.name_warning_reserved_words == ("reserved",)
    assert spec.name_warning_max_identifier_length == 42
