# ADR 0015 - Rejected: caller-authored code execution as a query surface

Status: superseded in part (see Revision)

Date: 2026-07-30

## Context

A dashboard assistant was asked to total the values of a `Json` dict field
(`tool_calls`, one key per tool, keys varying per row) across ~1,900 rows. The
aggregate stage rejected dotted Json paths as metric sources and the dashboard
clamped `limit` to 100, so the model paged rows into its own context, attempted
to sum dictionaries in prose, and finally reported fabricated totals.

Two mitigations were proposed alongside the engine fix in ADR 0009:

1. Ship a Python image with `heavenbase` preinstalled, run it through the
   `llm-sandbox` package, and give the model a tool that generates Python code
   instead of a JSON query spec. A remote `llm-sandbox` + Daytona backend was
   suggested as the deployment target.
2. Pass the model's generated Python to `eval()`/`exec()` in the host process.

## Decision

Both are rejected. The engine gains dynamic Json sources instead (ADR 0009):
dotted-path metric sources and the `explode` clause answer the motivating
question in one declarative call, for every caller, with no new runtime.

**In-process `eval()`/`exec()` of caller-authored code is rejected permanently.**
CPython offers no in-process boundary: object-graph traversal
(`().__class__.__base__.__subclasses__()`, `function.__globals__`,
`traceback.tb_frame`) reaches the interpreter regardless of a cleared
`__builtins__`, and a C-level loop cannot be interrupted by a Python watchdog.
RestrictedPython's own documentation declines the sandbox claim. This repository
already refuses to call its read-only SQL checker a sandbox, and every existing
`exec` site is gated on provenance — content-addressed SHA-256 module artifacts
or explicitly trusted local code. Model-authored strings satisfy neither.

**Container-sandboxed code execution is rejected for this problem**, on
architecture rather than on the isolation mechanism, which is real:

- It inverts the boundary. For a sandboxed workspace handle to read the same
  data the dashboard reads, the machine's control plane must be mounted into it
  writable: `system.db` carries credential-bearing plaintext payloads, and the
  module artifact tree is imported at workspace-open time. The sandbox would
  have to contain what the sandbox exists to protect.
- It voids an existing control. Assistant credential masking is a property of
  one wrapped function; `workspace.context` is public, so generated code reads
  the config store unmasked and can persist changes.
- It reads nothing on the default preset, whose backends are in-process
  (`:memory:` SQL plus `inmem` vector/search). A sandbox would open an empty
  workspace and report zeros — a silent wrong answer replacing a loud failure.
- It removes auditability. The `{entity, spec}` argument shape is what the
  dashboard renders beside each result table, and it is how a reader can see
  that no aggregation was requested. Generated code that *looks* computed
  launders fabrication rather than preventing it.
- The declarative surface is also the cheaper contract: a model-facing Python
  API reference is roughly an order of magnitude larger than the query-spec
  docstring, and it enlarges the space of method names a model can hallucinate.
  A JSON spec validated by the engine fails loudly and recoverably; a wrong
  Python program returns a plausible number with no signal.

The `llm-sandbox`/Daytona composition named in the proposal does not exist:
`llm-sandbox` supports Docker, Kubernetes, Podman, and Micromamba, and Daytona
ships its own SDK. Any future evaluation must treat them as alternatives, not
layers.

## Consequences

- Aggregation vocabulary grows through `register_aggregate_reducer` — a
  maintainer action that extends both surfaces from one registration and is
  never reachable from a read-only agent surface.
- Reopening code execution requires measured evidence: a class of question that
  survives ADR 0009's dynamic Json sources and cannot be closed by one reducer
  registration or one computed field. If reopened, it is scoped as an opt-in
  developer capability, not a dashboard assistant tool, and at minimum must
  never mount the HeavenBase root, must reach data through a host-side
  read-only proxy that holds no credentials, must default to no network egress,
  and must show generated code to the user before running it.

## Revision — 2026-07-31

Code execution was reopened by explicit decision, without the measured evidence
this ADR asked for and with its security argument set aside. The `python`
extension executes model-authored Python in the dashboard process, so the
consequences above stand unmitigated: no proxy, no container, no egress policy,
no pre-execution review. The `eval()` finding is unchanged and was demonstrated
again on this interpreter — from `eval` with `__builtins__` cleared, the object
graph yields `__import__`, the real uid, and the environment.

What survives from the analysis and is implemented: the declarative `query`
tool remains the recommended path, because a spec is validated and a program is
not; a frame returned from Python keeps its `total`/`truncated` coverage, so a
program cannot hand back a page that reads as a whole; and the dashboard
assistant gets the tools only under
`heavenbase.dashboard.assistant.run_python`, which is off by default.

## Revision — 2026-08-03

A second execution path was added after the revision above and is not covered by
it. The `code_reduce` aggregate operator takes an expression as an ordinary
metric option and evaluates it in the query process with real builtins, so
`__import__` is reachable exactly as the rejection describes. Two claims above
are therefore false as written and are recorded here rather than edited away:

- "In-process `eval()`/`exec()` of caller-authored code is rejected permanently"
  no longer holds.
- "never reachable from a read-only agent surface" no longer holds either: the
  MCP `query` tool forwards a caller's spec unchanged, so any profile exposing
  `query` exposes `code_reduce`.

It also differs from the `python` extension in the one place that matters for an
operator: `heavenbase.dashboard.assistant.run_python` is off by default, while
`heavenbase.query.allow_code_reduce` is **on**. Turning the latter off is not a
free choice either — `column_stats` folds every column through it, so refusing
code execution engine-wide also disables column statistics. That coupling is a
consequence worth removing (registering the summary folds as named reducers
would end it), not a property to rely on.

The factual finding that `llm-sandbox` has no Daytona backend also stands, and
`llm-sandbox` was not adopted — in-process execution has no use for it.

This section exists so the reasoning and the decision to override it are both
on the record. Anyone reconsidering should read the rejection above first.
