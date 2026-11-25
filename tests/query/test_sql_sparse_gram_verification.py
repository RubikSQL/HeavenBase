import heavenbase as hb
import pytest

from heavenbase.backends.sql.strategies.gram import runtime


def _workspace(tmp_path):
    database = hb.utils.path_to_file_uri(tmp_path / "gram-verification.db")
    return hb.HeavenBase("gram-verification", detached=True, backends={"main": {"type": "sqlite", "database": database}})


def _dash_tokens(text, _config):
    return text.split("-")


_tokenized_lengths = []


def _context_tokens(text, _config):
    _tokenized_lengths.append(len(text))
    return text[4:].split(",") if text.startswith("csv:") else text.split("-")


@pytest.mark.slow
@pytest.mark.parametrize(
    ("query", "starts"),
    [
        pytest.param("csv:Catfish,Cat,Fish", [12], id="contextual"),
        pytest.param("-".join(["Cat"] * 1024), range(0, 4095, 4), id="repeated"),
    ],
)
def test_contained_in_verifies_custom_tokens_in_full_query_context(tmp_path, query, starts):
    tokenizer = "r8gram_context_tokens"
    hb.ext.register_gram_tokenizer(tokenizer, _context_tokens, replace=True)

    class Doc(hb.Entity):
        identifier = "gram-contextual-token-verification"
        keywords = hb.field(hb.Array[hb.ShortText]).store(
            to="main", strategy=hb.SparseGramIndex(normalizer="none", gram_unit="token", tokenizer=tokenizer, hash_bits=1)
        )

    ws = _workspace(tmp_path)
    try:
        ws.register(Doc)
        ws.upsert(Doc, {"object_id": "x", "keywords": ["Cat"]})
        _tokenized_lengths.clear()
        result = ws.query(Doc).where(Doc.keywords.contained_in(query)).execute()
        expected = [[start, start + 3] for start in starts]
        assert result.ids == ["x"]
        assert [match["query_span"] for match in result.column("match")[0]] == expected
        assert sum(_tokenized_lengths) <= len(query) * 8

        adapter = ws.backends.get("main").strategy_adapter(hb.SparseGramIndex)
        scan = adapter.contained_in_scan_matcher(Doc.identifier, "keywords", query)
        assert [match["query_span"] for match in scan(["Cat"])] == expected
        assert adapter.contained_in_probe(Doc.identifier, "keywords", query)["exact_matches"] == 1
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize(
    ("bits", "tokenizer", "text", "spans"),
    [
        pytest.param(1, "space", "Catfish dog", [], id="collision"),
        pytest.param(32, "space", "Catfish and Cat", [[12, 15]], id="space"),
        pytest.param(32, "r8gram_dash", "Catfish-dog-Cat", [[12, 15]], id="custom"),
    ],
)
def test_contained_in_verifies_each_token_span(tmp_path, bits, tokenizer, text, spans):
    if tokenizer == "r8gram_dash":
        hb.ext.register_gram_tokenizer(tokenizer, _dash_tokens, replace=True)

    class Doc(hb.Entity):
        identifier = "gram-token-verification"
        keywords = hb.field(hb.Array[hb.ShortText]).store(
            to="main", strategy=hb.SparseGramIndex(normalizer="none", gram_unit="token", tokenizer=tokenizer, hash_bits=bits)
        )

    ws = _workspace(tmp_path)
    try:
        ws.register(Doc)
        ws.upsert(Doc, {"object_id": "x", "keywords": ["Cat"]})
        adapter = ws.backends.get("main").strategy_adapter(hb.SparseGramIndex)
        result = ws.query(Doc).where(Doc.keywords.contained_in(text)).execute()
        assert result.ids == (["x"] if spans else [])
        assert [match["query_span"] for row in result.column("match") for match in row] == spans
        scan = adapter.contained_in_scan_matcher(Doc.identifier, "keywords", text)
        assert [match["query_span"] for match in scan(["Cat"])] == spans
        probe = adapter.contained_in_probe(Doc.identifier, "keywords", text)
        assert probe["candidates"] == 1
        assert probe["exact_matches"] == int(bool(spans))
    finally:
        ws.release()


@pytest.mark.fast
def test_long_keyword_widths_share_query_hashes(tmp_path, monkeypatch):
    class Doc(hb.Entity):
        identifier = "gram-long-widths"
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="none", gram_width=3))

    ws = _workspace(tmp_path)
    try:
        ws.register(Doc)
        text = "abcdefghijklmnopqrstuvwxyz" * 8
        widths = range(4, 20)
        ws.upsert(Doc, {"object_id": "x", "keywords": [text[:width] for width in widths]})
        calls = []
        original = runtime.query_grams

        def query_grams(*args, **kwargs):
            calls.append(kwargs["short_widths"])
            return original(*args, **kwargs)

        monkeypatch.setattr(runtime, "query_grams", query_grams)
        result = ws.query(Doc).where(Doc.keywords.contained_in(text)).execute()
        assert result.ids == ["x"]
        assert {len(match["keyword"]) for match in result.column("match")[0]} == set(widths)
        assert len(calls) <= 2
    finally:
        ws.release()


def _priority_tokens(text, _config):
    return ["<priority>" if token == "urgent" else token for token in text.split()]


def _ticket_tokens(text, _config):
    words = text[4:].split(",") if text.startswith("csv:") else text.split()
    return ["<priority>" if word == "urgent" else word for word in words]


@pytest.mark.slow
def test_contained_in_preserves_context_when_a_token_is_canonicalized(tmp_path):
    tokenizer = "r9sql_ticket"
    hb.ext.register_gram_tokenizer(tokenizer, _ticket_tokens, replace=True)

    class Ticket(hb.Entity):
        identifier = "gram-contextual-canonical-tokens"
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="none", gram_unit="token", tokenizer=tokenizer))

    ws = _workspace(tmp_path)
    try:
        ws.register(Ticket)
        ws.upsert(Ticket, {"object_id": "invoice-104", "keywords": ["invoice"]})
        adapter = ws.backends.get("main").strategy_adapter(hb.SparseGramIndex)
        for query, spans in (
            ("urgent invoice", [[7, 14]]),
            ("csv:normal,invoice,pending", [[11, 18]]),
            ("csv:urgent,invoice,pending", [[11, 18]]),
            ("csv:urgent,invoiceable,invoice", [[23, 30]]),
        ):
            result = ws.query(Ticket).where(Ticket.keywords.contained_in(query)).execute()
            assert result.ids == ["invoice-104"]
            assert [match["query_span"] for match in result.column("match")[0]] == spans
            scan = adapter.contained_in_scan_matcher(Ticket.identifier, "keywords", query)
            assert [match["query_span"] for match in scan(["invoice"])] == spans
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize(
    ("text", "spans"),
    [
        ("ordinary Cat", [[9, 12]]),
        ("urgent Cat", [[7, 10]]),
        ("Cat urgent Catfish Cat", [[0, 3], [19, 22]]),
        ("urgent Catfish", []),
    ],
)
def test_contained_in_keeps_whole_tokens_beside_canonicalized_units(tmp_path, text, spans):
    tokenizer = "r9sql_priority"
    hb.ext.register_gram_tokenizer(tokenizer, _priority_tokens, replace=True)

    class Doc(hb.Entity):
        identifier = "gram-canonical-token-verification"
        keywords = hb.field(hb.Array[hb.ShortText]).store(
            to="main", strategy=hb.SparseGramIndex(normalizer="none", gram_unit="token", tokenizer=tokenizer, hash_bits=1)
        )

    ws = _workspace(tmp_path)
    try:
        ws.register(Doc)
        ws.upsert(Doc, {"object_id": "x", "keywords": ["Cat"]})
        result = ws.query(Doc).where(Doc.keywords.contained_in(text)).execute()
        assert result.ids == (["x"] if spans else [])
        assert [match["query_span"] for row in result.column("match") for match in row] == spans
        adapter = ws.backends.get("main").strategy_adapter(hb.SparseGramIndex)
        scan = adapter.contained_in_scan_matcher(Doc.identifier, "keywords", text)
        assert [match["query_span"] for match in scan(["Cat"])] == spans
    finally:
        ws.release()
