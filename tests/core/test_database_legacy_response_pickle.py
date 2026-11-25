"""SQLResponse pickle compatibility across the statement-rendering change."""

import base64
import pickle

import pytest

pytestmark = [pytest.mark.fast, pytest.mark.unit]

# Produced by pickle.dumps(SQLResponse.from_rows([{"n": 1}]), protocol=4)
# from the 0.1.2.2 source at c8976704.
_RESPONSE_0122 = (
    "gASV6QAAAAAAAACMHGhlYXZlbmJhc2UuZGF0YWJhc2UucmVzcG9uc2WUjAtTUUxSZXNwb25zZZSTlCmBlH2UKIwC"
    "b2uUiIwIX2NvbHVtbnOUXZSMAW6UYYwFX3Jvd3OUXZR9lGgISwFzYYwKX3Jvd19jb3VudJRLAYwKX2xhc3Ryb3dp"
    "ZJROjAtfZXJyb3JfdHlwZZSMAJSMDl9lcnJvcl9tZXNzYWdllGgPjApfZXhjZXB0aW9ulE6MBl9xdWVyeZROjAdf"
    "cGFyYW1zlE6MCF9lbGFwc2VklE6MCV9yb3dfaXRlcpROjAZfY2xvc2WUTnViLg=="
)


def test_response_pickle_without_query_label():
    response = pickle.loads(base64.b64decode(_RESPONSE_0122))

    assert response.to_dicts() == [{"n": 1}]
    assert response.query is None
    clone = response.clone()
    assert clone.query is None
    assert clone.to_dicts() == [{"n": 1}]
