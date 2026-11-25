"""RowOp isolates backend-ready object graphs and preserves their references."""

import pytest

from heavenbase.execution.contracts import RowOp

pytestmark = pytest.mark.fast


@pytest.mark.parametrize("fields", [None, ["left", "right"]])
def test_rowop_keeps_shared_cycles_in_detached_values(fields):
    loop = []
    pair = (loop,)
    loop.append(pair)
    source = {"left": loop, "right": loop}
    op = RowOp("item", "id", source)
    row = op.to_row(fields=fields)
    assert row["left"] is row["right"]
    assert row["left"][0][0] is row["left"]
    assert row["left"] is not loop
    assert row["left"] is not op.values["left"]
    assert row["object_id"] == "id"
