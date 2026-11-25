# HeavenBase Release Workflow

HeavenBase uses `.github/workflows/release.yml` for two deliberately separate actions:

1. A push to `release` prepares and tests a candidate. It never publishes.
2. A manual `workflow_dispatch` run rebuilds and smoke-tests the current candidate artifacts, then publishes only after a full release-SHA confirmation and a second authorization check. It does not repeat the source suite already validated before candidate staging.

The workflow uses PyPI trusted publishing with GitHub OIDC. The `pypi` environment remains attached to the publication job as defense in depth, but publication does not rely on environment reviewers: a release-branch push alone cannot reach the PyPI action.

## Enforced Release Invariants

Every preparation and publication run requires all of the following:

- `origin/release` and `origin/master` point to the same commit. This is the exact fast-forward state produced by `scripts/release.bash`.
- The release head commit message contains `[release]`.
- `src/heavenbase/version.py` contains a stable four-part version such as `0.1.2.0`; development suffixes are rejected.
- Poetry metadata and every packaged `meta.yaml` header project that exact version; descriptor compatibility spans from the package version to the next three-part release train.
- A release-branch push still points at the current remote release head. Superseded push runs fail.
- A manual publication supplies the full current release SHA and the exact confirmation text `publish-to-pypi`.

The publication job fetches both remote branches and rechecks the authorized SHA, branch equality, checked-out commit, release marker, and stable version immediately before requesting an OIDC token for upload. If either branch advances while a manual run is testing, publication fails closed.

## Candidate Gates

A release-branch push executes the candidate pipeline:

1. Check generated environment files, the documentation contract, and source quality once on Python 3.12.
2. Install the locked `dev` and `full` extras and run the default `fast and not full` integrity suite on Python 3.10, 3.11, 3.12, and 3.13. `UV_PYTHON` and a runtime assertion bind every job to its advertised matrix interpreter despite the repository `.python-version`.
3. Build exactly one wheel and one source distribution on Ubuntu.
4. Reject extra or missing distribution files, require both filenames to carry the source version, and run `twine check` on the exact two-file inventory.
5. Install and smoke-test the wheel on Linux, macOS, and Windows.
6. Install and smoke-test the source distribution on Linux.
7. Verify the installed package version and `hb` CLI entry point for every artifact job.

A manual publication run skips the source quality and test matrix, then repeats steps 3–7 against the exact authorized SHA before publishing. The broad credential-free `fast or full` suite remains a pre-staging local gate and an explicit single-job **Python Tests** workflow dispatch with a 125-minute timeout (a two-CPU, four-worker emulation of the hosted runner took about 82 minutes); it is not multiplied across interpreters or placed in the time-sensitive publication path.

The tested artifacts are retained as the `heavenbase-dist` workflow artifact for seven days. Artifacts are scoped to their workflow run, so a manual publication cannot reuse an artifact from an earlier preparation run.

## Repository and PyPI Setup

The GitHub repository does not have to be public to publish to PyPI. PyPI trusts the GitHub Actions OIDC identity for the configured owner, repository, workflow filename, and environment. A PyPI upload is public even when the repository is private, and the source distribution can expose packaged source, metadata, and documentation.

Configure the PyPI trusted publisher before the first upload:

1. On PyPI, create or configure the `HeavenBase` project publisher:
   - owner: `Magolor`
   - repository: `HeavenBase-legacy`
   - workflow: `release.yml`
   - environment: `pypi`
2. In GitHub, create an environment named `pypi`.
3. Add environment reviewers when the repository plan supports them. The workflow remains safe without reviewers because only an explicitly confirmed manual dispatch creates the publication job.

No `PYPI_TOKEN` secret is needed. Only the final manual publication job receives `id-token: write`; verification, source, build, and artifact-test jobs have read-only repository permissions.

## Release Procedure

Prepare the candidate locally:

1. Update only `src/heavenbase/version.py` to a stable four-part version, then run `rtk bash scripts/sync-env.bash --no-sync`. Review and stage the synchronized Poetry metadata, lock metadata, and generated four-line headers in every built-in `meta.yaml`; manifest bodies remain author-owned.
2. Run the release-grade local gates from a clean source checkout:
   - `rtk uv sync --extra dev --extra full --locked`
   - `rtk bash scripts/sync-env.bash --check`
   - `rtk uv run python scripts/docs.py check`
   - `rtk bash scripts/flake.bash --ci`
   - `rtk bash scripts/test.bash -m "fast or full"`
   - `rtk uv build`
   - `rtk uv run --with twine python -m twine check dist/*.whl dist/*.tar.gz`
3. Commit the release-ready `master` branch.
4. Run `rtk bash scripts/release.bash` from `master`.

`scripts/release.bash` delegates to `scripts/release.py stage`. It requires a clean worktree on `master`, fetches the remote branches, rejects divergent history, requires unreleased commits, creates or reuses a `[release]` head commit, pushes `master`, fast-forwards `release` to the same commit, and pushes `release`. That final push starts candidate preparation only. The legacy `scripts/release.py publish` command is a compatibility alias for `stage`; it does not publish to PyPI.

Before either branch is pushed, the release helper revalidates the same version projection. Artifact review also compares every descriptor's path and bytes across source, wheel, and source distribution, so stale or rewritten module metadata fails closed.

After the release-branch workflow succeeds:

1. Copy the full 40-character SHA at the head of `release`.
2. In GitHub Actions, open the **Release** workflow and choose **Run workflow**.
3. Enter that SHA as `release_sha` and enter `publish-to-pypi` as `confirmation`.
4. Start the workflow. It rebuilds and smoke-tests the candidate artifacts from that exact SHA without rerunning the source suite.
5. Confirm that the manual run completes its `Publish to PyPI` job. If the repository later adds required reviewers to the `pypi` environment, approve that deployment when prompted.

Do not rerun a stale manual publication after either branch advances. Start a new manual run with the new current release SHA instead.

## Local Artifact Review

The GitHub workflow builds in a fresh checkout. For local review, remove or disregard older ignored files in `dist/` and inspect only the version-specific wheel and source distribution produced by the current build. Confirm that there is exactly one of each before treating the local result as release evidence.

Version schema: `MAJOR.MINOR.PATCH.N[devK]` (for example `0.1.2.0` or `0.1.2.1dev1`). The optional `devK` suffix marks in-development snapshots; stabilized releases omit it.
