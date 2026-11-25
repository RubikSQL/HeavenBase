"""First imports of HeavenBase modules from several threads at once cannot deadlock.

CPython takes a module's import lock before it imports that module's missing
parent package. A thread that imports ``pkg.sub`` while ``pkg`` is not yet in
``sys.modules`` therefore holds ``pkg.sub`` and waits for ``pkg``. If ``pkg``'s
``__init__`` imports ``pkg.sub`` (directly or through other modules) while
another thread is running it, each thread waits for the other and Python
raises ``_DeadlockError`` or hands one of them a partially initialized module.

The static guard below rebuilds every wait a first import can make, from
HeavenBase's own import statements and from code outside HeavenBase that
imports any submodule directly, and checks that they form no cycle. The only
module it leaves out is the private ``heavenbase._lazy``, which the root
package imports. The fresh-interpreter tests drive many first uses and direct
submodule imports at once. Lock-order problems inside third-party packages are
out of scope: the guard only checks that HeavenBase imports a third-party
package before its submodules.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
import sys

import pytest

from heavenbase.utils import cmd

pytestmark = pytest.mark.fast

SRC = Path(__file__).resolve().parents[2] / "src"
ROOT = SRC / "heavenbase"


@dataclass
class _Module:
    """One HeavenBase source module."""

    name: str
    is_package: bool
    tree: ast.Module


@dataclass
class _WaitGraph:
    """Every import-lock wait a first import of HeavenBase can make.

    An edge ``a -> b`` means a thread holding ``a``'s import lock may wait for
    ``b``'s: either ``a`` imports ``b`` while it executes, or ``a`` is being
    imported from a module that cannot guarantee ``a``'s parent ``b`` is loaded.
    """

    modules: dict[str, _Module]
    edges: dict[str, dict[str, str]] = field(default_factory=dict)
    third_party: list[str] = field(default_factory=list)

    def add(self, waiter: str, target: str, reason: str) -> None:
        """Record that ``waiter``'s lock holder may wait for ``target``."""
        if waiter != target and waiter in self.modules and target in self.modules:
            self.edges.setdefault(waiter, {}).setdefault(target, reason)

    def cycles(self) -> list[list[str]]:
        """Return the strongly connected components that contain a cycle."""
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        found: list[list[str]] = []

        def visit(node: str) -> None:
            index[node] = low[node] = len(index)
            stack.append(node)
            on_stack.add(node)
            for target in self.edges.get(node, {}):
                if target not in index:
                    visit(target)
                    low[node] = min(low[node], low[target])
                elif target in on_stack:
                    low[node] = min(low[node], index[target])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    found.append(sorted(component))

        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(limit, 10 * len(self.modules)))
        try:
            for node in sorted(self.modules):
                if node not in index:
                    visit(node)
        finally:
            sys.setrecursionlimit(limit)
        return found

    def describe(self, component: list[str]) -> str:
        """Return the edges inside one cyclic component with their sources."""
        members = set(component)
        edges = [(waiter, target, reason) for waiter in component for target, reason in sorted(self.edges.get(waiter, {}).items())]
        return "\n".join(f"  {waiter} -> {target}  ({reason})" for waiter, target, reason in edges if target in members)


def _source_modules() -> dict[str, _Module]:
    modules = {}
    for path in sorted(ROOT.rglob("*.py")):
        parts = path.relative_to(SRC).with_suffix("").parts
        is_package = parts[-1] == "__init__"
        name = ".".join(parts[:-1] if is_package else parts)
        modules[name] = _Module(name, is_package, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return modules


def _absolute(module: _Module, level: int, target: str | None) -> str:
    """Resolve a possibly relative import target from ``module``."""
    if not level:
        return target or ""
    base = module.name if module.is_package else module.name.rpartition(".")[0]
    for _ in range(level - 1):
        base = base.rpartition(".")[0]
    return f"{base}.{target}" if target else base


def _lazy_exports(module: _Module) -> dict[str, str]:
    """Return ``{export: target module}`` from a facade's lazy export tables."""
    exports = {}
    for node in module.tree.body:
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        if not isinstance(value, ast.Dict):
            continue
        for key, entry in zip(value.keys, value.values):
            if not (isinstance(key, ast.Constant) and isinstance(entry, ast.Tuple) and entry.elts):
                continue
            first = entry.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.startswith((".", "heavenbase")):
                target = first.value
                level = len(target) - len(target.lstrip("."))
                exports[key.value] = _absolute(module, level, target.lstrip(".") or None) if level else target
    return exports


def _ancestors(name: str) -> list[str]:
    parts = name.split(".")
    return [".".join(parts[:index]) for index in range(1, len(parts))]


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def _statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield the statements that run when ``body`` runs, in source order, skipping functions."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            yield from _statements(node.orelse)
            continue
        yield node
        for name in ("body", "orelse", "finalbody"):
            nested = getattr(node, name, None)
            if isinstance(nested, list):
                yield from _statements(nested)
        for handler in getattr(node, "handlers", ()):
            yield from _statements(handler.body)


def _functions(body: list[ast.stmt]) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Yield every function defined anywhere under ``body``."""
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _wait_graph() -> _WaitGraph:
    graph = _WaitGraph(_source_modules())
    lazy = {name: _lazy_exports(module) for name, module in graph.modules.items()}
    stdlib = set(sys.stdlib_module_names)

    def targets(module: _Module, node: ast.Import | ast.ImportFrom) -> list[tuple[str, list[str]]]:
        """Return ``(imported module, modules it loads first)`` for one statement."""
        if isinstance(node, ast.Import):
            return [(alias.name, []) for alias in node.names]
        base = _absolute(module, node.level, node.module)
        found = [(base, [])]
        for alias in node.names:
            if f"{base}.{alias.name}" in graph.modules:
                # ``from pkg import sub`` imports ``pkg`` before ``pkg.sub``.
                found.append((f"{base}.{alias.name}", [base]))
            elif alias.name in lazy.get(base, {}):
                found.append((lazy[base][alias.name], [base]))
        return found

    top_level_imports = {
        name: [
            imported
            for node in _statements(module.tree.body)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for found in targets(module, node)
            for imported in (found[0], *found[1])
        ]
        for name, module in graph.modules.items()
    }
    closures: dict[str, set[str]] = {}

    def closure(name: str) -> set[str]:
        """Return every module in ``sys.modules`` once ``name`` finished loading."""
        if name in closures:
            return closures[name]
        found = closures[name] = {*_ancestors(name), name}
        for parent in _ancestors(name):
            if parent in graph.modules:
                found |= closure(parent)
        for imported in top_level_imports.get(name, ()):
            found |= closure(imported) if imported in graph.modules else {*_ancestors(imported), imported}
        return found

    def visit(module: _Module, node: ast.stmt, loaded: set[str], top_level: bool) -> None:
        """Record the waits of one import statement and mark what it loaded."""
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            return
        where = f"{module.name}:{node.lineno}"
        for imported, first in targets(module, node):
            for package in first:
                loaded |= closure(package)
            root = imported.partition(".")[0]
            if root != "heavenbase":
                if "." in imported and root not in stdlib and root != "__future__" and root not in loaded:
                    graph.third_party.append(f"{where} imports {imported} before {root}")
                loaded.update([*_ancestors(imported), imported])
                continue
            if top_level:
                graph.add(module.name, imported, f"top-level import at {where}")
            child = imported
            while "." in child and child.rpartition(".")[0] not in loaded:
                graph.add(child, child.rpartition(".")[0], f"{child} imported at {where} before its parent")
                child = child.rpartition(".")[0]
            loaded |= closure(imported) if imported in graph.modules else {*_ancestors(imported), imported}

    for module in graph.modules.values():
        loaded = set().union(*(closure(parent) for parent in _ancestors(module.name))) | {module.name}
        for node in _statements(module.tree.body):
            visit(module, node, loaded, top_level=True)
        # A function runs after its module finished loading; a parameter named
        # after a package carries that already imported package.
        for function in _functions(module.tree.body):
            scope = closure(module.name) | {arg.arg for arg in ast.walk(function.args) if isinstance(arg, ast.arg)}
            for node in _statements(function.body):
                visit(module, node, scope, top_level=False)
        # A lazy facade imports each target when the export is first read.
        for export, target in lazy[module.name].items():
            child = target
            while "." in child and child.rpartition(".")[0] not in {*_ancestors(module.name), module.name}:
                graph.add(child, child.rpartition(".")[0], f"{child} loaded for {module.name}.{export} before its parent")
                child = child.rpartition(".")[0]
    # Code outside HeavenBase may import any submodule before its package, for
    # example ``from heavenbase.placement.binding import StorageBinding``.
    # ``heavenbase._lazy`` is the one exception: the root package must import
    # it, and it is private, so only HeavenBase packages that are already
    # loading import it.
    for name in graph.modules:
        if "." in name and name != "heavenbase._lazy":
            graph.add(name, name.rpartition(".")[0], f"{name} imported directly before its parent")
    return graph


@pytest.fixture(scope="module")
def wait_graph():
    """Return the import-lock wait graph of the source tree."""
    return _wait_graph()


@pytest.mark.slow
def test_first_imports_cannot_wait_on_each_other_in_a_cycle(wait_graph):
    cycles = wait_graph.cycles()

    assert len(wait_graph.modules) > 400
    assert cycles == [], "\n\n".join(
        "Concurrent first imports can deadlock on this cycle. Make the package facade lazy, "
        "so its __init__ imports none of its own submodules:\n" + wait_graph.describe(component)
        for component in cycles
    )


def test_third_party_submodules_are_imported_after_their_package(wait_graph):
    # The same lock order applies to a third-party package whose ``__init__``
    # imports its own submodules (``sqlalchemy`` and ``sqlalchemy.engine``).
    late = wait_graph.third_party

    assert late == [], "Import the package itself first, so the submodule never waits for it:\n" + "\n".join(late)


@pytest.fixture(scope="module")
def set_up_home(tmp_path_factory, home_env):
    """Return a HOME whose packaged modules are indexed, as after ``hb setup``."""
    home = tmp_path_factory.mktemp("set-up-home")
    script = "import sys; sys.path.insert(0, sys.argv[1]); from heavenbase.workspace import WorkspaceSpec; WorkspaceSpec.setup()"
    cmd([sys.executable, "-I", "-c", script, str(SRC)], check=True, env=home_env(home))
    return home


_FIRST_USE_SCRIPT = """
import os
import sys
import threading

sys.path.insert(0, sys.argv[1])
root = sys.argv[2]
import heavenbase as hb


def sqlite_backend(i):
    backend = hb.backends.SQLiteBackend(f"main{i}", hb.utils.path_to_file_uri(os.path.join(root, f"{i}.db")))
    return backend.db.execute("select 1 as x").to_list()


def database(i):
    return hb.Database(provider="sqlite", database=":memory:", config=hb.CM_HVNB).execute("select 1 as x").to_list()


def llm_engine(i):
    from heavenbase.context import default_context

    return hb.LLMEngine(manager=hb.CM_HVNB).resolver is default_context().modules()


def llm_cache(i):
    return hb.LLMCache.current(config_manager=hb.CM_HVNB) is hb.LLMCache.current()


def workspace(i):
    class Doc(hb.Entity):
        identifier = f"first-use-doc-{i}"
        title = hb.field(hb.ShortText)
        embedding = hb.field(hb.Vector[3]).optional()

    backend = hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(os.path.join(root, f"ws{i}.db")))
    ws = hb.HeavenBase(f"first-use-{i}", detached=True, backends={"main": backend})
    ws.register(Doc)
    ws.upsert(Doc, {"object_id": "a", "title": "a", "embedding": [0.1, 0.2, 0.3]})
    ids = ws.query(Doc).where(Doc.title == "a").execute().ids
    ws.release()
    return ids


def query(i):
    spec = hb.QuerySpec("doc", filter=hb.filters.Filter("title", "eq", "a"), limit=3).to_dict()
    return spec["limit"], hb.Vector[3].to_dict()["dim"], type(hb.VectorIndex()).__name__, type(hb.SparseGramIndex()).__name__


def extension_contracts(i):
    return hb.ext.StorageBinding.__name__, hb.ext.IndexBinding.__name__, hb.ext.ScalarIndexSpec.__name__


entries = (sqlite_backend, database, llm_engine, llm_cache, workspace, query, extension_contracts)
expected = {
    "sqlite_backend": [{"x": 1}],
    "database": [{"x": 1}],
    "llm_engine": True,
    "llm_cache": True,
    "workspace": ["a"],
    "query": (3, 3, "VectorIndex", "SparseGramIndex"),
    "extension_contracts": ("StorageBinding", "IndexBinding", "ScalarIndexSpec"),
}
count = int(sys.argv[3])
barrier = threading.Barrier(count)
errors = []
results = {}


def run(i):
    entry = entries[i % len(entries)]
    try:
        barrier.wait(60)
        results[i] = (entry.__name__, entry(i))
    except BaseException as error:
        errors.append(f"{entry.__name__}: {type(error).__name__}: {error}")


threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(count)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(240)
assert not any(thread.is_alive() for thread in threads), "a first use never finished"
assert not errors, errors
wrong = {i: result for i, result in results.items() if expected[result[0]] != result[1]}
assert len(results) == count and not wrong, wrong
print("ok")
"""


@pytest.mark.slow
def test_concurrent_first_uses_in_fresh_interpreters(tmp_path, set_up_home, home_env):
    # Each interpreter starts with only ``heavenbase`` imported, then sixteen
    # threads reach backends, Database, LLM, Entity, query, and extension
    # facades at the same moment. Several interpreters run, because one race
    # can miss the window.
    runs = []
    for index in range(8):
        root = tmp_path / f"run{index}"
        root.mkdir()
        runs.append(
            subprocess.Popen(
                [sys.executable, "-I", "-c", _FIRST_USE_SCRIPT, str(SRC), str(root), "16"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=home_env(set_up_home),
            )
        )
    failures = []
    for process in runs:
        out, err = process.communicate(timeout=600)
        if process.returncode != 0 or out.strip() != "ok":
            failures.append(err.strip().splitlines()[-1:] or [f"exit {process.returncode}"])

    assert failures == []


_FIRST_LOG_SCRIPT = """
import os
import sys
import threading
import time

sys.path.insert(0, sys.argv[1])
count = int(sys.argv[2])
from heavenbase.utils import CM_HVNB, get_logger, hash_id

barrier = threading.Barrier(count)
errors = []


def run(i):
    try:
        barrier.wait(60)
        if i % 4 == 3:
            hash_id(f"value-{i}")
        elif i % 4 == 2:
            CM_HVNB.set(f"heavenbase.first_log_probe.k{i}", i)
        else:
            get_logger("heavenbase.first-log").warning("first record %d", i)
    except BaseException as error:
        errors.append(f"{type(error).__name__}: {error}")


threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(count)]
for thread in threads:
    thread.start()
deadline = time.monotonic() + 120
for thread in threads:
    thread.join(max(0.0, deadline - time.monotonic()))
hung = any(thread.is_alive() for thread in threads)
print("hung" if hung else (errors or "ok"), flush=True)
# A deadlocked thread holds a handler lock that interpreter shutdown would wait for.
os._exit(0)
"""


@pytest.mark.slow
def test_concurrent_first_log_records_in_fresh_interpreters(tmp_path, home_env):
    # The first HeavenBase log record reads the saved log settings, which
    # selects the default Context and imports its database modules. It used to
    # do so under the handler lock, while a thread creating a logger held the
    # logger table lock and waited for that handler lock: two threads logging
    # their first records deadlocked.
    runs = []
    for index in range(8):
        home = tmp_path / f"home{index}"
        home.mkdir()
        runs.append(
            subprocess.Popen(
                [sys.executable, "-I", "-c", _FIRST_LOG_SCRIPT, str(SRC), "8"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=home_env(home),
            )
        )
    outcomes = []
    for process in runs:
        out, _ = process.communicate(timeout=600)
        outcomes.append(out.strip())

    assert outcomes == ["ok"] * len(runs)


_DIRECT_IMPORT_SCRIPT = """
import importlib
import sys
import threading

sys.path.insert(0, sys.argv[1])
count = int(sys.argv[2])
names = sys.argv[3:]
import heavenbase as hb

barrier = threading.Barrier(count)
errors = []


def load(name, attribute):
    # ``import a.b.c``, or ``a.b.c`` read through the lazy facades' attributes;
    # a plain package binds a submodule only once it is imported.
    if not attribute:
        return importlib.import_module(name)
    value, prefix = hb, "heavenbase"
    for part in name.split(".")[1:]:
        prefix = f"{prefix}.{part}"
        value = getattr(value, part) if "__getattr__" in vars(value) else importlib.import_module(prefix)
    return value


def run(i):
    order = names[i % len(names):] + names[: i % len(names)]
    if i % 2:
        order.reverse()
    try:
        barrier.wait(60)
        for name in order:
            load(name, attribute=i % 4 >= 2)
    except BaseException as error:
        errors.append(f"{type(error).__name__}: {error}")


threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(count)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(240)
assert not any(thread.is_alive() for thread in threads), "a first import never finished"
assert not errors, errors
from heavenbase.placement import IndexRule, index
from heavenbase.placement.index import IndexRule as defined

assert IndexRule is defined and callable(index) and index is hb.index, index
print("ok")
"""

# Packages whose submodules code outside HeavenBase imports directly, such as
# ``heavenbase.placement.binding`` and ``heavenbase.utils.registry.codec``.
_DIRECT_IMPORT_PACKAGES = (
    "heavenbase.placement",
    "heavenbase.utils.registry",
    "heavenbase.database.resources.sql",
    "heavenbase.extensions.python",
    "heavenbase.backends.sql.handler",
    "heavenbase.cli.groups.config",
)


@pytest.mark.slow
def test_concurrent_direct_submodule_imports_in_fresh_interpreters(wait_graph):
    # Each thread imports the packages and all of their submodules directly, or
    # reaches them as attributes of ``heavenbase``, each in its own order, while
    # the others do the same.
    names = sorted(name for name in wait_graph.modules if name.startswith(_DIRECT_IMPORT_PACKAGES))
    runs = [
        subprocess.Popen(
            [sys.executable, "-I", "-c", _DIRECT_IMPORT_SCRIPT, str(SRC), "16", *names],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(8)
    ]
    failures = []
    for process in runs:
        out, err = process.communicate(timeout=600)
        if process.returncode != 0 or out.strip() != "ok":
            failures.append(err.strip().splitlines()[-1:] or [f"exit {process.returncode}"])

    assert len(names) > 30
    assert failures == []


def test_placement_index_stays_the_public_function_after_a_direct_submodule_import():
    # ``heavenbase.placement.index`` names both the ``index()`` export and its
    # submodule; importing the submodule first must not replace the export.
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import heavenbase.placement.index as imported; import heavenbase.placement as placement; "
        "from heavenbase.placement.index import IndexRule; "
        "assert callable(placement.index) and placement.index is imported, placement.index; "
        "assert placement.IndexRule is IndexRule and sys.modules['heavenbase.placement.index'].index is placement.index; "
        "print('ok')"
    )

    out = cmd([sys.executable, "-I", "-c", script, str(SRC)], include="out", check=True)

    assert out == "ok"


@pytest.mark.parametrize(
    "first",
    [
        "import heavenbase.modules.tags",
        "import heavenbase.backends.base",
        "from heavenbase.modules import tag_definition",
        "import heavenbase as hb; hb.HeavenBase",
    ],
)
def test_modules_tags_stays_the_public_function_after_the_submodule_is_imported(first):
    # ``heavenbase.modules.tags`` names both the ``tags()`` export and its
    # submodule; whatever imports the submodule first must not replace the
    # export, and the submodule stays importable by name.
    script = (
        "import importlib, sys; from types import ModuleType; sys.path.insert(0, sys.argv[1]); "
        f"{first}; "
        "from heavenbase.modules import tags; import heavenbase.modules as modules; "
        "submodule = importlib.import_module('heavenbase.modules.tags'); "
        "from heavenbase.modules.tags import TagView; "
        "assert not isinstance(tags, ModuleType) and callable(tags), tags; "
        "assert modules.tags is tags is submodule.tags is sys.modules['heavenbase.modules.tags'].tags; "
        "assert isinstance(submodule, ModuleType) and modules.TagView is TagView; "
        "print('ok')"
    )

    out = cmd([sys.executable, "-I", "-c", script, str(SRC)], include="out", check=True)

    assert out == "ok"
