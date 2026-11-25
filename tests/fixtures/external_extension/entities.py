"""Entity contributed by the external extension fixture."""

from heavenbase import Entity, ShortText, field


class ExternalParityNote(Entity):
    """Small note used to prove external Entity restoration."""

    identifier = "external-parity-note"

    name = field(ShortText).desc("External note name")
