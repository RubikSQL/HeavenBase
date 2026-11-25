"""Module setup memos stay exact under ordinary plugin, profile, resolver and fork use.

Handler seeding is reused only for the package's own leading contributions and only
for the ModuleService that seeded it; other plugin seeds run for each workspace under
its configuration and into its own registry. Storage profiles reach callers as
copies. A forked child gets unheld memo locks.
"""

from functools import partial
import gc
import multiprocessing
import threading

import pytest

import heavenbase as hb
from heavenbase.execution.contracts import HandlerContext, QueryFragment
from heavenbase.execution.handlers import HandlerPlugin
from heavenbase.execution.handlers.seed import seed_handlers
from heavenbase.modules import ModuleService, discovery
from heavenbase.modules import artifacts as module_artifacts
from heavenbase.modules import service as module_service
from heavenbase.modules.artifacts import ModuleArtifactStore
from heavenbase.placement import storage_profile_for
from heavenbase.utils.config import ConfigManager
from heavenbase.utils.registry import Registry

pytestmark = pytest.mark.full  # each test builds Contexts and workspaces (about 2-6 s)


class ClNote(hb.Entity):
    identifier = "cl14-note"
    title = hb.field(hb.ShortText)


def _context(root):
    return hb.Context.load(
        {
            "version": 1,
            "root": str(root),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 4 * 1024 * 1024},
        },
        config=ConfigManager(root=str(root), setup=False),
    )


def _label_compiler(field, value, context, *, label):
    return QueryFragment(context.backend, "display", "title", {"label": label, "value": value})


def _seed_label(registry, seed_context):
    """Install an equality compiler carrying this Context's display label option."""
    label = seed_context.resolver.authority_config.get("example.display_label", default="Standard")
    seed_context.handler(hb.ShortText, "eq", "inmem", hb.InlineColumn, partial(_label_compiler, label=label)).register(registry, replace=True)


def _delegate(field, value, context, *, registry):
    return registry.get(hb.ShortText, "eq", "inmem", hb.InlineColumn).compile(field, value, context)


def _seed_delegate(registry, seed_context):
    """Install a not-equal compiler that delegates to the equality handler of the registry it seeds."""
    seed_context.handler(hb.ShortText, "ne", "inmem", hb.InlineColumn, partial(_delegate, registry=registry)).register(registry, replace=True)


def _compile(registry, op):
    handler = registry.get(hb.ShortText, op, "inmem", hb.InlineColumn)
    context = HandlerContext("main", "inmem", "inline-column", resolver=registry.module_resolver, handlers=registry)
    return handler.compile(None, "Quarterly report", context)


BACKENDS = {"main": {"type": "inmem"}}


def test_seed_option_per_workspace_scope(tmp_path):
    context = _context(tmp_path)
    try:
        resolver = context.modules()
        HandlerPlugin("cl-label", _seed_label, priority=1000, resolver=resolver).register(resolver=resolver)
        context.config.set("example.display_label", "Reports", scope=f"{context.config.base_scope}.reports")
        context.config.set("example.display_label", "Large print", scope=f"{context.config.base_scope}.accessible")
        reports = hb.HeavenBase("reports", context=context, detached=True, backends=BACKENDS)
        accessible = hb.HeavenBase("accessible", context=context, detached=True, backends=BACKENDS)
        got = (_compile(reports.handlers, "eq").payload["label"], _compile(accessible.handlers, "eq").payload["label"])
        print("labels per workspace:", got)
        assert got == ("Reports", "Large print")
    finally:
        context.close()


def test_seed_option_changed_then_new_workspace(tmp_path):
    context = _context(tmp_path)
    try:
        resolver = context.modules()
        HandlerPlugin("cl-label", _seed_label, priority=1000, resolver=resolver).register(resolver=resolver)
        context.config.set("example.display_label", "Standard")
        first = hb.HeavenBase("first", context=context, detached=True, backends=BACKENDS)
        assert _compile(first.handlers, "eq").payload["label"] == "Standard"
        context.config.set("example.display_label", "Large print")
        second = hb.HeavenBase("second", context=context, detached=True, backends=BACKENDS)
        got = _compile(second.handlers, "eq").payload["label"]
        print("label after option change:", got)
        assert got == "Large print"
    finally:
        context.close()


def test_seeded_delegate_sees_workspace_registration(tmp_path):
    context = _context(tmp_path)
    try:
        resolver = context.modules()
        HandlerPlugin("cl-label", _seed_label, priority=1000, resolver=resolver).register(resolver=resolver)
        HandlerPlugin("cl-delegate", _seed_delegate, priority=1001, resolver=resolver).register(resolver=resolver)
        ws = hb.HeavenBase("reports", context=context, detached=True, backends=BACKENDS)
        hb.ext.Handler(hb.ShortText, "eq", "inmem", hb.InlineColumn, partial(_label_compiler, label="Custom"), resolver=ws.module_resolver).register(
            ws.handlers, replace=True
        )
        got = (_compile(ws.handlers, "eq").payload["label"], _compile(ws.handlers, "ne").payload["label"])
        print("eq/ne labels:", got)
        assert got == ("Custom", "Custom")
    finally:
        context.close()


def test_profile_edit_does_not_outlive_caller(tmp_path):
    context = _context(tmp_path)
    try:
        resolver = context.modules()
        hb.HeavenBase("warm", context=context, detached=True, backends=BACKENDS)

        def preview():
            profile = storage_profile_for(hb.ShortText, resolver=resolver)
            original = profile.default_strategy
            profile.default_strategy = "json"
            return original

        original = preview()
        gc.collect()
        after = storage_profile_for(hb.ShortText, resolver=resolver).default_strategy
        ws = hb.HeavenBase("later", context=context, detached=True, backends=BACKENDS)
        ws.register(ClNote)
        plan = ws.storage_plans.get(ClNote.schema().entity_id)
        print("original", original, "after", after, "plan", plan)
        assert after == original
    finally:
        context.close()


class _ReadOnlyResolver:
    """A read-only facade over a Context's module service (the ModuleResolver read contract)."""

    def __init__(self, service):
        self._service = service

    @property
    def catalog(self):
        return self._service.catalog

    def inspect(self, *args, **kwargs):
        return self._service.inspect(*args, **kwargs)

    def resolve(self, kind, identifier):
        return self._service.resolve(kind, identifier)

    def resolve_executable(self, reference, *, owner=None):
        return self._service.resolve_executable(reference, owner=owner)


def test_facade_seeding_then_workspace_query(tmp_path):
    context = _context(tmp_path)
    try:
        service = context.modules()
        hb.HeavenBase("warm", context=context, detached=True, backends=BACKENDS)
        facade = _ReadOnlyResolver(service)
        registry = seed_handlers(resolver=facade)
        print("facade registry bound to facade:", registry.module_resolver is facade)
        ws = hb.HeavenBase("notes", context=context, detached=True, backends=BACKENDS)
        print("workspace registry bound to service:", ws.handlers.module_resolver is service)
        ws.register(ClNote)
        ws.upsert_many(ClNote, [{"object_id": "n1", "title": "alpha"}, {"object_id": "n2", "title": "beta"}])
        rows = ws.query(ClNote).where(ClNote.title == "alpha").select("title").execute().rows()
        print("rows", rows)
        assert rows == [{"object_id": "n1", "title": "alpha"}]
        assert registry.module_resolver is facade
    finally:
        context.close()


def _setup_child(root):
    service = ModuleService(Registry("system-modules"), artifacts=ModuleArtifactStore(root))
    assert service.setup()


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="fork is POSIX-only")
@pytest.mark.parametrize("owner, name", [(discovery, "_DISCOVERED_LOCK"), (module_artifacts, "_MEMO_LOCK"), (module_service, "_PREPARED_LOCK")])
def test_fork_while_a_memo_lock_is_held(tmp_path, owner, name):
    lock = getattr(owner, name)
    held = threading.Event()
    release = threading.Event()

    def hold():
        with lock:  # stands in for a parent thread inside a memo's locked store
            held.set()
            release.wait(30)

    worker = threading.Thread(target=hold)
    worker.start()
    child = None
    try:
        assert held.wait(10)
        child = multiprocessing.get_context("fork").Process(target=_setup_child, args=(tmp_path / "child",))
        child.start()
        child.join(20)
        print("child exitcode", child.exitcode)
        assert child.exitcode == 0
    finally:
        release.set()
        worker.join(5)
        if child is not None and child.is_alive():
            child.kill()
            child.join(5)


def test_facade_seeding_first_then_workspace_query(tmp_path):
    context = _context(tmp_path)
    try:
        service = context.modules()
        service.setup()
        facade = _ReadOnlyResolver(service)
        publication = service.catalog.publication
        seed_handlers(resolver=facade)
        ws = hb.HeavenBase("notes", context=context, detached=True, backends=BACKENDS)
        print("publication unchanged by construction:", service.catalog.publication == publication)
        print("workspace registry bound to service:", ws.handlers.module_resolver is service, ws.handlers.module_resolver)
        ws.register(ClNote)
        ws.upsert_many(ClNote, [{"object_id": "n1", "title": "alpha"}, {"object_id": "n2", "title": "beta"}])
        rows = ws.query(ClNote).where(ClNote.title == "alpha").select("title").execute().rows()
        print("rows", rows)
        assert rows == [{"object_id": "n1", "title": "alpha"}]
        assert ws.handlers.module_resolver is service
    finally:
        context.close()


def test_registered_profiles_are_copies(tmp_path):
    from heavenbase.placement import registered_storage_profiles

    context = _context(tmp_path)
    try:
        resolver = context.modules()
        context._setup_modules()
        first = registered_storage_profiles(resolver=resolver)
        key = next(iter(first))
        original = first[key].default_strategy
        first[key].default_strategy = "json" if original != "json" else "inline"
        first[key].backend_preferences = ()
        again = registered_storage_profiles(resolver=resolver)
        assert again[key].default_strategy == original
        assert again[key] is not first[key]
    finally:
        context.close()


_SEEN_FILTER_TYPES: list[int] = []


def _seed_changes_its_context(registry, seed_context):
    """Record the seed context's vocabulary size, then empty it."""
    _SEEN_FILTER_TYPES.append(len(seed_context.filter_types))
    seed_context.filter_types = ()


def test_a_seed_that_changes_its_context_does_not_change_the_next_seeding(tmp_path):
    context = _context(tmp_path)
    try:
        resolver = context.modules()
        context._setup_modules()
        HandlerPlugin("cl-context", _seed_changes_its_context, priority=1000, resolver=resolver).register(resolver=resolver)
        _SEEN_FILTER_TYPES.clear()
        seed_handlers(resolver=resolver)
        seed_handlers(resolver=resolver)
        assert len(_SEEN_FILTER_TYPES) == 2
        assert _SEEN_FILTER_TYPES[0] == _SEEN_FILTER_TYPES[1] > 0
    finally:
        context.close()


class ClMemoType(hb.ShortText):
    identifier = "cl-memo-type"


def test_objects_that_registration_returns_do_not_change_later_workspaces(tmp_path):
    from heavenbase.placement import StorageProfile

    context = _context(tmp_path)
    try:
        resolver = context.modules()
        context._setup_modules()
        hb.ext.register_logical_type(ClMemoType, resolver=resolver)
        profile = StorageProfile(ClMemoType, ({"tag": "storage-roles", "contains": "row"},), hb.InlineColumn, "memo-profile", resolver=resolver).register(
            resolver=resolver
        )
        plugin = HandlerPlugin("cl-label", _seed_label, priority=1000, resolver=resolver).register(resolver=resolver)
        context.config.set("example.display_label", "Standard")
        first = seed_handlers(resolver=resolver)
        assert _compile(first, "eq").payload["label"] == "Standard"
        assert storage_profile_for(ClMemoType, resolver=resolver).reason == "memo-profile"

        profile.reason = "changed-by-its-holder"
        plugin.priority, plugin.seed = -1, _seed_delegate
        del profile, plugin
        gc.collect()

        assert storage_profile_for(ClMemoType, resolver=resolver).reason == "memo-profile"
        assert _compile(seed_handlers(resolver=resolver), "eq").payload["label"] == "Standard"
    finally:
        context.close()


class ClPlaced(hb.Entity):
    identifier = "cl14-placed"
    title = hb.field(hb.ShortText)


class ClDraftNote(hb.Entity):
    identifier = "cl14-draft-note"
    label = hb.field(ClMemoType)


class _SandboxResolver(_ReadOnlyResolver):
    """A resolver that tries another placement policy: every profile stores as JSON."""

    def inspect(self, kind=None, identifier=None, *, include_inactive=False):
        rows = super().inspect(kind, identifier, include_inactive=include_inactive)
        if kind != "storage_profile":
            return rows
        return tuple({**row, "record": {**row["record"], "strategy": "json"}} for row in rows)

    def resolve(self, kind, identifier):
        found = super().resolve(kind, identifier)
        return {**found, "default_strategy": "json", "strategy_rules": [], "reason": "sandbox"} if kind == "storage_profile" else found


class _ResolverWithoutProfiles(_ReadOnlyResolver):
    """A resolver that exposes no storage profiles."""

    def inspect(self, kind=None, identifier=None, *, include_inactive=False):
        return () if kind == "storage_profile" else super().inspect(kind, identifier, include_inactive=include_inactive)


def _placed(workspace, entity, field):
    plan = workspace.storage_plans[entity.schema().entity_id]
    return [(binding.strategy, binding.source) for binding in plan.bindings if binding.field_name == field]


@pytest.mark.parametrize("sandbox_first", [True, False])
def test_a_sandbox_resolver_does_not_change_where_later_workspaces_store_fields(tmp_path, sandbox_first):
    from heavenbase.placement import StorageResolver, registered_storage_profiles

    context = _context(tmp_path)
    try:
        service = context.modules()
        context._setup_modules()
        sandbox = _SandboxResolver(service)
        if not sandbox_first:
            assert storage_profile_for(hb.ShortText, resolver=service).reason != "sandbox"
        assert {profile.default_strategy for profile in registered_storage_profiles(resolver=sandbox).values()} == {"json"}
        assert StorageResolver(default_backend="main", resolver=sandbox).default_rule(hb.ShortText(), None).reason == "sandbox"
        workspace = hb.HeavenBase("later", context=context, detached=True, backends=BACKENDS)
        workspace.register(ClPlaced)
        placed = _placed(workspace, ClPlaced, "title")
        assert placed and all(source != "sandbox" and strategy != "json" for strategy, source in placed)
    finally:
        context.close()


def test_a_resolver_without_profiles_does_not_stop_later_workspaces(tmp_path):
    from heavenbase.placement import registered_storage_profiles

    context = _context(tmp_path)
    try:
        service = context.modules()
        context._setup_modules()
        assert registered_storage_profiles(resolver=_ResolverWithoutProfiles(service)) == {}
        workspace = hb.HeavenBase("later", context=context, detached=True, backends=BACKENDS)
        workspace.register(ClPlaced)
        workspace.upsert(ClPlaced, object_id="n1", title="alpha")
        rows = workspace.query(ClPlaced).where(ClPlaced.title == "alpha").select("title").execute().rows()
        assert [row["title"] for row in rows] == ["alpha"]
    finally:
        context.close()


def test_a_draft_edit_of_the_returned_profile_ends_with_its_holder(tmp_path):
    """The object that register() returns is edited before anything reads the profiles, then dropped."""
    from heavenbase.placement import StorageProfile

    context = _context(tmp_path)
    try:
        resolver = context.modules()
        context._setup_modules()
        hb.ext.register_logical_type(ClMemoType, resolver=resolver)
        profile = StorageProfile(ClMemoType, ({"tag": "storage-roles", "contains": "row"},), hb.InlineColumn, "as registered", resolver=resolver)
        profile = profile.register(resolver=resolver)
        profile.reason, profile.default_strategy = "draft", "json"
        storage_profile_for(ClMemoType, resolver=resolver)
        del profile
        gc.collect()

        after = storage_profile_for(ClMemoType, resolver=resolver)
        assert (after.reason, after.default_strategy) == ("as registered", hb.InlineColumn.identifier)
        workspace = hb.HeavenBase("later", context=context, detached=True, backends=BACKENDS)
        workspace.register(ClDraftNote)
        assert [source for _, source in _placed(workspace, ClDraftNote, "label")] == ["as registered"]
    finally:
        context.close()
