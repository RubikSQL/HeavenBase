# PR #87 merge-readiness review

## Scope

- Pull request: #87, `feat/exact-json-aggregation`
- Integration predecessor: prepared PR #84 (`2627e103`)
- Reviewed: exact aggregate coverage, result paging, typed custom reducers, dashboard/API presentation, Python-tool output limits, documentation, CI, and merge state

## Resolved findings

- [x] **P1 — result contract:** Limiting an already limited `ResultFrame` replaced the complete-row total with the current page size. Nested limits now preserve the original total; equality, serialization, and construction enforce the same metadata contract.
- [x] **P1 — type safety:** `code_reduce(dtype=...)` discarded parameterized logical-type settings and its declared result type did not drive typed `HAVING`. Metrics now retain the complete JSON descriptor and the registered reducer resolves that descriptor for predicate normalization.
- [x] **P1 — resource bound:** Python tool output used an unbounded in-memory buffer before truncation. Capture is now bounded while still reporting the complete emitted character count.
- [x] **P1 — context authority:** `code_reduce` consulted the process-global config even for workspaces owned by an isolated Context. Reducer execution now receives the workspace config authority, so a deployment-local refusal is enforced before evaluation.
- [x] **P2 — API validation:** Dashboard query limits silently converted zero and negative values to one and accepted Booleans. Invalid limits now fail at the boundary with typed errors.
- [x] **P2 — presentation accuracy:** Offset pages were labeled as rows starting at one, aggregate notes used the requested rather than returned count, and the UI discarded exactness flags for truncated JSON statistics. Labels now reflect the actual range and surface every truncation flag.
- [x] **P2 — streaming completeness:** Exhausting the executable-tool budget annotated only an internal aggregate for default/text-only streams. Those streams now emit the incomplete verdict as a final text chunk.
- [x] **P2 — documentation:** Stale claims that column statistics depend on caller-code permission and that keys are never display-capped were corrected.

## Verification

- Final affected-surface suite after integrating prepared PR #84: 177 passed.
- Repository-wide fast suite: 940 passed, one environment skip.
- Full opt-in suite: 1,145 passed; 60 external-service cases were skipped by the wrapper's explicit environment policy.
- Dashboard OpenAPI generation, Biome formatting and lint, TypeScript typecheck, Bun tests, generated-JavaScript drift check, and production build passed.
- Both prior inline review findings are outdated on the current diff and are covered by the workspace-Context and default-text-stream regressions.
- Black, Flake8, MyPy, Heaven changed-source import scanning, documentation-contract, environment/README-sync, OpenAPI, capability-report, and generated-metadata gates passed.

## Residual risk

The branch intentionally enables unrestricted caller-authored `code_reduce` expressions by default, as accepted in ADR 0015. This review preserved that product decision and verified that deployments can refuse it globally; it remains a high-trust deployment capability rather than a sandbox.
