# External Extension Tutorial

Use this path when an application ships a cohesive Entity, Extension API,
Toolkit family, MCP profile, and Skill outside the HeavenBase package.
Built-in and external modules use the same folder protocol. Installation
captures the complete folder into one verified, content-addressed artifact
before publishing its Registry records.

Python package dependencies remain ordinary wheel metadata and must already be
installed in the environment. `ModuleService.install(...)` installs Registry
contributions; it does not invoke a package manager.

## 1. Create one cohesive folder

```text
acme_notes/
  __init__.py
  api.py
  entities.py
  toolkit.py
  meta.yaml
  skill/
    SKILL.md
```

`entities.py` owns the Entity:

```python
from heavenbase import Entity, ShortText, field


class AcmeNote(Entity):
    identifier = "acme-note"

    name = field(ShortText)
```

`api.py` owns the workspace-bound API. Activation supplies both the workspace
and its Context-owned resolver:

```python
from heavenbase.utils import Any


class AcmeNotesAPI:
    def __init__(self, workspace: Any, *, resolver: Any = None) -> None:
        self.workspace = workspace
        self.resolver = resolver

    def add(self, name: str) -> str:
        Note = self.workspace.entities["acme-note"]
        return self.workspace.upsert(
            Note,
            {"object_id": f"note-{name.lower()}", "name": name},
        )
```

`toolkit.py` receives the public builder-context contract. Keep the dynamic
facade import type-only inside artifact code so an explicit non-default
`Context` remains the sole runtime authority. Tool failures can use the
supported owner-local sanitizer:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from heavenbase.toolkit.errors import raise_tool_error
from heavenbase.utils import Any

if TYPE_CHECKING:
    from heavenbase.toolkit import WorkspaceToolContext


def acme_tool_functions(context: WorkspaceToolContext) -> dict[str, Any]:
    Note = context.require_entity("acme-note")

    def list_acme_notes() -> list[dict[str, Any]]:
        try:
            return context.workspace.query(Note).execute().rows()
        except Exception as exc:
            raise_tool_error(exc)

    return {"acme_list_notes": list_acme_notes}
```

`raise_tool_error(...)` converts an implementation exception to a redacted
`RuntimeError` without exposing an exception chain. Keep domain-specific stable
error codes in the application payload when clients need machine-readable
failure handling.

The Skill is an ordinary directory-backed Skill:

```markdown
---
name: acme-notes
description: Work with Acme notes.
---

# Acme notes

Use `acme_list_notes` to inspect notes in this workspace.
```

## 2. Declare the complete Registry bundle

Path targets are relative to the module folder. Exact dependencies make install
validation and activation failures deterministic.

```yaml
manifest_version: 2
coordinate: acme/notes
version: 1.0.0
compatibility: {}
bundle: acme-notes
items:
  - kind: entity
    identifier: acme-note
    name: AcmeNote
    desc: Example external note.
    source: path
    target: {module: entities, qualname: AcmeNote}
    active: true
    meta: {schema_version: 1}

  - kind: extension
    identifier: acme-notes
    groups: {tag: [external]}
    name: Acme Notes
    desc: Example external Extension.
    source: inline
    target: definition
    active: true
    meta:
      schema_version: 1
      dependencies: [entity:acme-note]
      definition:
        identifier: acme-notes
        name: Acme Notes
        version: 1.0.0
        desc: Example external Extension.
        required: false
        requires: [agent, toolkit]
        tags: {external: true}
        entities: [acme-note]
        meta: {}
        setup: null
        api:
          kind: heavenbase.executable
          version: 1
          source: path
          target: {module: api, qualname: AcmeNotesAPI}
        api_name: acme_notes

  - kind: toolkit_family
    identifier: acme-notes
    name: Acme Notes
    desc: Acme note tools.
    source: inline
    target: definition
    active: true
    meta:
      schema_version: 1
      tags: {external: true, mcp: true}
      dependencies: [extension:acme-notes]
      workspace_requires: [acme-notes, toolkit]
      definition:
        tool_names: [acme_list_notes]
        builder:
          kind: heavenbase.executable
          version: 1
          source: path
          target: {module: toolkit, qualname: acme_tool_functions}
        meta:
          builder_context: workspace_tool_context
          tool_descriptions:
            acme_list_notes: Return notes owned by the Acme Extension.

  - kind: mcp_profile
    identifier: acme-notes
    name: Acme Notes
    desc: Acme note and Skill read surface.
    source: inline
    target: definition
    active: true
    meta:
      schema_version: 1
      tags: {external: true, mcp: true}
      dependencies:
        - serializer:jstr
        - toolkit_family:acme-notes
        - toolkit_family:agent
        - entity:acme-note
      workspace_requires: [acme-notes, agent, toolkit]
      definition:
        extends: []
        tools: [acme_list_notes, read_skill]
        entities: [acme-note]
        skills: [acme-notes]
        serializer: jstr
        desc: Acme note and Skill read surface.
        meta: {}
```

## 3. Install, inspect, activate, and serve

Use one explicit `Context` for installation and workspace construction. An
application normally performs module setup once at its installation/setup
boundary.

```python
from pathlib import Path

import heavenbase as hb


context = hb.Context.load()
modules = context.modules()
modules.setup()
receipt = modules.install(Path("acme_notes"))
workspace = None

try:
    # Inspection is inert: it does not import entities.py, api.py, or toolkit.py.
    record = modules.inspect("extension", "acme-notes")[0]
    assert record["record"]["source"] == "inline"

    workspace = hb.HeavenBase("acme-extension-smoke", context=context)
    workspace.enable_extension("acme-notes")
    workspace.acme_notes.add("Installed")

    skill_id = hb.Skill.from_path(
        "acme_notes/skill",
        ws=workspace,
    ).register(ws=workspace)
    assert skill_id

    toolkit = workspace.to_mcp(profile="acme-notes")
    assert toolkit.run("acme_list_notes")[0]["name"] == "Installed"

    # Blocks for stdio by default. The workspace lock is released before the
    # server wait, so workspace-backed tools remain callable.
    workspace.serve(profile="acme-notes", transport="stdio")
finally:
    if workspace is not None:
        workspace.drop()
    modules.uninstall(receipt)
    context.close()
```

A long-lived application keeps the installation and enables `acme-notes` on
each intended workspace. The uninstall above is smoke-test cleanup. A separate
process must load the same persisted machine bootstrap and workspace before
calling `serve(...)`; process-local or in-memory Context state cannot cross a
stdio boundary.

## 4. Respect artifact identity and resources

Artifact modules load under a private namespace derived from their coordinate,
installation generation, and content digest. The restored `AcmeNote` class is
therefore local to one `ModuleResolver` generation:

- resolve the class through `workspace.entities["acme-note"]`;
- use the Registry identifier and schema as durable identity;
- do not compare cross-Context classes with `is` or ambient `isinstance`;
- reinstall the folder after editing source.

Use `importlib.resources` for non-Python files captured beside the module:

```python
from importlib.resources import files


template = files(__package__).joinpath("templates", "prompt.txt").read_text(
    encoding="utf-8"
)
```

The artifact resource reader serves verified, read-only bytes. Do not use
`Path(__file__).resolve()` or assume the virtual
`heavenbase-artifact://...` source location is a local filesystem path.

## 5. Production checklist

- Use stable coordinates, identifiers, versions, and compatibility bounds.
- Declare every required Registry record in `dependencies` and every activation
  prerequisite in `workspace_requires` or Extension `requires`.
- Keep imports inside the captured folder or use stable HeavenBase owner APIs.
- Test install, inert inspection, enablement, API calls, Skill registration,
  direct Toolkit calls, FastMCP calls, fresh-process stdio, uninstall, and
  Context restore.
- Treat installed executable source as trusted code and review it before
  activation.

The repository parity fixture is
`tests/registry/test_external_extension.py`. For the full ownership protocol,
see [ADR 0012](../architecture/adr/0012-unified-module-folder-protocol.md),
[Package and Extension Layout](../architecture/extension-layout.md), and
[Workspace Foundation and Extensions](../architecture/workspace-foundation-and-extensions.md).
