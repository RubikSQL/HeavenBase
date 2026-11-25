"""Behavior contracts for the unified Registry foundation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from heavenbase.utils.registry import Registry, class_identifier


def test_registry_crud_aliases_groups_and_none_values():
    registry = Registry[str, object](
        "demo",
        normalize=str.lower,
        normalize_alias=str.lower,
        normalize_group=str.lower,
        max_items=4,
        value_policy="process_identity",
    )
    alpha = object()

    assert registry.create("Alpha", alpha, aliases=("A", "First"), groups=("Core",)) is alpha
    assert registry.create("factory", None, aliases=("FactoryAlias",), groups=("Factory",)) is None
    assert registry.resolve("A") == "alpha"
    assert registry.get("first") is alpha
    assert registry.get("missing", "fallback") == "fallback"
    assert registry.list() == ("alpha", "factory")
    assert registry.values() == (alpha, None)
    assert registry.items("core") == (("alpha", alpha),)
    assert registry.aliases("alpha") == ("A", "First")
    assert registry.aliases("a", include_canonical=True) == ("alpha", "A", "First")
    assert registry.groups("A") == ("Core",)
    assert registry.in_group("a", "CORE")
    assert "FactoryAlias" in registry
    assert registry["factory"] is None

    assert registry.delete("A") is alpha
    assert registry.list() == ("factory",)
    assert registry.aliases("alpha") == ()
    assert registry.delete("missing", missing_ok=True) is None
    with pytest.raises(KeyError, match="missing"):
        registry.delete("missing")


def test_registry_create_update_contract_and_conflicts():
    registry = Registry[str, object]("demo", normalize=str.lower, normalize_alias=str.lower, value_policy="process_identity")
    registry.create("alpha", 1, aliases=("a",), groups=("old",))
    registry.create("beta", 2, aliases=("b",))

    with pytest.raises(ValueError, match="already registered"):
        registry.create("ALPHA", 9)
    with pytest.raises(ValueError, match="already registered for"):
        registry.create("gamma", 3, aliases=("A",))
    with pytest.raises(ValueError, match="already registered for"):
        registry.update("beta", 4, aliases=("a",))

    def fn() -> str:
        return "value"

    assert registry.update("alpha", fn, aliases=("First",), groups=("new",)) is fn
    assert registry["alpha"] is fn
    assert registry.get("a") is None
    assert registry.update("first", derive=lambda current: (current, 5)) == (fn, 5)
    assert registry.aliases("alpha") == ("First",)
    assert registry.groups("alpha") == ("new",)
    assert registry.update("gamma", derive=lambda current: 7 if current is None else current) == 7

    with pytest.raises(TypeError, match="exactly one"):
        registry.update("alpha")

    assert not hasattr(registry, "set")
    assert not hasattr(registry, "aset")
    with pytest.raises(TypeError, match="exactly one"):
        registry.update("alpha", 6, derive=lambda current: current)
    with pytest.raises(TypeError, match="exactly one"):
        registry.update("alpha")


def test_registry_mapping_pop_distinguishes_missing_keys_from_none_values():
    registry = Registry[str, object]("demo")
    registry.create("none", None)

    assert registry.pop("none") is None
    assert registry.pop("missing", "fallback") == "fallback"
    with pytest.raises(KeyError, match="missing"):
        registry.pop("missing")


def test_registry_snapshot_is_immutable_and_stable_across_writes():
    registry = Registry[str, int]("demo")
    registry.create("one", 1)
    before = registry.snapshot()

    with pytest.raises(TypeError):
        before["two"] = 2

    registry.create("two", 2)
    assert dict(before) == {"one": 1}
    assert dict(registry.snapshot()) == {"one": 1, "two": 2}


def test_registry_limits_are_optional_and_fail_before_mutation():
    assert Registry("default-capacity").max_items == 65536

    unbounded = Registry[str, int]("unbounded", max_items=None)
    for index in range(10_000):
        unbounded.create(str(index), index)
    assert len(unbounded) == 10_000

    bounded = Registry[str, int]("bounded", max_items=1)
    bounded.create("one", 1)
    with pytest.raises(OverflowError, match="capacity 1"):
        bounded.create("two", 2)
    assert bounded.list() == ("one",)

    with pytest.raises(ValueError, match="positive integer or None"):
        Registry("invalid", max_items=0)


@pytest.mark.parametrize("value", (True, "1", 1.0, 1.5, -1))
def test_registry_rejects_non_integer_item_limits(value):
    with pytest.raises(ValueError, match="positive integer or None"):
        Registry("invalid-limit", max_items=value)


def test_registry_reads_never_enter_the_writer_lock():
    registry = Registry[str, int]("demo")
    registry.create("one", 1, aliases=("first",), groups=("numbers",))

    class FailingLock:
        def __enter__(self):
            raise AssertionError("read acquired writer lock")

        def __exit__(self, *args):
            return None

    registry._lock = FailingLock()
    assert registry.get("first") == 1
    assert registry.list("numbers") == ("one",)
    assert registry.values() == (1,)
    assert registry.items() == (("one", 1),)
    assert registry.aliases("one") == ("first",)
    assert registry.groups("one") == ("numbers",)
    assert registry.in_group("one", "numbers")
    assert registry.snapshot()["one"] == 1
    assert len(registry) == 1
    assert tuple(registry) == ("one",)
    assert "first" in registry
    assert registry["one"] == 1


def test_registry_readers_never_observe_partial_alias_or_group_state():
    registry = Registry[str, int]("parallel", normalize=str.lower, normalize_alias=str.lower)
    registry.create("item", 0, aliases=("old",), groups=("before",))
    failures: list[tuple[object, ...]] = []

    def writer() -> None:
        for index in range(2_000):
            even = index % 2 == 0
            registry.update("item", index, aliases=(("even" if even else "odd"),), groups=(("left" if even else "right"),))

    def reader() -> None:
        allowed = {
            (("old",), ("before",)),
            (("even",), ("left",)),
            (("odd",), ("right",)),
        }
        for _ in range(4_000):
            snapshot = registry._state
            aliases = snapshot.alias_names["item"]
            groups = tuple(snapshot.group_names[group] for group, keys in snapshot.groups.items() if "item" in keys)
            if (aliases, groups) not in allowed:
                failures.append((aliases, groups))

    with ThreadPoolExecutor(max_workers=5) as pool:
        tasks = [pool.submit(writer), *(pool.submit(reader) for _ in range(4))]
        for task in tasks:
            task.result()
    assert failures == []


@pytest.mark.anyio
async def test_registry_async_mutations_share_the_same_atomic_state_machine():
    registry = Registry[str, int]("async")
    await registry.acreate("counter", 0)
    await asyncio.gather(*(registry.aupdate("counter", derive=lambda current: current + 1) for _ in range(200)))

    assert registry["counter"] == 200
    assert await registry.aupdate("other", 2) == 2
    assert await registry.adelete("other") == 2
    assert await registry.arefresh() is False


def test_registry_normalization_and_class_identity_errors_are_contextual():
    class Parent:
        identifier = "parent"

    class Child(Parent):
        pass

    class Explicit(Parent):
        identifier = "explicit"

    assert class_identifier(Child) == "child"
    assert class_identifier(Explicit) == "explicit"
    with pytest.raises(ValueError, match="registry identifier"):
        Registry("Bad Registry")
    registry = Registry("demo", normalize=lambda value: value)
    with pytest.raises(TypeError, match="must normalize to a hashable"):
        registry.create([], 1)
