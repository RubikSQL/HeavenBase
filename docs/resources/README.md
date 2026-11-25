# Engineering Resources

This folder contains durable architecture, public/operational reference, and current evidence.

- [`architecture/`](architecture/) owns the mental model, boundary contracts, and ADRs. Every forward-looking design must distinguish current behavior, accepted target, remaining gap, and non-goal.
- [`reference/`](reference/) describes current public and operational contracts.
- [`reports/`](reports/README.md) contains generated or deliberately refreshed evidence that remains relevant to the current source surface.

Resources do not own active task state or temporary analysis. Use [`../tasks.yaml`](../tasks.yaml) for resumable work, [`../DEVLOG.md`](../DEVLOG.md) for concise handoff evidence, and [`../scratch/`](../scratch/README.md) for expiring notes. Promote durable conclusions here and delete the superseded source in the same change.
