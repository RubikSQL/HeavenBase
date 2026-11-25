"""Objective: centralize HeavenBase release checks and candidate staging."""

from __future__ import annotations

import argparse
import runpy
import sys
from importlib import metadata

_OS = __import__("os")
ROOT = _OS.path.dirname(_OS.path.dirname(_OS.path.abspath(__file__)))

_RETIRED_PACKAGE_FILES = frozenset(
    {
        "heavenbase/bootstrap.py",
        "heavenbase/context.py",
        "heavenbase/mcp_runtime.py",
        "heavenbase/system_entities.py",
        "heavenbase/backends/elasticsearch/backend.py",
        "heavenbase/backends/context.py",
        "heavenbase/backends/contracts.py",
        "heavenbase/extensions/system/extension.py",
        "heavenbase/utils/cmd.py",
        "heavenbase/utils/lazy.py",
        "heavenbase/workspace/catalog.py",
        "heavenbase/workspace/errors.py",
        "heavenbase/workspace/mcp.py",
    }
)
_RETIRED_PACKAGE_PREFIXES = (
    "heavenbase/discovery/",
    "heavenbase/frame/",
    "heavenbase/handlers/",
    "heavenbase/registry/",
    "heavenbase/storage/",
    "heavenbase/utils/db/",
    "heavenbase/utils/dialect/",
    "heavenbase/utils/llm/",
    "heavenbase_registry/",
)


def _retired_package_paths(names: object) -> list[str]:
    """Return retired package and descriptor paths present in one inventory."""
    return sorted(
        name
        for name in names
        if isinstance(name, str)
        and (
            name.endswith("registration.yaml")
            or "heavenbase/resources/registry/" in name
            or name in _RETIRED_PACKAGE_FILES
            or name.startswith(_RETIRED_PACKAGE_PREFIXES)
            or (name.startswith("heavenbase/workspace/_") and not name.startswith("heavenbase/workspace/__"))
        )
    )


def pj(*parts: str) -> str:
    """Join path parts without importing HeavenBase before release artifact checks."""

    return _OS.path.normpath(_OS.path.join(*(str(part) for part in parts if part)))


def run(cmd: list[str], *, check: bool = True, capture: bool = False) -> object:
    """Run a command from the repository root."""

    subprocess = __import__("subprocess")
    proc = subprocess.run(cmd, cwd=ROOT, check=False, text=True, stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None)
    if check and proc.returncode:
        detail = f"\n{proc.stderr}" if capture and proc.stderr else ""
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(cmd)}{detail}")
    return proc


def output(cmd: list[str]) -> str:
    """Return stripped command output."""

    return run(cmd, capture=True).stdout.strip()


def git(args: list[str], *, check: bool = True, capture: bool = False) -> object:
    """Run a Git command from the repository root."""

    return run(["git", *args], check=check, capture=capture)


def git_output(args: list[str]) -> str:
    """Return stripped Git command output."""

    return output(["git", *args])


def ref_exists(ref: str) -> bool:
    """Return whether a Git ref exists."""

    return git(["rev-parse", "--verify", "--quiet", ref], check=False).returncode == 0


def is_ancestor(ancestor: str, descendant: str) -> bool:
    """Return whether one Git ref is an ancestor of another."""

    return git(["merge-base", "--is-ancestor", ancestor, descendant], check=False).returncode == 0


def source_version() -> Version:
    """Return the canonical stable four-part version declared by the source tree."""

    from packaging.version import Version

    re = __import__("re")
    raw = runpy.run_path(pj(ROOT, "src", "heavenbase", "version.py"))["__version__"]
    if not isinstance(raw, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+", raw) is None:
        raise SystemExit(f"release version must be a stable four-part value, found {raw!r}")

    version = Version(raw)
    if str(version) != raw:
        raise SystemExit(f"release version must be canonical, found {raw!r} (normalizes to {version})")
    return version


def source_artifact_version() -> tuple[str, Version]:
    """Return canonical source text and its PEP 440 artifact version."""

    from packaging.version import Version

    re = __import__("re")
    raw = runpy.run_path(pj(ROOT, "src", "heavenbase", "version.py"))["__version__"]
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)\.([0-9]+)(?:dev([0-9]+))?", raw) if isinstance(raw, str) else None
    if match is None:
        raise SystemExit(f"artifact version must use MAJOR.MINOR.PATCH.N[devK], found {raw!r}")

    numeric = match.groups()[:4]
    development = match.group(5)
    canonical = ".".join(str(int(part)) for part in numeric)
    if development is not None:
        canonical += f"dev{int(development)}"
    if raw != canonical:
        raise SystemExit(f"artifact version must be canonical, found {raw!r}")
    return raw, Version(raw)


def check_version(_: argparse.Namespace) -> None:
    """Validate and print the stable source package version and projections."""

    version = source_version()
    _source_module_payloads(version)
    print(f"HeavenBase source version: {version}")


def install_wheel(args: argparse.Namespace) -> None:
    """Install the single built wheel from a distribution directory."""

    dist = pj(ROOT, args.dist)
    wheels = sorted(name for name in _OS.listdir(dist) if name.endswith(".whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected one wheel in {dist}, found {wheels}")

    run([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "packaging"])
    run([sys.executable, "-m", "pip", "install", pj(dist, wheels[0])])


def check_modules(args: argparse.Namespace) -> None:
    """Verify module metadata parity across source, wheel, and sdist."""

    from pathlib import Path
    import tarfile
    import zipfile

    source_text, artifact_version = source_artifact_version()
    source_payloads = _source_module_payloads(source_text)
    dist = Path(ROOT, args.dist)
    wheels = tuple(dist.glob(f"heavenbase-{artifact_version}-*.whl"))
    sdists = tuple(dist.glob(f"heavenbase-{artifact_version}.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit(f"expected one wheel and one sdist for {artifact_version}, found wheels={list(wheels)!r}, sdists={list(sdists)!r}")

    package_root = Path(ROOT, "src", "heavenbase")
    source_names = {f"heavenbase/{path.relative_to(package_root).as_posix()}" for path in package_root.rglob("*") if path.is_file()}
    if Path(ROOT, "src", "heavenbase_registry").exists():
        source_names.add("heavenbase_registry/")
    source_modules = set(source_payloads)
    misplaced = sorted(name for name in source_modules if name.startswith("heavenbase/resources/"))
    legacy_source = _retired_package_paths(source_names)
    if misplaced or legacy_source:
        raise SystemExit(f"source contains retired or misplaced module paths: {sorted({*misplaced, *legacy_source})!r}")
    with zipfile.ZipFile(wheels[0]) as archive:
        wheel_names = set(archive.namelist())
        wheel_payloads = {name: archive.read(name) for name in wheel_names if name.startswith("heavenbase/") and name.endswith("meta.yaml")}
    with tarfile.open(sdists[0]) as archive:
        members = tuple(member for member in archive.getmembers() if member.isfile() and "/" in member.name)
        sdist_names = {member.name.split("/", 1)[1] for member in members}
        sdist_payloads: dict[str, bytes] = {}
        for member in members:
            relative = member.name.split("/", 1)[1]
            if not relative.startswith("src/heavenbase/") or not relative.endswith("meta.yaml"):
                continue
            stream = archive.extractfile(member)
            if stream is None:
                raise SystemExit(f"could not read module metadata from sdist member {member.name!r}")
            sdist_payloads[relative.removeprefix("src/")] = stream.read()

    _verify_module_payloads("wheel", source_payloads, wheel_payloads)
    _verify_module_payloads("sdist", source_payloads, sdist_payloads)
    required = {"heavenbase/resources/modules.yaml", "heavenbase/backends/elasticsearch/README.md"}
    gui_assets = {
        "host/HeavenBase.icns",
        "host/macos.js",
        "host/macos_app.js",
        "ui/index.html",
        "ui/styles.css",
        "ui/js/api.js",
        "ui/js/app.js",
    }
    gui_required = {
        "wheel": {f"heavenbase-{artifact_version}.data/data/share/heavenbase/gui/{name}" for name in gui_assets},
        "sdist": {f"apps/gui/{name}" for name in gui_assets},
    }
    openapi_required = {
        "wheel": {f"heavenbase-{artifact_version}.data/data/share/heavenbase/openapi/dashboard-openapi.json"},
        "sdist": {"docs/resources/reference/dashboard-openapi.json"},
    }
    for label, names in (("wheel", wheel_names), ("sdist", sdist_names)):
        normalized = {name.removeprefix("src/") for name in names}
        missing_required = sorted((required | gui_required[label] | openapi_required[label]) - normalized)
        if missing_required:
            raise SystemExit(f"{label} is missing required distribution assets: {missing_required!r}")
        legacy = _retired_package_paths(normalized)
        if legacy:
            raise SystemExit(f"{label} contains retired module paths: {legacy!r}")
    print(f"Verified {len(source_modules)} module folders in source, wheel, and sdist")


def _source_module_payloads(version: object) -> dict[str, bytes]:
    """Validate source projections and return canonical module payloads."""
    from pathlib import Path

    tools = runpy.run_path(pj(ROOT, "scripts", "sync-env.py"))
    projection = tools["version_metadata_projection"](Path(ROOT), str(version))
    drift = sorted(path.relative_to(ROOT).as_posix() for path, expected in projection.items() if path.read_text(encoding="utf-8") != expected)
    if drift:
        raise SystemExit(f"package version metadata is out of date: {drift!r}; run bash scripts/sync-env.bash")
    package_root = Path(ROOT, "src", "heavenbase")
    return {
        f"heavenbase/{path.relative_to(package_root).as_posix()}": path.read_bytes()
        for path in projection
        if path.name == "meta.yaml" and path.is_relative_to(package_root)
    }


def _verify_module_payloads(label: str, source: dict[str, bytes], artifact: dict[str, bytes]) -> None:
    """Require one artifact's descriptor paths and bytes to match source."""
    source_names = set(source)
    artifact_names = set(artifact)
    if artifact_names != source_names:
        missing = sorted(source_names - artifact_names)
        extra = sorted(artifact_names - source_names)
        raise SystemExit(f"{label} module metadata differs from source: missing={missing!r}, extra={extra!r}")
    changed = sorted(name for name in source_names if artifact[name] != source[name])
    if changed:
        raise SystemExit(f"{label} module metadata content differs from source: changed={changed!r}")


def smoke(_: argparse.Namespace) -> None:
    """Run a minimal installed-package smoke test in isolated machine state."""

    import heavenbase as hb
    from heavenbase.context import BootstrapSpec, load_bootstrap
    from packaging.version import Version
    from pathlib import Path
    from tempfile import TemporaryDirectory

    class ReleaseSmokeItem(hb.Entity):
        """Small entity used by release artifact smoke tests."""

        name = hb.field(hb.ShortText).desc("Display name")
        status = hb.field(hb.ShortText).desc("Workflow status").default("new")

    installed_version = Version(metadata.version("heavenbase"))
    runtime_version = Version(hb.__version__)
    if installed_version != runtime_version:
        raise AssertionError(f"installed metadata version {installed_version} != runtime version {runtime_version}")

    with TemporaryDirectory(prefix="heavenbase-release-smoke-") as root:
        packaged = load_bootstrap(home_path=Path(root) / "missing-bootstrap.yaml")
        backend = dict(packaged.backend)
        backend["database"] = (Path(root) / "system.db").as_uri()
        context = hb.Context(BootstrapSpec(root, "release-smoke-control", backend, dict(packaged.registry)))
        workspace = None
        try:
            workspace = hb.HeavenBase(
                "release-smoke",
                context=context,
                backends={"main": {"type": "inmem"}},
            )
            workspace.register(ReleaseSmokeItem)

            row_id = workspace.upsert(ReleaseSmokeItem, {"object_id": "smoke-1", "name": "Artifact"})
            workspace.set(row_id, {"status": "ready"}, entity=ReleaseSmokeItem)
            frame = workspace.query(ReleaseSmokeItem).where(ReleaseSmokeItem.status == "ready").select("name", "status").execute()

            rows = frame.rows()
            if rows != [{"object_id": "smoke-1", "name": "Artifact", "status": "ready"}]:
                raise AssertionError(f"unexpected release smoke rows: {rows}")
        finally:
            if workspace is not None:
                workspace.drop()
            context.close()

    print(f"HeavenBase {installed_version} smoke test passed from {hb.__file__}")


def ensure_clean() -> None:
    """Require a clean working tree before branch orchestration."""

    dirty = git_output(["status", "--porcelain"])
    if dirty:
        raise SystemExit(f"release requires a clean worktree:\n{dirty}")


def ensure_current_branch(branch: str) -> None:
    """Require the current branch name."""

    current = git_output(["branch", "--show-current"])
    if current != branch:
        raise SystemExit(f"release must be run from {branch!r}; current branch is {current!r}")


def fetch_release_refs(remote: str, master: str, release: str) -> None:
    """Fetch master and release refs when the remote has them."""

    # Use explicit heads refs. A bare `master:`/`release:` refspec can resolve to
    # no remote source and delete the local tracking ref on some Git/setups.
    git(["fetch", remote, f"+refs/heads/{master}:refs/remotes/{remote}/{master}"])
    git(["fetch", remote, f"+refs/heads/{release}:refs/remotes/{remote}/{release}"], check=False)


def ensure_local_release_branch(remote: str, release: str) -> None:
    """Create the local release branch from the remote if needed."""

    if ref_exists(release):
        return
    remote_release = f"{remote}/{release}"
    if ref_exists(remote_release):
        git(["branch", release, remote_release])
        return
    raise SystemExit(f"missing local or remote {release!r} branch; create it once with: git branch {release} master")


def ensure_remote_sync(remote: str, master: str, release: str) -> None:
    """Reject branch states that are not safe to publish from."""

    remote_master = f"{remote}/{master}"
    remote_release = f"{remote}/{release}"
    if ref_exists(remote_master) and not is_ancestor(remote_master, master):
        raise SystemExit(f"{master!r} is behind or diverged from {remote_master!r}; pull or rebase before release")
    if ref_exists(remote_release) and git_output(["rev-parse", release]) != git_output(["rev-parse", remote_release]):
        raise SystemExit(f"local {release!r} differs from {remote_release!r}; update it before release")


def ensure_master_only_ahead(master: str, release: str) -> None:
    """Require release to be an ancestor of master and master to have unreleased commits."""

    counts = git_output(["rev-list", "--left-right", "--count", f"{release}...{master}"]).split()
    release_only, master_only = int(counts[0]), int(counts[1])
    if release_only:
        raise SystemExit(f"{release!r} has {release_only} commit(s) not in {master!r}; refusing divergent release")
    if master_only <= 0:
        raise SystemExit(f"{master!r} has no unreleased commits ahead of {release!r}")


def stage(args: argparse.Namespace) -> None:
    """Fast-forward release from master and push it for candidate validation."""

    ensure_clean()
    ensure_current_branch(args.master)
    fetch_release_refs(args.remote, args.master, args.release)
    ensure_local_release_branch(args.remote, args.release)
    ensure_remote_sync(args.remote, args.master, args.release)
    ensure_master_only_ahead(args.master, args.release)

    version = source_version()
    _source_module_payloads(version)
    message = args.message or f"[release] Stage HeavenBase {version}"
    head_message = git_output(["log", "-1", "--pretty=%B", args.master])

    if "[release]" in head_message:
        print(f"Reusing existing [release] head commit on {args.master}")
    else:
        print(f"Creating release commit on {args.master}: {message}")
        git(["commit", "--allow-empty", "-m", message])

    print(f"Pushing {args.master} to {args.remote}")
    git(["push", args.remote, args.master])

    print(f"Fast-forwarding {args.release} from {args.master}")
    git(["switch", args.release])
    try:
        git(["merge", "--ff-only", args.master])
        print(f"Pushing {args.release} to {args.remote}; this triggers candidate validation and artifact builds")
        git(["push", args.remote, args.release])
        print("PyPI publication still requires a separately confirmed manual workflow dispatch")
    finally:
        git(["switch", args.master], check=False)


def parser() -> argparse.ArgumentParser:
    """Build the release helper CLI."""

    p = argparse.ArgumentParser(description="HeavenBase release helper")
    sub = p.add_subparsers(dest="cmd", required=True)

    check = sub.add_parser("check-version", help="validate and print the source version")
    check.set_defaults(func=check_version)

    install = sub.add_parser("install-wheel", help="install the built release wheel")
    install.add_argument("dist", nargs="?", default="dist", help="distribution directory")
    install.set_defaults(func=install_wheel)

    modules = sub.add_parser("check-modules", help="verify packaged module metadata parity")
    modules.add_argument("dist", nargs="?", default="dist", help="distribution directory")
    modules.set_defaults(func=check_modules)

    smoke_cmd = sub.add_parser("smoke", help="smoke-test the installed release artifact")
    smoke_cmd.set_defaults(func=smoke)

    def add_stage_command(name: str, help_text: str) -> None:
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--remote", default="origin")
        command.add_argument("--master", default="master")
        command.add_argument("--release", default="release")
        command.add_argument("--message")
        command.set_defaults(func=stage)

    add_stage_command("stage", "stage master through the release branch for validation")
    add_stage_command("publish", "compatibility alias for 'stage'; it does not publish to PyPI")

    return p


def main() -> None:
    """Run the release helper CLI."""

    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
