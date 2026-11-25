"""An iterator position can only be advanced by one call at a time."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
import sqlalchemy as sa

import heavenbase as hb

pytestmark = [pytest.mark.fast, pytest.mark.unit]


def test_overlapping_next_on_the_same_response_iterator(tmp_path):
    converting = Event()
    resume = Event()

    class PausedInteger(sa.types.TypeDecorator):
        impl = sa.Integer
        cache_ok = False

        def process_result_value(self, value, dialect):
            if value == 1:
                converting.set()
                assert resume.wait(10)
            return value

    db = hb.Database(provider="sqlite", database=hb.utils.path_to_file_uri(tmp_path / "iter.db"))
    statement = sa.text("SELECT 1 AS id UNION ALL SELECT 2 UNION ALL SELECT 3").columns(id=PausedInteger())
    response = db.execute(statement)
    rows = iter(response)
    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(next, rows)
            try:
                assert converting.wait(10)
                second = pool.submit(next, rows)
                with pytest.raises(ValueError, match="already executing"):
                    second.result(timeout=3)
            finally:
                resume.set()
            assert first.result(timeout=10) == {"id": 1}
        assert list(rows) == [{"id": 2}, {"id": 3}]
    finally:
        response.close()
        db.dispose()
