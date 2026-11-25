from concurrent.futures import ThreadPoolExecutor
import datetime
import threading
from types import MappingProxyType

import pytest

import heavenbase as hb
from heavenbase.context.registry import HeavenBaseRegistryStore
from heavenbase.utils import dflat, dget, dmerge, dset, dsetdef, dunflat, dunset, path_to_file_uri, pj
from heavenbase.utils import copy_data, to_mutable_data
from heavenbase.utils.config import ConfigManager, InterpolationPolicy
from heavenbase.utils.config.api import ConfigOps


def _store(backend):
    return HeavenBaseRegistryStore(backend)


def _db(tmp_path, name: str = "config.db") -> str:
    return path_to_file_uri(tmp_path / name)


def _default(cache=None):
    out = {"x": {"y": 1}, "items": [1, 2], "a": {"b": 1, "c": 2}}
    if cache is not None:
        out["heavenbase"] = {"config": {"cache": cache}}
    return out


def _cm(tmp_path, default=None, keep_last_k=3):
    data = default or _default()
    backend = hb.backends.SQLiteBackend("config", _db(tmp_path), ws_id="config")
    registry = hb.Registry.load("test-system-config", store=_store(backend))
    return ConfigManager(
        package="heavenbase-test",
        scope="heavenbase",
        root=str(tmp_path),
        default=data,
        registry=registry,
        keep_last_k=keep_last_k,
        refresh_interval=999.0,
    )


def test_config_ops_is_the_scope_aware_presentation_protocol(tmp_path):
    cm = _cm(tmp_path, keep_last_k=8)
    ops = ConfigOps(cm)

    assert ops.get("x.y") == 1
    assert ops.set("enabled", "true")
    assert ops.set("payload", '{"count": 2}', parse="json")
    assert ops.set("literal", "001", parse="raw", scope="app")
    assert ops.add("items", "3")

    assert ops.get("enabled") is True
    assert ops.get("payload") == {"count": 2}
    assert ops.get("literal", scope="app") == "001"
    assert ops.get("items") == [1, 2, 3]
    assert {row["key"] for row in ops.list("x.")} == {"x.y"}


def test_config_ops_validates_modes_and_resolves_history(tmp_path):
    cm = _cm(tmp_path, keep_last_k=8)
    ops = ConfigOps(cm)

    ops.set("x.y", "2")
    ops.set("x.y", "3")

    assert ops.get("x.y", version=-1) == 3
    assert ops.get("x.y", version=-2) == 2
    assert ops.copy("x", source="default", target_scope="copy")
    assert ops.get("x.y", scope="copy") == 1
    assert "heavenbase" in ops.diff()

    with pytest.raises(ValueError, match="parse mode"):
        ops.set("x.y", "4", parse="yaml")
    with pytest.raises(ValueError, match="copy source"):
        ops.copy(source="unknown")
    with pytest.raises(TypeError, match="ConfigManager"):
        ConfigOps(object())


def test_scoped_context_and_decorator(tmp_path):
    cm = _cm(tmp_path)
    cm.set("x.y", 2, scope="heavenbase.app")

    @cm.scoped("app")
    def read_value():
        return cm.scope, cm.get("x.y")

    assert read_value() == ("heavenbase.app", 2)
    assert cm.get("x.y") == 1


def test_base_qualified_scope_replaces_and_restores_ambient_chain(tmp_path):
    cm = _cm(tmp_path)
    cm.set("x.y", 2, scope="heavenbase.app")
    cm.set("x.y", 3, scope="heavenbase.other")

    with cm.scoped("outer"):
        assert cm.scope == "heavenbase.outer"
        with cm.scoped("heavenbase.app"):
            assert cm.scope_chain == ["heavenbase", "heavenbase.app"]
            assert cm.get("x.y") == 2
            with cm.scoped("heavenbase.app"):
                assert cm.scope_chain == ["heavenbase", "heavenbase.app"]
                assert cm.get("x.y") == 2
            with cm.scoped("heavenbase.other"):
                assert cm.scope_chain == ["heavenbase", "heavenbase.other"]
                assert cm.get("x.y") == 3
            assert cm.scope_chain == ["heavenbase", "heavenbase.app"]
        assert cm.scope == "heavenbase.outer"
        with pytest.raises(RuntimeError, match="scope failure"):
            with cm.scoped("heavenbase.app"):
                raise RuntimeError("scope failure")
        assert cm.scope == "heavenbase.outer"
        with cm.scoped("heavenbase"):
            assert cm.scope_chain == ["heavenbase"]
        assert cm.scope == "heavenbase.outer"

    assert cm.scope == "heavenbase"


def test_base_qualified_scopes_restore_across_independent_managers(tmp_path):
    first = _cm(tmp_path / "first")
    second = _cm(tmp_path / "second")
    first.set("marker", "first", scope="heavenbase.alpha")
    second.set("marker", "second", scope="heavenbase.beta")

    with first.scoped("heavenbase.alpha"):
        assert first.get("marker") == "first"
        with second.scoped("heavenbase.beta"):
            assert second.get("marker") == "second"
        assert first.scope == "heavenbase.alpha"
        assert first.get("marker") == "first"

    assert first.scope == second.scope == "heavenbase"


def test_snapshot_cache_and_latest_k(tmp_path):
    cm = _cm(tmp_path, keep_last_k=3)
    s1 = cm.snapshot()
    assert cm.snapshot() is s1
    for value in range(5):
        cm.set("x.y", value)
    assert cm.get("x.y") == 4
    assert cm.snapshot() is not s1
    assert [row["ver"] for row in cm.history("heavenbase", limit=10)] == [6, 5, 4]


def test_config_cache_lru_bounds_and_zero_disable(tmp_path):
    cm = _cm(tmp_path, default=_default({"refresh_interval": 999.0, "max_snapshots": 2, "max_generations": 1}))
    s1 = cm.snapshot({"case": 1})
    s2 = cm.snapshot({"case": 2})

    assert cm.snapshot({"case": 1}) is s1

    s3 = cm.snapshot({"case": 3})
    assert s1 in cm._snap_cache.values()
    assert s2 not in cm._snap_cache.values()
    assert s3 in cm._snap_cache.values()
    with cm.scoped("app"):
        cm.snapshot()
    assert len(cm._gen_cache) <= 1

    disabled = _cm(tmp_path / "disabled", default=_default({"refresh_interval": 999.0, "max_snapshots": 0, "max_generations": 0}))
    disabled.snapshot()
    assert not disabled._snap_cache
    assert not disabled._gen_cache
    assert not disabled._stamp_memo


def test_config_cache_rejects_negative_bounds(tmp_path):
    with pytest.raises(ValueError, match="max_snapshots"):
        _cm(tmp_path, default=_default({"max_snapshots": -1, "max_generations": 1}))


def test_config_cache_skips_stale_insert_after_refresh_epoch(tmp_path, monkeypatch):
    cm = _cm(tmp_path, default=_default({"refresh_interval": 999.0, "max_snapshots": 2, "max_generations": 2}))
    real_chain_state = cm._chain_state
    real_resolved = cm._resolved
    gen_calls = 0
    layer_calls = 0

    def chain_state(chain, **kwargs):
        nonlocal gen_calls
        gen_calls += 1
        out = real_chain_state(chain, **kwargs)
        if gen_calls == 1:
            cm.refresh()
        return out

    def resolved(*args, **kwargs):
        nonlocal layer_calls
        layer_calls += 1
        out = real_resolved(*args, **kwargs)
        if layer_calls == 1:
            cm.refresh()
        return out

    monkeypatch.setattr(cm, "_chain_state", chain_state)
    monkeypatch.setattr(cm, "_resolved", resolved)

    cm.snapshot()
    assert gen_calls == 3
    assert layer_calls == 2
    assert len(cm._gen_cache) == 1
    assert len(cm._snap_cache) == 1


def test_scope_stamps_are_digested_once_per_registry_publication(tmp_path, monkeypatch):
    # Every generation refresh used to copy and digest each scope record in the
    # chain, so config reads on a write-heavy path paid a full-layer digest every
    # refresh interval. A digest now lasts until the Registry publishes again.
    from heavenbase.utils.config import manager

    cm = _cm(tmp_path, default=_default({"refresh_interval": 999.0, "max_snapshots": 8, "max_generations": 8}))
    digested = []
    real_stamp = manager._stamp
    monkeypatch.setattr(manager, "_stamp", lambda generation, layer: digested.append(layer) or real_stamp(generation, layer))
    chain = ("heavenbase", "heavenbase.app")

    first = cm._chain_state(chain, reload=True)[1]
    assert digested
    digested.clear()
    assert cm._chain_state(chain, reload=True)[1] == first
    assert digested == []

    cm.set("x.y", 7, scope="heavenbase.app")
    second = cm._chain_state(chain, reload=True)[1]
    assert second[0] == first[0] and second[1] != first[1]
    digested.clear()
    assert cm._chain_state(chain, reload=True)[1] == second
    assert digested == []
    with cm.scoped("app"):
        assert cm.get("x.y") == 7


def test_scope_stamp_memo_is_bounded_and_cleared_with_the_caches(tmp_path):
    # The memo grew by one entry for every distinct scope read within one
    # Registry publication, whatever the cache bounds, and a cache clear kept it.
    cm = _cm(tmp_path, default=_default({"refresh_interval": 999.0, "max_snapshots": 8, "max_generations": 2}))
    for name in ("a", "b", "c", "d"):
        with cm.scoped(name):
            cm.snapshot()
    assert set(cm._stamp_memo) == {"heavenbase", "heavenbase.d"}

    cm.set("x.y", 7, scope="heavenbase.d")
    assert "heavenbase.d" not in cm._stamp_memo
    with cm.scoped("d"):
        assert cm.get("x.y") == 7
    assert set(cm._stamp_memo) == {"heavenbase", "heavenbase.d"}

    cm.refresh()
    assert not cm._stamp_memo


def test_scope_stamp_memos_of_two_managers_follow_each_others_writes():
    _, (first, second) = _peer_managers(2)
    chain = (first.base_scope, f"{first.base_scope}.app")
    before = first._chain_state(chain, reload=True)[1]
    assert second._chain_state(chain, reload=True)[1] == before

    second.set("items[]", 2, scope=chain[1])
    after = second._chain_state(chain, reload=True)[1]
    assert after[0] == before[0] and after[1] != before[1]
    # Without a reload the first manager still serves the publication it read.
    assert first._chain_state(chain, reload=False)[1] == before
    assert first._chain_state(chain, reload=True)[1] == after
    assert first._chain_state(chain, reload=False)[1] == after
    with first.scoped("app"):
        assert first.load(refresh=True)["items"] == [1, 2]


def test_config_mutation_preserves_unrelated_scope_snapshots(tmp_path):
    cm = _cm(tmp_path, default=_default({"refresh_interval": 999.0, "max_snapshots": 8, "max_generations": 8}))
    with cm.scoped("app"):
        app_before = cm.snapshot()
    with cm.scoped("other"):
        other_before = cm.snapshot()

    cm.set("x.y", 7, scope="heavenbase.app")

    with cm.scoped("app"):
        assert cm.snapshot() is not app_before
    with cm.scoped("other"):
        assert cm.snapshot() is other_before


def test_config_manager_layer_history_features(tmp_path):
    cm = _cm(tmp_path, keep_last_k=5)
    assert cm.load_default()["x"]["y"] == 1
    assert cm.load()["x"]["y"] == 1
    assert cm.layer("heavenbase")["x"]["y"] == 1
    assert cm.version == 1
    assert cm.version_chain == [1]

    assert cm.setdef("x.z", 3)
    assert not cm.setdef("x.z", 4)
    assert cm.get("x.z") == 3
    assert "heavenbase" in cm.scopes()

    for value in range(3):
        cm.set("x.y", value)
    assert cm.compact("heavenbase", keep_last_k=2) >= 1
    assert len(cm.history("heavenbase", limit=10)) == 2

    latest = cm.history("heavenbase", limit=1)[0]["ver"]
    assert cm.layer("heavenbase", version=latest)["x"]["y"] == 2
    assert cm.remove("heavenbase", version=latest)
    assert cm.remove("heavenbase")
    assert "heavenbase" not in cm.scopes()


def test_config_manager_layers_current_defaults_under_stored_config(tmp_path):
    old = _cm(tmp_path, default={"x": {"y": 1}}, keep_last_k=5)
    old.snapshot()

    cm = _cm(tmp_path, default={"x": {"y": 2, "z": 3}}, keep_last_k=5)

    assert cm.get("x.y") == 1
    assert cm.get("x.z") == 3


def test_config_manager_init_and_resource(tmp_path):
    cm = _cm(tmp_path)
    assert not cm.init(scope="heavenbase")
    assert cm.init(scope="heavenbase.extra", data={"alpha": 1})
    assert cm.layer("heavenbase.extra") == {"alpha": 1}
    assert cm.init(scope="heavenbase.extra", reset=True, data={"alpha": 2})
    assert cm.layer("heavenbase.extra") == {"alpha": 2}
    assert cm.resource("configs", "default.yaml") is None


def test_config_manager_pj_resolves_package_aliases(tmp_path):
    cm = _cm(tmp_path)
    resource_cm = ConfigManager(package="heavenbase", scope="heavenbase", root=str(tmp_path), default={}, setup=False)

    assert cm.pj("%/cache", abs=True) == pj(str(tmp_path), "cache", abs=True)
    assert cm.pj("%cache", abs=True) == pj(str(tmp_path), "cache", abs=True)
    assert resource_cm.pj("&/configs/default.yaml", abs=True).endswith("default.yaml")


def test_config_manager_uses_caller_owned_registry_store(tmp_path):
    cm = _cm(tmp_path)

    assert cm._scope_registry.persistent
    assert cm._scope_registry._store.backend.database == path_to_file_uri(tmp_path / "config.db")


def test_tombstone_hides_parent_value(tmp_path):
    cm = _cm(tmp_path)
    cm.set("x.y", 2, scope="heavenbase.app")
    with cm.scoped("app.sub"):
        cm.unset("x.y")
        assert cm.get("x.y", default="missing") == "missing"


def test_list_op_resurrects_tombstoned_ancestor(tmp_path):
    cm = _cm(tmp_path)
    cm.set("custom.items", [1])
    cm.unset("custom")
    cm.set("custom.items[]", 2)
    assert list(cm.get("custom.items")) == [2]


def test_set_whole_layer_none_path_survives_tombstone_guard(tmp_path):
    cm = _cm(tmp_path)
    cm.set("custom.x", 1)
    cm.set(None, cm.layer())
    assert cm.get("custom.x") == 1


def test_overwrite_merge_strips_nested_tombstones():
    base = {"a": {"old": 2}}
    incoming = {"a": {"__HB_OVERWRITE__": True, "b": {"__HB_REMOVE__": True}, "c": 1}}

    merged = dmerge([incoming], start=base)

    assert merged["a"] == {"c": 1}
    assert "__HB_REMOVE__" not in repr(merged)


def test_dict_overwrite_marker(tmp_path):
    cm = _cm(tmp_path)
    with cm.scoped("app"):
        cm.set("a", {"__HB_OVERWRITE__": True, "d": 3})
        assert cm.snapshot().get("a").get("d") == 3
        assert cm.get("a.b", default=None) is None


def test_list_path_set_and_unset(tmp_path):
    cm = _cm(tmp_path)
    with cm.scoped("app"):
        cm.set("items[]", 3)
        cm.set("items[1]", 20)
        cm.set("items[1+]", 10)
        cm.unset("items[-1]")
        assert cm.get("items") == (1, 10, 20)


def test_to_mutable_data_copies_frozen_config_values(tmp_path):
    cm = _cm(tmp_path)

    value = to_mutable_data(cm.get())

    assert value == {"x": {"y": 1}, "items": [1, 2], "a": {"b": 1, "c": 2}}
    value["items"].append(3)
    assert cm.get("items") == (1, 2)


def test_copy_data_isolates_json_like_values_and_shares_only_immutable_leaves():
    stamp = datetime.datetime(2026, 9, 11, 12, 0, 0)
    source = {"embedding": [1.0, 2.0, 3.0], "meta": {"tags": ("a", "b"), "nested": [{"k": b"raw"}]}, "when": stamp}

    copied = copy_data(source)

    assert copied == source
    assert copied is not source and copied["embedding"] is not source["embedding"]
    assert copied["meta"] is not source["meta"] and copied["meta"]["nested"][0] is not source["meta"]["nested"][0]
    assert copied["embedding"][0] is source["embedding"][0]
    assert copied["when"] == stamp and copied["when"] is not stamp
    source["embedding"].append(4.0)
    source["meta"]["nested"][0]["k"] = b"changed"
    assert copied["embedding"] == [1.0, 2.0, 3.0]
    assert copied["meta"]["nested"][0]["k"] == b"raw"
    assert copy_data(None) is None and copy_data("text") == "text"


def test_copy_data_and_config_thaw_skip_deepcopy_for_json_leaves(monkeypatch):
    import copy

    calls = []
    original = copy.deepcopy
    monkeypatch.setattr(copy, "deepcopy", lambda value, memo=None: calls.append(type(value)) or original(value, memo))

    vector = [float(index) for index in range(2048)]
    assert copy_data({"embedding": vector, "flags": [True, None, "x"]})["embedding"] == vector
    assert calls == []
    assert to_mutable_data(MappingProxyType({"a": (1, 2), "b": MappingProxyType({"c": "d"})})) == {"a": [1, 2], "b": {"c": "d"}}
    assert dmerge([{"a": {"b": [1, 2]}}, {"a": {"c": 3.5}}]) == {"a": {"b": [1, 2], "c": 3.5}}
    assert calls == []
    assert isinstance(copy_data({"when": datetime.date(2026, 9, 11)})["when"], datetime.date)
    assert calls[:1] == [datetime.date]


def test_config_manager_stores_versioned_scope_records_in_registry(tmp_path):
    cm = _cm(tmp_path)
    cm.set("meta.enabled", True)

    record = cm._scope_registry["heavenbase"]
    assert record["scope"] == "heavenbase"
    assert record["generation"] == 2
    assert record["versions"][-1]["data"]["meta"] == {"enabled": True}


def test_config_managers_converge_on_one_deterministic_initial_seed():
    backend = hb.backends.InMemBackend("config", ws_id="config")
    registries = [hb.Registry.load("config-seed", store=_store(backend)) for _ in range(2)]
    managers = [ConfigManager(package="seed-test", default={"enabled": True}, registry=registry) for registry in registries]

    with ThreadPoolExecutor(max_workers=2) as pool:
        loaded = list(pool.map(lambda manager: manager.load(), managers))

    assert loaded == [{"enabled": True}, {"enabled": True}]
    stored = hb.Registry.load("config-seed", store=_store(backend))["seed-test"]
    assert stored["generation"] == 1
    assert len(stored["versions"]) == 1


@pytest.mark.anyio
async def test_config_manager_refresh_matches_registry_visibility_contract():
    backend = hb.backends.InMemBackend("config-refresh", ws_id="config-refresh")
    registries = [hb.Registry.load("config-refresh", store=_store(backend)) for _ in range(2)]
    writer, reader = [
        ConfigManager(
            package="refresh-test",
            default={"enabled": True},
            registry=registry,
            refresh_interval=999.0,
        )
        for registry in registries
    ]
    assert writer.load() == reader.load() == {"enabled": True}

    writer.set("enabled", False)

    assert reader.get("enabled") is True
    assert await reader.arefresh() is True
    assert reader.get("enabled") is False
    assert reader.refresh() is False


def _peer_managers(count, backend=None):
    """Return managers that, like separate processes, each hold their own Registry over one store."""
    backend = backend or hb.backends.InMemBackend("config-peers", ws_id="config-peers")
    managers = [
        ConfigManager(
            package="peer-test",
            default={"items": [1]},
            registry=hb.Registry.load("config-peers", store=_store(backend)),
            refresh_interval=999.0,
        )
        for _ in range(count)
    ]
    return backend, managers


def test_a_config_write_keeps_what_another_manager_saved_since_its_last_read():
    backend, (stale, other) = _peer_managers(2)
    assert stale.load() == other.load() == {"items": [1]}

    other.set("other", 1)
    other.set("items[]", 2)
    # ``stale`` still holds the revision it loaded; its writes apply to the newer one.
    stale.set("mine", 2)
    stale.set("items[]", 3)

    _, (fresh,) = _peer_managers(1, backend)
    assert fresh.load() == {"items": [1, 2, 3], "other": 1, "mine": 2}

    other.set("late", 3)
    assert stale.remove(version=1) is True
    assert stale.compact(keep_last_k=2) > 0
    other.unset("other")
    stale.unset("items[0]")
    _, (fresh,) = _peer_managers(1, backend)
    assert fresh.load() == {"items": [2, 3], "mine": 2, "late": 3}


def test_concurrent_config_writers_with_their_own_registries_keep_every_value():
    backend, managers = _peer_managers(16)
    for manager in managers:
        manager.load()
    barrier = threading.Barrier(len(managers))

    def write(index):
        barrier.wait()
        managers[index].set(f"k.v{index}", index)
        managers[index].set("items[]", index)

    with ThreadPoolExecutor(max_workers=len(managers)) as pool:
        list(pool.map(write, range(len(managers))))

    _, (fresh,) = _peer_managers(1, backend)
    loaded = fresh.load()
    assert loaded["k"] == {f"v{index}": index for index in range(len(managers))}
    assert sorted(loaded["items"]) == sorted([1, *range(len(managers))])


@pytest.mark.fast
def test_a_merged_write_after_another_manager_reset_the_scope_keeps_its_value():
    # The reset deletes the scope record, so its generation restarts and reached
    # 2 again with other content; a snapshot cached under generation 2 was
    # reused and the next list append dropped the other manager's value.
    backend, (first, second) = _peer_managers(2)
    first.set("items[]", "a1")
    assert first.load() == {"items": [1, "a1"]}

    second.setup(reset=True)
    second.set("items[]", "b1")

    assert first.load(refresh=True) == {"items": [1, "b1"]}
    first.set("items[]", "a2")
    _, (fresh,) = _peer_managers(1, backend)
    assert fresh.load() == {"items": [1, "b1", "a2"]}


@pytest.mark.fast
def test_an_up_to_date_write_reads_the_store_only_to_publish_and_keeps_a_racing_value(monkeypatch):
    # Every write refreshed the store before its compare-and-set, one extra
    # store read per write even with no other writer. The resolved view comes
    # from the revision the write publishes against; a value another process
    # publishes while the view is built makes the compare-and-set fail, and
    # the retry keeps both values.
    backend, (writer, peer) = _peer_managers(2)
    writer.load()
    registry = writer._scope_registry
    refreshes = 0
    real_refresh = registry.refresh

    def counting_refresh():
        nonlocal refreshes
        refreshes += 1
        return real_refresh()

    monkeypatch.setattr(registry, "refresh", counting_refresh, raising=False)
    writer.set("items[]", 2)
    writer.set("plain", 1)
    writer.unset("plain")
    assert refreshes == 0

    real_draft_view = writer._draft_view
    raced = []

    def view_with_a_racing_write(draft, chain, known=None, stored=None):
        if not raced:
            raced.append(True)
            peer.set("items[]", "peer")
        return real_draft_view(draft, chain, known, stored)

    monkeypatch.setattr(writer, "_draft_view", view_with_a_racing_write)
    writer.set("items[]", 3)
    assert refreshes == 1

    _, (fresh,) = _peer_managers(1, backend)
    assert fresh.load() == {"items": [1, 2, "peer", 3]}


def _inject_once(monkeypatch, registry, before_mutate):
    """Run ``before_mutate`` once, just before the first ``mutate`` of ``registry``."""
    real_mutate = registry.mutate
    done = []

    def mutate(change):
        if not done:
            done.append(True)
            before_mutate()
        return real_mutate(change)

    monkeypatch.setattr(registry, "mutate", mutate, raising=False)


@pytest.mark.fast
def test_a_list_write_that_changes_nothing_cannot_lose_a_compare_and_set(monkeypatch):
    # A list set checked that the revision its resolved view came from was
    # still current before its no-op test, so a refresh by another thread in
    # between raised a conflict and the write backed off for up to 300 s
    # although it saved nothing.
    import heavenbase.utils.config.manager as manager_module

    backend, (writer, other) = _peer_managers(2)
    writer.set("items", [1, 2])
    writer.load()
    other.load()
    registry = writer._scope_registry

    def refresh_by_another_thread():
        other.set("unrelated", 1)
        registry.refresh()

    _inject_once(monkeypatch, registry, refresh_by_another_thread)
    monkeypatch.setattr(manager_module, "_COMMIT_DEADLINE", -1.0)
    versions = len(writer.history(limit=100))
    assert writer.set("items[0]", 1) is True

    # Only the other manager's write saved a version.
    _, (fresh,) = _peer_managers(1, backend)
    assert len(fresh.history(limit=100)) == versions + 1
    assert fresh.load() == {"items": [1, 2], "unrelated": 1}


@pytest.mark.fast
def test_setdef_keeps_a_value_another_process_saved_after_its_check(monkeypatch):
    # setdef checked for the value and then called set, outside the
    # compare-and-set, so a value another process saved in between was
    # overwritten with the default.
    backend, (writer, other) = _peer_managers(2)
    writer.load()
    _inject_once(monkeypatch, writer._scope_registry, lambda: other.set("mode", "theirs"))

    assert writer.setdef("mode", "default") is False
    _, (fresh,) = _peer_managers(1, backend)
    assert fresh.get("mode") == "theirs"

    assert writer.setdef("fresh", "default") is True
    assert writer.setdef("fresh", "again") is False
    _, (fresh,) = _peer_managers(1, backend)
    assert fresh.get("fresh") == "default"


def _stale_no_op_cases():
    """Return writes that look like no-ops on a stale revision, each with its setup and expected outcome.

    Each case is ``(prepare, stale_write, other_write, result, check)``:
    ``prepare`` runs on the stale manager, ``other_write`` on a peer that saves
    after the stale manager's last read, and ``check`` reads a fresh manager.
    """
    return {
        "set": (
            lambda m: m.set("k", 1),
            lambda m: m.set("k", 1),
            lambda o: o.set("k", 2),
            True,
            lambda f: f.get("k") == 1,
        ),
        "unset": (
            lambda m: m.unset("u"),
            lambda m: m.unset("u"),
            lambda o: o.set("u", 7),
            True,
            lambda f: f.get("u") is None,
        ),
        "setdef": (
            lambda m: m.set("d", 1),
            lambda m: m.setdef("d", 5),
            lambda o: o.unset("d"),
            True,
            lambda f: f.get("d") == 5,
        ),
        "list-item": (
            lambda m: m.set("items", ["a"]),
            lambda m: m.set("items[0]", "a"),
            lambda o: o.set("items", ["z"]),
            True,
            lambda f: list(f.get("items")) == ["a"],
        ),
        "remove": (
            lambda m: m.load(),
            lambda m: m.remove(scope="peer-test.other"),
            lambda o: o.set("v", 1, scope="peer-test.other"),
            True,
            lambda f: "peer-test.other" not in f.scopes(),
        ),
        "compact": (
            lambda m: m.load(),
            lambda m: m.compact(scope="peer-test.other", keep_last_k=1),
            lambda o: [o.set("v", value, scope="peer-test.other") for value in range(3)],
            2,
            lambda f: len(f.history(scope="peer-test.other", limit=10)) == 1,
        ),
        "init": (
            lambda m: m.init(scope="peer-test.seeded", data={"v": "mine"}),
            lambda m: m.init(scope="peer-test.seeded", data={"v": "mine"}),
            lambda o: o.remove(scope="peer-test.seeded"),
            True,
            lambda f: f.history(scope="peer-test.seeded", limit=1)[0]["data"] == {"v": "mine"},
        ),
        "setup": (
            lambda m: m.load(),
            lambda m: m.setup(),
            lambda o: o.remove(scope="peer-test"),
            True,
            # Read before a fresh manager opens: its first open seeds the base scope itself.
            lambda f: True,
        ),
        "setup-reset": (
            lambda m: m.load(),
            lambda m: m.setup(reset=True),
            lambda o: o.set("v", 1, scope="peer-test.other"),
            True,
            lambda f: "peer-test.other" not in f.scopes(),
        ),
    }


@pytest.mark.fast
@pytest.mark.parametrize("case", sorted(_stale_no_op_cases()))
def test_a_write_that_looks_like_a_no_op_on_a_stale_revision_checks_the_latest_one(case):
    # A write that changed nothing in its draft published nothing, so no
    # compare-and-set checked the revision it decided on: a process that had
    # not yet seen another process's write dropped its own write (set,
    # unset, list items, a global log helper) or answered from the old
    # revision (setdef, remove, compact, init, setup(reset=True)).
    prepare, stale_write, other_write, result, check = _stale_no_op_cases()[case]
    backend, (stale, other) = _peer_managers(2)
    prepare(stale)
    stale.load()
    other.load()
    other_write(other)

    assert stale_write(stale) == result
    assert hb.Registry.load("config-peers", store=_store(backend)).get("peer-test") is not None
    _, (fresh,) = _peer_managers(1, backend)
    assert check(fresh)


@pytest.mark.fast
def test_a_no_op_write_on_the_latest_revision_reads_the_store_once(monkeypatch):
    # The check that a write which changes nothing decided on the latest
    # revision costs one store read, and only that write pays it; it keeps
    # the derived caches, so a read after it reads no store either.
    backend, (writer,) = _peer_managers(1)
    writer.set("k", 1)
    assert writer.get("k") == 1
    registry = writer._scope_registry
    real_refresh = registry.refresh
    refreshes = 0

    def counting_refresh():
        nonlocal refreshes
        refreshes += 1
        return real_refresh()

    monkeypatch.setattr(registry, "refresh", counting_refresh, raising=False)
    versions = len(writer.history(limit=100))
    assert writer.set("k", 1) is True
    assert writer.setdef("k", 2) is False
    assert writer.get("k") == 1
    assert refreshes == 2
    assert len(writer.history(limit=100)) == versions
    writer.set("k", 3)
    assert refreshes == 2


@pytest.mark.fast
@pytest.mark.parametrize(
    "write, expected",
    [
        (lambda m: m.unset("items[1]"), ["x"]),
        (lambda m: m.set("items[1]", "z"), ["x", "z"]),
    ],
    ids=["unset-item", "set-item"],
)
def test_a_list_index_past_the_end_of_a_stale_list_runs_on_the_latest_revision(write, expected):
    # The edit indexed the list in this process's revision, so an item another
    # process added since its last read raised IndexError before any
    # compare-and-set could send the write to the newer revision.
    backend, (stale, other) = _peer_managers(2)
    assert stale.load() == other.load() == {"items": [1]}
    other.set("items", ["x", "y"])

    assert write(stale) is True
    _, (fresh,) = _peer_managers(1, backend)
    assert list(fresh.get("items")) == expected


@pytest.mark.fast
def test_a_list_index_past_the_end_of_the_latest_list_raises_after_one_store_read(monkeypatch):
    _, (writer,) = _peer_managers(1)
    writer.set("items", ["x"])
    registry = writer._scope_registry
    real_refresh = registry.refresh
    refreshes = 0

    def counting_refresh():
        nonlocal refreshes
        refreshes += 1
        return real_refresh()

    monkeypatch.setattr(registry, "refresh", counting_refresh, raising=False)
    with pytest.raises(IndexError):
        writer.unset("items[5]")
    with pytest.raises(IndexError):
        writer.set("items[5]", "z")
    assert refreshes == 2
    assert list(writer.get("items")) == ["x"]


@pytest.mark.fast
def test_a_write_that_reads_resolved_values_resolves_nothing_under_the_registry_lock(monkeypatch):
    # setdef and list writes read the resolved config of the revision they
    # write. Resolving it inside Registry.mutate made a setdef that finds a
    # value about ten times slower than in 0.1.2.2 and held the Registry lock
    # meanwhile; a process holding the latest revision now resolves it before
    # the write, from the snapshot cache when a read already resolved it.
    _, (writer,) = _peer_managers(1)
    writer.set("k", 1)
    writer.set("items", ["x"])
    registry = writer._scope_registry
    real_mutate, real_resolved = registry.mutate, writer._resolved
    inside = False
    resolved = {"inside": 0, "outside": 0}

    def mutate(change):
        nonlocal inside
        inside = True
        try:
            return real_mutate(change)
        finally:
            inside = False

    def counting_resolved(*args, **kwargs):
        resolved["inside" if inside else "outside"] += 1
        return real_resolved(*args, **kwargs)

    monkeypatch.setattr(registry, "mutate", mutate, raising=False)
    monkeypatch.setattr(writer, "_resolved", counting_resolved)
    assert writer.get("k") == 1
    assert writer.setdef("k", 2) is False
    assert writer.setdef("k", 3) is False
    assert resolved == {"inside": 0, "outside": 1}
    assert writer.set("items[]", "y") is True
    assert writer.unset("items[0]") is True
    assert writer.setdef("missing", 4) is True
    assert resolved["inside"] == 0
    assert list(writer.get("items")) == ["y"] and writer.get("missing") == 4


@pytest.mark.fast
def test_a_snapshot_is_never_cached_under_stamps_of_other_content():
    # The stamps came from the generation cache while the layers came from the
    # in-memory records, which another write had refreshed since; the snapshot
    # was cached under the stamps of the old content, so after the other
    # process removed the scope even snapshot(refresh=True) served the removed value.
    backend, (reader, other) = _peer_managers(2)
    with reader.scoped("x"):
        assert reader.load().get("v") is None
    other.set("v", "from-other", scope="peer-test.x")
    reader.set("z", 1, scope="peer-test.y")
    with reader.scoped("x"):
        assert reader.snapshot(overrides={"o": 1}).get("v") == "from-other"

    assert other.remove(scope="peer-test.x") is True
    with reader.scoped("x"):
        assert reader.snapshot(overrides={"o": 1}, refresh=True).get("v") is None
        assert reader.snapshot(refresh=True).get("v") is None


@pytest.mark.fast
@pytest.mark.parametrize("first_write", ["compact", "remove", "setup-reset", "init"])
def test_a_first_write_opens_the_config_without_holding_a_lock_a_context_lock_holder_needs(first_write):
    # remove, compact and setup(reset=True) opened the Registry inside the
    # manager's write lock, and the open takes the Context lock; a thread
    # holding the Context lock and writing config (activate_workspace,
    # deactivate_workspace) waited for the write lock, so both waited forever.
    context_lock = threading.RLock()
    manager = ConfigManager(package="lock-order-test", default={"k": 0})
    registry = hb.Registry[str, dict]("lock-order-config", normalize=manager._scope_key)
    opening = threading.Event()

    def open_registry():
        opening.set()
        with context_lock:
            return registry

    manager._bind_registry_factory(open_registry)
    errors = []

    holding = threading.Event()

    def context_lock_holder():
        try:
            with context_lock:
                holding.set()
                assert opening.wait(10)
                manager.set("k", 1)
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    def first_writer():
        try:
            {
                "compact": lambda: manager.compact(),
                "remove": lambda: manager.remove(version=99),
                "setup-reset": lambda: manager.setup(reset=True),
                "init": lambda: manager.init(scope="lock-order-test.child"),
            }[first_write]()
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    holder = threading.Thread(target=context_lock_holder, daemon=True)
    holder.start()
    assert holding.wait(10)
    writer = threading.Thread(target=first_writer, daemon=True)
    writer.start()
    holder.join(20)
    writer.join(20)

    assert not holder.is_alive() and not writer.is_alive(), "a first config write deadlocked against a Context-lock holder"
    assert errors == []
    assert manager.get("k") in (0, 1)


@pytest.mark.fast
def test_a_config_write_keeps_retrying_past_many_lost_compare_and_sets(monkeypatch):
    # The retry was bounded at 64 attempts, so a writer among many contending
    # processes could lose every one and raise RegistryConflictError.
    import heavenbase.utils.config.manager as manager_module
    from heavenbase.utils.registry import RegistryConflictError

    backend, (writer,) = _peer_managers(1)
    writer.load()
    registry = writer._scope_registry
    real_mutate = registry.mutate
    lost = 0

    def losing_mutate(change):
        nonlocal lost
        if lost < 200:
            lost += 1
            raise RegistryConflictError("lost the compare-and-set")
        return real_mutate(change)

    monkeypatch.setattr(registry, "mutate", losing_mutate, raising=False)
    monkeypatch.setattr(manager_module.random, "uniform", lambda low, high: 0.0)
    writer.set("k", 1)

    assert lost == 200
    _, (fresh,) = _peer_managers(1, backend)
    assert fresh.get("k") == 1

    # Past the deadline the last conflict is raised.
    lost = 0
    monkeypatch.setattr(manager_module, "_COMMIT_DEADLINE", -1.0)
    with pytest.raises(RegistryConflictError):
        writer.set("k", 2)
    assert lost == 1


@pytest.mark.fast
def test_tolerant_config_reads_fall_back_to_packaged_values_after_one_failed_open():
    # Log records and the serialization and parallel helpers must not fail
    # over config, and must not retry a broken home on every call.
    opens = 0

    def broken():
        nonlocal opens
        opens += 1
        raise OSError("unable to open database file")

    manager = ConfigManager(package="tolerant-test", default={"heavenbase": {"log": {"level": "WARNING"}}})
    manager._bind_registry_factory(broken)

    assert manager._get_or_packaged("heavenbase.log.level", "INFO") == "WARNING"
    assert manager._get_or_packaged("heavenbase.log.missing", "INFO") == "INFO"
    assert opens == 1
    with pytest.raises(OSError):
        manager.get("heavenbase.log.level")
    assert opens == 2

    manager._bind_registry(hb.Registry[str, dict]("tolerant-test-config", normalize=manager._scope_key))
    manager.set("heavenbase.log.level", "ERROR")
    assert manager._get_or_packaged("heavenbase.log.level", "INFO") == "ERROR"


def _wrapped_unopenable_database() -> Exception:
    error = RuntimeError("Context open failed")
    error.__cause__ = Exception("(sqlite3.OperationalError) unable to open database file")
    return error


@pytest.mark.fast
@pytest.mark.parametrize(
    "error",
    [
        PermissionError(13, "Permission denied"),
        ValueError("HeavenBase bootstrap version must be 1, got 999"),
        _wrapped_unopenable_database(),
    ],
    ids=["unwritable-home", "invalid-bootstrap", "unopenable-database"],
)
def test_tolerant_config_reads_fall_back_only_for_a_lasting_open_failure(error):
    # Each documented cause falls back to the packaged value and is not retried.
    opens = 0

    def broken():
        nonlocal opens
        opens += 1
        raise error

    manager = ConfigManager(package="tolerant-test", default={"heavenbase": {"entity": {"id": {"hash_length": 42}}}})
    manager._bind_registry_factory(broken)

    assert manager._get_or_packaged("heavenbase.entity.id.hash_length", 30) == 42
    assert manager._set_if_openable("heavenbase.log.level", "ERROR") is False
    assert manager._get_or_packaged("heavenbase.entity.id.hash_length", 30) == 42
    assert opens == 1


@pytest.mark.fast
def test_tolerant_config_reads_raise_and_retry_an_open_failure_that_may_pass():
    # Any error made the fallback sticky, so one busy open fixed the process on
    # packaged values: hash_id then used the packaged length for the rest of the
    # process although the home saved another one.
    from heavenbase.utils.registry import RegistryIntegrityError

    class OperationalError(Exception):
        pass

    failures = [OperationalError("(sqlite3.OperationalError) database is locked"), RegistryIntegrityError("stale revision")]
    opens = 0
    saved = hb.Registry[str, dict]("tolerant-retry-config")

    def factory():
        nonlocal opens
        opens += 1
        if failures:
            raise failures.pop(0)
        return saved

    manager = ConfigManager(package="tolerant-retry", default={"heavenbase": {"entity": {"id": {"hash_length": 42}}}})
    manager._bind_registry_factory(factory)

    with pytest.raises(OperationalError, match="database is locked"):
        manager._get_or_packaged("heavenbase.entity.id.hash_length")
    with pytest.raises(RegistryIntegrityError):
        manager._set_if_openable("heavenbase.entity.id.hash_length", 30)
    assert manager._open_failed is False

    assert manager._set_if_openable("heavenbase.entity.id.hash_length", 30) is True
    assert manager._get_or_packaged("heavenbase.entity.id.hash_length") == 30
    assert opens == 3


@pytest.mark.fast
def test_tolerant_config_reads_raise_an_error_after_a_successful_open():
    manager = ConfigManager(package="tolerant-open", default={"heavenbase": {"log": {"level": "WARNING"}}})
    manager._bind_registry(hb.Registry[str, dict]("tolerant-open-config", normalize=manager._scope_key))
    assert manager._get_or_packaged("heavenbase.log.level") == "WARNING"

    def failing_snapshot(*args, **kwargs):
        raise OSError(13, "Permission denied")

    manager.snapshot = failing_snapshot
    with pytest.raises(OSError):
        manager._get_or_packaged("heavenbase.log.level")
    assert manager._open_failed is False


@pytest.mark.fast
def test_a_config_write_that_changes_nothing_saves_no_version():
    backend, (writer,) = _peer_managers(1)
    writer.set("level", "ERROR")
    history = writer.history(limit=100)
    revision = writer._scope_registry.revision

    writer.set("level", "ERROR")
    writer.set("nested", {"a": [1, {"b": True}]})
    nested = writer.history(limit=100)
    writer.set("nested", {"a": [1, {"b": True}]})

    assert writer.history(limit=100) == nested
    assert len(nested) == len(history) + 1
    assert writer._scope_registry.revision == revision + 1

    # Values that compare equal but differ in type are saved.
    writer.set("nested", {"a": [1.0, {"b": True}]})
    writer.set("flag", 1)
    writer.set("flag", True)
    _, (fresh,) = _peer_managers(1, backend)
    assert to_mutable_data(fresh.get("nested")) == {"a": [1.0, {"b": True}]}
    assert type(fresh.get("nested.a")[0]) is float
    assert fresh.get("flag") is True


@pytest.mark.fast
def test_a_config_write_that_changes_nothing_cannot_lose_a_compare_and_set(monkeypatch):
    # Every write appended a version, so processes that saved the same setting
    # at once (each calling suppress_logs() at startup, for example) all
    # contended for the compare-and-set and waited many seconds in backoff.
    import heavenbase.utils.config.manager as manager_module

    backend, (writer, other) = _peer_managers(2)
    writer.set("suppress", True)
    writer.load()
    other.set("unrelated", 1)

    # ``writer`` publishes against a revision that ``other`` has replaced, as
    # when another process publishes between its refresh and its write.
    monkeypatch.setattr(writer._scope_registry, "refresh", lambda: False, raising=False)
    monkeypatch.setattr(manager_module, "_COMMIT_DEADLINE", -1.0)
    writer.set("suppress", True)

    _, (fresh,) = _peer_managers(1, backend)
    assert fresh.get("suppress") is True
    assert fresh.get("unrelated") == 1


def test_omegaconf_env_and_disabled_cmd(tmp_path, monkeypatch):
    monkeypatch.setenv("HB_CFG_TEST", "ok")
    cm = _cm(tmp_path, default={"value": "${env:HB_CFG_TEST}"})
    assert cm.get("value") == "ok"
    bad = _cm(tmp_path / "bad", default={"value": "${cmd:echo ok}"})
    with pytest.raises(Exception):
        bad.snapshot(policy=InterpolationPolicy(allow_cmd=False))


def test_snapshot_cache_tracks_referenced_environment_values(tmp_path, monkeypatch):
    cm = _cm(tmp_path, default={"value": "${env:HB_DYNAMIC_CONFIG}"})
    monkeypatch.setenv("HB_DYNAMIC_CONFIG", "first")
    first = cm.snapshot()

    monkeypatch.setenv("HB_DYNAMIC_CONFIG", "second")
    second = cm.snapshot()

    assert first.get("value") == "first"
    assert second.get("value") == "second"
    assert second is not first


def test_backend_defaults_are_explicit_not_repaired(tmp_path):
    default = {
        "heavenbase": {
            "backends": {"main": {"type": "sqlite", "database": "main.db"}, "vec": {"type": "inmem"}},
            "backend_defaults": {
                "sql": {"id_lookup": {"batch_size": 500}},
                "lance": {"uri": "file://%/lance", "filter_limit": 10000},
            },
        }
    }
    cm = ConfigManager(
        package="heavenbase",
        scope="heavenbase",
        root=str(tmp_path),
        default=default,
    )

    assert cm.get("heavenbase.backends.main.database") == "main.db"
    assert cm.get("heavenbase.backend_defaults.lance.uri") == "file://%/lance"
    assert cm.get("heavenbase.backend_defaults.sql.id_lookup.batch_size") == 500


def test_database_token_normalization_ignores_schema_type_dicts(tmp_path):
    default = {
        "heavenbase": {
            "workspace_registry": {
                "items": {
                    "workspace": {
                        "demo": {
                            "manifest": {
                                "entities": [
                                    {
                                        "entity_id": "demo-note",
                                        "fields": {"object_id": {"type": {"type": "identifier"}}, "title": {"type": {"type": "short-text"}}},
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        }
    }
    cm = ConfigManager(package="heavenbase", scope="heavenbase", root=str(tmp_path), default=default)

    assert cm.get("heavenbase.workspace_registry.items.workspace.demo.manifest.entities[0].fields.object_id.type.type") == "identifier"


def test_dict_path_utilities_are_exported():
    data = {}

    assert dset(data, "a.b.c", 42)
    assert dset(data, "items[]", "first")
    assert dset(data, "items[1]", "third")
    assert dset(data, "items[1+]", "second")
    assert dget(data, "a.b.c") == 42
    assert dget(data, "items[-1]") == "third"
    assert not dsetdef(data, "a.b.c", 100)
    assert dsetdef(data, "a.b.d", 100)
    assert dunset(data, "items[1]")
    assert data["items"] == ["first", "third"]

    flat = dict(dflat(data))
    assert flat["a.b.c"] == 42
    assert dunflat({"x.y[0]": 1, "x.y[1]": 2}) == {"x": {"y": [1, 2]}}
    assert dmerge([{"a": {"e": 1}}, {"a": {"__HB_OVERWRITE__": True, "f": 2}}]) == {"a": {"f": 2}}


def _plain_cm(default):
    return ConfigManager(package="heavenbase-test", scope="heavenbase", default=default, refresh_interval=999.0)


@pytest.mark.fast
@pytest.mark.parametrize(
    ("write", "expected"),
    [
        (lambda cm: cm.set("x.items[]", "c"), ["${env:HBT_LIST_SECRET}", "b", "c"]),
        (lambda cm: cm.set("x.items[1+]", "c"), ["${env:HBT_LIST_SECRET}", "c", "b"]),
        (lambda cm: cm.set("x.items[1]", "c"), ["${env:HBT_LIST_SECRET}", "c"]),
        (lambda cm: cm.unset("x.items[1]"), ["${env:HBT_LIST_SECRET}"]),
        (lambda cm: cm.setdef("x.more[]", "c") and cm.set("x.items[]", "c"), ["${env:HBT_LIST_SECRET}", "b", "c"]),
    ],
)
def test_a_list_write_saves_an_interpolated_item_as_written_never_its_value(monkeypatch, write, expected):
    # A list write rebuilt the list from the resolved config, so it saved the
    # value of every ``${env:...}`` item in the list as plain text, such as a
    # credential in the packaged local-lts search hosts, and lost the reference.
    monkeypatch.setenv("HBT_LIST_SECRET", "s3cr3t")
    cm = _plain_cm({"x": {"items": ["${env:HBT_LIST_SECRET}", "b"]}})
    assert cm.get("x.items")[0] == "s3cr3t"

    write(cm)

    assert cm.layer()["x"]["items"] == expected
    assert "s3cr3t" not in repr(cm._scope_registry.get("heavenbase"))
    assert list(cm.get("x.items")) == ["s3cr3t", *expected[1:]]
    monkeypatch.setenv("HBT_LIST_SECRET", "rotated")
    assert cm.get("x.items")[0] == "rotated"


@pytest.mark.fast
def test_a_list_write_to_another_scope_edits_the_list_that_scope_reads():
    # A list write read the list of the current scope chain whatever scope it
    # wrote, so a write to the base scope from a child scope replaced the base
    # scope's own items with the child's.
    cm = _plain_cm({"x": {"items": ["a", "b"]}})
    with cm.scoped("child"):
        cm.set("x.items", ["child-only"])
        assert cm.set("x.items[]", "d", scope="heavenbase") is True
        assert cm.unset("x.items[0]", scope="heavenbase") is True
        assert cm.set("x.items[0+]", "z", scope="heavenbase") is True
        assert list(cm.get("x.items")) == ["child-only"]
    assert cm.layer(scope="heavenbase")["x"]["items"] == ["z", "b", "d"]
    assert list(cm.get("x.items")) == ["z", "b", "d"]

    assert cm.set("x.items[]", "e", scope="heavenbase.child") is True
    assert cm.set("x.items[]", "f", scope="heavenbase.other") is True
    with cm.scoped("child"):
        assert list(cm.get("x.items")) == ["child-only", "e"]
    with cm.scoped("other"):
        assert list(cm.get("x.items")) == ["z", "b", "d", "f"]
    assert list(cm.get("x.items")) == ["z", "b", "d"]


@pytest.mark.fast
def test_a_list_write_follows_an_interpolation_that_holds_the_list_to_its_stored_items(monkeypatch):
    # A list stored as one interpolation, or under one, was read resolved, so
    # a list write saved the value of each ``${env:...}`` item it held.
    monkeypatch.setenv("HBT_LIST_SECRET", "s3cr3t")
    cm = _plain_cm(
        {
            "shared": {"hosts": ["${env:HBT_LIST_SECRET}"]},
            "svc": {"hosts": "${shared.hosts}"},
            "src": {"items": ["p"]},
            "x": {"items": "${src.items}"},
            "w": {"items": "${src.items}"},
        }
    )
    assert cm.set("svc.hosts[]", "h2") is True
    assert cm.layer()["svc"]["hosts"] == ["${env:HBT_LIST_SECRET}", "h2"]
    assert "s3cr3t" not in repr(cm._scope_registry.get("heavenbase"))
    assert list(cm.get("svc.hosts")) == ["s3cr3t", "h2"]
    assert cm.unset("svc.hosts[1]") is True and cm.layer()["svc"]["hosts"] == ["${env:HBT_LIST_SECRET}"]

    assert cm.set("x.items[]", "q") is True
    assert cm.layer()["x"]["items"] == ["p", "q"]
    assert list(cm.get("src.items")) == ["p"]
    with cm.scoped("child"):
        cm.set("src.items", ["from-child"])
        assert cm.set("x.items[]", "s", scope="heavenbase") is True
        assert cm.set("w.items[]", "t", scope="heavenbase") is True
        assert list(cm.get("w.items")) == ["p", "t"]
    assert list(cm.get("x.items")) == ["p", "q", "s"]
    assert cm.layer()["w"]["items"] == ["p", "t"]


@pytest.mark.fast
def test_a_write_below_an_interpolation_keeps_what_the_interpolation_holds(monkeypatch):
    # A write below a node held by an interpolation, such as ``y: ${src}``,
    # saved a mapping holding only the written key, which replaced the
    # interpolation and lost every other key it held; an unset left ``{}``.
    monkeypatch.setenv("HBT_LIST_SECRET", "s3cr3t")
    source = {"items": ["p"], "host": "h", "token": "${env:HBT_LIST_SECRET}"}
    for write, expected in [
        (lambda cm: cm.set("y.items[]", "r"), {"items": ["p", "r"], "host": "h", "token": "${env:HBT_LIST_SECRET}"}),
        (lambda cm: cm.set("y.port", 5), {"items": ["p"], "host": "h", "token": "${env:HBT_LIST_SECRET}", "port": 5}),
        (lambda cm: cm.unset("y.host"), {"items": ["p"], "token": "${env:HBT_LIST_SECRET}"}),
    ]:
        cm = _plain_cm({"src": dict(source), "y": "${src}"})
        assert write(cm) is True
        assert cm.layer()["y"] == expected
        assert "s3cr3t" not in repr(cm._scope_registry.get("heavenbase"))
        assert dict(cm.get("src")) == {"items": ("p",), "host": "h", "token": "s3cr3t"}
        resolved = {key: (value if key != "token" else "s3cr3t") for key, value in expected.items()}
        assert to_mutable_data(cm.get("y")) == resolved

    # A mapping replacing an interpolation that the written layer itself saved
    # replaces the mapping below it rather than extending it.
    cm = _plain_cm({"src": {"host": "h"}, "y": {"kept": 1}})
    with cm.scoped("child"):
        assert cm.set("y", "${src}") is True
        assert cm.set("y.port", 5) is True
        assert to_mutable_data(cm.get("y")) == {"host": "h", "port": 5}
    assert to_mutable_data(cm.get("y")) == {"kept": 1}


@pytest.mark.fast
def test_a_list_write_below_an_indexed_interpolated_list_keeps_its_items():
    # ``servers[0]`` under ``servers: ${src.servers}``: the write found no
    # stored list, so an append saved only the new item and a replace raised.
    default = {"src": {"servers": [{"hosts": ["h1", "h3"]}, {"hosts": ["k"]}]}, "servers": "${src.servers}"}
    cm = _plain_cm(default)
    assert cm.set("servers[0].hosts[]", "h2") is True
    assert cm.layer()["servers"] == [{"hosts": ["h1", "h3", "h2"]}, {"hosts": ["k"]}]
    cm = _plain_cm(default)
    assert cm.set("servers[0].hosts[1]", "h2") is True
    assert to_mutable_data(cm.get("servers")) == [{"hosts": ["h1", "h2"]}, {"hosts": ["k"]}]
    assert to_mutable_data(cm.get("src.servers")) == default["src"]["servers"]


@pytest.mark.fast
def test_a_write_below_a_list_item_keeps_the_other_items_and_keys():
    # The written layer's list replaces the list a read merges, so a write
    # below an item of a list that only a lower layer saved kept nothing else,
    # and an unset saved its marker inside the list, where reads returned it.
    cm = _plain_cm({"servers": [{"host": "a", "port": 0}, {"host": "b"}]})
    with cm.scoped("child"):
        assert cm.set("servers[0].port", 1) is True
        assert to_mutable_data(cm.get("servers")) == [{"host": "a", "port": 1}, {"host": "b"}]
        assert cm.unset("servers[0].host") is True
        assert to_mutable_data(cm.get("servers")) == [{"port": 1}, {"host": "b"}]
        assert cm.unset("servers[1].missing") is False
        assert cm.unset("servers[5].host") is False
        assert to_mutable_data(cm.get("servers")) == [{"port": 1}, {"host": "b"}]
    assert to_mutable_data(cm.get("servers")) == [{"host": "a", "port": 0}, {"host": "b"}]
    assert cm.unset("servers[0].port") is True
    assert to_mutable_data(cm.get("servers")) == [{"host": "a"}, {"host": "b"}]


@pytest.mark.fast
def test_a_relative_interpolation_copied_by_a_list_write_names_the_same_node():
    cm = _plain_cm({"src": {"host": "h", "items": ["${..host}", "x"]}, "dst": {"host": "other", "items": "${src.items}"}})
    assert list(cm.get("dst.items")) == ["h", "x"]
    assert cm.set("dst.items[]", "y") is True
    assert cm.layer()["dst"]["items"] == ["${src.host}", "x", "y"]
    assert list(cm.get("dst.items")) == ["h", "x", "y"]


@pytest.mark.fast
def test_a_list_write_to_a_value_that_is_not_a_list_raises_and_none_starts_a_list(monkeypatch):
    # A string was split into characters, so a list write at ``${env:...}``
    # saved the variable's value one character per item.
    monkeypatch.setenv("HBT_LIST_SECRET", "s3cr3t")
    cm = _plain_cm({"name": "abc", "x": {"items": "${env:HBT_LIST_SECRET}"}, "empty": None})
    with pytest.raises(TypeError, match="not a list"):
        cm.set("name[]", "d")
    with pytest.raises(TypeError, match="not a list"):
        cm.set("x.items[]", "d")
    assert "s3cr3t" not in repr(cm._scope_registry.get("heavenbase"))
    assert cm.set("empty[]", "d") is True
    assert list(cm.get("empty")) == ["d"]


@pytest.mark.fast
def test_snapshot_overrides_that_compare_equal_but_differ_in_type_get_their_own_snapshot():
    cm = _plain_cm({"y": 1})
    assert cm.snapshot(overrides={"flag": True}).get("flag") is True
    flag = cm.snapshot(overrides={"flag": 1}).get("flag")
    assert flag == 1 and type(flag) is int
    assert type(cm.snapshot(overrides={"ratio": 1}).get("ratio")) is int
    assert type(cm.snapshot(overrides={"ratio": 1.0}).get("ratio")) is float
    assert dict(cm.snapshot(overrides={"o": {"k": 1}}).get("o")) == {"k": 1}
    assert cm.snapshot(overrides={"o": [["k", 1]]}).get("o") == (("k", 1),)
    assert cm.snapshot(overrides={"o": {"k": 1}}) is cm.snapshot(overrides={"o": {"k": 1}})


@pytest.mark.fast
def test_config_reads_keep_working_after_a_mapping_with_mixed_key_types_is_saved():
    # Snapshot stamps digest each stored layer, and the digest sorted mapping
    # keys, so one mapping with keys of different types (such as ``404`` beside
    # ``"default"``) made every later read raise TypeError; 0.1.2.2 read it back.
    cm = _plain_cm({"y": 1})
    codes = {404: "missing", "default": "x"}
    assert cm.set("codes", codes) is True
    assert dict(cm.get("codes")) == codes
    assert cm.get("y") == 1
    assert cm.snapshot(refresh=True).get("y") == 1
    assert dict(cm.snapshot(overrides={"o": {1: "a", "b": 2}}).get("o")) == {1: "a", "b": 2}
    with cm.scoped("child"):
        assert cm.set("more", {2.5: "f", "s": None}) is True
        assert cm.get("codes.default") == "x" and dict(cm.get("more")) == {2.5: "f", "s": None}


@pytest.mark.fast
def test_a_copied_resolver_that_reads_where_it_is_stored_keeps_its_value():
    # ``${oc.select:..host}`` selects relative to where it is stored, so the
    # copy a list write saved changed the value of an item already in the list.
    cm = _plain_cm({"src": {"host": "h", "items": ["${oc.select:..host,zz}", "x"]}, "dst": {"host": "other", "items": "${src.items}"}})
    assert list(cm.get("dst.items")) == ["h", "x"]
    assert cm.set("dst.items[]", "y") is True
    assert cm.layer()["dst"]["items"] == ["${src.items[0]}", "x", "y"]
    assert list(cm.get("dst.items")) == ["h", "x", "y"]


@pytest.mark.fast
def test_a_write_below_a_node_a_resolver_yields_raises_instead_of_saving_its_secrets(monkeypatch):
    # Such a node was read resolved and saved whole, ``${env:...}`` values
    # included; 0.1.2.2 saved only the written key and lost the other keys.
    monkeypatch.setenv("HBT_LIST_SECRET", "s3cr3t")
    default = {
        "y": "${oc.create:{a: 1, t: '${env:HBT_LIST_SECRET}'}}",
        "src": {"tok": "${env:HBT_LIST_SECRET}", "items": ["p"]},
        "z": "${oc.select:src,null}",
        "w": "${oc.create:${src}}",
    }
    writes = [
        lambda cm: cm.set("y.port", 1),
        lambda cm: cm.unset("y.a"),
        lambda cm: cm.set("z.port", 1),
        lambda cm: cm.set("z.items[]", "q"),
        lambda cm: cm.unset("z.items[0]"),
        lambda cm: cm.set("w.port", 1),
    ]
    for write in writes:
        cm = _plain_cm(default)
        before = cm.layer()
        with pytest.raises(ValueError, match="not a plain node reference"):
            write(cm)
        assert cm.layer() == before
        assert "s3cr3t" not in repr(cm._scope_registry.get("heavenbase"))
    cm = _plain_cm(default)
    assert cm.set("y", {"a": 2}) is True and dict(cm.get("y")) == {"a": 2}


@pytest.mark.fast
def test_a_write_below_an_interpolation_that_yields_no_container_resolves_nothing_else():
    # The write resolved the whole config to learn what the interpolation held,
    # so an unrelated interpolation that fails made the write raise.
    default = {"bad": "${nope}", "src": {"host": "h"}, "y": "${src.host}", "s": "pre ${oc.env:HBT_UNSET_VAR,d}", "r": "${oc.env:HBT_UNSET_VAR,d}"}
    cm = _plain_cm(default)
    for path in ("y.port", "s.port", "r.port"):
        assert cm.set(path, 1) is True
    assert {key: cm.layer()[key] for key in ("y", "s", "r")} == {"y": {"port": 1}, "s": {"port": 1}, "r": {"port": 1}}


@pytest.mark.fast
def test_a_write_follows_a_node_interpolation_with_any_key_omegaconf_accepts(monkeypatch):
    # Only ``[A-Za-z0-9_-]`` keys were followed, so a write below ``${服务.db}``
    # saved the resolved node, ``${env:...}`` values included.
    monkeypatch.setenv("HBT_LIST_SECRET", "s3cr3t")
    secret = "${env:HBT_LIST_SECRET}"
    cases = [
        ({"服务": {"db": {"tok": secret, "host": "h"}}, "y": "${服务.db}"}, {"tok": secret, "host": "h", "port": 1}),
        ({"src": {"db@main": {"tok": secret}}, "y": "${src.db@main}"}, {"tok": secret, "port": 1}),
        ({"src": {"db": {"tok": secret}}, "y": "${ src[db] }"}, {"tok": secret, "port": 1}),
        ({"src": {"my key": {"tok": secret, "sib": "${.tok}"}}, "y": "${src}"}, {"my key": {"tok": secret, "sib": "${.tok}"}, "port": 1}),
    ]
    for default, expected in cases:
        cm = _plain_cm(default)
        assert cm.set("y.port", 1) is True
        assert cm.layer()["y"] == expected
        assert "s3cr3t" not in repr(cm._scope_registry.get("heavenbase"))
        assert "s3cr3t" in repr(to_mutable_data(cm.get("y"))) and cm.get("y.port") == 1
    cm = _plain_cm({"共享": {"hosts": [secret]}, "svc": {"hosts": "${共享.hosts}"}})
    assert cm.set("svc.hosts[]", "h2") is True
    assert cm.layer()["svc"]["hosts"] == [secret, "h2"]
    assert "s3cr3t" not in repr(cm._scope_registry.get("heavenbase"))
    assert list(cm.get("svc.hosts")) == ["s3cr3t", "h2"]


@pytest.mark.fast
def test_a_copy_names_a_node_outside_it_relatively_when_its_absolute_path_cannot_be_written():
    cm = _plain_cm({"a b": {"host": "h", "items": ["${..host}", "${oc.select:..host,zz}"], "mirror": "${.items}"}})
    assert cm.set("a b.mirror[]", "x") is True
    assert cm.layer()["a b"]["mirror"] == ["${..host}", "${..items[1]}", "x"]
    assert list(cm.get("a b.mirror")) == ["h", "h", "x"]
    # A node no interpolation can name, here below an integer key, cannot be
    # copied without changing a value, so the write raises.
    cm = _plain_cm({"src": {"m": {5: {"k": "${oc.select:..b,z}"}, "b": 1}}, "y": "${src.m}"})
    before = cm.layer()
    with pytest.raises(ValueError, match="would change"):
        cm.set("y.port", 1)
    assert cm.layer() == before
