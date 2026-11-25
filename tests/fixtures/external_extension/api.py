"""Workspace API contributed by the external extension fixture."""

from __future__ import annotations

from heavenbase.utils import Any


class ExternalParityAPI:
    """External note operations bound to one workspace.

    Args:
        workspace: Workspace receiving the external Extension.
        resolver: Context-owned module resolver supplied by activation.
    """

    def __init__(self, workspace: Any, *, resolver: Any = None) -> None:
        """Bind the API to its owning workspace and resolver.

        Args:
            workspace (Any): Workspace receiving the external Extension.
            resolver (Any): Context-owned module resolver. Defaults to ``None``.

        Returns:
            None: This initializer stores the owners.
        """
        self.workspace = workspace
        self.resolver = resolver

    def add(self, name: str) -> str:
        """Insert one external note.

        Args:
            name (str): Human-readable note name.

        Returns:
            str: Stored note identifier.
        """
        note = self.workspace.entities["external-parity-note"]
        return self.workspace.upsert(note, {"object_id": f"note-{name.lower()}", "name": name})
