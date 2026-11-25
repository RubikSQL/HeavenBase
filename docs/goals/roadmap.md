# Roadmap

This page records durable ordering pressure, not task status. The current/target/gap baseline lives in [`current.md`](current.md); active resumable work lives only in [`../tasks.yaml`](../tasks.yaml); architectural constraints apply from the [mental model](../resources/architecture/mental-model.md).

## Current Baseline

The `0.1.2.3` train builds on the published `0.1.2.2` baseline. ADR 0011 and ADR 0012 remain the implemented Registry/module-folder baseline; package ownership, value-snapshot safety, and Context authority are converged on that single path. Publication remains separately authorized.

## Short Term

- Preserve the implemented [ADR 0011](../resources/architecture/adr/0011-standalone-registry-and-lego-resolution.md) boundary: one Registry authority, one control-Backend bootstrap, import-free inspection, and no built-in fallback path.
- Design remote artifact acquisition, signature verification, and ecosystem trust policy as a separate accepted target before enabling network-backed installation. Preserve the implemented immutable content-addressed cache for local root-package sources.
- Keep focused behavioral tests and review guidance aligned with every new mandatory mental-model rule.
- Keep GraphEdge conformance green across portable, embedded, and live provider tiers without backend-specific dangling semantics.
- Keep aggregation/HAVING, Json dotted paths, and text-proof behavior registry-driven, parity-tested, and visible through capabilities and `explain()`.
- Improve generated-evidence freshness and fast pull-request feedback without restoring a broad repository-shaped architecture checker.
- Deepen provider-native SQL/vector/search execution only behind operation-specific proof and honest fallback diagnostics.
- Preserve the full-compliance
  [dashboard baseline](../resources/architecture/dashboard.md): one
  `src/heavenbase/api/` service and generated protocol, thin CLI/host consumers,
  external `apps/gui/` presentation, fully strict zero-bundler TypeScript,
  WebView-first local launch, installable macOS `.app`, and tested
  browser/Chromium fallbacks.

## Mid Term

- Improve multi-placement binding selection beyond the first effective binding once Registry resolution has one authority.
- Extend data-first inspection only when new module families have a concrete consumer contract; do not create a second catalog or generic component schema.
- Add row-data snapshot/export only for a concrete portable-bundle requirement.
- Add a pluggable merge policy for cross-backend results when the current frame operation is insufficient.
- Converge callable serialization on Capsule through the accepted
  [Registry-module layer](../resources/architecture/capsule-toolkit.md#future-callable-serialization-convergence):
  make exact installed-module restoration the first eligible layer, preserve
  explicit source/import fallbacks, and migrate callable-owning domains without
  implicit Capsule persistence or a second Registry authority.
- Promote lineage or automation ADRs only after concrete database-extension demand establishes their runtime contract.
- Replace the API-reference stub with generated, source-backed public API documentation.
- Sign/notarize the generated dashboard macOS `.app` or add Windows packaging
  only as separately accepted platform-delivery slices.
- Keep HeavenBase package and dependency compatibility deliberate at release boundaries; consume `heaven-style` through its separately maintained global installation rather than a repository synchronization contract.

## Long Term

- Make agentic components, prompts, schemas, toolkits, configuration, and extension metadata increasingly inspectable and manageable as HeavenBase data.
- Implement the deferred [memory-first LLM session persistence boundary](../resources/architecture/llm-session-persistence.md) through a future LLM-related extension: explicit workspace persistence by default and automatic recording only after deliberate enablement.
- Add multi-agent orchestration only on top of a stable agent-extension contract.
- Materialize a `QueryPlan` module only if planning becomes independently testable and routing outgrows the workspace engine.
- Replace the exclusive-write side of the workspace reader-writer lock with a runtime snapshot model if measured parallel-write demand justifies it.
- Harden file backends and JSON-defined entities for untrusted inputs.
- Consider workspace federation or cross-machine sync only for a demonstrated use case.

## Standing Non-Goals

- No speculative second planner.
- No central provider-routing branches.
- No replaceable Extension ownership for fixed Catalog or MetaSchema, and no
  kitchen-sink system bundle combining fixed, required, and optional domains.
- No permanent compatibility shims or parallel Registry authorities.
- No over-engineered federation, orchestration, lineage, or automation without concrete demand.
