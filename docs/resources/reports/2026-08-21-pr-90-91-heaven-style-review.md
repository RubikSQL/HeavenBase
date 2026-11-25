# PR #90 and #91 merge-readiness review

## Scope

- Pull requests: #90, `dashboard-assist-fixes`; #91,
  `assistant-extensibility`
- Integration order: #90 into `master`, then #91 into the reviewed #90 head
- Reviewed: packaged-module setup, assistant sync/async parity, capture and
  compaction, streaming liveness, Registry contributions, MCP interoperability,
  configuration authority, documentation, and test evidence

## Resolved findings

- [x] **P1 — package-version policy:** Packaged-module setup compared version
  ordering and recognized development suffixes to decide whether the running
  installation should replace existing content. Setup now reconciles the exact
  packaged composition and treats its version as opaque identity; there is no
  package-version-specific branch in that lifecycle.
- [x] **P1 — execution parity:** Result clipping and in-turn compaction differed
  between synchronous and asynchronous tool loops. Both paths now use the same
  explicit character and context policies while generic LLM defaults remain
  neutral.
- [x] **P1 — MCP ownership:** PR #91 introduced a dashboard-specific JSON-RPC
  client and subprocess lifecycle. External tools now use FastMCP's official
  client transports and shared result conversion, exposed as ordinary
  schema-bearing callables; the dashboard owns only configuration, namespacing,
  allowlisting, and its existing tool pipeline.
- [x] **P1 — configuration authority:** The proposed request path mutated saved
  routing configuration and contained a dated provider/model rewrite. Both were
  removed. Serving reads configuration and reports failures without silently
  repairing operator state or special-casing a package/model version.
- [x] **P2 — extension architecture:** Workspace groups and `meta.surface`
  created a second activation axis and a second tool-policy DSL. The retained
  `assistant_tools` contribution is a normal Registry provider gated by existing
  `meta.workspace_requires`; providers compose plain tools with standard schema,
  documentation, annotations, and partial binding.
- [x] **P2 — context and liveness:** Evidence and compaction caches are scoped by
  workspace, question, and route. Background semantic summarization and provider
  probing were removed, and streaming now has server keepalives plus a bounded
  client inactivity timeout.
- [x] **P2 — failure isolation:** Invalid or failing assistant providers and MCP
  servers are skipped without taking down built-in tools. MCP warnings are
  deduplicated per API instance and exact configuration; name collisions remain
  deterministic and favor the earlier tool.

## Verification

- Complete dashboard and LLM tool-session files: 120 passed.
- FastMCP persisted-Toolkit and runtime-callable import regressions: 2 passed.
- Repository fast integrity tier: 981 passed; 10 optional-backend or environment
  cases skipped by explicit policy.
- Black, Flake8, lazy-facade type contracts, documentation contracts,
  environment/README sync, and generated-file drift checks: passed.

## Residual risk

No unresolved review finding remains in the local tree. HTTP MCP interoperability
relies on FastMCP's own transport suite; this review exercises the same adapter
with an actual standards-owned stdio server and an in-process FastMCP server.
