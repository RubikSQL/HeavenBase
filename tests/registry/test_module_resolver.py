"""Module resolver contract and typed materialization-cache tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import gc
import threading
import weakref

from heavenbase.modules import ModuleResolver, ModuleService, module_resolver
from heavenbase.modules.base import ResolverCache
from heavenbase.utils.registry import Registry


class _Catalog:
    """Minimal mutable publication source for resolver-cache tests."""

    def __init__(self, publication: int = 1) -> None:
        self.publication = publication


class _Resolver:
    """Small structurally conforming resolver with independent identity."""

    def __init__(self, publication: int = 1) -> None:
        self.catalog = _Catalog(publication)
        self._rows = {}
        self._targets = {}

    def inspect(self, kind=None, identifier=None, *, include_inactive=False):
        rows = tuple(self._rows.values())
        if kind is not None:
            rows = tuple(row for row in rows if row["kind"] == kind)
        if identifier is not None:
            rows = tuple(row for row in rows if row["identifier"] == identifier)
        return rows

    def resolve(self, kind, identifier):
        return self._targets.get((kind, identifier), (kind, identifier))

    def resolve_executable(self, reference, *, owner=None):
        raise NotImplementedError("Resolver-cache tests do not materialize executables")

    def publish(self, row, *, target=None):
        """Replace one row and advance publication after its target is ready."""
        key = row["kind"], row["identifier"]
        self._rows[key] = row
        if target is not None:
            self._targets[key] = target
        self.catalog.publication = row["publication"]


def _row(
    identifier: str = "alpha",
    *,
    kind: str = "example",
    publication: int = 1,
    revision: int = 7,
    fingerprint: str = "same-record",
) -> dict:
    """Return the cache identity fields from one canonical inspection row."""
    return {
        "kind": kind,
        "identifier": identifier,
        "publication": publication,
        "revision": revision,
        "fingerprint": fingerprint,
    }


def test_module_service_structurally_satisfies_resolver_contract():
    service = ModuleService(Registry("system-modules"))

    assert isinstance(service, ModuleResolver)
    assert module_resolver(service) is service
    assert service.catalog.publication == 0


def test_resolver_cache_isolates_resolvers_at_the_same_record_state():
    cache = ResolverCache[str]()
    first = _Resolver()
    second = _Resolver()
    row = _row()

    assert cache.get(first, row, lambda _current_row: "first") == "first"
    assert cache.get(second, row, lambda _current_row: "second") == "second"
    assert cache.get(first, row, lambda _current_row: "wrong") == "first"
    assert cache.get(second, row, lambda _current_row: "wrong") == "second"


def test_resolver_cache_weakly_owns_resolver_state():
    cache = ResolverCache[str]()
    resolver = _Resolver()
    reference = weakref.ref(resolver)
    cache.get(resolver, _row(), lambda _current_row: "value")

    assert len(cache) == 1
    del resolver
    gc.collect()

    assert reference() is None
    assert len(cache) == 0


def test_resolver_cache_does_not_complete_a_value_to_resolver_cycle():
    class ResolverBoundValue:
        def __init__(self, resolver):
            self.resolver = resolver

    cache = ResolverCache[ResolverBoundValue]()
    resolver = _Resolver()
    resolver_reference = weakref.ref(resolver)
    value = cache.get(resolver, _row(), lambda _current_row: ResolverBoundValue(resolver))
    value_reference = weakref.ref(value)

    assert value.resolver is resolver
    assert len(cache) == 1
    del value
    del resolver
    gc.collect()

    assert value_reference() is None
    assert resolver_reference() is None
    assert len(cache) == 0


def test_resolver_cache_separates_canonical_keys_with_matching_fingerprints():
    cache = ResolverCache[str]()
    resolver = _Resolver()

    assert cache.get(resolver, _row("alpha"), lambda _current_row: "alpha") == "alpha"
    assert cache.get(resolver, _row("beta"), lambda _current_row: "beta") == "beta"
    assert cache.get(resolver, _row("alpha"), lambda _current_row: "wrong") == "alpha"
    assert cache.get(resolver, _row("beta"), lambda _current_row: "wrong") == "beta"


def test_resolver_cache_invalidates_on_process_publication_without_revision_change():
    cache = ResolverCache[str]()
    resolver = _Resolver(publication=1)
    first = _row(publication=1, revision=4)
    second = _row(publication=2, revision=4)

    assert cache.get(resolver, first, lambda _current_row: "first") == "first"
    resolver.publish(second)
    assert cache.get(resolver, second, lambda _current_row: "second") == "second"
    assert len(cache) == 1
    assert cache.get(resolver, second, lambda _current_row: "wrong") == "second"

    assert cache.get(resolver, first, lambda _current_row: "stale") == "second"
    assert cache.get(resolver, second, lambda _current_row: "wrong") == "second"


def test_resolver_cache_runs_builders_outside_its_lock():
    cache = ResolverCache[str]()
    resolver = _Resolver()
    entered = threading.Event()
    release = threading.Event()

    def blocked_builder(_current_row) -> str:
        entered.set()
        assert release.wait(timeout=2)
        return "slow"

    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(cache.get, resolver, _row("slow"), blocked_builder)
        assert entered.wait(timeout=2)
        try:
            fast = pool.submit(cache.get, resolver, _row("fast"), lambda _current_row: "fast")
            assert fast.result(timeout=1) == "fast"
        finally:
            release.set()
        assert slow.result(timeout=2) == "slow"


def test_resolver_cache_can_validate_dynamic_materializations_without_caching():
    cache = ResolverCache[int]()
    resolver = _Resolver()
    attempts = []

    def build(_current_row):
        attempts.append(len(attempts) + 1)
        return attempts[-1]

    assert cache.get(resolver, _row(), build, should_cache=lambda _current_row: False) == 1
    assert cache.get(resolver, _row(), build, should_cache=lambda _current_row: False) == 2
    assert attempts == [1, 2]
    assert len(cache) == 0


def test_resolver_cache_retries_the_whole_materialization_after_publication_changes():
    cache = ResolverCache[tuple[str, str]]()
    resolver = _Resolver(publication=1)
    first = _row(publication=1, fingerprint="first")
    first["factory"] = "old-factory"
    second = _row(publication=2, fingerprint="second")
    second["factory"] = "new-factory"
    resolver.publish(first, target="old-target")
    entered = threading.Event()
    release = threading.Event()
    attempts = []

    def build(current_row):
        attempts.append(current_row["factory"])
        if len(attempts) == 1:
            entered.set()
            assert release.wait(timeout=2)
        target = resolver.resolve(current_row["kind"], current_row["identifier"])
        return current_row["factory"], target

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(cache.get, resolver, first, build)
        assert entered.wait(timeout=2)
        resolver.publish(second, target="new-target")
        release.set()
        assert pending.result(timeout=2) == ("new-factory", "new-target")

    assert attempts == ["old-factory", "new-factory"]
    assert len(cache) == 1
