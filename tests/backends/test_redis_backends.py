"""Tests for the redis (KV) and redisearch (Query Engine) backends.

Two layers:

* **Offline unit tests** (no server, no ``redis`` client) cover the pure logic:
  the binary row codec, metric-aware scoring, the native/scan decision matrix,
  the SET-index set-algebra, and the provider-unavailable degrade path.
* **Docker-gated tests** (``@pytest.mark.external``) exercise live behavior; they
  skip when the ``redis`` client is missing or no Redis server is reachable on
  localhost:6379, and the redisearch ones also skip when the server lacks the
  Redis Query Engine module (plain Redis without Stack/8).

Bring a server up with, e.g.::

    docker run -p 6379:6379 redis/redis-stack-server:latest
"""

import math
import os
import pytest
import sys
from importlib.util import find_spec

sys.path.insert(0, "src")

import heavenbase as hb
from heavenbase.execution import QueryFragment, RowOp
from heavenbase.backends.redis.backend import RedisBackend
from heavenbase.backends.vector.redisearch import RediSearchBackend, tag_escape, _pack_vector, _unpack_vector, _TAG_SEPARATOR
from heavenbase.backends.vector.handler.redisearch import _clause, _native_ok

WORKSPACES = hb.DEFAULT_CONTEXT._workspace_registry

_OFFLINE_URL = "redis://127.0.0.1:1"  # nothing listens here -> connect refused fast, client degrades
_REDIS_URL = os.environ.get("HVNB_REDIS_URL", "redis://localhost:6379")
pytestmark = pytest.mark.detail


@pytest.fixture(autouse=True)
def _isolate_workspaces():
    before = set(WORKSPACES.keys())
    yield
    for wid in set(WORKSPACES.keys()) - before:
        WORKSPACES.pop(wid, None)


def _skip_unless_redis():
    if find_spec("redis") is None:
        pytest.skip("redis client is not installed")
    from redis import Redis

    client = Redis.from_url(_REDIS_URL, socket_connect_timeout=0.5, socket_timeout=0.5)
    try:
        client.ping()
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if "HVNB_REDIS_URL" in os.environ:
            pytest.fail(f"configured Redis test service is unavailable: {detail}", pytrace=False)
        pytest.skip(f"default Redis endpoint is not an authenticated test service: {detail}")
    finally:
        client.close()


def _skip_unless_query_engine(backend):
    """Skip when the reachable Redis server has no Query Engine (RediSearch) module."""
    try:
        backend.client.execute_command("FT._LIST")
    except Exception as exc:  # ResponseError: unknown command 'FT._LIST'
        pytest.skip(f"Redis Query Engine module unavailable: {type(exc).__name__}: {exc}")


@pytest.mark.external
def test_redis_kv_roundtrip_and_scan_filter():
    """Plain redis stores rows and answers filters through the scan fallback."""
    _skip_unless_redis()
    backend = hb.backends.RedisBackend("redis", url=_REDIS_URL)
    ws = hb.HeavenBase("docker-redis", backends={"redis": backend})

    class RedisItem(hb.Entity):
        identifier = "docker-redis-item"

        title = hb.field(hb.ShortText)
        category = hb.field(hb.ShortText)
        price = hb.field(hb.Float)

    ws.upsert_many(
        RedisItem,
        [
            {"object_id": "i1", "title": "cheap", "category": "a", "price": 9.0},
            {"object_id": "i2", "title": "mid", "category": "a", "price": 50.0},
            {"object_id": "i3", "title": "dear", "category": "b", "price": 200.0},
        ],
    )
    assert ws.get("i2", entity=RedisItem)["title"] == "mid"
    assert ws.count(RedisItem) == 3

    # range (lt) has no SET index -> scan fallback
    range_q = ws.query(RedisItem).where(RedisItem.price < 100).select("title")
    assert sorted(range_q.execute().ids) == ["i1", "i2"]
    assert range_q.explain()["steps"][0]["handler_mode"] == "py_scan"

    # eq / in resolve via native SET indexes
    eq_q = ws.query(RedisItem).where(RedisItem.category == "a")
    assert sorted(eq_q.execute().ids) == ["i1", "i2"]
    assert eq_q.explain()["steps"][0]["handler_mode"] == "native"
    assert sorted(ws.query(RedisItem).where(RedisItem.category.in_(["b"])).execute().ids) == ["i3"]

    # object_id eq/in resolve via direct key lookup (no SET index for the key)
    objid_q = ws.query(RedisItem).where(RedisItem.object_id == "i2")
    assert objid_q.execute().ids == ["i2"]
    assert objid_q.explain()["steps"][0]["handler_mode"] == "native"
    assert ws.query(RedisItem).where(RedisItem.object_id == "missing").execute().ids == []
    assert sorted(ws.query(RedisItem).where(RedisItem.object_id.in_(["i1", "i3"])).execute().ids) == ["i1", "i3"]

    # index stays consistent after an update (i2: a -> b)
    ws.set(RedisItem.from_dict({"object_id": "i2", "title": "mid", "category": "b", "price": 50.0}))
    assert sorted(ws.query(RedisItem).where(RedisItem.category == "a").execute().ids) == ["i1"]
    assert sorted(ws.query(RedisItem).where(RedisItem.category == "b").execute().ids) == ["i2", "i3"]

    ws.drop()
    assert ws.count(RedisItem) == 0


@pytest.mark.external
def test_redisearch_native_filter_and_near():
    """RediSearch executes native scalar filters, vector near, and combined_native."""
    _skip_unless_redis()
    backend = hb.backends.RediSearchBackend("rsearch", url=_REDIS_URL)
    _skip_unless_query_engine(backend)
    ws = hb.HeavenBase("docker-redisearch", backends={"rs": backend})

    class RsThing(hb.Entity):
        identifier = "docker-rsearch-thing"

        category = hb.field(hb.ShortText)
        label = hb.field(hb.ShortText)
        score = hb.field(hb.Float)
        embedding = hb.field(hb.Vector[2])

    ws.upsert_many(
        RsThing,
        [
            {"object_id": "a", "category": "x", "label": "Apple", "score": 10.0, "embedding": [1.0, 0.0]},
            {"object_id": "b", "category": "x", "label": "apricot", "score": 90.0, "embedding": [0.9, 0.1]},
            {"object_id": "c", "category": "y", "label": "Banana", "score": 50.0, "embedding": [0.0, 1.0]},
        ],
    )

    # native numeric range filter
    range_q = ws.query(RsThing).where(RsThing.score > 40)
    assert sorted(range_q.execute().ids) == ["b", "c"]
    assert range_q.explain()["steps"][0]["type"] == "redisearch"

    # native tag equality
    tag_q = ws.query(RsThing).where(RsThing.category == "x")
    assert sorted(tag_q.execute().ids) == ["a", "b"]

    # native tag prefix (starts_with) is CASE-SENSITIVE: case_sensitive=True TAG index
    # matches str.startswith exactly. 'Ap' -> 'Apple', 'ap' -> 'apricot' (no folding).
    starts_upper = ws.query(RsThing).where(RsThing.label.starts_with("Ap"))
    assert starts_upper.execute().ids == ["a"]
    assert starts_upper.explain()["steps"][0]["handler_mode"] == "native"
    assert ws.query(RsThing).where(RsThing.label.starts_with("ap")).execute().ids == ["b"]

    # ends_with has no native TAG suffix clause -> scan fallback, also case-sensitive
    ends_q = ws.query(RsThing).where(RsThing.label.ends_with("a"))
    assert ends_q.execute().ids == ["c"]
    assert ends_q.explain()["steps"][0]["handler_mode"] == "py_scan"

    # native vector near
    near_q = ws.query(RsThing).near(RsThing.embedding, [1.0, 0.0], top_k=1)
    assert near_q.execute().ids == ["a"]

    # combined filter + near in one FT.SEARCH -> combined_native
    combined_q = ws.query(RsThing).where(RsThing.category == "x").near(RsThing.embedding, [0.85, 0.15], top_k=1)
    assert combined_q.execute().ids == ["b"]
    assert "combined_native" in str(combined_q.explain())

    ws.drop()
    assert ws.count(RsThing) == 0


@pytest.mark.external
def test_redisearch_ensure_rejects_changed_vector_dim():
    """A pre-existing FT index whose vector dim differs from the schema is surfaced, not silently reused."""
    _skip_unless_redis()
    backend = hb.backends.RediSearchBackend("rsdim", url=_REDIS_URL)
    _skip_unless_query_engine(backend)
    ws = hb.HeavenBase("docker-rsearch-dim", backends={"rs": backend})

    class DimThing(hb.Entity):
        identifier = "docker-rsearch-dim-thing"

        embedding = hb.field(hb.Vector[3])

    ws.upsert_many(DimThing, [{"object_id": "a", "embedding": [1.0, 0.0, 0.0]}])  # creates a dim=3 index

    class DimThing4(hb.Entity):
        identifier = "docker-rsearch-dim-thing"  # same entity_id -> same FT index, but dim=4

        embedding = hb.field(hb.Vector[4])

    with pytest.raises(RuntimeError, match="dim="):
        backend.ensure(DimThing4.schema(), [])

    ws.drop()


@pytest.mark.external
def test_redisearch_ensure_rebuilds_stale_tag_separator():
    """A pre-existing ','-separator TAG index is rebuilt in place: comma values match, docs preserved."""
    _skip_unless_redis()

    from redis.commands.search.field import TagField
    from redis.commands.search.index_definition import IndexDefinition, IndexType

    backend = hb.backends.RediSearchBackend("rssep", url=_REDIS_URL)
    _skip_unless_query_engine(backend)
    ws = hb.HeavenBase("docker-rsearch-sep", backends={"rs": backend})

    class SepThing(hb.Entity):
        identifier = "docker-rsearch-sep-thing"

        code = hb.field(hb.ShortText)

    schema = SepThing.schema()
    index = backend._index(schema.entity_id)
    try:
        backend.text_client.ft(index).dropindex(delete_documents=True)
    except Exception:
        pass
    # stale index with the OLD ',' separator, plus an existing comma-containing row
    backend.text_client.ft(index).create_index(
        [TagField("object_id", separator=",", case_sensitive=True), TagField("code", separator=",", case_sensitive=True)],
        definition=IndexDefinition(prefix=[backend._prefix(schema.entity_id)], index_type=IndexType.HASH),
    )
    backend.client.hset(backend._key(schema.entity_id, "1"), mapping={b"object_id": b"1", b"code": "New York, NY".encode()})
    backend._ensured.clear()

    # upsert -> ensure() detects the separator drift and rebuilds in place (keeping row "1")
    ws.upsert_many(SepThing, [{"object_id": "2", "code": "Boston"}])
    assert ws.query(SepThing).where(SepThing.code == "New York, NY").execute().ids == ["1"]  # comma value now matches
    assert ws.count(SepThing) == 2  # the pre-existing row survived the rebuild
    ws.drop()


@pytest.mark.external
def test_redisearch_combined_scan_near_falls_back_not_crash():
    """near + a non-native filter (contains) returns scan-scored rows instead of raising NotImplementedError."""
    _skip_unless_redis()
    backend = hb.backends.RediSearchBackend("rscs", url=_REDIS_URL)
    _skip_unless_query_engine(backend)
    ws = hb.HeavenBase("docker-rsearch-cs", backends={"rs": backend})

    class CsThing(hb.Entity):
        identifier = "docker-rsearch-cs-thing"

        label = hb.field(hb.ShortText)
        embedding = hb.field(hb.Vector[2])

    ws.upsert_many(
        CsThing,
        [
            {"object_id": "a", "label": "alpha", "embedding": [1.0, 0.0]},
            {"object_id": "b", "label": "beta", "embedding": [0.9, 0.1]},
            {"object_id": "c", "label": "alphabet", "embedding": [0.0, 1.0]},
        ],
    )
    query = ws.query(CsThing).where(CsThing.label.contains("alpha")).near(CsThing.embedding, [1.0, 0.0], top_k=5)
    assert set(query.execute().ids) == {"a", "c"}  # only labels containing 'alpha'
    assert "combined_scan" in str(query.explain())
    ws.drop()


@pytest.mark.external
def test_redisearch_tag_eq_matches_value_with_metachars():
    """Native TAG eq matches a value with spaces/dots/commas.

    Regression for two bugs: tag_escape double-escaping (dot/space) and the default ','
    TAG separator splitting a comma-containing value into two tags at index time.
    """
    _skip_unless_redis()
    backend = hb.backends.RediSearchBackend("rstag", url=_REDIS_URL)
    _skip_unless_query_engine(backend)
    ws = hb.HeavenBase("docker-rsearch-tag", backends={"rs": backend})

    class TagThing(hb.Entity):
        identifier = "docker-rsearch-tag-thing"

        code = hb.field(hb.ShortText)

    ws.upsert_many(TagThing, [{"object_id": "1", "code": "a.b c,d"}, {"object_id": "2", "code": "other"}])
    query = ws.query(TagThing).where(TagThing.code == "a.b c,d")
    assert query.execute().ids == ["1"]  # old double-escape returned [] here
    assert query.explain()["steps"][0]["handler_mode"] == "native"
    ws.drop()


@pytest.mark.external
def test_redisearch_text_eq_is_exact_and_quote_safe():
    """Tokenized TEXT eq scans for an EXACT match (not a phrase) and tolerates a double-quote value."""
    _skip_unless_redis()
    backend = hb.backends.RediSearchBackend("rstext", url=_REDIS_URL)
    _skip_unless_query_engine(backend)
    ws = hb.HeavenBase("docker-rsearch-text", backends={"rs": backend})

    class TextThing(hb.Entity):
        identifier = "docker-rsearch-text-thing"

        body = hb.field(hb.LongText)

    ws.upsert_many(
        TextThing,
        [{"object_id": "1", "body": "foo"}, {"object_id": "2", "body": "foo bar"}, {"object_id": "3", "body": 'has "quote"'}],
    )
    exact = ws.query(TextThing).where(TextThing.body == "foo")
    assert exact.execute().ids == ["1"]  # exact match only, NOT the 'foo bar' phrase hit
    assert exact.explain()["steps"][0]["handler_mode"] == "py_scan"
    assert ws.query(TextThing).where(TextThing.body == 'has "quote"').execute().ids == ["3"]  # quote does not break the query
    ws.drop()


@pytest.mark.external
def test_redisearch_empty_in_returns_empty():
    """An empty in([]) returns no rows instead of compiling an invalid FT clause."""
    _skip_unless_redis()
    backend = hb.backends.RediSearchBackend("rsein", url=_REDIS_URL)
    _skip_unless_query_engine(backend)
    ws = hb.HeavenBase("docker-rsearch-ein", backends={"rs": backend})

    class EinThing(hb.Entity):
        identifier = "docker-rsearch-ein-thing"

        cat = hb.field(hb.ShortText)

    ws.upsert_many(EinThing, [{"object_id": "1", "cat": "x"}])
    assert ws.query(EinThing).where(EinThing.cat.in_([])).execute().ids == []
    ws.drop()


# ── offline unit tests (no server, no redis client required) ──


class _StubRedis:
    """Minimal client exposing only the SET reads ``_resolve_index`` performs."""

    def __init__(self, sets):
        self._sets = sets

    def smembers(self, key):
        return set(self._sets.get(key, set()))

    def sunion(self, keys):
        out = set()
        for key in keys:
            out |= self._sets.get(key, set())
        return out


class _FakeRedis:
    """In-memory redis covering the KV + SET ops the redis backend's write/index path uses."""

    def __init__(self):
        self.kv: dict = {}
        self.sets: dict = {}

    def mget(self, keys):
        return [self.kv.get(key) for key in keys]

    def get(self, key):
        return self.kv.get(key)

    def scan_iter(self, match=None):
        if match and match.endswith("*"):
            prefix = match[:-1]
            return [key for key in list(self.kv) if key.startswith(prefix)]
        return list(self.kv)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def sunion(self, keys):
        out = set()
        for key in keys:
            out |= self.sets.get(key, set())
        return out

    def pipeline(self, transaction=True):
        return _FakePipe(self)


class _FakePipe:
    def __init__(self, store):
        self._store = store
        self._ops = []

    def set(self, key, value):
        self._ops.append(("set", key, value))

    def delete(self, key):
        self._ops.append(("delete", key))

    def sadd(self, key, member):
        self._ops.append(("sadd", key, member))

    def srem(self, key, member):
        self._ops.append(("srem", key, member))

    def execute(self):
        for op, *args in self._ops:
            if op == "set":
                self._store.kv[args[0]] = args[1]
            elif op == "delete":
                self._store.kv.pop(args[0], None)
            elif op == "sadd":
                self._store.sets.setdefault(args[0], set()).add(args[1])
            elif op == "srem":
                self._store.sets.get(args[0], set()).discard(args[1])
        self._ops.clear()


def _index_leaf(backend, entity_id, op, field, value):
    """Build the ``index_filter`` fragment ``indexed_filter_handler`` would emit."""
    payload = {"value": value} if op == "eq" else {"values": list(value)}
    return QueryFragment(backend.name, op, field, payload, notes={"kind": "index_filter", "op": op, "entity_id": entity_id})


def test_pack_unpack_vector_roundtrips():
    assert _unpack_vector(_pack_vector([1.0, -0.5, 0.25])) == pytest.approx([1.0, -0.5, 0.25])
    assert _unpack_vector(None) is None
    assert _unpack_vector(b"") is None


def test_redisearch_codec_roundtrips_every_logical_type():
    """The binary HASH codec round-trips every logical type (vectors, json, non-ascii) and drops None."""
    backend = RediSearchBackend("unit-rs-codec", url=_OFFLINE_URL)  # offline: codec needs no client

    class CodecDoc(hb.Entity):
        identifier = "unit-rs-codec-doc"

        name = hb.field(hb.ShortText)
        score = hb.field(hb.Float)
        count = hb.field(hb.Integer)
        active = hb.field(hb.Boolean)
        meta = hb.field(hb.Json)
        embedding = hb.field(hb.Vector[3])

    schema = CodecDoc.schema()
    values = {"name": "café—ünïcode", "score": 1.5, "count": 7, "active": True, "meta": {"k": [1, 2]}, "embedding": [0.1, 0.2, 0.3]}
    decoded = backend._decode_row(schema, backend._encode_row(schema, "o1", values))

    assert decoded["object_id"] == "o1"
    assert decoded["name"] == "café—ünïcode"  # UTF-8 text round-trip
    assert decoded["score"] == pytest.approx(1.5)
    assert decoded["count"] == 7 and isinstance(decoded["count"], int)
    assert decoded["active"] is True
    assert decoded["meta"] == {"k": [1, 2]}  # json field via dumps_json/loads_json
    assert decoded["embedding"] == pytest.approx([0.1, 0.2, 0.3])

    # None values are not stored, so they are absent after decode (matches scan-predicate NULL handling).
    sparse = backend._decode_row(schema, backend._encode_row(schema, "o2", {"name": None, "score": 2.0}))
    assert "name" not in sparse and sparse["score"] == pytest.approx(2.0)


def test_redisearch_codec_roundtrips_artifact_bytes():
    """Artifact (BLOB) fields round-trip as raw bytes through the HASH codec.

    Regression: the encoder fell through to ``str(value).encode()``, turning ``b"\\x00\\xff"`` into
    ``b"b'\\\\x00\\\\xff'"`` (corruption). Redis HASH values are binary-safe, so bytes store/read raw.
    """
    backend = RediSearchBackend("unit-rs-artifact", url=_OFFLINE_URL)

    class BlobDoc(hb.Entity):
        identifier = "unit-rs-artifact-doc"

        name = hb.field(hb.ShortText)
        blob = hb.field(hb.Artifact)

    schema = BlobDoc.schema()
    payload = b"\x00\x01\xfe\xff binary \x00 payload"
    decoded = backend._decode_row(schema, backend._encode_row(schema, "o1", {"name": "n", "blob": payload}))
    assert decoded["blob"] == payload and isinstance(decoded["blob"], bytes)


def test_redis_backends_declare_binary_storage_tag():
    """redis stores rows as JSON, so Artifact bytes need base64 text; redisearch HASH stores raw bytes.

    A wrong contract here corrupts/rejects Artifact writes: a JSON backend declaring "bytes" would
    fail to serialise raw bytes, and a HASH backend declaring "base64" would double-encode.
    """
    assert RedisBackend("unit-redis-bin", url=_OFFLINE_URL).tag("binary-storage").value == "base64"
    assert RediSearchBackend("unit-rs-bin", url=_OFFLINE_URL).tag("binary-storage").value == "bytes"


def test_redisearch_score_is_metric_aware():
    backend = RediSearchBackend("unit-rs-score", url=_OFFLINE_URL)
    assert backend._score("COSINE", 0.25) == pytest.approx(0.75)  # 1 - distance == cosine similarity
    assert backend._score("L2", 2.0) == pytest.approx(-2.0)  # ordering only
    assert backend._score("IP", 2.0) == pytest.approx(-2.0)
    assert backend._metric("euclidean") == "L2"
    assert backend._metric("inner_product") == "IP"
    assert backend._metric(None) == "COSINE"


def test_redisearch_native_ok_matrix():
    class MatrixDoc(hb.Entity):
        identifier = "unit-rs-matrix-doc"

        tag = hb.field(hb.ShortText)
        body = hb.field(hb.LongText)
        score = hb.field(hb.Float)
        meta = hb.field(hb.Json)

    fields = MatrixDoc.schema().fields
    num, tag, text, js = fields["score"], fields["tag"], fields["body"], fields["meta"]
    assert all(_native_ok(num, op) for op in ("eq", "ne", "lt", "lte", "gt", "gte", "in"))
    # FT TEXT is tokenized (phrase match != exact equality) and unsafe to value-escape -> all text ops scan.
    assert not any(_native_ok(text, op) for op in ("eq", "ne", "starts_with", "in"))
    assert all(_native_ok(tag, op) for op in ("eq", "ne", "in", "starts_with"))
    assert not any(_native_ok(tag, op) for op in ("contains", "ends_with", "lt", "gt"))
    assert not any(_native_ok(js, op) for op in ("eq", "in"))  # json/array/hyper-g/vector never native


def test_redisearch_ft_type_mirrors_ft_field():
    # _ft_type is the single redis-free mirror of _ft_field's attribute-type decision; index drift
    # detection compares it against FT.INFO type names, so lock the per-logical-type mapping.
    class FtTypeDoc(hb.Entity):
        identifier = "unit-rs-fttype-doc"

        tag = hb.field(hb.ShortText)
        body = hb.field(hb.LongText)
        score = hb.field(hb.Float)
        n = hb.field(hb.Integer)
        flag = hb.field(hb.Boolean)
        vec = hb.field(hb.Vector[2])
        meta = hb.field(hb.Json)

    backend = RediSearchBackend("unit-rs-fttype", url=_OFFLINE_URL)
    fields = FtTypeDoc.schema().fields
    assert backend._ft_type(fields["tag"]) == "TAG"
    assert backend._ft_type(fields["flag"]) == "TAG"
    assert backend._ft_type(fields["body"]) == "TEXT"
    assert backend._ft_type(fields["score"]) == "NUMERIC"
    assert backend._ft_type(fields["n"]) == "NUMERIC"
    assert backend._ft_type(fields["vec"]) == "VECTOR"
    assert backend._ft_type(fields["meta"]) is None  # store-only -> Python scan


def test_redisearch_clause_syntax():
    class ClauseDoc(hb.Entity):
        identifier = "unit-rs-clause-doc"

        tag = hb.field(hb.ShortText)
        score = hb.field(hb.Float)

    fields = ClauseDoc.schema().fields
    num, tag = fields["score"], fields["tag"]
    assert _clause(num, "eq", 5) == "@score:[5 5]"
    assert _clause(num, "gt", 5) == "@score:[(5 +inf]"
    assert _clause(num, "in", [1, 2]) == "(@score:[1 1] | @score:[2 2])"
    assert _clause(tag, "eq", "a") == "@tag:{a}"
    assert _clause(tag, "starts_with", "a") == "@tag:{a*}"
    assert _clause(tag, "in", ["a", "b"]) == "@tag:{a|b}"


def test_tag_escape_single_pass_no_double_escape():
    # Each metacharacter gets exactly ONE backslash; the escape backslash is never
    # itself re-escaped (the old multi-pass loop doubled it for '.', ',', spaces, ...).
    assert tag_escape("a.b") == "a\\.b"
    assert tag_escape("a b,c") == "a\\ b\\,c"
    assert tag_escape("x{y}z") == "x\\{y\\}z"
    assert tag_escape("plain") == "plain"
    assert tag_escape("a\\b") == "a\\\\b"  # a literal backslash escapes to exactly two


def test_redisearch_empty_in_falls_back_to_scan():
    # Empty in([]) must not compile to a native @f:{} / () (invalid FT); it scans
    # (matching nothing), staying consistent with the scan backend.
    from heavenbase.execution import HandlerContext
    from heavenbase.backends.vector.handler.redisearch import redisearch_filter_handler

    class EmptyInDoc(hb.Entity):
        identifier = "unit-rs-emptyin-doc"

        tag = hb.field(hb.ShortText)

    field = EmptyInDoc.schema().fields["tag"]
    fragment = redisearch_filter_handler("in")(field, [], HandlerContext("rs", "redisearch", "inline"))
    assert fragment.notes.get("mode") == "py_scan"


def test_redisearch_empty_tag_operand_falls_back_to_scan():
    # An empty-string TAG operand compiles to invalid FT (@f:{} / -@f:{} / a leading empty alternative),
    # but the scan predicate still matches empty-string rows -> eq/ne/in with "" must scan to stay consistent.
    from heavenbase.execution import HandlerContext
    from heavenbase.backends.vector.handler.redisearch import redisearch_filter_handler

    class EmptyTagDoc(hb.Entity):
        identifier = "unit-rs-emptytag-doc"

        tag = hb.field(hb.ShortText)

    field = EmptyTagDoc.schema().fields["tag"]
    ctx = HandlerContext("rs", "redisearch", "inline")
    assert redisearch_filter_handler("eq")(field, "", ctx).notes["mode"] == "py_scan"
    assert redisearch_filter_handler("ne")(field, "", ctx).notes["mode"] == "py_scan"
    assert redisearch_filter_handler("in")(field, ["", "a"], ctx).notes["mode"] == "py_scan"
    # a non-empty value still pushes down natively
    assert redisearch_filter_handler("eq")(field, "a", ctx).notes["mode"] == "native"


def test_redisearch_none_numeric_operand_falls_back_to_scan():
    # A None operand on a NUMERIC field (here an integer-backed Categorical, the one numeric type whose
    # normalization preserves None) would compile to an invalid @f:[None None] range; it must scan,
    # matching the scan predicate that selects NULL rows. Regression: the None guard was scoped to
    # non-numeric fields, so numeric None operands slipped through to a native clause.
    from heavenbase.execution import HandlerContext
    from heavenbase.backends.vector.handler.redisearch import redisearch_filter_handler

    class NumNoneDoc(hb.Entity):
        identifier = "unit-rs-numnone-doc"

        level = hb.field(hb.Categorical([1, 2, 3], base="integer"))  # NUMERIC via stores_as_integer

    field = NumNoneDoc.schema().fields["level"]
    ctx = HandlerContext("rs", "redisearch", "inline")
    assert redisearch_filter_handler("eq")(field, None, ctx).notes["mode"] == "py_scan"
    assert redisearch_filter_handler("ne")(field, None, ctx).notes["mode"] == "py_scan"
    assert redisearch_filter_handler("in")(field, [1, None], ctx).notes["mode"] == "py_scan"


def test_redisearch_starts_with_short_prefix_falls_back_to_scan():
    # A prefix shorter than the index MINPREFIX (default 2) matches NOTHING natively in FT, but the
    # scan predicate matches any prefix -> sub-MINPREFIX prefixes must scan to stay consistent.
    from heavenbase.execution import HandlerContext
    from heavenbase.backends.vector.handler.redisearch import redisearch_filter_handler

    class PrefixDoc(hb.Entity):
        identifier = "unit-rs-prefix-doc"

        tag = hb.field(hb.ShortText)

    field = PrefixDoc.schema().fields["tag"]
    ctx = HandlerContext("rs", "redisearch", "inline")
    assert redisearch_filter_handler("starts_with")(field, "a", ctx).notes["mode"] == "py_scan"  # 1-char -> scan
    native = redisearch_filter_handler("starts_with")(field, "ap", ctx)  # >= MINPREFIX -> native
    assert native.notes["mode"] == "native" and native.notes["kind"] == "redis_filter"


def test_redis_and_redisearch_backends_are_registered():
    # Registry wiring is discoverable without a live server (offline regression guard).
    ids = hb.BackendType.identifiers()
    assert "redis" in ids and "redisearch" in ids
    assert isinstance(hb.BackendType.build("redis", {"type": "redis"}, ws_id="reg-check"), RedisBackend)
    assert isinstance(hb.BackendType.build("redisearch", {"type": "redis_query_engine"}, ws_id="reg-check"), RediSearchBackend)


def test_redis_indexed_fields_excludes_key_and_nonindexable():
    backend = RedisBackend("unit-redis-fields", url=_OFFLINE_URL)

    class IndexDoc(hb.Entity):
        identifier = "unit-redis-index-doc"

        category = hb.field(hb.ShortText)
        color = hb.field(hb.ShortText)
        embedding = hb.field(hb.Vector[2])
        meta = hb.field(hb.Json)

    schema = IndexDoc.schema()
    backend._schemas[schema.entity_id] = schema
    # object_id (the key) and vector/json (non-hashable) carry no SET index.
    assert sorted(backend._indexed_fields(schema.entity_id)) == ["category", "color"]


def test_redis_resolve_index_set_algebra():
    backend = RedisBackend("unit-redis-resolve", url=_OFFLINE_URL)
    entity_id = "ent"
    backend.client = _StubRedis(
        {
            backend._idx_key(entity_id, "category", "a"): {"r1", "r2"},
            backend._idx_key(entity_id, "category", "b"): {"r3"},
            backend._idx_key(entity_id, "color", "red"): {"r1", "r3"},
            backend._idx_key(entity_id, "color", "blue"): {"r2"},
        }
    )
    cat_a = _index_leaf(backend, entity_id, "eq", "category", "a")
    assert backend._resolve_index(entity_id, cat_a) == {"r1", "r2"}  # SMEMBERS
    assert backend._resolve_index(entity_id, _index_leaf(backend, entity_id, "in", "category", ["a", "b"])) == {"r1", "r2", "r3"}  # SUNION

    conj = QueryFragment(backend.name, "and", "", [cat_a, _index_leaf(backend, entity_id, "eq", "color", "red")])
    assert backend._resolve_index(entity_id, conj) == {"r1"}  # intersection

    disj = QueryFragment(
        backend.name,
        "or",
        "",
        [_index_leaf(backend, entity_id, "eq", "category", "b"), _index_leaf(backend, entity_id, "eq", "color", "blue")],
    )
    assert backend._resolve_index(entity_id, disj) == {"r2", "r3"}  # union

    # object_id resolves to the value(s) directly (it is the key, not an indexed column).
    assert backend._resolve_index(entity_id, _index_leaf(backend, entity_id, "eq", "object_id", "r9")) == {"r9"}
    assert backend._resolve_index(entity_id, _index_leaf(backend, entity_id, "in", "object_id", ["r1", "r5"])) == {"r1", "r5"}


def test_redis_index_execute_rechecks_candidate_rows():
    backend = RedisBackend("unit-redis-execute", url=_OFFLINE_URL)
    backend.client = _FakeRedis()

    class IndexedDoc(hb.Entity):
        identifier = "unit-redis-execute-doc"

        category = hb.field(hb.ShortText)

    schema = IndexedDoc.schema()
    backend.ensure(schema, [])
    backend.upsert([RowOp(schema.entity_id, "r1", {"category": "a"}), RowOp(schema.entity_id, "r2", {"category": "b"})])
    fragment = QueryFragment(
        backend.name,
        "eq",
        "category",
        {"value": "a"},
        notes={"kind": "index_filter", "op": "eq", "entity_id": schema.entity_id, "row_payload": lambda row: row.get("category") == "a"},
    )
    backend.client.sets[backend._idx_key(schema.entity_id, "category", "a")].add("r2")

    assert backend.execute(fragment).ids == ["r1"]


def test_redis_delete_removes_stale_set_index_members():
    backend = RedisBackend("unit-redis-delete", url=_OFFLINE_URL)
    backend.client = _FakeRedis()

    class DelDoc(hb.Entity):
        identifier = "unit-redis-delete-doc"

        category = hb.field(hb.ShortText)

    schema = DelDoc.schema()
    backend._schemas[schema.entity_id] = schema
    ent = schema.entity_id
    backend.upsert([RowOp(ent, "r1", {"category": "a"}), RowOp(ent, "r2", {"category": "a"})])
    idx = backend._idx_key(ent, "category", "a")
    assert backend.client.sets[idx] == {"r1", "r2"}

    # deleting r1 must srem it from the value index (else eq/in would return a ghost id)
    backend.upsert([RowOp(ent, "r1", {}, op="delete")])
    assert backend.client.sets[idx] == {"r2"}
    assert backend.client.kv.get(backend._key(ent, "r1")) is None


def test_redis_repeated_same_key_upserts_advance_index_shadow_state():
    """Every queued mutation computes index deltas from the preceding mutation's value."""
    backend = RedisBackend("unit-redis-repeated", url=_OFFLINE_URL)
    backend.client = _FakeRedis()

    class RepeatedDoc(hb.Entity):
        identifier = "unit-redis-repeated-doc"

        category = hb.field(hb.ShortText)

    schema = RepeatedDoc.schema()
    backend.ensure(schema, [])
    backend.upsert(
        [
            RowOp(schema.entity_id, "r1", {"category": "old"}),
            RowOp(schema.entity_id, "r1", {"category": "new"}),
        ]
    )

    old_index = backend._idx_key(schema.entity_id, "category", "old")
    new_index = backend._idx_key(schema.entity_id, "category", "new")
    assert backend.client.sets[old_index] == set()
    assert backend.client.sets[new_index] == {"r1"}
    assert backend.get_many(schema.entity_id, ["r1"])[0]["category"] == "new"


def test_redis_stops_before_delete_phase_after_rewrite_command_error():
    """A Redis runtime error in a rewrite phase must not continue to a target delete."""

    class FailingPipe(_FakePipe):
        def execute(self):
            failed = False
            for op, *args in self._ops:
                if self._store.fail and op == "sadd" and args[1] == "source":
                    failed = True
                    continue
                if op == "set":
                    self._store.kv[args[0]] = args[1]
                elif op == "delete":
                    self._store.kv.pop(args[0], None)
                elif op == "sadd":
                    self._store.sets.setdefault(args[0], set()).add(args[1])
                elif op == "srem":
                    self._store.sets.get(args[0], set()).discard(args[1])
            self._ops.clear()
            if failed:
                raise RuntimeError("injected Redis index rewrite failure")

    class FailingRedis(_FakeRedis):
        fail = False

        def pipeline(self, transaction=True):
            return FailingPipe(self)

    backend = RedisBackend("unit-redis-ordered-failure", url=_OFFLINE_URL)
    backend.client = FailingRedis()

    class OrderedDoc(hb.Entity):
        identifier = "unit-redis-ordered-failure-doc"

        category = hb.field(hb.ShortText)

    schema = OrderedDoc.schema()
    backend.ensure(schema, [])
    backend.upsert(
        [
            RowOp(schema.entity_id, "source", {"category": "old"}),
            RowOp(schema.entity_id, "target", {"category": "target"}),
        ]
    )
    backend.client.fail = True

    with pytest.raises(RuntimeError, match="injected Redis index rewrite failure"):
        backend.upsert(
            [
                RowOp(schema.entity_id, "source", {"category": "new"}),
                RowOp(schema.entity_id, "target", op="delete"),
            ]
        )

    assert backend.client.kv.get(backend._key(schema.entity_id, "target")) is not None


def test_redisearch_stops_before_delete_phase_after_rewrite_command_error():
    """A RediSearch runtime error must not continue to a later target delete."""

    class FailingHashPipe:
        def __init__(self, store):
            self._store = store
            self._ops = []

        def delete(self, key):
            self._ops.append(("delete", key))

        def hset(self, key, *, mapping):
            self._ops.append(("hset", key, mapping))

        def execute(self):
            failed = False
            for op, key, *args in self._ops:
                if self._store.fail and op == "hset" and key.endswith(":source"):
                    failed = True
                    continue
                if op == "delete":
                    self._store.hashes.pop(key, None)
                else:
                    self._store.hashes[key] = dict(args[0])
            self._ops.clear()
            if failed:
                raise RuntimeError("injected RediSearch hash rewrite failure")

    class FailingHashRedis:
        def __init__(self):
            self.hashes = {}
            self.fail = False

        def pipeline(self, transaction=True):
            return FailingHashPipe(self)

    backend = RediSearchBackend("unit-redisearch-ordered-failure", url=_OFFLINE_URL)
    backend.client = FailingHashRedis()

    class OrderedDoc(hb.Entity):
        identifier = "unit-redisearch-ordered-failure-doc"

        category = hb.field(hb.ShortText)

    schema = OrderedDoc.schema()
    backend._schemas[schema.entity_id] = schema
    backend.upsert(
        [
            RowOp(schema.entity_id, "source", {"category": "old"}),
            RowOp(schema.entity_id, "target", {"category": "target"}),
        ]
    )
    backend.client.fail = True

    with pytest.raises(RuntimeError, match="injected RediSearch hash rewrite failure"):
        backend.upsert(
            [
                RowOp(schema.entity_id, "source", {"category": "new"}),
                RowOp(schema.entity_id, "target", op="delete"),
            ]
        )

    assert backend._key(schema.entity_id, "target") in backend.client.hashes


def test_redis_ensure_backfills_index_for_preexisting_rows():
    """ensure() backfills the SET index for rows already in the keyspace.

    Without backfill, rows written before a field became indexable (or before this process started)
    are absent from the SET index, so native eq/in silently drops them (a false negative) even though
    a scan would find them. ensure() must reindex them so native eq/in == scan.
    """
    from heavenbase.utils import dumps_json

    backend = RedisBackend("unit-redis-backfill", url=_OFFLINE_URL)
    backend.client = _FakeRedis()

    class BackfillDoc(hb.Entity):
        identifier = "unit-redis-backfill-doc"

        category = hb.field(hb.ShortText)

    schema = BackfillDoc.schema()
    ent = schema.entity_id
    # Seed rows directly in KV with NO SET-index entries (as if written before indexing / in a prior process).
    backend.client.kv[backend._key(ent, "r1")] = dumps_json({"object_id": "r1", "category": "a"})
    backend.client.kv[backend._key(ent, "r2")] = dumps_json({"object_id": "r2", "category": "a"})
    assert backend.client.sets == {}  # no index yet

    backend.ensure(schema, [])  # records schema + backfills the SET index
    assert backend.client.sets[backend._idx_key(ent, "category", "a")] == {"r1", "r2"}

    # idempotent: a second ensure() does not error and keeps the index intact (run once per field-set).
    backend.ensure(schema, [])
    assert backend.client.sets[backend._idx_key(ent, "category", "a")] == {"r1", "r2"}


def test_redis_idx_key_is_stable_and_distinct():
    backend = RedisBackend("unit-redis-key", url=_OFFLINE_URL)
    assert backend._idx_key("e", "f", "v") == backend._idx_key("e", "f", "v")  # deterministic
    assert backend._idx_key("e", "f", "v1") != backend._idx_key("e", "f", "v2")  # distinct values
    assert backend._idx_key("e", "f1", "v") != backend._idx_key("e", "f2", "v")  # field-scoped
    assert backend.index_hash_function == "crc32"
    assert len(backend._idx_key("e", "f", "v").rsplit(":", 1)[-1]) == 10

    md5 = RedisBackend("unit-redis-key", url=_OFFLINE_URL, index_hash_function="md5", index_hash_length=42)
    assert md5._idx_key("e", "f", "v") != backend._idx_key("e", "f", "v")
    assert len(md5._idx_key("e", "f", "v").rsplit(":", 1)[-1]) == 42

    with pytest.raises(ValueError, match="index_hash_length"):
        RedisBackend("unit-redis-key", url=_OFFLINE_URL, index_hash_length=0)


def test_redis_idx_key_reads_hash_config(monkeypatch):
    manager = hb.DEFAULT_CONTEXT.config
    real_get = manager.get
    values = {"heavenbase.backend_defaults.redis.index_hash_function": "crc32", "heavenbase.backend_defaults.redis.index_hash_length": 10}
    monkeypatch.setitem(vars(manager), "get", lambda key, default=None: values.get(key, real_get(key, default=default)))

    backend = RedisBackend(
        "unit-redis-key-config",
        url=_OFFLINE_URL,
        resolver=hb.DEFAULT_CONTEXT.modules(),
        config=manager,
    )
    assert backend.index_hash_function == "crc32"
    assert len(backend._idx_key("e", "f", "v").rsplit(":", 1)[-1]) == 10


def test_redis_upsert_requires_ensure():
    """upsert raises (like redisearch) when ensure() has not registered the schema."""
    backend = RedisBackend("unit-redis-noensure", url=_OFFLINE_URL)

    class _StubWrite:
        def mget(self, keys):
            return [None for _ in keys]

        def pipeline(self, transaction=False):
            return self

    backend.client = _StubWrite()
    with pytest.raises(RuntimeError, match="ensure"):
        backend.upsert([RowOp("ghost-entity", "x", {"a": 1})])


def test_redis_unreachable_degrades_and_raises_lazily():
    backend = RedisBackend("unit-redis-down", url=_OFFLINE_URL)
    assert backend.client is None
    assert isinstance(backend.client_error, str) and backend.client_error
    with pytest.raises(RuntimeError, match="unavailable"):
        backend.scan("ent")


def test_redisearch_unreachable_degrades_and_raises_lazily():
    backend = RediSearchBackend("unit-rs-down", url=_OFFLINE_URL)
    assert backend.client is None and backend.text_client is None
    assert isinstance(backend.client_error, str) and backend.client_error
    with pytest.raises(RuntimeError, match="unavailable"):
        backend.scan("ent")


def test_redis_redisearch_keys_are_backend_namespaced():
    """A redis and a redisearch backend in one workspace+server use disjoint key/index namespaces.

    Both used to key rows as ``{ws}:{entity}:{object_id}`` — redis as a JSON string, redisearch as a
    HASH — so co-locating them on one server collided (WRONGTYPE / cross-overwrite, and one ``drop()``
    wiped the other's keys). The instance name now scopes every row key, FT index, and value index.
    """
    redis_b = RedisBackend("redis", url=_OFFLINE_URL, ws_id="shared")
    rsearch_b = RediSearchBackend("rsearch", url=_OFFLINE_URL, ws_id="shared")

    assert redis_b._key("e", "1") != rsearch_b._key("e", "1")  # no row-key collision on one server
    assert redis_b.name in redis_b._key("e", "1") and redis_b.name in redis_b._idx_key("e", "f", "v")
    assert rsearch_b.name in rsearch_b._key("e", "1") and rsearch_b.name in rsearch_b._index("e")

    # two redis instances on one server+workspace are isolated too (name, not just type, scopes keys)
    other = RedisBackend("redis2", url=_OFFLINE_URL, ws_id="shared")
    assert redis_b._key("e", "1") != other._key("e", "1")
    assert redis_b._idx_key("e", "f", "v") != other._idx_key("e", "f", "v")


def test_redisearch_ensure_raises_when_stale_index_drop_fails():
    """A real (non-missing) dropindex failure during a rebuild surfaces instead of leaving drift active.

    Old code swallowed every dropindex error and then treated the follow-up "Index already exists" as
    success — leaving the drifted index (wrong metric/separator) live while ensure() reported OK.
    """

    class _FailDropFT:
        def __init__(self, info):
            self._info = info

        def info(self):
            return self._info

        def dropindex(self, delete_documents=False):
            raise ConnectionError("connection refused")

    class _FailDropText:
        def __init__(self, info):
            self._ft = _FailDropFT(info)

        def ft(self, name):
            return self._ft

    backend = RediSearchBackend("unit-rs-driftfail", url=_OFFLINE_URL)

    class DriftDoc(hb.Entity):
        identifier = "unit-rs-driftfail-doc"

        code = hb.field(hb.ShortText)

    # FT.INFO reporting the old ',' separator on object_id -> _index_needs_rebuild() is True.
    drifted_info = {"attributes": [["identifier", "object_id", "type", "TAG", "SEPARATOR", ","]]}
    backend.client = object()  # non-None so ensure() does not early-return on a degraded client
    backend.text_client = _FailDropText(drifted_info)

    with pytest.raises(RuntimeError, match="could not drop stale index"):
        backend.ensure(DriftDoc.schema(), [])


@pytest.mark.full
def test_redisearch_filter_pages_past_one_page(monkeypatch):
    """A native filter pages through every match instead of truncating at one filter_limit page.

    Old code did a single ``paging(0, filter_limit)``, so a filter matching more than one page
    silently dropped rows before the public query's own limit/offset layer ran.
    """
    if find_spec("redis") is None:
        pytest.skip("redis client is not installed")
    from types import SimpleNamespace

    manager = hb.DEFAULT_CONTEXT.config
    real_get = manager.get
    monkeypatch.setitem(
        vars(manager),
        "get",
        lambda key, default=None: 2 if key == "heavenbase.backend_defaults.redisearch.filter_limit" else real_get(key, default=default),
    )
    backend = RediSearchBackend(
        "unit-rs-page",
        url=_OFFLINE_URL,
        resolver=hb.DEFAULT_CONTEXT.modules(),
        config=manager,
    )

    class PageDoc(hb.Entity):
        identifier = "unit-rs-page-doc"

        cat = hb.field(hb.ShortText)

    entity_id = PageDoc.schema().entity_id
    backend._schemas[entity_id] = PageDoc.schema()
    oids = ["o1", "o2", "o3", "o4", "o5"]
    docs = [SimpleNamespace(id=f"{backend._prefix(entity_id)}{oid}") for oid in oids]

    class _PageFT:
        def search(self, query, query_params=None):
            return SimpleNamespace(docs=docs[query._offset : query._offset + query._num], total=len(docs))

    class _PageText:
        def ft(self, name):
            return _PageFT()

    class _PagePipe:
        def __init__(self):
            self._keys: list = []

        def hgetall(self, key):
            self._keys.append(key)

        def execute(self):
            return [{b"object_id": key.rsplit(":", 1)[1].encode()} for key in self._keys]

    class _PageBin:
        def pipeline(self, transaction=False):
            return _PagePipe()

    backend.text_client = _PageText()
    backend.client = _PageBin()
    # The explicit backend config forces the page size below the match count so
    # the single-page path would have truncated.

    fragment = QueryFragment(backend.name, "in", "object_id", "@object_id:{o1|o2|o3|o4|o5}", notes={"kind": "redis_filter", "mode": "native"})
    frame = backend._run_filter(entity_id, backend._index(entity_id), fragment)
    assert sorted(frame.ids) == oids  # all 5 across 3 pages, not just the first page of 2


# --------------------------------------------------------------------------- review-fix regressions


def test_redisearch_pure_negation_is_anchored():
    """A purely-negative FT expression (bare ne / top-level NOT / OR-of-NOTs) is scoped with '*'.

    Regression: ``-@f:{v}`` alone returns ZERO docs under DIALECT 2, while the scan predicate
    returns the complement. ``_scoped_expr`` must anchor such expressions against the keyspace,
    and must NOT needlessly scope a query that already has a positive anchor.
    """
    backend = RediSearchBackend("unit-rs-neg-expr", url=_OFFLINE_URL)

    def leaf(op, clause):
        return QueryFragment(backend.name, op, "f", clause, notes={"kind": "redis_filter", "mode": "native"})

    eq, ne = leaf("eq", "@f:{x}"), leaf("ne", "-@f:{x}")
    assert not backend._is_negative(eq) and backend._scoped_expr(eq) == "@f:{x}"
    assert backend._is_negative(ne) and backend._scoped_expr(ne) == "* -@f:{x}"

    both = QueryFragment(backend.name, "and", "", [eq, ne], notes={})  # positive child anchors -> no scope
    nots = QueryFragment(backend.name, "or", "", [ne, ne], notes={})  # every branch negative -> scope
    negated = QueryFragment(backend.name, "not", "", [eq], notes={})  # negation of a positive -> scope
    assert not backend._is_negative(both) and backend._scoped_expr(both) == "(@f:{x}) (-@f:{x})"
    assert backend._is_negative(nots) and backend._scoped_expr(nots).startswith("* ")
    assert backend._is_negative(negated) and backend._scoped_expr(negated) == "* -(@f:{x})"


def test_redisearch_bare_ne_query_is_sent_scoped(monkeypatch):
    """End-to-end: _run_filter sends a bare native ``ne`` as ``* -@f:{v}``, not a bare negation."""
    if find_spec("redis") is None:
        pytest.skip("redis client is not installed")
    from types import SimpleNamespace

    backend = RediSearchBackend("unit-rs-neg-run", url=_OFFLINE_URL)

    class NegDoc(hb.Entity):
        identifier = "unit-rs-neg-doc"

        cat = hb.field(hb.ShortText)

    entity_id = NegDoc.schema().entity_id
    backend._schemas[entity_id] = NegDoc.schema()
    captured = {}

    class _FT:
        def search(self, query, query_params=None):
            captured["expr"] = query._query_string
            return SimpleNamespace(docs=[], total=0)

    backend.text_client = SimpleNamespace(ft=lambda name: _FT())
    backend.client = SimpleNamespace(pipeline=lambda transaction=False: SimpleNamespace(hgetall=lambda k: None, execute=lambda: []))

    ne_fragment = QueryFragment(backend.name, "ne", "cat", "-@cat:{x}", notes={"kind": "redis_filter", "mode": "native"})
    backend._run_filter(entity_id, backend._index(entity_id), ne_fragment)
    assert captured["expr"] == "* -@cat:{x}"
    eq_fragment = QueryFragment(backend.name, "eq", "cat", "@cat:{x}", notes={"kind": "redis_filter", "mode": "native"})
    backend._run_filter(entity_id, backend._index(entity_id), eq_fragment)
    assert captured["expr"] == "@cat:{x}"  # positive query is not needlessly scoped


def test_redisearch_mixed_native_scan_logic_degrades_to_scan(monkeypatch):
    """A logical filter mixing a native child with a scan-only child degrades to a Python scan.

    Regression: ``execute`` routed every ``and``/``or``/``not`` fragment into the native FT branch
    whenever an index existed, then raised ``NotImplementedError`` for any non-native combination
    (e.g. ``tag == "x" AND body.contains("y")``), leaving the scan fallback unreachable. Such mixed
    fragments must degrade to ``super().execute`` instead of crashing.
    """
    backend = RediSearchBackend("unit-rs-mixed", url=_OFFLINE_URL)

    class MixDoc(hb.Entity):
        identifier = "unit-rs-mixed-doc"

        tag = hb.field(hb.ShortText)
        body = hb.field(hb.LongText)

    entity_id = MixDoc.schema().entity_id
    backend._schemas[entity_id] = MixDoc.schema()
    rows = [
        {"object_id": "a", "tag": "x", "body": "has needle"},
        {"object_id": "b", "tag": "x", "body": "no match"},
        {"object_id": "c", "tag": "y", "body": "has needle"},
    ]
    # have=True so the old code would enter the native branch; scan supplies the rows the degraded path filters.
    monkeypatch.setattr(backend, "_need_client", lambda: None)
    monkeypatch.setattr(backend, "_has_index", lambda eid: True)
    monkeypatch.setattr(backend, "scan", lambda eid: rows)

    def leaf(op, field, predicate, *, native):
        notes = {"row_payload": predicate, "entity_id": entity_id}
        if native:
            notes.update(kind="redis_filter", mode="native")
        return QueryFragment(backend.name, op, field, "", notes=notes)

    native_tag = leaf("eq", "tag", lambda row: row.get("tag") == "x", native=True)
    scan_body = leaf("contains", "body", lambda row: "needle" in (row.get("body") or ""), native=False)
    mixed = QueryFragment(backend.name, "and", "*", [native_tag, scan_body], notes={"kind": "logic", "entity_id": entity_id})

    assert not backend._is_rs_fragment(mixed)  # genuinely mixed -> not natively executable
    assert backend.execute(mixed).ids == ["a"]  # degrades to scan instead of raising; tag == x AND body contains needle


def test_redisearch_index_rebuilds_when_indexed_field_is_added(monkeypatch):
    """An indexed schema field absent from the existing FT index is recoverable drift -> rebuild.

    Regression: ``_index_needs_rebuild`` only checked metric/separator drift and skipped any field
    missing from the index (``spec is None``). Adding an indexed field to the schema after the index
    was built left ``ensure`` returning early, so native filters / vector ``near`` on the new field
    queried an index that did not contain it. A store-only field (json/array) absent from the index
    must NOT force a rebuild.
    """
    backend = RediSearchBackend("unit-rs-add-field", url=_OFFLINE_URL)

    class AddDoc(hb.Entity):
        identifier = "unit-rs-add-doc"

        cat = hb.field(hb.ShortText)  # indexed TAG, present in the old index
        score = hb.field(hb.Float)  # newly indexed NUMERIC, absent from the old index
        blob = hb.field(hb.Json)  # store-only, never indexed

    schema = AddDoc.schema()
    index = backend._index(schema.entity_id)
    old = {
        "object_id": {"type": "TAG", "separator": _TAG_SEPARATOR},
        "cat": {"type": "TAG", "separator": _TAG_SEPARATOR},
    }
    monkeypatch.setattr(backend, "_index_field_specs", lambda idx: old)
    assert backend._index_needs_rebuild(schema, index) is True  # missing indexed `score` -> rebuild

    full = dict(old, score={"type": "NUMERIC"})  # `score` now indexed; only store-only `blob` is absent
    monkeypatch.setattr(backend, "_index_field_specs", lambda idx: full)
    assert backend._index_needs_rebuild(schema, index) is False  # store-only field absent is not drift


def test_redisearch_index_rebuilds_when_field_type_changes(monkeypatch):
    """A schema field whose FT attribute type changed under the same name is recoverable drift -> rebuild.

    Regression: ``_index_needs_rebuild`` compared within-type drift (vector dim/metric, tag separator)
    but never the attribute TYPE itself, so changing a field e.g. ShortText(TAG) -> Integer(NUMERIC) left
    ``ensure`` returning early against an index whose attribute had the wrong FT type.
    """
    backend = RediSearchBackend("unit-rs-type-change", url=_OFFLINE_URL)

    class TypeDoc(hb.Entity):
        identifier = "unit-rs-type-doc"

        code = hb.field(hb.Integer)  # NUMERIC now; the old index built `code` as a ShortText TAG

    schema = TypeDoc.schema()
    index = backend._index(schema.entity_id)
    old_tag = {
        "object_id": {"type": "TAG", "separator": _TAG_SEPARATOR},
        "code": {"type": "TAG", "separator": _TAG_SEPARATOR},
    }
    monkeypatch.setattr(backend, "_index_field_specs", lambda idx: old_tag)
    assert backend._index_needs_rebuild(schema, index) is True  # TAG -> NUMERIC type drift -> rebuild

    matching = {"object_id": {"type": "TAG", "separator": _TAG_SEPARATOR}, "code": {"type": "NUMERIC"}}
    monkeypatch.setattr(backend, "_index_field_specs", lambda idx: matching)
    assert backend._index_needs_rebuild(schema, index) is False  # types already match -> no rebuild


def test_boolean_filter_value_canonicalizes_operands():
    """Boolean.filter_value re-validates eq/ne/in operands so 1/0 match the stored bool, and None passes through."""
    b = hb.Boolean()
    assert b.filter_value("eq", 1) is True
    assert b.filter_value("eq", 0) is False
    assert b.filter_value("eq", "yes") is True
    assert b.filter_value("in", [1, 0]) == [True, False]
    assert b.filter_value("eq", None) is None  # passes through to the handler None scan-guard


def test_redis_indexed_boolean_numeric_operand_matches_stored_key():
    """``flag == 1`` must probe the same SET-index key as a stored ``True`` (Boolean.filter_value canonicalizes)."""
    from heavenbase.execution import HandlerContext
    from heavenbase.execution.handlers.indexed import indexed_filter_handler

    class FlagDoc(hb.Entity):
        identifier = "unit-redis-flag-doc"

        flag = hb.field(hb.Boolean)

    field = FlagDoc.schema().fields["flag"]
    frag = indexed_filter_handler("eq")(field, 1, HandlerContext("r", "redis", "inline", scalar_indexed=True))
    assert frag.notes["mode"] == "native" and frag.payload["value"] is True
    backend = RedisBackend("unit-redis-flag", url=_OFFLINE_URL)
    assert backend._idx_key("e", "flag", frag.payload["value"]) == backend._idx_key("e", "flag", True)


def test_redis_indexed_none_operand_falls_back_to_scan():
    """``eq None`` / ``in [None]`` on the SET index must scan (None is never indexed -> false negatives)."""
    from heavenbase.execution import HandlerContext
    from heavenbase.execution.handlers.indexed import indexed_filter_handler

    class NoneDoc(hb.Entity):
        identifier = "unit-redis-none-doc"

        cat = hb.field(hb.ShortText)

    field = NoneDoc.schema().fields["cat"]
    ctx = HandlerContext("r", "redis", "inline", scalar_indexed=True)
    assert indexed_filter_handler("eq")(field, None, ctx).notes["mode"] == "py_scan"
    assert indexed_filter_handler("in")(field, [None, "a"], ctx).notes["mode"] == "py_scan"
    assert indexed_filter_handler("eq")(field, "a", ctx).notes["mode"] == "native"  # non-None still native


def test_redis_array_contains_uses_text_item_index_and_updates():
    """Text-like array_contains uses Redis SET indexes; numeric arrays stay on scan for Python equality parity."""
    from heavenbase.execution import HandlerContext
    from heavenbase.execution.handlers.seed import seed_handlers

    class RedisArrayDoc(hb.Entity):
        identifier = "unit-redis-array-doc"

        tags = hb.field(hb.Array[hb.ShortText])
        numbers = hb.field(hb.Array[hb.Integer])

    backend = RedisBackend("unit-redis-array", url=_OFFLINE_URL)
    backend.client = _FakeRedis()
    backend.client_error = ""
    schema = RedisArrayDoc.schema()
    backend.ensure(schema, [])
    backend.upsert(
        [
            RowOp(schema.entity_id, "a", {"tags": ["red", "blue"], "numbers": [1]}),
            RowOp(schema.entity_id, "b", {"tags": ["blue"], "numbers": [2]}),
        ]
    )

    handlers = seed_handlers()
    ctx = HandlerContext(backend.name, "redis", hb.InlineColumn.identifier)
    fragment = handlers.get(hb.Array, "array_contains", "redis", hb.InlineColumn.identifier)(schema.fields["tags"], "red", ctx)
    fragment.notes["entity_id"] = schema.entity_id
    assert fragment.notes["mode"] == "native"
    assert backend.execute(fragment).ids == ["a"]

    backend.upsert([RowOp(schema.entity_id, "a", {"tags": ["green"], "numbers": [1]})])
    assert backend.execute(fragment).ids == []

    numeric_fragment = handlers.get(hb.Array, "array_contains", "redis", hb.InlineColumn.identifier)(schema.fields["numbers"], 1, ctx)
    assert numeric_fragment.notes["mode"] == "py_scan"


def test_redisearch_none_tag_operand_falls_back_to_scan():
    """``eq/ne None`` and ``in [None]`` on a TAG/boolean field must scan, not compile a literal-"None" clause."""
    from heavenbase.execution import HandlerContext
    from heavenbase.backends.vector.handler.redisearch import redisearch_filter_handler

    class NoneTagDoc(hb.Entity):
        identifier = "unit-rs-none-tag-doc"

        cat = hb.field(hb.ShortText)
        flag = hb.field(hb.Boolean)

    fields = NoneTagDoc.schema().fields
    ctx = HandlerContext("rs", "redisearch", "inline")
    assert redisearch_filter_handler("eq")(fields["cat"], None, ctx).notes["mode"] == "py_scan"
    assert redisearch_filter_handler("ne")(fields["cat"], None, ctx).notes["mode"] == "py_scan"
    assert redisearch_filter_handler("in")(fields["cat"], [None, "a"], ctx).notes["mode"] == "py_scan"
    assert redisearch_filter_handler("eq")(fields["flag"], None, ctx).notes["mode"] == "py_scan"  # boolean eq None matched all-False natively
    assert redisearch_filter_handler("eq")(fields["cat"], "a", ctx).notes["mode"] == "native"  # non-None still native


def test_vector_metric_scores_rank_correctly():
    """euclidean_score / inner_product_score rank monotonically with their metric (ordering parity with native KNN)."""
    from heavenbase.types import cosine_score, euclidean_score, inner_product_score

    q, aligned_far, nearer = [1.0, 0.0], [10.0, 0.0], [1.0, 0.2]
    assert cosine_score(aligned_far, q) > cosine_score(nearer, q)  # cosine prefers the aligned vector
    assert euclidean_score(nearer, q) > euclidean_score(aligned_far, q)  # L2 prefers the nearer vector
    assert inner_product_score(aligned_far, q) > inner_product_score(nearer, q)  # IP prefers the larger dot product


def test_redisearch_near_scan_scorer_follows_the_requested_metric(monkeypatch):
    """The near scan scorer ranks by the REQUESTED metric; only a near matching the index ranks natively.

    Regressions: the payload hardcoded cosine_score, so under an L2 index combined_scan and
    combined_native returned different top-k; then it followed the index metric, so near(metric='l2')
    on a cosine index ranked by cosine. Two vectors where cosine and L2 disagree must flip order
    with the requested metric.
    """
    from heavenbase.execution import HandlerContext
    from heavenbase.backends.vector.handler.redisearch import redisearch_near_handler
    from heavenbase.strategies import VectorIndex

    class NearDoc(hb.Entity):
        identifier = "unit-rs-near-metric-doc"

        embedding = hb.field(hb.Vector[2])

    field = NearDoc.schema().fields["embedding"]
    manager = hb.DEFAULT_CONTEXT.config
    far_by_l2, near_by_l2 = {"embedding": [10.0, 0.0]}, {"embedding": [1.0, 0.2]}
    real_get = manager.get

    def _near(index_metric, metric):
        monkeypatch.setitem(
            vars(manager),
            "get",
            lambda key, default=None: index_metric if key == "heavenbase.backend_defaults.redisearch.metric" else real_get(key, default=default),
        )
        ctx = HandlerContext("rs", "redisearch", VectorIndex.identifier, metric=metric, resolver=hb.DEFAULT_CONTEXT.modules(), config=manager)
        return redisearch_near_handler(field, [1.0, 0.0], ctx)

    cosine = _near("cosine", "cosine")
    assert cosine.mode() == "native"
    assert cosine.notes["row_payload"](far_by_l2) > cosine.notes["row_payload"](near_by_l2)  # cosine prefers the aligned vector

    for index_metric in ("l2", "cosine"):
        l2 = _near(index_metric, "l2")
        assert l2.mode() == ("native" if index_metric == "l2" else "py_scan")
        l2_payload = l2.notes["row_payload"]
        assert l2_payload(near_by_l2) > l2_payload(far_by_l2)  # L2 prefers the nearer vector, whatever the index ranks by
    assert cosine.notes["row_payload"]({}) is None and l2_payload({}) is None  # KNN never returns a row without a vector
    with pytest.raises(ValueError, match="unsupported metric"):
        _near("cosine", "not-a-metric")


@pytest.mark.parametrize("filtered", [False, True])
def test_redisearch_near_routed_to_the_scan_does_not_run_knn(monkeypatch, filtered):
    """A near whose metric is not the index metric runs the scan, with or without a native filter.

    Regression: execute ran FT KNN for every near once the index existed, so near(metric='l2') on a
    cosine index ranked and scored by cosine while explain() reported py_scan.
    """
    from heavenbase.execution import HandlerContext
    from heavenbase.backends.vector.handler.redisearch import redisearch_filter_handler, redisearch_near_handler
    from heavenbase.strategies import VectorIndex

    class ScanNearDoc(hb.Entity):
        identifier = "unit-rs-scan-near-doc"

        title = hb.field(hb.ShortText)
        embedding = hb.field(hb.Vector[2])

    schema = ScanNearDoc.schema()
    backend = RediSearchBackend("unit-rs-scan-near", url=_OFFLINE_URL)
    rows = [
        {"object_id": "far", "title": "x-far", "embedding": [10.0, 1.0]},  # nearest by cosine
        {"object_id": "close", "title": "x-close", "embedding": [1.0, 0.5]},  # nearest by L2
    ]
    monkeypatch.setattr(backend, "_need_client", lambda: None)
    monkeypatch.setattr(backend, "_has_index", lambda entity_id: True)
    monkeypatch.setattr(backend, "scan", lambda entity_id: [dict(row) for row in rows])

    def _knn(*args, **kwargs):
        raise AssertionError("a py_scan near must not run KNN, which ranks by the index metric")

    monkeypatch.setattr(backend, "_run_near", _knn)
    ctx = HandlerContext("unit-rs-scan-near", "redisearch", VectorIndex.identifier, top_k=2, metric="l2", resolver=hb.DEFAULT_CONTEXT.modules())
    near = redisearch_near_handler(schema.fields["embedding"], [1.0, 0.0], ctx)
    assert near.mode() == "py_scan"  # the default index metric is cosine
    if filtered:
        filt = redisearch_filter_handler("starts_with")(schema.fields["title"], "x-", ctx)
        assert filt.kind() == "redis_filter"  # pushable on its own, so only the near decides the route
        near.notes.update({"filter": filt, "near_filter_mode": "combined_scan"})

    result = backend.execute(near.bind(schema.entity_id)).rows()

    assert [(row["object_id"], round(row["score"], 6)) for row in result] == [("close", -0.5), ("far", round(-math.sqrt(82.0), 6))]


def test_redisearch_near_routes_and_scores_by_the_metric_its_index_was_built_with(monkeypatch):
    """A config change after an FT index exists does not change how the index ranks, so it must not change the route.

    Regression: the route and the native distance-to-score conversion read
    heavenbase.backend_defaults.redisearch.metric, so after that key changed with no re-ensure a near
    requesting the new metric ran KNN, which ranked by the index's own metric, and scored that
    distance as if it were the new one.
    """
    if find_spec("redis") is None:
        pytest.skip("redis client is not installed")  # the native near imports the client to build its query
    from heavenbase.execution import HandlerContext
    from heavenbase.backends.vector.handler.redisearch import redisearch_near_handler
    from heavenbase.strategies import VectorIndex

    class BuiltMetricDoc(hb.Entity):
        identifier = "unit-rs-built-metric-doc"

        embedding = hb.field(hb.Vector[2])

    schema = BuiltMetricDoc.schema()
    backend = RediSearchBackend("unit-rs-built-metric", url=_OFFLINE_URL)
    backend._schemas[schema.entity_id] = schema
    real_get = backend.config.get
    monkeypatch.setattr(backend, "client", object())  # stands in for a connected client
    monkeypatch.setattr(backend, "_need_client", lambda: None)
    monkeypatch.setattr(backend, "_has_index", lambda entity_id: True)
    monkeypatch.setattr(backend, "_index_field_specs", lambda index: {"embedding": {"type": "VECTOR", "dim": 2, "metric": "COSINE"}})
    monkeypatch.setattr(backend, "_fetch_rows", lambda entity_id, ids: [{"object_id": object_id} for object_id in ids])
    # The index was built as COSINE; the config now names L2 (changed after the build).
    monkeypatch.setitem(
        vars(backend.config),
        "get",
        lambda key, default=None, **kwargs: "l2" if key == "heavenbase.backend_defaults.redisearch.metric" else real_get(key, default=default, **kwargs),
    )

    class Doc:
        def __init__(self, object_id, distance):
            self.id = f"{backend._prefix(schema.entity_id)}{object_id}"
            setattr(self, "__score", distance)

    class Search:
        def search(self, query, query_params=None):
            return type("Result", (), {"docs": [Doc("far", 0.25), Doc("close", 0.5)], "total": 2})()

    monkeypatch.setattr(backend, "text_client", type("TextClient", (), {"ft": lambda self, index: Search()})())

    def _near(metric):
        ctx = HandlerContext(
            "unit-rs-built-metric",
            "redisearch",
            VectorIndex.identifier,
            top_k=2,
            metric=metric,
            resolver=hb.DEFAULT_CONTEXT.modules(),
            config=backend.config,
            backend_instance=backend,
            entity_id=schema.entity_id,
        )
        return redisearch_near_handler(schema.fields["embedding"], [1.0, 0.0], ctx)

    assert backend.near_metric(schema.entity_id, "embedding") == "COSINE"
    l2 = _near("l2")
    assert l2.mode() == "py_scan" and "RediSearch indexes use COSINE" in l2.fallback_reason()
    cosine = _near("cosine")
    assert cosine.mode() == "native" and cosine.payload["index_metric"] == "COSINE"

    rows = backend.execute(cosine.bind(schema.entity_id)).rows()

    assert [(row["object_id"], row["score"]) for row in rows] == [("far", 0.75), ("close", 0.5)]  # 1 - cosine distance
    backend._schemas.clear()  # no index exists for an entity not stored here: the configured metric applies
    assert backend.near_metric(schema.entity_id, "embedding") == "L2"
