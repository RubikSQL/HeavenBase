# Agent Command Policy

## RTK — Token-Optimized CLI

**rtk** is a CLI proxy that filters and compresses command outputs, saving 60-90% tokens.

Always prefix shell commands with `rtk` when the session provides it:

```bash
# Instead of:              Use:
git status                 rtk git status
git log -10                rtk git log -10
bash scripts/test.bash     rtk bash scripts/test.bash
uv sync --all-extras       rtk uv sync --all-extras
```

Meta commands (use directly):

```bash
rtk gain              # Token savings dashboard
rtk gain --history    # Per-command savings history
rtk discover          # Find missed rtk opportunities
rtk proxy <cmd>       # Run raw (no filtering) but track usage
```

## UV and repo wrappers

HeavenBase is `uv`-first. Read `AGENTS.md` first, then use repo Bash wrappers instead of bare `pytest`, `black`, `flake8`, or `python -m ...` when wrappers exist.

```bash
rtk bash scripts/sync-env.bash
rtk bash scripts/flake.bash --ci
rtk bash scripts/test.bash
```

Wrappers source `scripts/_env.bash` and prefer `uv run` over bare `python`. Full agent command policy lives in the globally installed `heaven-style` skill.
