"""SQL filter metadata reuse follows the module publication, not row data."""

import heavenbase as hb
import pytest

from heavenbase.backends.sql import capabilities

pytestmark = [pytest.mark.fast, pytest.mark.unit]


class ChangingDomain(hb.LogicalType):
    """Importable test type whose filter-domain registration changes."""

    identifier = "changing-filter-cache-domain"


def test_sql_filter_cache_reuses_metadata_and_observes_republication(monkeypatch):
    modules = hb.DEFAULT_CONTEXT.modules()
    original = capabilities.inspect_tags
    reads = []

    def inspect(*args, **kwargs):
        reads.append(args[2])
        return original(*args, **kwargs)

    monkeypatch.setattr(capabilities, "inspect_tags", inspect)
    try:
        hb.ext.register_logical_type(ChangingDomain, tags={"filter-domains": ["struct"]}, resolver=modules)
        assert capabilities.sql_filter_requires_scan(ChangingDomain, resolver=modules)
        assert capabilities.sql_filter_requires_scan(ChangingDomain, resolver=modules)
        assert reads == [ChangingDomain.identifier]
        hb.ext.register_logical_type(ChangingDomain, tags={"filter-domains": ["text"]}, resolver=modules)
        assert not capabilities.sql_filter_requires_scan(ChangingDomain, resolver=modules)
        assert reads == [ChangingDomain.identifier, ChangingDomain.identifier]
    finally:
        modules.unregister("logical_type", ChangingDomain.identifier)
