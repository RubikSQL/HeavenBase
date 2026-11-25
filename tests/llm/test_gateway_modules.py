"""Context authority and module-folder contracts for LLM gateways."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import gc
import multiprocessing
import os
from pathlib import Path
import sys
import threading
import types
import weakref

import pytest
import yaml

import heavenbase as hb
from heavenbase.modules import python_executable_ref
from heavenbase.llm.cache import LLMCache
from heavenbase.llm import base as llm_base
from heavenbase.llm.gateway import _GATEWAY_CACHE
from heavenbase.llm.gateways import _protocols
from heavenbase.llm.repair import LLMToolCallRepair
from heavenbase.utils.config import ConfigManager

pytestmark = pytest.mark.fast


def _bootstrap(root: Path) -> dict:
    return {
        "version": 1,
        "root": str(root),
        "workspace": "default",
        "backend": {
            "type": "inmem",
            "module": "heavenbase.backends.inmem.backend",
            "name": "system",
        },
        "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
    }


def _context(root: Path) -> hb.Context:
    manager = ConfigManager(root=str(root), setup=False)
    return hb.Context.load(_bootstrap(root), config=manager)


def _first_compile(spec, gateway, override):
    return {"gateway": gateway, "model": spec.model_id, "args": dict(override)}


def _second_compile(spec, gateway, override):
    return {"gateway": gateway, "model": spec.model_id, "args": dict(override)}


def _first_chat(spec, messages, stream=False, **kwargs):
    return {"choices": [{"message": {"role": "assistant", "content": "first-context"}}], "usage": {}}


def _second_chat(spec, messages, stream=False, **kwargs):
    return {"choices": [{"message": {"role": "assistant", "content": "second-context"}}], "usage": {}}


def _updated_compile(spec, gateway, override):
    return {"gateway": gateway, "model": spec.model_id, "args": dict(override), "generation": "updated"}


class _ProbeCatalog:
    publication = 1


class _ProbeResolver:
    def __init__(self):
        self.catalog = _ProbeCatalog()
        self.row = {
            "kind": "llm_gateway",
            "identifier": "gc-probe",
            "aliases": (),
            "publication": 1,
            "revision": 1,
            "fingerprint": "gc-probe-v1",
        }

    def inspect(self, kind=None, identifier=None, *, include_inactive=False):
        del include_inactive
        if kind not in (None, "llm_gateway") or identifier not in (None, "gc-probe"):
            return ()
        return (dict(self.row),)

    def resolve(self, kind, identifier):
        if (kind, identifier) != ("llm_gateway", "gc-probe"):
            raise KeyError((kind, identifier))
        return {
            "compiler": "semantic/v1",
            "endpoints": ["openai"],
            "compile": python_executable_ref(_first_compile, label="GC probe compiler"),
        }

    def resolve_executable(self, reference, *, owner=None):
        assert owner is not None
        if reference["target"].endswith(":_first_compile"):
            return _first_compile
        raise KeyError(reference["target"])


def test_bundled_gateway_folders_are_open_module_family_records():
    gateway_root = Path(hb.__file__).resolve().parent / "llm" / "gateways"
    expected = {"anthropic", "bifrost", "litellm", "mock", "openai", "portkey"}
    descriptors = {path.parent.name: yaml.safe_load(path.read_text(encoding="utf-8")) for path in gateway_root.glob("*/meta.yaml")}

    assert set(descriptors) == expected
    for identifier, manifest in descriptors.items():
        assert manifest["coordinate"] == f"heavenbase/llm/gateways/{identifier}"
        assert len(manifest["items"]) == 1
        item = manifest["items"][0]
        assert (item["kind"], item["identifier"], item["source"], item["target"]) == ("llm_gateway", identifier, "inline", "definition")
        compile_ref = item["meta"]["definition"]["compile"]
        assert compile_ref == {
            "kind": "heavenbase.executable",
            "version": 1,
            "source": "python-import",
            "target": f"heavenbase.llm.gateways.{identifier}.runtime:compile",
        }

    rows = hb.DEFAULT_CONTEXT.modules().inspect("llm_gateway")
    assert {row["identifier"] for row in rows} >= expected
    gateway = hb.LLMGateway.load("mock")
    with pytest.raises(AttributeError, match="immutable"):
        gateway.compiler = "mutated/v1"


def test_programmatic_gateways_and_runtime_caches_are_context_isolated(tmp_path):
    first = _context(tmp_path / "first")
    second = _context(tmp_path / "second")
    try:
        first._setup_modules()
        second._setup_modules()
        hb.LLMGateway("context-probe", _first_compile, chat=_first_chat).register(resolver=first.modules())
        hb.LLMGateway("context-probe", _second_compile, chat=_second_chat).register(resolver=second.modules())
        first_record = first.modules().inspect("llm_gateway", "context-probe")[0]["record"]

        first_gateway = hb.LLMGateway.load("context-probe", resolver=first.modules())
        second_gateway = hb.LLMGateway.load("context-probe", resolver=second.modules())
        first_llm = hb.LLM(
            model="mock",
            provider="mock",
            gateway="context-probe",
            endpoint="openai",
            context=first,
            cache=False,
            stream_chat=False,
        )
        second_llm = hb.LLM(
            model="mock",
            provider="mock",
            gateway="context-probe",
            endpoint="openai",
            context=second,
            cache=False,
            stream_chat=False,
        )

        assert first_gateway is not second_gateway
        assert first_record["meta"]["definition"]["compile"]["source"] == "python-import"
        assert first_gateway.fn is _first_compile
        assert second_gateway.fn is _second_compile
        assert first_llm.chat("hello") == "first-context"
        assert second_llm.chat("hello") == "second-context"
        assert first_llm.spec.resolver is first.modules()
        assert second_llm.spec.resolver is second.modules()
        assert first_llm.spec.engine.manager is first.config
        assert second_llm.spec.engine.manager is second.config
        first_cache = LLMCache.current(config_manager=first.config)
        second_cache = LLMCache.current(config_manager=second.config)
        assert first_cache is not second_cache
        assert first_llm.spec.hash_key() != second_llm.spec.hash_key()
        assert (
            first_cache.text_key(first_llm.spec, [{"role": "user", "content": "same"}], {})["spec_key"]
            != second_cache.text_key(
                second_llm.spec,
                [{"role": "user", "content": "same"}],
                {},
            )["spec_key"]
        )
        assert LLMToolCallRepair.current(config_manager=first.config) is not LLMToolCallRepair.current(config_manager=second.config)
        assert LLMToolCallRepair.current(config_manager=first.config) is LLMToolCallRepair.current(config_manager=first.config)
        LLMCache.reset(config_manager=first.config)
        assert LLMCache.current(config_manager=first.config) is not first_cache
        assert LLMCache.current(config_manager=second.config) is second_cache
    finally:
        LLMCache.reset()
        LLMToolCallRepair.reset()
        first.close()
        second.close()


def test_context_services_and_gateway_views_do_not_retain_authorities(tmp_path):
    LLMCache.reset()
    LLMToolCallRepair.reset()
    manager = ConfigManager(root=str(tmp_path / "weak-services"), setup=False)
    cache = LLMCache.current(config_manager=manager)
    repair = LLMToolCallRepair.current(config_manager=manager)
    assert LLMCache.current(config_manager=manager) is cache
    assert LLMToolCallRepair.current(config_manager=manager) is repair

    manager_ref = weakref.ref(manager)
    cache_ref = weakref.ref(cache)
    repair_ref = weakref.ref(repair)
    del cache, repair, manager
    gc.collect()

    assert manager_ref() is None
    assert cache_ref() is None
    assert repair_ref() is None

    baseline = len(_GATEWAY_CACHE)
    resolver = _ProbeResolver()
    gateway = hb.LLMGateway.load("gc-probe", resolver=resolver)
    assert gateway.resolver is resolver
    assert len(_GATEWAY_CACHE) == baseline + 1
    resolver_ref = weakref.ref(resolver)
    gateway_ref = weakref.ref(gateway)
    del gateway, resolver
    gc.collect()

    assert resolver_ref() is None
    assert gateway_ref() is None
    assert len(_GATEWAY_CACHE) == baseline


def test_cache_persistence_fails_closed_without_a_context(tmp_path):
    """A standalone manager must never redirect cache rows to DEFAULT_CONTEXT."""
    manager = ConfigManager(root=str(tmp_path / "standalone-cache"), setup=False)
    cache = LLMCache(config_manager=manager)

    assert cache.context is None
    with pytest.raises(RuntimeError, match="requires a Context-bound ConfigManager"):
        _ = cache.workspace


def test_client_and_image_runtime_state_include_context_authority(tmp_path, monkeypatch):
    first = _context(tmp_path / "client-first")
    second = _context(tmp_path / "client-second")
    try:
        first._setup_modules()
        second._setup_modules()
        first.config.set("heavenbase.llm.image_url_timeout", 3)
        second.config.set("heavenbase.llm.image_url_timeout", 7)
        first_spec = hb.LLMEngine(context=first).resolve({"model": "haiku", "provider": "anthropic", "gateway": "anthropic", "api_key": "same-key"})
        second_spec = hb.LLMEngine(context=second).resolve({"model": "haiku", "provider": "anthropic", "gateway": "anthropic", "api_key": "same-key"})
        created = []

        class FakeAnthropic:
            def __init__(self, **kwargs):
                created.append(kwargs)
                self.closed = False

            def close(self):
                self.closed = True

        monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=FakeAnthropic, AsyncAnthropic=FakeAnthropic))
        _protocols._ANTHROPIC_CLIENTS.clear()
        first_client, _ = _protocols._anthropic_client(first_spec)
        second_client, _ = _protocols._anthropic_client(second_spec)

        assert first_client is not second_client
        assert _protocols._anthropic_client(first_spec)[0] is first_client
        assert len(created) == 2
        assert first_spec.authority_key != second_spec.authority_key
        assert llm_base._image_inputs("https://example.test/image.png", config_manager=first.config)[0]._url_timeout == 3
        assert llm_base._image_inputs("https://example.test/image.png", config_manager=second.config)[0]._url_timeout == 7
        first.close()
        assert first_client.closed
        assert not second_client.closed
        with pytest.raises(RuntimeError, match="retired"):
            _protocols._anthropic_client(first_spec)
        assert len(created) == 2
        assert _protocols._anthropic_client(second_spec)[0] is second_client
    finally:
        _protocols.dispose_llm_clients()
        first.close()
        second.close()


@pytest.mark.slow
@pytest.mark.parametrize("workspace", ["separate-cache", "llm-cache"])
def test_llm_cache_drop_preserves_independent_in_memory_cache(tmp_path, workspace):
    with closing(_context(tmp_path / "cache-drop")) as context:
        registered = LLMCache.current(config_manager=context.config)
        backend = {"main": {"type": "inmem"}}
        registered.config["backend"] = backend
        independent = LLMCache.from_dict({"workspace": workspace, "backend": backend}, config_manager=context.config)
        key = {"object_id": "seed-row", "spec_key": "spec", "request_key": "request", "args": {}}
        for cache in (registered, independent):
            cache.set_text(key, [], {"text": "cached"}, [], policy="deterministic")
            assert cache.get_text(key)["response"] == {"text": "cached"}
        opened = independent.workspace

        LLMCache.reset(drop=True, config_manager=context.config)

        kept_workspace = independent.workspace is opened
        kept_row = independent.get_text(key)
        assert (kept_workspace, kept_row is not None) == (True, True)
        assert kept_row["response"] == {"text": "cached"}
        assert registered.get_text(key) is None


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="requires fork")
def test_sdk_client_build_in_parent_does_not_block_forked_child(tmp_path, monkeypatch):
    context = _context(tmp_path / "sdk-fork")
    context._setup_modules()
    engine = hb.LLMEngine(context=context)
    options = {"model": "text-embedding-3-small", "provider": "openai", "gateway": "openai"}
    spec = engine.resolve({**options, "api_key": "test-key"})
    warm_spec = engine.resolve({**options, "api_key": "test-warm-key"})
    parent_pid = os.getpid()
    entered = threading.Event()
    release = threading.Event()
    parent_clients = []

    class LocalOpenAI:
        def __init__(self, **kwargs):
            self.pid = os.getpid()
            if self.pid == parent_pid and kwargs["api_key"] == "test-key":
                entered.set()
                assert release.wait(30)

    def build_in_child():
        warm_client, _ = _protocols._openai_client(warm_spec, "openai")
        assert warm_client.pid == os.getpid()
        client, _ = _protocols._openai_client(spec, "openai")
        assert client.pid == os.getpid()
        assert _protocols._openai_client(spec, "openai")[0] is client

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=LocalOpenAI))
    assert _protocols._openai_client(warm_spec, "openai")[0].pid == parent_pid
    builder = threading.Thread(target=lambda: parent_clients.append(_protocols._openai_client(spec, "openai")[0]), daemon=True)
    child = multiprocessing.get_context("fork").Process(target=build_in_child)
    builder.start()
    try:
        assert entered.wait(10)
        child.start()
        child.join(5)
        assert child.exitcode == 0, "forked child could not build its own SDK client while the parent was building"
    finally:
        if child.pid is not None:
            if child.is_alive():
                child.kill()
                child.join(5)
            child.close()
        release.set()
        builder.join(10)
        context.close()
    assert len(parent_clients) == 1 and parent_clients[0].pid == parent_pid


def test_llm_rejects_cache_and_repair_services_from_another_context(tmp_path):
    first = _context(tmp_path / "service-first")
    second = _context(tmp_path / "service-second")
    try:
        first._setup_modules()
        second._setup_modules()
        cache = LLMCache.current(config_manager=first.config)
        repair = LLMToolCallRepair.current(config_manager=first.config)
        route = {
            "model": "mock",
            "provider": "mock",
            "gateway": "mock",
            "endpoint": "openai",
            "context": second,
        }

        with pytest.raises(ValueError, match="cache and engine belong to different Contexts"):
            hb.LLM(**route, cache=cache)
        with pytest.raises(ValueError, match="repair and engine belong to different Contexts"):
            hb.LLM(**route, cache=False, tool_call_repair=repair)
    finally:
        LLMCache.reset()
        LLMToolCallRepair.reset()
        first.close()
        second.close()


def test_open_cache_workspace_does_not_root_its_context(tmp_path):
    LLMCache.reset()
    context = _context(tmp_path / "cache-cycle")
    context._setup_modules()
    cache = LLMCache.current(config_manager=context.config)
    cache.config["backend"] = {"main": {"type": "inmem"}}
    workspace = cache.workspace
    context_ref = weakref.ref(context)
    manager_ref = weakref.ref(context.config)
    cache_ref = weakref.ref(cache)

    del workspace, cache, context
    gc.collect()

    assert context_ref() is None
    assert manager_ref() is None
    assert cache_ref() is None


@pytest.mark.slow
def test_cache_survives_installing_its_context_as_the_default(tmp_path, home_env):
    """A Context used before ``set_default_context`` keeps one cache, one store and a complete scoped reset."""
    from heavenbase.utils import cmd

    home = tmp_path / "home"
    (home / ".heavenbase").mkdir(parents=True)
    (home / ".heavenbase" / "bootstrap.yaml").write_text("version: 999\n", encoding="utf-8")  # must never be read
    script = """
import gc
import sys
import weakref

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
from heavenbase.context import set_default_context
from heavenbase.llm.cache import LLMCache
from heavenbase.utils.config import ConfigManager

bootstrap = {
    "version": 1,
    "root": sys.argv[2],
    "workspace": "default",
    "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
    "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
}
context = hb.Context.load(bootstrap, config=ConfigManager(root=sys.argv[2], setup=False))
first = LLMCache.current(config_manager=context.config)
first.config["backend"] = {"main": {"type": "inmem"}}
workspace = first.workspace
store = first._store
stores = len(context._hidden_stores)
assert set_default_context(context) is context
assert LLMCache.current(config_manager=context.config) is first, "installing the default split the cache"
assert LLMCache.current() is first
assert first.workspace is workspace and len(context._hidden_stores) == stores
LLMCache.reset(config_manager=context.config)
assert store.retired and first._store is None, "a scoped reset left the cache store open"
again = LLMCache.current()
assert again is not first and LLMCache.current(config_manager=context.config) is again
pinned = weakref.ref(again)
del again
gc.collect()
kept = pinned()
assert kept is not None and LLMCache.current() is kept, "the default Context's cache was not kept alive"
LLMCache.reset(config_manager=context.config)
assert LLMCache.current(config_manager=context.config) is not kept
context.close()
print("ok")
"""
    source = Path(__file__).resolve().parents[2] / "src"
    result = cmd(
        [sys.executable, "-I", "-c", script, str(source), str(tmp_path / "root")],
        include="out",
        check=True,
        env=home_env(home),
    )

    assert result.strip().splitlines()[-1] == "ok"


def test_existing_engine_reconciles_gateway_publications_and_fails_closed(tmp_path):
    context = _context(tmp_path / "live-publication")
    try:
        context._setup_modules()
        service = context.modules()
        engine = hb.LLMEngine(context=context)
        hb.LLMGateway("live-probe", _first_compile, compiler="semantic/v1", chat=_first_chat).register(resolver=service)
        first = engine.resolve({"model": "mock", "provider": "mock", "gateway": "live-probe", "endpoint": "openai"})

        assert engine.apply(first)["compiler"] == "openai/live-probe/semantic/v1"
        hb.LLMGateway("live-probe", _updated_compile, compiler="semantic/v2", chat=_second_chat).register(resolver=service, replace=True)

        with pytest.raises(ValueError, match="changed; re-resolve"):
            engine.apply(first)
        updated = engine.resolve({"model": "mock", "provider": "mock", "gateway": "live-probe", "endpoint": "openai"})
        assert updated.bindings["compiler"] == "openai/live-probe/semantic/v2"
        assert engine.apply(updated)["generation"] == "updated"

        service.retire("llm_gateway", "live-probe")
        with pytest.raises(KeyError, match="retired"):
            engine.apply(updated)
        with pytest.raises(ValueError, match="Unknown LLM gateway"):
            engine.resolve({"model": "mock", "provider": "mock", "gateway": "live-probe", "endpoint": "openai"})
    finally:
        if context.modules().inspect("llm_gateway", "live-probe", include_inactive=True):
            context.modules().unregister("llm_gateway", "live-probe")
        context.close()


def test_external_path_gateway_uses_artifact_owned_nested_callbacks(tmp_path):
    context = _context(tmp_path / "context")
    plugin = tmp_path / "external_gateway"
    plugin.mkdir()
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    (plugin / "runtime.py").write_text(
        """def compile(spec, gateway, override):
    return {"gateway": gateway, "model": spec.model_id, "args": dict(override)}

def available(spec):
    return True

def chat(spec, messages, stream=False, **kwargs):
    return {"choices": [{"message": {"role": "assistant", "content": "artifact-gateway"}}], "usage": {}}

""",
        encoding="utf-8",
    )
    (plugin / "meta.yaml").write_text(
        """manifest_version: 2
coordinate: example/llm-gateway-artifact
version: 1.0.0
compatibility: {}
items:
  - kind: llm_gateway
    identifier: artifact-probe
    aliases: [artifact-alias]
    name: Artifact Probe Gateway
    desc: External artifact-backed LLM gateway.
    source: inline
    target: definition
    active: true
    meta:
      schema_version: 1
      tags: {llm: true}
      definition:
        compiler: external/v1
        endpoints: [openai]
        compile:
          kind: heavenbase.executable
          version: 1
          source: path
          target: {module: runtime, qualname: compile}
        available:
          kind: heavenbase.executable
          version: 1
          source: path
          target: {module: runtime, qualname: available}
        chat:
          kind: heavenbase.executable
          version: 1
          source: path
          target: {module: runtime, qualname: chat}
""",
        encoding="utf-8",
    )

    receipt = None
    try:
        context._setup_modules()
        receipt = context.modules().install(plugin)
        row = context.modules().inspect("llm_gateway", "artifact-probe")[0]
        gateway = hb.LLMGateway.load("artifact-alias", resolver=context.modules())
        llm = hb.LLM(
            model="mock",
            provider="mock",
            gateway="artifact-alias",
            endpoint="openai",
            context=context,
            cache=False,
            stream_chat=False,
        )

        assert row["record"]["source"] == "inline"
        assert row["record"]["meta"]["tags"] == {"llm": True}
        assert context.modules().tag("llm_gateway", "artifact-probe", "llm").status == "resolved"
        compile_ref = row["record"]["meta"]["definition"]["compile"]
        assert compile_ref["source"] == "artifact"
        assert len(compile_ref["target"]["artifact"]) == 64
        assert gateway.identifier == "artifact-probe"
        assert gateway.fn.__module__.startswith("_heavenbase_artifact_")
        assert gateway.compiler == "external/v1"
        assert llm.spec.gateway == "artifact-probe"
        assert llm.chat("hello") == "artifact-gateway"
        assert hb.LLMGateway.load("artifact-probe", resolver=context.modules()) is gateway
    finally:
        if receipt is not None:
            context.modules().uninstall(receipt)
        context.close()


@pytest.mark.slow
@pytest.mark.parametrize("registered_pool, peer_pool", [("queue", "null"), ("null", "queue")])
def test_llm_cache_drop_reopens_sqlite_peer_with_another_pool(tmp_path, registered_pool, peer_pool):
    with closing(_context(tmp_path / "sqlite-cache-context")) as context:
        database = hb.utils.path_to_file_uri(tmp_path / "shared-cache.db")

        def backends(pool_class):
            return {"main": {"type": "sqlite", "database": database, "pool": {"pool_class": pool_class}}}

        registered = LLMCache.current(config_manager=context.config)
        registered.config["backend"] = backends(registered_pool)
        peer = LLMCache.from_dict({"backend": backends(peer_pool)}, config_manager=context.config)
        key = {"object_id": "seed-row", "spec_key": "spec", "request_key": "request", "args": {}}
        registered.set_text(key, [], {"text": "cached"}, [], policy="deterministic")
        assert peer.get_text(key)["response"] == {"text": "cached"}
        opened = peer.workspace
        peer_db = opened.backends.get("main").db
        assert registered.workspace.backends.get("main").db.engine is not peer_db.engine

        LLMCache.reset(drop=True, config_manager=context.config)

        assert peer_db.disposed
        assert peer.get_text(key) is None
        assert peer.workspace is not opened
        peer.set_text(key, [], {"text": "new"}, [], policy="deterministic")
        assert peer.get_text(key)["response"] == {"text": "new"}


def test_llm_cache_can_construct_unhashable_subclasses(tmp_path):
    @dataclass(init=False)
    class UnhashableCache(LLMCache):
        pass

    with closing(_context(tmp_path / "unhashable-cache")) as context:
        cache = UnhashableCache.from_dict(config_manager=context.config)
        assert isinstance(cache, UnhashableCache)
