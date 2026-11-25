"""Small typed consumer of the installed HeavenBase public facade."""

from __future__ import annotations

import heavenbase as hb


class ConsumerNote(hb.Entity):
    """Entity owned by an application rather than HeavenBase."""

    identifier = "typed-consumer-note"

    name = hb.field(hb.ShortText)


@hb.Tool(name="typed_add")
def typed_add(left: int, right: int) -> int:
    """Add two integers through the public Tool decorator."""
    return left + right


def open_database(database: str) -> hb.Database:
    """Open the public default database preset."""
    return hb.Database(database, preset="default")


def store_note(workspace: hb.HeavenBase, name: str) -> str:
    """Register one external Entity and persist a row."""
    workspace.register(ConsumerNote)
    return str(
        workspace.upsert(
            ConsumerNote,
            {"object_id": f"note-{name}", "name": name},
        )
    )
