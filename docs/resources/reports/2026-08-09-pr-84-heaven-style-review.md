# PR #84 merge-readiness review

## Scope

- Pull request: #84, `agent/fix-catalog-field-collision`
- Comparison base: current `origin/master` (`5d59408a`)
- Reviewed: Catalog projection behavior, tests, user/reference documentation, current CI and merge state

## Resolved findings

- [x] **P1 — integration:** The branch was based on `b103211f` and conflicted with current `master` in `docs/DEVLOG.md`. Current `master` was merged and both durable log entries were preserved.
- [x] **P1 — CI contract:** Enabling the Agent extension by default on `master` left nine tests asserting the former opt-in default. Expectations now cover the configured default, while tests that specifically require an Agent-free workspace use a scoped fixture.
- [x] **P2 — test coverage:** The projection test covered different logical type identifiers but not different parameters of the same type. A bounded-text case now proves that the complete durable descriptor must match.
- [x] **P2 — test isolation:** The dashboard invalid-model contract left its
  process-global assistant selection behind. The test now restores both config
  keys even when an assertion fails.
- [x] **P2 — documentation:** A native `name` with an incompatible descriptor
  can supply fallback identity without projecting into Catalog. The data-flow,
  JSON-authoring, and identifier references now state that distinction.

## Verification

- Fifteen focused Catalog, inherited default-Agent, and assistant-isolation
  regressions passed.
- Fast suite: 905 passed, one environment skip.
- Black, Flake8, MyPy, documentation-contract, environment/README-sync, and generated-metadata gates passed.

## Residual risk

No unresolved review finding remains. External backend behavior is unchanged; the full external-service suite is outside this merge gate.
