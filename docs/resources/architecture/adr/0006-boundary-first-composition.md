# ADR 0006 - Boundary-First Workspace Composition

Status: accepted

Date: 2026-06-27

## Context

HeavenBase needs a durable architecture rule that keeps the runtime easy to explain and cheap to extend as provider, storage, toolkit, and extension support grows. The primary design criteria are architectural clarity, long-term extensibility, and composability.

The codebase already has several independent variation axes: logical types, storage strategies, backend providers, handler compilers, storage profiles, MCP profiles, serializers, toolkit families, and optional entity extensions. Without one explicit architectural rule, those axes can drift into central provider branches, parallel facade packages, or hidden metadata paths that are difficult for agents and new developers to inspect.

## Decision

Treat the `HeavenBase` workspace as the orchestration boundary:

- Users start from `import heavenbase as hb`, define `Entity` schemas, register them in a workspace, and use workspace verbs for CRUD, query, discovery, search, and MCP exposure.
- Variation is added by registering pieces: logical types, strategies, operations, backends, handlers, storage profiles, extensions, MCP profiles, toolkit families, serializers, and interop inferers.
- Backends execute `QueryFragment`s and must not parse user `QuerySpec` objects.
- Handlers compile operations and must not perform IO.
- Storage strategies describe placement/index semantics and must not hold provider clients or execute work.
- Extensions are entity bundles with enable hooks and optional APIs/tools/profiles; they must not introduce a second planner, storage runtime, or config system.
- Catalog, MetaSchema, capability, dialect, profile, toolkit-family, serializer, and storage-profile metadata should be queryable as system rows when it affects agent behavior.
- `query.explain()` is the runtime truth for backend selection, native execution, scan fallback, vector filter mode, and unsupported reasons.

## Consequences

### Positive

- New providers and capabilities can be added as small registered pieces rather than central rewrites.
- A new developer can explain the runtime as `schema -> placement -> handler -> backend -> frame`, all coordinated by a workspace.
- Agent-facing discovery is source-backed because important metadata is stored as queryable rows.
- Focused tests and review guidance make the architecture easier to preserve across future refactors.
- The root facade stays friendly for demos while `hb.ext` and role-specific modules remain available for extension authors.

### Negative

- The system has more named pieces than a single-database ORM or a one-provider SDK.
- Contributors must learn where a change belongs before editing routing or provider code.
- Some docs must intentionally link back to the mental model to avoid repeating registry rules in every guide.

### Neutral

- A future materialized `QueryPlan` may still be added, but only if routing becomes independently testable and outgrows the workspace router.
- Row-data export, lineage, automation, multi-agent orchestration, and federation remain deferred until concrete use cases justify them.

## Alternatives Considered

### Central planner with provider branches

Rejected. It would make new providers easy at first but would turn every backend, operation, and strategy addition into planner edits.

### Separate simple API package

Rejected. A second package or facade would create parallel mental models. The root `heavenbase as hb` facade is already the simple entry point; docs tier the surface instead.

### Backend-owned query parsing

Rejected. Backends can optimize fragment execution, but parsing `QuerySpec` inside each provider would duplicate routing policy and make cross-backend behavior inconsistent.

### Extension-specific runtimes

Rejected. Optional domains such as memory, database, agent, lineage, and automation should contribute entity rows, APIs, tools, and profiles through the workspace rather than becoming separate control planes.

## References

- [Mental model](../mental-model.md)
- [Architecture design philosophy](../design-philosophy.md)
- [Extension layout](../extension-layout.md)
- [Backend strategy handlers](../backend-strategy-handlers.md)
