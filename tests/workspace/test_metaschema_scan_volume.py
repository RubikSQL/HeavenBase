import heavenbase as hb
import pytest

from heavenbase.backends.base import Backend
from heavenbase.backends.inmem.backend import InMemBackend

pytestmark = pytest.mark.fast


class ScanOnlyBackend(InMemBackend):
    get_many = Backend.get_many

    def __init__(self):
        super().__init__("main")
        self.metaschema_scan_rows = 0

    def tag(self, name):
        if name == "row-lookup":
            return None
        return super().tag(name)

    def scan(self, entity_id):
        rows = super().scan(entity_id)
        if entity_id == "sys-metaschema":
            self.metaschema_scan_rows += len(rows)
        return rows


def registration_scan_cost(count):
    backend = ScanOnlyBackend()
    workspace = hb.HeavenBase(f"r9ws-scan-{count}", detached=True, backends={"main": backend})
    try:
        backend.metaschema_scan_rows = 0
        for number in range(count):
            identifier = f"r9ws-scan-note-{number}"
            workspace.register({"entity_id": identifier, "fields": {"name": {"type": "short-text"}}})
            assert identifier in workspace.entities
        return backend.metaschema_scan_rows
    finally:
        workspace.release()


@pytest.mark.slow
def test_metaschema_scan_volume_scales_with_registrations():
    small, medium, large = (registration_scan_cost(count) for count in (10, 20, 40))
    assert large - medium <= 2 * (medium - small), (small, medium, large)
