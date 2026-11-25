# Goals

This folder owns HeavenBase outcomes and roadmap pressure. It does not own task state.

- [`current.md`](current.md) distinguishes the implemented source baseline, accepted targets, observable gaps, operating priorities, and current non-goals.
- [`roadmap.md`](roadmap.md) records durable ordering across short, mid, and long horizons.

Active resumable work belongs only in [`../tasks.yaml`](../tasks.yaml). A goal or roadmap item may link to a task or ADR, but it must not duplicate owner, status, dependency, or checklist state.

Update goals when shipped behavior or an accepted decision changes the desired outcome. Record completed implementation evidence concisely in [`../DEVLOG.md`](../DEVLOG.md), then rely on Git for older detail.
