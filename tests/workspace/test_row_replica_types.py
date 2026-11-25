"""Strict replica reads distinguish Booleans from JSON numbers and compare byte values exactly."""

import pytest

import heavenbase as hb
from heavenbase.execution.contracts import RowOp

pytestmark = pytest.mark.fast


@pytest.mark.slow
@pytest.mark.parametrize(("boolean", "number"), [(True, 1), (False, 0), (True, 1.0), (False, 0.0)])
def test_strict_reads_reject_boolean_number_replica_conflicts(boolean, number):
    class Doc(hb.Entity):
        identifier = "boolean-replica-doc"

        payload = hb.field(hb.Json).store(to="main").store(to="replica")

    ws = hb.HeavenBase("boolean-replica", detached=True, backends={"main": {"type": "inmem"}, "replica": {"type": "inmem"}})
    try:
        ws.register(Doc, catalog="none")
        ws.upsert(Doc, {"object_id": "a", "payload": {"flag": [boolean]}})
        assert ws.get("a", entity=Doc)["payload"]["flag"][0] is boolean
        dtype = Doc.schema().get("payload").dtype
        ws.backends.get("replica").upsert([RowOp(Doc.identifier, "a", {"payload": dtype.encode({"flag": [number]})})])
        with pytest.raises(ValueError, match="not unique across routed backends"):
            ws.get("a", entity=Doc)
        assert ws.get("a", entity=Doc, unique=False)["payload"]["flag"][0] is boolean
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize(("stored", "replica"), [(b"42\n", b"42"), (b"19.99", b"19.990000001"), (b"1e3", b"1000")])
def test_strict_reads_compare_byte_replicas_exactly_even_when_they_look_like_numbers(stored, replica):
    class Attachment(hb.Entity):
        identifier = "bytes-replica-attachment"

        body = hb.field(hb.Artifact).store(to="main").store(to="replica")

    ws = hb.HeavenBase("bytes-replica", detached=True, backends={"main": {"type": "inmem"}, "replica": {"type": "inmem"}})
    try:
        ws.register(Attachment, catalog="none")
        ws.upsert(Attachment, {"object_id": "count", "body": stored})
        assert ws.get("count", entity=Attachment)["body"] == stored
        dtype = Attachment.schema().get("body").dtype
        ws.backends.get("replica").upsert([RowOp(Attachment.identifier, "count", {"body": dtype.encode(replica)})])
        with pytest.raises(ValueError, match="not unique across routed backends"):
            ws.get("count", entity=Attachment)
        assert ws.get("count", entity=Attachment, unique=False)["body"] == stored
    finally:
        ws.release()


@pytest.mark.parametrize(("stored", "replica"), [(19.99, 19.990000001), (3, 3.0000001)])
def test_strict_reads_keep_tolerating_float_noise_between_number_replicas(stored, replica):
    class Reading(hb.Entity):
        identifier = "number-replica-reading"

        value = hb.field(hb.Json).store(to="main").store(to="replica")

    ws = hb.HeavenBase("number-replica", detached=True, backends={"main": {"type": "inmem"}, "replica": {"type": "inmem"}})
    try:
        ws.register(Reading, catalog="none")
        ws.upsert(Reading, {"object_id": "sensor", "value": stored})
        dtype = Reading.schema().get("value").dtype
        ws.backends.get("replica").upsert([RowOp(Reading.identifier, "sensor", {"value": dtype.encode(replica)})])
        assert ws.get("sensor", entity=Reading)["value"] == stored
    finally:
        ws.release()
