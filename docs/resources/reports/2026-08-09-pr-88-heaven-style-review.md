# PR #88 merge-readiness review

## Scope

- Pull request: #88, `fix/soft-vector-replica-merge`
- Integration predecessor: prepared PR #87 (`e0454b5d`), including prepared PR #84
- Reviewed: routed row authority, vector-replica precedence, null semantics, conflict preservation, tests, CI, and merge state

## Resolved findings

- [x] **P1 — null authority:** An authoritative inline null was treated as a gap and could be replaced by a stale vector value. Merge state now distinguishes an authoritative null from an absent field.
- [x] **P1 — query hydration:** Near-query hydration reapplied its vector result frame after routed-row merging and could therefore overwrite the authoritative null the merge had preserved. Hydration now carries field authority through the merge and only fills a null when no authoritative fragment supplied it.
- [x] **P1 — conflict safety:** Every vector route was marked soft, including vector-only fields with no non-vector source of truth. Only a vector route accompanied by a non-vector binding is now a replica; disagreement between vector-only owners remains a conflict.
- [x] **P2 — architecture:** Replica detection swallowed malformed or missing storage-plan state and compared a raw strategy string. It now relies on the registered `VectorIndex` identifier and the workspace invariant that a registered schema has a storage plan.
- [x] **P2 — typing and coverage:** Merge bookkeeping and replica pairs now carry concrete types. Tests cover both fragment orders, authoritative nulls, vector-only reads, vector-only disagreements, and unchanged non-vector conflicts.

## Verification

- Direct vector-replica and hydration regressions: 8 passed.
- Placement, workspace, query, and core changed-surface suite: 496 passed; seven external-service cases were skipped by explicit policy.
- Repository-wide fast suite: 940 passed, one environment skip.
- Full opt-in suite: 1,153 passed; 60 external-service cases were skipped by the wrapper's explicit environment policy.
- Black, Flake8, MyPy, Heaven changed-source import scanning, documentation-contract, environment/README-sync, and generated-metadata gates passed. The cumulative dashboard gate passed on the prepared PR #87 predecessor.

## Residual risk

No unresolved review finding remains. Provider-specific vector reads requiring external services are outside this local merge gate; the merge policy is covered at the decoded row-fragment boundary shared by providers.
