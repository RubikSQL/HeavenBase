from types import MappingProxyType

import pytest

from heavenbase.llm.trace import request_fingerprint

pytestmark = pytest.mark.fast


def test_read_only_request_mappings_have_the_same_fingerprint_at_every_depth():
    plain = {"route": {"model": "m", "provider": "p"}, "messages": [{"role": "user", "content": "x"}]}
    wrapped = MappingProxyType(
        {
            "messages": [MappingProxyType({"content": "x", "role": "user"})],
            "route": MappingProxyType({"provider": "p", "model": "m"}),
        }
    )
    assert wrapped == plain
    assert request_fingerprint(wrapped) == request_fingerprint(plain)
