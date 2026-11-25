from __future__ import annotations

import io
import math
import os
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

from heavenbase.utils import (
    StableRNG,
    append_jsonl,
    append_txt,
    cmd,
    coerce_bool,
    coerce_dict,
    coerce_tuple,
    configure_logs,
    copy_dir,
    copy_file,
    copy_path,
    crc32hash,
    crc32int,
    cprint,
    cstr,
    delete_path,
    dump_b64,
    dump_bin,
    dump_hex,
    dump_jstr,
    dump_json,
    dump_pkl,
    dumps_jstr,
    dumps_json,
    enum_dirs,
    enum_files,
    enum_paths,
    evolve_seed,
    error_str,
    exists_file,
    fast_prompt_section,
    fmt_short_hash,
    get_file_basename,
    get_file_dir,
    get_file_ext,
    get_logger,
    has_file_ext,
    hash_int,
    hash_id,
    interpret_seed,
    is_link,
    iter_jsonl,
    iter_txt,
    load_b64,
    load_bin,
    load_hex,
    load_lazy_export,
    list_files,
    lflat,
    load_jstr,
    load_json,
    load_txt,
    load_pkl,
    loads_jstr,
    loads_json,
    loads_yaml,
    md5int,
    md5hash,
    NetworkProxy,
    path_inside,
    pj,
    pmap,
    print_debug,
    print_error,
    print_info,
    print_success,
    print_warning,
    raise_mismatch,
    redirect_logs,
    resolve_hash_int,
    restore_logs,
    save_txt,
    sha256int,
    set_log_level,
    sql_index_name,
    strip_color,
    suppress_logs,
    touch_dir,
    touch_file,
)
from heavenbase._lazy import _attach_lazy_exports


def _keyword_hash_int(obj, *, salt=None, sep="||"):
    data = f"{obj}" if salt is None else f"{obj}{sep}{salt}"
    return len(data)


def test_file_path_helpers_are_stable(tmp_path):
    root = tmp_path / "root"
    assert Path(pj("%/nested", aliases={"%": root})) == root / "nested"
    assert Path(pj("%nested", aliases={"%": root})) == root / "nested"
    assert Path(pj("&/nested", aliases={"&": root})) == root / "nested"
    assert Path(pj("$nested", aliases={"$": root})) == root / "nested"
    assert Path(pj("~", abs=True)) == Path.home().resolve()

    file_path = touch_file(root / "b.txt", content="bee")
    touch_file(root / "a.py")
    touch_dir(root / "folder")

    assert exists_file(file_path)
    assert get_file_ext(file_path) == "txt"
    assert get_file_basename(file_path, ext=False) == "b"
    assert get_file_dir(file_path, abs=True).endswith("root")
    assert has_file_ext(file_path, "md;txt") is True
    assert path_inside(root, file_path) is True
    assert path_inside(root, tmp_path / "root-sibling" / "b.txt") is False
    assert list(list_files(root)) == ["a.py", "b.txt"]
    assert list(enum_files(root, ext="py")) == ["a.py"]
    linked = root / "link.txt"
    linked.symlink_to(file_path)
    assert is_link(linked) is True
    assert is_link(file_path) is False
    assert delete_path(root / "folder") is True


def test_recursive_path_helpers_propagate_nested_walk_errors(tmp_path, monkeypatch):
    root = tmp_path / "root"
    blocked = root / "blocked"
    blocked.mkdir(parents=True)
    (blocked / "hidden.txt").write_text("hidden", encoding="utf-8")
    original_scandir = os.scandir

    def deny_blocked(path):
        if Path(path) == blocked:
            raise PermissionError("blocked traversal")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", deny_blocked)

    for enumerate_paths in (enum_dirs, enum_files, enum_paths):
        with pytest.raises(PermissionError, match="blocked traversal"):
            list(enumerate_paths(root))


def test_copy_helpers_support_modes(tmp_path):
    src_file = touch_file(tmp_path / "src" / "a.txt", content="one")
    dst_file = tmp_path / "dst" / "a.txt"
    copy_file(src_file, dst_file)
    touch_file(src_file, content="two", clear=True)

    assert copy_file(src_file, dst_file, mode="skip") == str(dst_file.resolve())
    assert dst_file.read_text(encoding="utf-8") == "one\n"
    with pytest.raises(FileExistsError):
        copy_file(src_file, dst_file, mode="strict")
    copy_file(src_file, dst_file, mode="replace")
    assert dst_file.read_text(encoding="utf-8") == "two\n"

    src_dir = tmp_path / "srcdir"
    dst_dir = tmp_path / "dstdir"
    touch_file(src_dir / "keep.txt", content="new")
    touch_file(src_dir / "fresh.txt", content="fresh")
    touch_file(dst_dir / "keep.txt", content="old")

    copy_dir(src_dir, dst_dir, mode="skip")
    assert (dst_dir / "keep.txt").read_text(encoding="utf-8") == "old\n"
    assert (dst_dir / "fresh.txt").read_text(encoding="utf-8") == "fresh\n"
    with pytest.raises(FileExistsError):
        copy_dir(src_dir, dst_dir, mode="strict")
    copy_path(src_dir, dst_dir, mode="replace")
    assert (dst_dir / "keep.txt").read_text(encoding="utf-8") == "new\n"


def test_jstr_serialization_round_trips_scalars_and_containers(tmp_path):
    assert dumps_jstr(42) == "42"
    assert dumps_jstr("hello") == "hello"
    assert dumps_jstr(3.5) == "3.5"
    assert dumps_jstr(True) == "True"
    assert dumps_jstr({"a": 1, "b": [2]}) == '{"a": 1, "b": [2]}'
    assert dumps_jstr([1, {"nested": {"x": 3}}, [4, 5]]) == '[1, {"nested": {"x": 3}}, [4, 5]]'
    assert dumps_jstr({"outer": {"inner": [1, 2]}}) == '{"outer": {"inner": [1, 2]}}'
    assert dumps_jstr((1, 2)) == "(1, 2)"
    assert dumps_jstr({1, 2}) == "{1, 2}"
    assert loads_jstr("42") == "42"
    assert loads_jstr('{"a": 1}') == {"a": 1}
    assert loads_jstr("[1, 2]") == [1, 2]
    assert loads_jstr('[1, {"nested": {"x": 3}}]') == [1, {"nested": {"x": 3}}]
    assert loads_jstr(dumps_jstr("{not json")) == "{not json"
    assert loads_jstr(dumps_jstr("[broken")) == "[broken"

    path = tmp_path / "value.txt"
    dump_jstr({"items": [1, 2]}, path)
    assert load_jstr(path) == {"items": [1, 2]}
    dump_jstr("plain", path)
    assert load_jstr(path) == "plain"
    assert load_jstr(tmp_path / "missing.txt") == ""


def test_serialization_round_trips_common_payloads(tmp_path):
    payload = {"name": "alpha", "items": (1, 2), "tags": {"b", "a"}, "amount": Decimal("12.3400")}
    json_path = tmp_path / "payload.json"
    pkl_path = tmp_path / "payload.pkl"
    txt_path = tmp_path / "note.txt"

    dump_json(payload, json_path, sort_keys=True)
    dump_pkl(payload, pkl_path)
    save_txt("hello", txt_path)

    assert load_json(json_path)["items"] == (1, 2)
    assert load_json(json_path)["tags"] == {"a", "b"}
    assert load_json(json_path)["amount"] == Decimal("12.3400")
    assert load_pkl(pkl_path) == payload
    assert txt_path.read_text(encoding="utf-8") == "hello\n"
    assert load_json(tmp_path / "missing.json") == {}
    with pytest.raises(FileNotFoundError):
        load_json(tmp_path / "missing.json", strict=True)


def test_shared_json_serializes_callables_as_refs():
    def local_tool() -> str:
        return "ok"

    restored = loads_json(dumps_json({"tool": local_tool}))

    assert restored == {"tool": f"{local_tool.__module__}.{local_tool.__qualname__}"}


def test_json_dump_with_the_hb_encoder_writes_what_dumps_writes():
    # HbJsonEncoder adapted values only in encode(), which json.dumps calls;
    # json.dump calls iterencode, so it raised on sets and datetimes and
    # wrote tuples and big integers untagged.
    import datetime as dt
    import json

    from heavenbase.utils import HbJsonDecoder, HbJsonEncoder

    value = {"t": (1, 2), "s": {3}, "d": dt.date(2020, 1, 2), "n": Decimal("1.5"), "b": 1 << 60, "e": ..., "nested": [{"t": ("x",)}]}
    buffer = io.StringIO()
    json.dump(value, buffer, cls=HbJsonEncoder)
    indented = io.StringIO()
    json.dump(value, indented, cls=HbJsonEncoder, indent=2)

    assert buffer.getvalue() == json.dumps(value, cls=HbJsonEncoder)
    assert json.loads(buffer.getvalue(), cls=HbJsonDecoder) == value
    assert json.loads(indented.getvalue(), cls=HbJsonDecoder) == value
    assert "".join(HbJsonEncoder().iterencode(value)) == HbJsonEncoder().encode(value)


def test_yaml_deserialization_uses_the_fast_safe_loader(monkeypatch):
    import yaml

    loaders = []
    yaml_load = yaml.load

    def tracked_load(stream, *, Loader, **kwargs):
        loaders.append(Loader)
        return yaml_load(stream, Loader=Loader, **kwargs)

    monkeypatch.setattr(yaml, "load", tracked_load)

    assert loads_yaml("enabled: true\nitems: [1, 2]\n") == {
        "enabled": True,
        "items": [1, 2],
    }
    assert loaders == [getattr(yaml, "CSafeLoader", yaml.SafeLoader)]
    with pytest.raises(yaml.constructor.ConstructorError):
        loads_yaml("value: !!python/object/apply:builtins.str [unsafe]\n")


def test_serialization_config_defaults(monkeypatch, tmp_path):
    import heavenbase.utils.serialize as serialize_mod

    def fake_get(path, default=None):
        values = {"heavenbase.serialize.encoding": "utf-16", "heavenbase.serialize.indent": 2}
        return values.get(path, default)

    monkeypatch.setitem(vars(serialize_mod.CM_HVNB), "get", fake_get)
    txt_path = tmp_path / "encoded.txt"

    save_txt("hello", txt_path)
    assert load_txt(txt_path) == "hello\n"
    assert dumps_json({"outer": {"inner": 1}}).splitlines()[1].startswith("  ")


def test_encoded_and_streaming_serialization_helpers(tmp_path):
    bin_path = tmp_path / "payload.bin"
    b64_path = tmp_path / "payload.b64.bin"
    hex_path = tmp_path / "payload.hex.bin"
    txt_path = tmp_path / "stream.txt"
    jsonl_path = tmp_path / "stream.jsonl"

    dump_bin(b"hello", bin_path)
    assert load_b64(bin_path) == "aGVsbG8="
    assert load_hex(bin_path) == "68656c6c6f"

    dump_b64("AAECAw==", b64_path)
    dump_hex("00ff10", hex_path)
    assert load_bin(b64_path) == b"\x00\x01\x02\x03"
    assert load_bin(hex_path) == b"\x00\xff\x10"

    append_txt("alpha", txt_path)
    append_txt("beta", txt_path)
    append_jsonl({"idx": 1}, jsonl_path)
    append_jsonl({"idx": 2}, jsonl_path)
    assert list(iter_txt(txt_path)) == ["alpha", "beta"]
    assert list(iter_jsonl(jsonl_path)) == [{"idx": 1}, {"idx": 2}]


def test_hash_and_rng_are_deterministic():
    assert md5hash({"b": 2, "a": 1}) == md5hash({"a": 1, "b": 2})
    assert isinstance(md5hash("alpha"), str)
    assert len(md5hash("alpha")) == 42
    assert md5hash("alpha").isdigit()
    assert isinstance(md5int("alpha"), int)
    assert isinstance(crc32int("alpha"), int)
    assert len(crc32hash("alpha")) == 10
    assert crc32hash({"b": 2, "a": 1}) == crc32hash({"a": 1, "b": 2})
    assert crc32hash("alpha", salt="one") != crc32hash("alpha", salt="two")
    assert isinstance(sha256int("alpha"), int)
    assert sha256int({"b": 2, "a": 1}) == sha256int({"a": 1, "b": 2})
    assert hash_int("alpha", method="md5") == md5int("alpha")
    assert hash_int("alpha", method="crc32") == crc32int("alpha")
    assert hash_int("alpha", method="sha256") == sha256int("alpha")
    assert resolve_hash_int("crc32")("alpha") == crc32int("alpha")
    dotted_method = f"{_keyword_hash_int.__module__}._keyword_hash_int"
    assert hash_int("alpha", method=dotted_method, salt="beta") == _keyword_hash_int("alpha", salt="beta")
    with pytest.raises(ValueError):
        hash_int("alpha", method="unknown")
    assert fmt_short_hash(md5hash("alpha"), 6).isdigit()
    assert hash_id("thing", "Alpha") == hash_id("thing", "Alpha")
    assert sql_index_name("hb_gram_keywords", "ns", "entity_id", "field_name") == f"ix_{md5hash(['hb_gram_keywords', 'ns', 'entity_id', 'field_name'])}"

    root = StableRNG(seed=42)
    first = root.step("rows").rnd(3)
    second = root.step("rows").rnd(3)
    assert first == second
    assert root.seed == 42

    with StableRNG(seed=42) as rng:
        chosen = rng.choice(["a", "b", "c"], k=2, replace=False)
        vec = rng.rnd_vec(dim=4)
    assert chosen == StableRNG(seed=42).choice(["a", "b", "c"], k=2, replace=False)
    assert math.isclose(sum(item * item for item in vec), 1.0, rel_tol=1e-7)


@pytest.mark.full
def test_rng_extended_features_and_batch_shapes():
    assert interpret_seed(None, fallback="none") is None
    assert evolve_seed(42, "rows") == evolve_seed(42, "rows")

    assert StableRNG(seed=7).rnd_str(8) == StableRNG(seed=7).rnd_str(8)
    strings = StableRNG(seed=7).rnd_str(4, n=(2, 2))
    assert [[len(item) for item in row] for row in strings] == [[4, 4], [4, 4]]

    perms = StableRNG(seed=9).perm(5, n=(2, 2))
    assert len(perms) == 2 and len(perms[0]) == 2
    assert all(sorted(perm) == list(range(5)) for row in perms for perm in row)

    shuffled = StableRNG(seed=9).shuffled(["a", "b", "c"], n=2)
    assert sorted(shuffled[0]) == ["a", "b", "c"]
    assert len(shuffled) == 2

    sample_a = StableRNG(seed=11).hash_sample(["a", "b", "c", "d"], k=2)
    sample_b = StableRNG(seed=11).hash_sample(["d", "c", "b", "a"], k=2)
    assert sample_a == sample_b
    assert StableRNG(seed=11).hash_function == "crc32"
    crc_sample_a = StableRNG(seed=11, hash_function="crc32").hash_sample(["a", "b", "c", "d"], k=2)
    crc_sample_b = StableRNG(seed=11).hash_sample(["d", "c", "b", "a"], k=2, hash_function="crc32")
    assert crc_sample_a == crc_sample_b
    tied_sample_a = StableRNG(seed=11, hash_modulo=1).hash_sample(["a", "b", "c", "d"], k=2)
    tied_sample_b = StableRNG(seed=11, hash_modulo=1).hash_sample(["d", "c", "b", "a"], k=2)
    assert tied_sample_a == tied_sample_b
    assert StableRNG(seed=11, hash_function="sha256").step("rows").hash_function == "sha256"
    with pytest.raises(ValueError, match="hash_modulo"):
        StableRNG(seed=11, hash_modulo=0)

    selected, remaining = StableRNG(seed=13).hash_split(["a", "b", "c", "d"], r=0.5)
    assert sorted(selected + remaining) == ["a", "b", "c", "d"]
    selected, remaining = StableRNG(seed=13, hash_function="crc32").hash_split(["a", "b", "c", "d"], r=0.5)
    assert sorted(selected + remaining) == ["a", "b", "c", "d"]

    vecs = StableRNG(seed=17).rnd_vec(dim=3, n=(2, 2))
    assert len(vecs) == 2 and len(vecs[0]) == 2
    assert all(math.isclose(sum(value * value for value in vec), 1.0, rel_tol=1e-7) for row in vecs for vec in row)

    assert StableRNG(seed=19).abm(100.0, t=2) == StableRNG(seed=19).abm(100.0, t=2)
    paths = StableRNG(seed=19).abm_path(100.0, t=3, n=2)
    assert len(paths) == 2 and all(path[0] == 100.0 and len(path) == 4 for path in paths)


def test_rng_hash_defaults_can_come_from_config(monkeypatch):
    import heavenbase.utils.rng as rng_mod

    def fake_get(path, default=None):
        values = {"heavenbase.rng.hash_function": "sha256", "heavenbase.rng.hash_modulo": 99991}
        return values.get(path, default)

    monkeypatch.setitem(vars(rng_mod.CM_HVNB), "get", fake_get)
    rng = rng_mod.StableRNG(seed=5)

    assert rng.hash_function == "sha256"
    assert rng.hash_modulo == 99991
    assert rng.hash_sample(["a", "b", "c"], k=2) == rng_mod.StableRNG(seed=5, hash_function="sha256", hash_modulo=99991).hash_sample(["a", "b", "c"], k=2)


def test_command_parallel_log_and_color_helpers():
    result = cmd([sys.executable, "-c", "print('ok')"])
    assert result.ok is True
    assert result.out == "ok"
    assert pmap(lambda item: item * 2, [{"item": 3}, {"item": 1}, {"item": 2}], max_workers=2) == [6, 2, 4]

    if os.environ.get("NO_COLOR"):
        pytest.skip("NO_COLOR is set")
    colored = cstr("ready", "green", console=True)
    assert strip_color(colored) == "ready"
    assert "\x1b[" in colored


def test_print_helpers_share_logger_level_colors(monkeypatch):
    class TtyStream(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = TtyStream()

    for printer, code, message in (
        (print_debug, "90", "debug"),
        (print_info, "34", "info"),
        (print_success, "32", "success"),
        (print_warning, "33", "warning"),
        (print_error, "31", "error"),
    ):
        printer(message, file=stream)
        assert f"\x1b[1;{code}m{message}\x1b[0m" in stream.getvalue()

    plain = io.StringIO()
    cprint("plain", color="red", file=plain)
    assert plain.getvalue() == "plain\n"


def test_container_helpers_flatten_one_level():
    assert lflat([[1, 2], (3, 4), (item for item in (5, 6))]) == [1, 2, 3, 4, 5, 6]


def test_coerce_helpers_normalize_primitive_boundary_shapes():
    source = {"key": [1]}

    assert coerce_bool(True) is True
    assert coerce_tuple(["a", "b"]) == ("a", "b")
    assert coerce_dict(source) == source
    assert coerce_dict(source) is not source

    with pytest.raises(TypeError, match="flag must be bool"):
        coerce_bool(1, "flag")
    with pytest.raises(TypeError, match="items must be a sequence"):
        coerce_tuple("item", "items")
    with pytest.raises(TypeError, match="options must be a mapping"):
        coerce_dict([], "options")


def test_network_proxy_restores_environment(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://old")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    with NetworkProxy(http_proxy="http://new", https_proxy="", no_proxy="localhost"):
        assert os.environ["HTTP_PROXY"] == "http://new"
        assert "HTTPS_PROXY" not in os.environ
        assert os.environ["NO_PROXY"] == "localhost"
    assert os.environ["HTTP_PROXY"] == "http://old"
    assert "HTTPS_PROXY" not in os.environ
    assert "NO_PROXY" not in os.environ


def test_network_proxy_without_settings_changes_nothing(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://old")
    proxy = NetworkProxy()
    assert not proxy.active
    with proxy:
        assert proxy.backup == {} and os.environ["HTTP_PROXY"] == "http://old"
    assert os.environ["HTTP_PROXY"] == "http://old"


def _prime_proxy_environment(monkeypatch, value: str) -> None:
    """Set ``HTTP_PROXY`` and clear the lowercase spelling where it is a distinct variable.

    Windows environment names are case-insensitive, so deleting ``http_proxy`` there would
    remove ``HTTP_PROXY`` as well; the two spellings are one variable on that platform.
    """
    monkeypatch.setenv("HTTP_PROXY", value)
    if os.name != "nt":
        monkeypatch.delenv("http_proxy", raising=False)


def _lowercase_proxy_absent() -> bool:
    """Whether ``http_proxy`` is absent, treating the Windows alias of ``HTTP_PROXY`` as absent."""
    return os.name == "nt" or "http_proxy" not in os.environ


def test_network_proxy_shared_holds_restore_the_environment_once(monkeypatch):
    _prime_proxy_environment(monkeypatch, "http://old")
    proxy = NetworkProxy(http_proxy="http://new")
    first_in, release_first = threading.Event(), threading.Event()

    def first_hold():
        with proxy:
            first_in.set()
            assert release_first.wait(5.0)

    first = threading.Thread(target=first_hold, daemon=True)
    first.start()
    assert first_in.wait(5.0)
    with proxy:  # a second hold of the same instance while the first thread's hold is open
        assert os.environ["HTTP_PROXY"] == "http://new"
        release_first.set()
        first.join(5.0)
        assert not first.is_alive() and os.environ["HTTP_PROXY"] == "http://new"  # the first exit does not restore
    assert os.environ["HTTP_PROXY"] == "http://old" and proxy.backup == {}  # the last exit restores the original
    with proxy:
        with proxy:  # nested on one thread
            assert os.environ["HTTP_PROXY"] == "http://new"
        assert os.environ["HTTP_PROXY"] == "http://new"
    assert os.environ["HTTP_PROXY"] == "http://old"


def test_network_proxy_survives_many_concurrent_holds(monkeypatch):
    _prime_proxy_environment(monkeypatch, "http://old")
    proxy = NetworkProxy(http_proxy="http://new")

    def hold_repeatedly(_index: int) -> int:
        seen = 0
        for _ in range(2000):
            with proxy:
                seen += os.environ["HTTP_PROXY"] == "http://new"
        return seen

    with ThreadPoolExecutor(max_workers=16) as pool:
        assert list(pool.map(hold_repeatedly, range(16))) == [2000] * 16
    assert os.environ["HTTP_PROXY"] == "http://old" and _lowercase_proxy_absent()


def test_network_proxy_distinct_instances_restore_the_environment_in_any_exit_order(monkeypatch):
    _prime_proxy_environment(monkeypatch, "http://old")
    first, second = NetworkProxy(http_proxy="http://a"), NetworkProxy(http_proxy="http://b")
    first.__enter__()
    second.__enter__()
    assert os.environ["HTTP_PROXY"] == "http://b"  # the latest hold wins while both are open
    first.__exit__(None, None, None)  # not the last-entered hold: the remaining hold keeps its value
    assert os.environ["HTTP_PROXY"] == "http://b" and first.backup == {}
    second.__exit__(None, None, None)
    assert os.environ["HTTP_PROXY"] == "http://old" and _lowercase_proxy_absent()

    first.__enter__()
    second.__enter__()
    second.__exit__(None, None, None)  # the latest hold exits first: the earlier hold's value returns
    assert os.environ["HTTP_PROXY"] == "http://a"
    first.__exit__(None, None, None)
    assert os.environ["HTTP_PROXY"] == "http://old" and _lowercase_proxy_absent()


def test_network_proxy_overlapping_clears_restore_the_user_proxy(monkeypatch):
    _prime_proxy_environment(monkeypatch, "http://corp:8080")
    first, second = NetworkProxy(http_proxy=""), NetworkProxy(http_proxy="")  # what local providers set by default
    first.__enter__()
    second.__enter__()
    assert "HTTP_PROXY" not in os.environ
    first.__exit__(None, None, None)
    assert "HTTP_PROXY" not in os.environ
    second.__exit__(None, None, None)
    assert os.environ["HTTP_PROXY"] == "http://corp:8080"


def test_network_proxy_restores_only_variables_no_remaining_hold_sets(monkeypatch):
    _prime_proxy_environment(monkeypatch, "http://old")
    monkeypatch.setenv("HTTPS_PROXY", "https://old")
    plain, secure = NetworkProxy(http_proxy="http://a"), NetworkProxy(https_proxy="https://b")
    plain.__enter__()
    secure.__enter__()
    plain.__exit__(None, None, None)
    assert os.environ["HTTP_PROXY"] == "http://old" and os.environ["HTTPS_PROXY"] == "https://b"
    secure.__exit__(None, None, None)
    assert os.environ["HTTP_PROXY"] == "http://old" and os.environ["HTTPS_PROXY"] == "https://old"


def test_network_proxy_exit_without_enter_changes_nothing(monkeypatch):
    _prime_proxy_environment(monkeypatch, "http://old")
    held, stray = NetworkProxy(http_proxy="http://a"), NetworkProxy(http_proxy="http://b")
    with held:
        assert stray.__exit__(None, None, None) is False
        assert os.environ["HTTP_PROXY"] == "http://a"
    assert os.environ["HTTP_PROXY"] == "http://old"


def test_network_proxy_survives_concurrent_holds_of_distinct_instances(monkeypatch):
    _prime_proxy_environment(monkeypatch, "http://old")

    def hold_repeatedly(index: int) -> int:
        seen = 0
        for _ in range(500):
            with NetworkProxy(http_proxy=f"http://p{index % 2}"):  # a fresh instance per hold, as each LLM call makes
                seen += os.environ["HTTP_PROXY"] in {"http://p0", "http://p1"}
        return seen

    with ThreadPoolExecutor(max_workers=16) as pool:
        assert list(pool.map(hold_repeatedly, range(16))) == [500] * 16
    assert os.environ["HTTP_PROXY"] == "http://old" and _lowercase_proxy_absent()


def test_network_proxy_restores_the_environment_after_out_of_order_async_holds(monkeypatch):
    import asyncio

    _prime_proxy_environment(monkeypatch, "http://old")
    seen = []

    async def call(index: int) -> None:
        with NetworkProxy(http_proxy="http://proxy:3128"):
            await asyncio.sleep(0.002 * index)  # the first hold entered exits first: not LIFO
            seen.append(os.environ["HTTP_PROXY"])

    async def main() -> None:
        await asyncio.gather(*(call(index) for index in range(16)))

    asyncio.run(main())
    assert seen == ["http://proxy:3128"] * 16
    assert os.environ["HTTP_PROXY"] == "http://old" and _lowercase_proxy_absent()


@pytest.mark.parametrize("no_proxy", [["localhost"], [], 0], ids=["list", "empty-list", "zero"])
def test_network_proxy_setting_that_cannot_be_applied_leaves_no_hold(monkeypatch, no_proxy):
    _prime_proxy_environment(monkeypatch, "http://mine")
    bad = NetworkProxy(http_proxy="http://bad", no_proxy=no_proxy)  # type: ignore[arg-type]  # e.g. a YAML value
    with pytest.raises(TypeError):
        bad.__enter__()
    assert os.environ["HTTP_PROXY"] == "http://mine" and _lowercase_proxy_absent() and bad.backup == {}
    with NetworkProxy(http_proxy="http://good"):  # later holds are unaffected
        assert os.environ["HTTP_PROXY"] == "http://good"
    assert os.environ["HTTP_PROXY"] == "http://mine"
    with NetworkProxy(http_proxy=""):
        assert "HTTP_PROXY" not in os.environ
    assert os.environ["HTTP_PROXY"] == "http://mine" and _lowercase_proxy_absent()


def test_network_proxy_nested_exit_keeps_a_change_made_inside_the_outer_hold(monkeypatch):
    _prime_proxy_environment(monkeypatch, "http://old")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    with NetworkProxy(http_proxy="http://a"):
        os.environ["HTTPS_PROXY"] = "http://user"  # user code changes a variable while the outer hold is open
        with NetworkProxy(http_proxy="http://b"):
            assert os.environ["HTTP_PROXY"] == "http://b"
        assert os.environ["HTTP_PROXY"] == "http://a" and os.environ["HTTPS_PROXY"] == "http://user"  # as on 0.1.2.2
    assert os.environ["HTTP_PROXY"] == "http://old" and "HTTPS_PROXY" not in os.environ


def test_network_proxy_shared_instance_gives_up_its_newest_hold(monkeypatch):
    _prime_proxy_environment(monkeypatch, "http://old")
    shared, local = NetworkProxy(http_proxy="http://db"), NetworkProxy(http_proxy="")
    with shared:
        with local:
            with shared:  # nested on one thread: the inner hold exits first
                assert os.environ["HTTP_PROXY"] == "http://db"
            assert "HTTP_PROXY" not in os.environ  # the hold entered before the inner one is back in force
        assert os.environ["HTTP_PROXY"] == "http://db"
    assert os.environ["HTTP_PROXY"] == "http://old" and _lowercase_proxy_absent() and shared.backup == {}


_PROXY_REENTRY_PRELUDE = """
import faulthandler, gc, os, sys
faulthandler.dump_traceback_later(30, exit=True)  # a deadlock prints the stacks and fails instead of hanging the suite
gc.disable()  # only the explicit collection below may close the abandoned stream
from heavenbase.utils.network import NetworkProxy

for name in NetworkProxy.PROXY_VARS:
    os.environ.pop(name, None)
os.environ["HTTP_PROXY"] = "http://old"


class Interrupting(str):
    # A proxy value whose comparison runs ``hook`` inside NetworkProxy's locked section, where any allocation can start the
    # cyclic collector and a signal handler can run.
    hook = None

    def __eq__(self, other):
        hook, Interrupting.hook = Interrupting.hook, None
        if hook is not None:
            hook()
        return str.__eq__(self, other)

    __hash__ = str.__hash__


def stream():
    with NetworkProxy(http_proxy="http://stream"):  # held across yields, as LLM.stream holds its proxy
        yield 1
        yield 2


def abandon():
    gen = stream()
    next(gen)
    loop = [gen]
    loop.append(loop)  # in a reference cycle, so only the cyclic collector closes it


def settled():
    return not NetworkProxy._held and not getattr(NetworkProxy, "_deferred", ()) and os.environ["HTTP_PROXY"] == "http://old"
"""


def _run_proxy_reentry_script(body: str) -> None:
    """Run ``body`` after the reentry prelude in a fresh interpreter, so a deadlocked lock cannot hang this process."""
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[2] / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    code = _PROXY_REENTRY_PRELUDE + body + '\nprint("REENTRY-OK")\n'
    result = subprocess.run([sys.executable, "-c", code], env=env, text=True, capture_output=True, timeout=120)
    assert result.returncode == 0 and "REENTRY-OK" in result.stdout, result.stdout + result.stderr


def test_network_proxy_survives_the_collector_closing_a_stream_while_another_hold_enters():
    _run_proxy_reentry_script("""
abandon()
assert os.environ["HTTP_PROXY"] == "http://stream"
Interrupting.hook = gc.collect  # the stream's proxy exits inside the next hold's locked enter
with NetworkProxy(http_proxy=Interrupting("http://outer")):
    assert Interrupting.hook is None and os.environ["HTTP_PROXY"] == "http://outer"
assert settled(), (os.environ.get("HTTP_PROXY"), NetworkProxy._held)
""")


def test_network_proxy_survives_the_collector_closing_a_stream_while_an_older_hold_exits():
    _run_proxy_reentry_script("""
outer = NetworkProxy(http_proxy=Interrupting("http://outer"))
outer.__enter__()
abandon()  # the stream's hold is newer than ``outer``
assert os.environ["HTTP_PROXY"] == "http://stream"
Interrupting.hook = gc.collect  # the stream's proxy exits while ``outer`` hands over what it found
outer.__exit__(None, None, None)
# Applying the stream's exit in the middle of the hand-over would leave ``http://outer`` behind.
assert Interrupting.hook is None and settled(), (os.environ.get("HTTP_PROXY"), NetworkProxy._held)
""")


def test_network_proxy_never_hangs_interpreter_shutdown_on_a_lock_a_stopped_daemon_thread_holds():
    """Shutdown closes an abandoned proxied stream while a daemon thread, which never runs again, holds the lock."""
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[2] / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    code = """
import gc, os, sys, threading
from heavenbase.utils.network import NetworkProxy

def stream():
    with NetworkProxy(https_proxy="http://stream:3128"):
        yield 1
        yield 2

gc.disable()  # only interpreter shutdown collects the cycle below
gen = stream()
next(gen)
loop = [gen]
loop.append(loop)
del gen, loop
held = threading.Event()

def hold():
    with NetworkProxy._lock:
        held.set()
        threading.Event().wait()  # like a daemon thread that the interpreter stopped inside a section

threading.Thread(target=hold, daemon=True).start()
held.wait()
print("EXITING", flush=True)
sys.exit(0)
"""
    result = subprocess.run([sys.executable, "-c", code], env=env, text=True, capture_output=True, timeout=120)
    assert result.returncode == 0 and "EXITING" in result.stdout, result.stdout + result.stderr


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="needs a POSIX signal to raise")
def test_network_proxy_survives_a_signal_handler_that_uses_proxies_inside_a_locked_section():
    _run_proxy_reentry_script("""
import signal
gen = stream()
next(gen)  # an older hold the handler closes
seen = []


def handler(signum, frame):
    with NetworkProxy(http_proxy="http://handler"):  # entered and exited entirely inside the handler
        seen.append(os.environ["HTTP_PROXY"])
    gen.close()


signal.signal(signal.SIGUSR1, handler)
Interrupting.hook = lambda: signal.raise_signal(signal.SIGUSR1)
with NetworkProxy(http_proxy=Interrupting("http://outer")):
    assert seen == ["http://handler"] and os.environ["HTTP_PROXY"] == "http://outer"
assert settled(), (os.environ.get("HTTP_PROXY"), NetworkProxy._held)
""")


def test_network_proxy_never_leaves_its_lock_held_when_an_exception_interrupts_it():
    # A signal handler that raises, for example ``KeyboardInterrupt`` on Ctrl-C, can interrupt ``__enter__`` or
    # ``__exit__`` wherever a handler can run: when a function is entered and after a call returns. A profiler raises at
    # each such point in turn, one point per run, including inside an exit queued by a nested call and applied later.
    _run_proxy_reentry_script("""
import threading


class Interrupt(BaseException):
    pass


sys.unraisablehook = lambda unraisable: None  # where one raised while a finished generator is finalized goes


def inject(action, point, seen):
    # Run ``action``, count in ``seen`` the places where a signal handler could run, and raise ``Interrupt`` at the
    # ``point``-th; report whether it reached the caller. One raised while a finished generator is finalized does not.

    def profile(frame, event, arg):
        if frame.f_code.co_filename == "<string>" or event == "c_call":  # handlers run after a call returns, not before
            return
        if (event == "call" and frame.f_code is NetworkProxy.__exit__.__code__) or (event == "return" and frame.f_code is NetworkProxy.__enter__.__code__):
            return  # before ``__exit__`` starts or after ``__enter__`` ends, the interruption belongs to the caller
        seen[0] += 1
        if seen[0] == point + 1:
            raise Interrupt

    sys.setprofile(profile)
    try:
        action()
    except Interrupt:
        return True
    finally:
        sys.setprofile(None)
    return False


def lock_is_free():
    got = []

    def take():
        if NetworkProxy._lock.acquire(timeout=5):
            got.append(True)
            NetworkProxy._lock.release()

    thread = threading.Thread(target=take)
    thread.start()
    thread.join(30)
    return got == [True]


os.environ["https_proxy"] = "http://keep"
original = {name: os.environ.get(name) for name in NetworkProxy.PROXY_VARS}
for scenario in ("enter", "exit", "older exits first", "queued exit"):
    point, points, raised = -1, None, 0  # point -1 raises nowhere
    while points is None or point < points:
        seen = [0]
        a = NetworkProxy(http_proxy="http://a", https_proxy="")
        b = NetworkProxy(http_proxy=Interrupting("http://b"), no_proxy="local")
        if scenario == "enter":
            fired = inject(a.__enter__, point, seen)
            if not fired:
                a.__exit__(None, None, None)
        elif scenario == "exit":
            a.__enter__()
            fired = inject(lambda: a.__exit__(None, None, None), point, seen)
        elif scenario == "older exits first":
            a.__enter__()
            b.__enter__()
            fired = inject(lambda: a.__exit__(None, None, None), point, seen)  # hands over what it found to ``b``
            b.__exit__(None, None, None)
        else:
            a.__enter__()
            Interrupting.hook = lambda: a.__exit__(None, None, None)  # queued inside ``b``'s section, applied when it ends
            fired = inject(b.__enter__, point, seen)
            if Interrupting.hook is not None:  # interrupted before the nested exit ran
                Interrupting.hook = None
                a.__exit__(None, None, None)
            if not fired:
                b.__exit__(None, None, None)
        label = (scenario, point)
        assert lock_is_free(), label
        assert NetworkProxy._sections == [] and not NetworkProxy._deferred and not NetworkProxy._held and not NetworkProxy._exits, label
        assert {name: os.environ.get(name) for name in NetworkProxy.PROXY_VARS} == original, label
        if points is None:
            points = seen[0]  # the first run counts the places
        else:
            raised += fired
        point += 1
    assert raised > 20, (scenario, points, raised)  # the sweep reached the locked code, not just its entry
""")


def test_network_proxy_runs_a_recovery_again_when_a_second_exception_cuts_it_short():
    # A first exception interrupts ``__enter__`` or ``__exit__`` at each place in turn, one place per run, and a second
    # one, for example a second Ctrl-C, arrives as the recovery starts. The recovery runs again and the first exception
    # is the one that reaches the caller.
    _run_proxy_reentry_script("""
import threading


class Interrupt(BaseException):
    pass


sys.unraisablehook = lambda unraisable: None
ENTER, EXIT = NetworkProxy.__enter__.__code__, NetworkProxy.__exit__.__code__
section = NetworkProxy.__dict__["_section"]


def second(work, *args):
    # Stands in for ``_section`` once the first exception is raised: the recovery's first attempt, the locked section
    # that ``__enter__`` or ``__exit__`` starts while it handles that exception, is cut short as it starts.
    if sys._getframe(1).f_code in (ENTER, EXIT) and sys.exc_info()[1] is raised[0]:
        NetworkProxy._section = section
        raised.append(Interrupt("second"))
        raise raised[-1]
    return section.__func__(work, *args)


def inject(action, point, seen):
    # A profiler disables itself once it raises, so the second exception comes from ``second`` instead.
    def profile(frame, event, arg):
        if frame.f_code.co_filename == "<string>" or event == "c_call":
            return
        if (event == "call" and frame.f_code is EXIT) or (event == "return" and frame.f_code is ENTER):
            return
        seen[0] += 1
        if seen[0] == point + 1:
            NetworkProxy._section = staticmethod(second)
            raised.append(Interrupt("first"))
            raise raised[-1]

    sys.setprofile(profile)
    try:
        action()
    except Interrupt as error:
        return error
    finally:
        sys.setprofile(None)
        NetworkProxy._section = section
    return None


def lock_is_free():
    got = []

    def take():
        if NetworkProxy._lock.acquire(timeout=5):
            got.append(True)
            NetworkProxy._lock.release()

    thread = threading.Thread(target=take)
    thread.start()
    thread.join(30)
    return got == [True]


original = {name: os.environ.get(name) for name in NetworkProxy.PROXY_VARS}
raised = []
for scenario in ("enter", "exit"):
    point, points, rerun = -1, None, 0
    while points is None or point < points:
        seen = [0]
        del raised[:]
        proxy = NetworkProxy(http_proxy="http://a", https_proxy="")
        if scenario == "enter":
            caught = inject(proxy.__enter__, point, seen)
            if caught is None:
                proxy.__exit__(None, None, None)
        else:
            proxy.__enter__()
            caught = inject(lambda: proxy.__exit__(None, None, None), point, seen)
        label = (scenario, point, [str(error) for error in raised])
        assert caught is None or caught is raised[0], label  # one raised while a finished generator is finalized is lost
        assert lock_is_free(), label
        assert NetworkProxy._sections == [] and not NetworkProxy._deferred and not NetworkProxy._held and not NetworkProxy._exits, label
        assert {name: os.environ.get(name) for name in NetworkProxy.PROXY_VARS} == original, label
        if points is None:
            points = seen[0]
        rerun += len(raised) == 2
        point += 1
    assert rerun > 20, (scenario, points, rerun)
""")


def test_network_proxy_shared_instance_gives_up_two_holds_when_a_finalizer_exits_it_during_its_own_exit():
    # One instance is held by a stream across its yields and by other code. A finalizer that closes the stream, or a
    # signal handler, can run wherever a handler can run inside the other code's ``__enter__`` or ``__exit__``; a profiler
    # closes it at each such point in turn, one point per run, and optionally raises an exception right after. The two
    # exits must give up two different holds, so the original environment returns.
    _run_proxy_reentry_script("""
import threading


class Interrupt(BaseException):
    pass


sys.unraisablehook = lambda unraisable: None
shared = NetworkProxy(http_proxy="http://shared", https_proxy="")


def shared_stream():
    with shared:
        yield 1
        yield 2


def inject(action, point, seen, close, also_raise):
    # Run ``action``, count in ``seen`` the places where a finalizer or signal handler could run, and at the
    # ``point``-th close the stream and, when ``also_raise``, raise ``Interrupt``; report whether that reached the caller.

    def profile(frame, event, arg):
        if frame.f_code.co_filename == "<string>" or event == "c_call":
            return
        if (event == "call" and frame.f_code is NetworkProxy.__exit__.__code__) or (event == "return" and frame.f_code is NetworkProxy.__enter__.__code__):
            return
        seen[0] += 1
        if seen[0] == point + 1:
            close()
            if also_raise:
                raise Interrupt

    sys.setprofile(profile)
    try:
        action()
    except Interrupt:
        return True
    finally:
        sys.setprofile(None)
    return False


def lock_is_free():
    got = []

    def take():
        if NetworkProxy._lock.acquire(timeout=5):
            got.append(True)
            NetworkProxy._lock.release()

    thread = threading.Thread(target=take)
    thread.start()
    thread.join(30)
    return got == [True]


original = {name: os.environ.get(name) for name in NetworkProxy.PROXY_VARS}
for scenario in ("exit, stream newer", "exit, stream older", "enter, stream older"):
    for also_raise in (False, True):
        point, points, closed_inside = -1, None, 0
        while points is None or point < points:
            seen = [0]
            if scenario == "exit, stream newer":
                shared.__enter__()
            gen = shared_stream()
            next(gen)
            if scenario == "exit, stream older":
                shared.__enter__()
            if scenario.startswith("exit"):
                fired = inject(lambda: shared.__exit__(None, None, None), point, seen, gen.close, also_raise)
            else:
                fired = inject(shared.__enter__, point, seen, gen.close, also_raise)
                if not fired:
                    shared.__exit__(None, None, None)
            if gen.gi_frame is not None:
                gen.close()  # the point was past the end
            elif points is not None:
                closed_inside += 1
            label = (scenario, also_raise, point, os.environ.get("HTTP_PROXY"), len(NetworkProxy._held))
            assert lock_is_free(), label
            assert NetworkProxy._sections == [] and not NetworkProxy._deferred and not NetworkProxy._exits, label
            assert not NetworkProxy._held and shared.backup == {}, label
            assert {name: os.environ.get(name) for name in NetworkProxy.PROXY_VARS} == original, label
            if points is None:
                points = seen[0]  # the first run counts the places
            point += 1
        assert closed_inside > 20, (scenario, also_raise, points, closed_inside)
""")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_network_proxy_works_in_a_child_forked_while_another_thread_holds_its_lock():
    _run_proxy_reentry_script("""
import threading, time, warnings
warnings.simplefilter("ignore", DeprecationWarning)  # Python 3.12+ warns about forking with threads
inside, release = threading.Event(), threading.Event()


def pause():
    inside.set()
    release.wait(30)


def hold():
    with NetworkProxy(http_proxy=Interrupting("http://thread")):
        pass


Interrupting.hook = pause
thread = threading.Thread(target=hold)
thread.start()
assert inside.wait(30)  # the thread is now inside the locked section
pid = os.fork()
if pid == 0:
    with NetworkProxy(http_proxy="http://child"):
        ok = os.environ["HTTP_PROXY"] == "http://child"
    os._exit(0 if ok else 1)
deadline = time.monotonic() + 20
while True:
    done, status = os.waitpid(pid, os.WNOHANG)
    if done or time.monotonic() > deadline:
        break
    time.sleep(0.05)
if not done:
    os.kill(pid, 9)
    os.waitpid(pid, 0)
release.set()
thread.join(30)
assert done and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, "the forked child deadlocked or failed"
assert not thread.is_alive() and settled()
""")


def test_command_include_and_live_handle_helpers():
    assert cmd([sys.executable, "-c", "print('chosen')"], include="out") == "chosen"
    selected = cmd([sys.executable, "-c", "print('chosen')"], include=["out", "code"])
    assert selected == {"out": "chosen", "code": 0}

    handle = cmd([sys.executable, "-c", "print('live')"], wait=False, include="handle")
    assert handle.wait(timeout=5) == 0

    with pytest.raises(subprocess.CalledProcessError):
        cmd([sys.executable, "-c", "import sys; sys.exit(3)"], check=True)


def test_debug_helpers_preserve_errors_and_suggest_values():
    with pytest.raises(ValueError) as mismatch:
        raise_mismatch(["json", "yaml"], "jsno", name="format")
    assert "Did you mean 'json'?" in str(mismatch.value)

    try:
        raise RuntimeError("token=abc123")
    except RuntimeError as exc:
        text = error_str(exc, tb=False)
    assert text == "RuntimeError: token=abc123"


def test_logging_file_stream_and_suppression_switches(tmp_path):
    logger_name = f"heavenbase.tests.{os.getpid()}"
    logger = get_logger(logger_name, level="DEBUG", fmt="%(levelname)s:%(message)s")
    set_log_level("INFO", [logger_name])

    log_path = tmp_path / "heavenbase.log"
    redirect_logs(log_path, loggers=[logger_name], fmt="%(levelname)s:%(message)s")
    try:
        logger.warning("ready")
        logger.success("done")
    finally:
        restore_logs([logger_name])
    assert log_path.read_text(encoding="utf-8").splitlines() == ["WARNING:ready", "SUCCESS:done"]

    stream = io.StringIO()
    redirect_logs(stream)
    try:
        configure_logs(level="INFO", stream_format="%(message)s")
        stream_logger = get_logger(f"heavenbase.tests.stream.{os.getpid()}")
        stream_logger.warning("stream-ready")
        suppress_logs()
        stream_logger.warning("hidden")
    finally:
        restore_logs()
    assert "\x1b[" not in stream.getvalue()
    assert strip_color(stream.getvalue()).splitlines() == ["stream-ready"]


def test_a_log_handler_subclass_that_overrides_emit_receives_every_record():
    # HbLogHandler.handle reads the log settings before its lock and called a
    # private emit, so an overriding emit of a subclass never ran.
    import logging

    from heavenbase.utils import HbLogHandler

    stream = io.StringIO()
    seen = []

    class Recording(HbLogHandler):
        def emit(self, record):
            seen.append(record.getMessage())
            super().emit(record)

    logger = logging.getLogger(f"heavenbase.tests.subclass.{os.getpid()}")
    logger.propagate = False
    handler = Recording(stream=stream, fmt="%(message)s")
    handler.level_override = logging.DEBUG
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        logger.info("first")
        logger.debug("second")
    finally:
        logger.removeHandler(handler)

    assert seen == ["first", "second"]
    assert strip_color(stream.getvalue()).splitlines() == ["first", "second"]


def test_public_utility_exports_are_available_from_package_root():
    import heavenbase as hb
    import heavenbase.utils as utils

    assert len(utils.__all__) == len(set(utils.__all__))
    assert hb.md_section(title="Root") == "# Root\n"
    assert not hasattr(hb, "fast_prompt_section")
    assert hb.utils.strings.fast_prompt_section is utils.fast_prompt_section
    assert hb.utils.parsers.parse_md is utils.parse_md
    assert hb.StableRNG(seed=3).rnd_int(0, 10) == StableRNG(seed=3).rnd_int(0, 10)
    for name in (
        "ErrorRecord",
        "HbJsonEncoder",
        "NetworkProxy",
        "append_jsonl",
        "browse",
        "clipboard",
        "coerce_bool",
        "coerce_dict",
        "coerce_tuple",
        "fast_prompt_section",
        "interpret_seed",
        "lflat",
        "load_lazy_export",
        "md_section",
        "raise_mismatch",
        "set_log_level",
    ):
        assert hasattr(utils, name)


def test_fast_prompt_section_assembles_agent_prompt():
    messages = fast_prompt_section(
        system="You are a concise worker.",
        descriptions={"Task": ["Read the files.", "Write the result."]},
        examples=[{"inputs": {"numbers": [1, 2]}, "output": 3}],
        instructions=["Use tools for filesystem access.", "Return one line."],
        instance={"inputs": {"numbers": [5, 8]}},
    )

    assert [message["role"] for message in messages] == ["user"]
    content = messages[0]["content"]
    assert "You are a concise worker." in content
    assert "## Task" in content
    assert "- Read the files." in content
    assert "<example_1>" in content
    assert "<new_instance>" in content
    assert "TODO" in content


def test_fast_prompt_section_separates_system_and_translates_titles():
    messages = fast_prompt_section(
        system="System",
        descriptions="Describe",
        instructions={"Rules": "Act"},
        separate_system=True,
        tr=lambda text: f"tr:{text}",
    )

    assert messages[0] == {"role": "system", "content": "tr:System"}
    assert messages[1]["role"] == "user"
    assert "## tr:Task Descriptions" in messages[1]["content"]
    assert "## tr:Rules" in messages[1]["content"]
    assert "- tr:Describe" in messages[1]["content"]


def test_lazy_export_helper_resolves_caches_and_reports_missing():
    namespace = {}

    value = load_lazy_export("sqrt", {"sqrt": ("math", "sqrt")}, None, namespace, owner="demo")
    assert value is math.sqrt
    assert namespace["sqrt"] is math.sqrt

    module = load_lazy_export("math", {"math": ("math", None)}, None, namespace, owner="demo")
    assert module is math
    assert namespace["math"] is math

    with pytest.raises(AttributeError, match="module 'demo' has no attribute 'missing'"):
        load_lazy_export("missing", {}, None, namespace, owner="demo")


def test_lazy_export_attachment_supports_introspection_and_caching():
    namespace = {"eager": object()}
    getter, directory = _attach_lazy_exports(
        "demo",
        namespace,
        {"sqrt": ("math", "sqrt")},
        ["eager", "sqrt"],
    )

    assert directory() == ["eager", "sqrt"]
    assert getter("sqrt") is math.sqrt
    assert namespace["sqrt"] is math.sqrt
    assert directory() == ["eager", "sqrt"]


def test_utils_import_without_legacy_basic():
    code = """
import builtins

orig_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == 'legacy.utils.basic' or name.startswith('legacy.utils.basic.'):
        raise RuntimeError(f'blocked import: {name}')
    return orig_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
from heavenbase import utils
assert utils.pj('a', 'b')
assert utils.md5hash({'a': 1}) == utils.md5hash({'a': 1})
"""
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[2] / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.fast
def test_serialization_and_parallel_settings_fall_back_on_any_config_error(monkeypatch):
    # Only a lasting failed open falls back inside the config manager now; these
    # helpers keep 0.1.2.2's rule that any config error gives their default.
    from heavenbase.utils import parallel, serialize
    from heavenbase.utils.config import CM_HVNB

    class OperationalError(Exception):
        pass

    def busy(*args, **kwargs):
        raise OperationalError("database is locked")

    monkeypatch.setitem(vars(CM_HVNB), "_get_or_packaged", busy)
    assert serialize._cfg("encoding", "utf-8") == "utf-8"
    assert parallel._cfg("max_workers", 3) == 3


@pytest.mark.fast
def test_a_global_log_setting_whose_save_fails_applies_to_the_process(monkeypatch):
    from heavenbase.utils import log
    from heavenbase.utils.config import CM_HVNB
    from heavenbase.utils.registry import RegistryConflictError

    def losing(path, value):
        raise RegistryConflictError("lost every compare-and-set")

    monkeypatch.setattr(log, "_PROCESS_SETTINGS", {})
    monkeypatch.setitem(vars(CM_HVNB), "_set_if_openable", losing)
    log.set_log_level("ERROR", persist=True)

    assert log._PROCESS_SETTINGS == {"level": "ERROR"}
    assert log._log_settings()["level"] == "ERROR"
