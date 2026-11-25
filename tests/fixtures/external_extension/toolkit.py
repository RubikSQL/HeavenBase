"""Workspace Toolkit family contributed by the external extension fixture."""

from __future__ import annotations

from typing import TYPE_CHECKING

from heavenbase.toolkit.errors import raise_tool_error
from heavenbase.utils import Any

if TYPE_CHECKING:
    from heavenbase.toolkit import WorkspaceToolContext


def external_tool_functions(context: WorkspaceToolContext) -> dict[str, Any]:
    """Build profile-aware tools for one external workspace.

    Args:
        context (WorkspaceToolContext): Active workspace, profile, and tool
            authority.

    Returns:
        dict[str, Any]: External tool functions keyed by MCP name.
    """
    note = context.require_entity("external-parity-note")

    def list_external_notes() -> list[dict[str, Any]]:
        """Return external notes visible in the owning workspace."""
        try:
            return context.workspace.query(note).execute().rows()
        except Exception as exc:
            raise_tool_error(exc)

    return {"external_list_notes": list_external_notes}
