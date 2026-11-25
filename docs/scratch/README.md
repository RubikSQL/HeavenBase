# Scratch Notes

Use this folder only for short-lived requirements, brainstorms, rough comparisons, or design questions that another maintainer may need before a decision is accepted.

Pure local slop belongs in ignored `.temp/notes/` or `docs/scratch/local/`. A tracked Markdown note must start with:

```yaml
---
status: scratch
created: 2026-07-21
expires: 2026-08-20
task: PROJECT-001
---
```

The expiry may be at most 45 days after creation. Before it expires, either promote the durable conclusion into user or engineering documentation and delete the note, or delete it without promotion. Scratch is never an authority source.

## Current Contents

One architecture subject remains deferred:

- [LLM session persistence](../resources/architecture/llm-session-persistence.md)
  is an accepted but explicitly deferred boundary for a future LLM-related
  extension; the dashboard assistant session log resolves the authority
  question for its own domain (see that page's Partial Resolution).

The RAW modularization-requirements and SDK public-API scratch notes reached
their recorded expiry (2026-08-22) and were deleted per the rule above; their
shipped conclusions were already reconciled into the current mental model,
public reference, and architecture contracts. Workspace-owned Toolkit
persistence is implemented and recorded in
[ADR 0014](../resources/architecture/adr/0014-workspace-owned-toolkit-persistence.md).
