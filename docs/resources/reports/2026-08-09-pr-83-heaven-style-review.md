# PR #83 merge-readiness review

## Scope

- Pull request: #83, `agent/fix-agent-canonical-restoration`
- Comparison base: current `origin/master` (`5d59408a`)
- Reviewed: callable identity and persistence boundaries, schema alteration
  validation, cross-Context restoration tests, engineering documentation,
  current CI, review, draft, and merge state

## Resolved findings

- [x] **P1 — integration:** The branch was based on `b103211f` and conflicted
  with current `master` in `docs/DEVLOG.md`. Current `master` was merged
  and both newest-first durable log entries were preserved.
- [x] **P1 — CI contract:** Current `master` enables Agent by default, but
  inherited tests still asserted the former opt-in behavior. Expectations now
  cover the configured default; tests that specifically require an Agent-free
  workspace use a scoped fixture.
- [x] **P2 — test isolation:** The dashboard's invalid-model test left its
  process-global assistant selection behind, making later stream tests
  order-dependent under xdist. The test now restores both config keys in a
  `finally` block.

## Verification

- Fourteen Agent-extension contracts passed.
- Twelve inherited default-Agent and assistant-isolation regressions passed.
- Two focused structural-schema and alteration guards passed.
- Fast suite: 905 passed, one environment skip.
- Black, Flake8, MyPy, documentation-contract, environment/README-sync, and
  generated-metadata gates passed.

## Residual risk

No unresolved review finding remains. Source inspection can be unavailable for
some ambient callables; that path intentionally retains the conservative
module-qualified structural identity and is documented as such.
