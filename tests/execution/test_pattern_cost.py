"""Portable LIKE/ILIKE and regex scans stay bounded on long values."""

from __future__ import annotations

import copy
import pickle
import random
import re
import sys
import time

import pytest

import heavenbase as hb
from heavenbase.execution.handlers import _text_patterns

_SENTENCE = "The quick brown fox jumps over the lazy dog near the riverbank. "


class _Config:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key, default=None):
        return self.values.get(key, default)


def _reference_like(pattern: str, value: str, *, ignore_case: bool) -> bool:
    """Return the SQL LIKE answer through a whole-value regex (exact, but backtracking)."""
    parts = (".*" if char == "%" else "." if char == "_" else re.escape(char) for char in pattern)
    flags = re.DOTALL | (re.IGNORECASE if ignore_case else 0)
    return re.fullmatch("".join(parts), value, flags) is not None


def _timed(call):
    started = time.perf_counter()
    result = call()
    return result, time.perf_counter() - started


@pytest.mark.parametrize("op", ["like", "ilike"])
def test_like_keeps_whole_value_sql_semantics(op):
    """The bounded LIKE matcher should agree with the whole-value regex on every shape."""
    predicate = hb.ext.Predicate.load(op)
    ignore_case = op == "ilike"
    fixed = [
        ("%second%", "first line\nsecond line"),
        ("a_b", "a\nb"),
        ("tail", "tail\n"),
        ("tail_", "tail\n"),
        ("100%", "100% sure"),
        ("1.0", "100"),
        ("%%", ""),
        ("%", ""),
        ("", ""),
        ("_", ""),
        ("a%a", "a"),
        ("a%a", "aa"),
        ("ab%ba", "aba"),
        ("ab%ba", "abba"),
        ("%ab%ab%", "abab"),
        ("%ab%ab%", "aba"),
        ("x%_%y", "xy"),
        ("x%_%y", "x\ny"),
        ("[a]%(b)*?", "[a]zz(b)*?"),
        ("\\%", "\\zz"),
        ("Straße%", "STRASSE"),
        ("%ǅ%", "xǆx"),
    ]
    for pattern, value in fixed:
        assert predicate.match(value, predicate.compile(pattern)) is _reference_like(pattern, value, ignore_case=ignore_case), (pattern, value)
    rng = random.Random(20261120)
    alphabet = "aAb%_\n.\\é"
    for _ in range(4000):
        pattern = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 7)))
        value = "".join(rng.choice("aAb\n.\\é%_") for _ in range(rng.randint(0, 9)))
        assert predicate.match(value, predicate.compile(pattern)) is _reference_like(pattern, value, ignore_case=ignore_case), (pattern, value)


@pytest.mark.parametrize("op", ["like", "ilike"])
def test_like_cost_stays_linear_in_the_stored_value(op):
    """A multi-wildcard pattern that fails on a long value should not backtrack polynomially."""
    predicate = hb.ext.Predicate.load(op)
    cases = [
        ("%a%a%a%a%b", "a" * 240),
        ("%the%lazy%dog%cat%", _SENTENCE * 230),
        ("%a%a%a%a%a%a%a%a%b", "a" * 200_000),
        ("%the%lazy%dog%cat%", _SENTENCE * 3_000),
        ("%_a_%_a_%_a_%b", "a" * 200_000),
    ]
    for pattern, value in cases:
        prepared = predicate.compile(pattern)
        matched, elapsed = _timed(lambda: predicate.match(value, prepared))
        assert matched is False
        assert elapsed < 1.0, (pattern, len(value), elapsed)
    assert predicate.match(_SENTENCE * 3_000 + "cat", predicate.compile("%the%lazy%dog%cat"))


def test_sqlite_like_scan_on_a_long_row_finishes(tmp_path):
    """Case-sensitive LIKE runs as a Python scan on SQLite; one long row must not stall the query."""
    backend = hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(tmp_path / "like-cost.db"))
    ws = hb.HeavenBase("sqlite-like-cost-ws", backends={"main": backend})

    class LongDoc(hb.Entity):
        identifier = "like-cost-doc"

        body = hb.field(hb.LongText)

    try:
        ws.register(LongDoc)
        ws.upsert(LongDoc, {"object_id": "long", "body": _SENTENCE * 230})
        query = ws.query(LongDoc).where(LongDoc.body.like("%the%lazy%dog%cat%"))
        step = next(step for step in query.explain()["steps"] if step.get("node") == "filter")
        assert step["handler_mode"] == "py_scan"
        ids, elapsed = _timed(lambda: query.execute().ids)
        assert ids == []
        assert elapsed < 10.0, elapsed
        assert ws.query(LongDoc).where(LongDoc.body.like("The%lazy%dog%bank. ")).execute().ids == ["long"]
    finally:
        ws.drop()


def _compile_regex(pattern, values=None):
    resolver = hb.DEFAULT_CONTEXT.modules()
    return hb.ext.Predicate.load("regex", resolver=resolver).compile(pattern, resolver=resolver, config=_Config(values))


def _regex_engine(pattern, values=None):
    return _compile_regex(pattern, values)[0].engine


def _linear(pattern):
    """Build the linear-time matcher for a regular pattern whatever its estimated cost."""
    parsed = _text_patterns._sre_parse.parse(pattern)
    nfa = _text_patterns._Nfa()
    start = nfa.build(parsed, int(parsed.state.flags), 0)
    return _text_patterns._LinearRegex(nfa, start)


# Shapes whose Python re backtracking grows exponentially or as the cube or more of the text length.
_SUPER_QUADRATIC = [
    r"a.*a.*a.*a.*b",
    r"the.*lazy.*dog.*cat",
    r".*foo.*bar",
    r"a+aa+aa+aa+c",
    r"\d+.\d+.\d+.\d+x",
    r"(a|aa)+c",
    r"(?:x|x)*y",
    r"(ab?|b)+c",
    r"((a+))+c",
    r"(?:(?:a)+b?)*c",
    r"(a{1,3})+c",
    r"(a{1,40}){1,40}c",
    r"a*a*a*a*c",
    r"\w+\s*\w+x",
    r"\d+\.?\d*$",
    r".*\d+x",
    r"(?i)A+a+c",
    r"(?s)(.|\n)+c",
    "(a|aa)" * 30 + "c",
]

# Ordinary patterns whose Python re cost stays at most quadratic; 0.1.2.2 accepted all of them.
_ON_RE = [
    r"^alpha",
    r"oak\s+desk",
    r"^A.*$",
    r"foo.*bar",
    r"(\d{1,3}\.){3}\d{1,3}",
    r"\w+@\w+\.com",
    r"\d+\.?\d*",
    r"^\d+\.?\d*$",
    r"(?:\s|,)+x",
    r"[A-Z][a-z]+",
    r"(?i)hello\s+world",
    r"^\s*(\w+)\s*=\s*(\w+)\s*$",
    r"\d+\w+",
    r".*\d+",
    r"(?=a+)b",
    r"(?=a*a*)",
    r"(\w+)\s+\1",
    r"^(?=.*\d)(?=.*[a-z]).{8,}$",
    r"(?=.*\d)(?=.*[a-z]).{8,}",
    r"(a)?(?(1)a*|b*)x",
    # Backreferences and lookarounds whose Python re cost stays at most quadratic.
    r"""(['"]).*?\1""",
    r"""(["']).*\1""",
    r"<(\w+)>.*</\1>",
    r"<(\w+)[^>]*>.*?</\1>",
    r"\b(\w+)\b.*\b\1\b",
    r"(.+)\1",
    r"(.)\1{2,}",
    r"""(?P<q>['"])(?:(?!(?P=q)).)*(?P=q)""",
    r"(?m)^(?!#).*\S.*$",
    r"(?s)BEGIN(.*?)END\1",
    r"(?s)<(\w+)>(.*?)</\1>",
]

# Super-quadratic shapes that need Python re features the linear-time matcher lacks.
_REJECTED = [
    r"(?=(a|aa)+c)",
    r"(a|aa)+(?=c)",
    r"(?<=a)(a|aa)+c",
    r"(?<=x)a*a*a*a*c",
    r"(?=.*a.*a.*a.*b)",
    r"((ab?|b)+)\1c",
    r"(a+)\1x",
    r"^(a+).*\1x",
    r"(?m)^(?=[\s\S]*a)[\s\S]*[\s\S]*b",
    # CPython tests the leading class of a scoped (?u:...) group inside an ASCII pattern with ASCII classes.
    r"(?a)(?u:\w).*a.*a.*a.*b",
]


def test_regex_routes_by_estimated_backtracking():
    """Super-quadratic regular shapes run on the linear matcher; ordinary ones stay on Python re."""
    for pattern in _SUPER_QUADRATIC:
        assert _regex_engine(pattern) == "linear", pattern
    for pattern in _ON_RE:
        assert _regex_engine(pattern) == "re", pattern
    for pattern in _REJECTED:
        with pytest.raises(ValueError, match="Unsafe regex pattern .*estimated worst-case backtracking"):
            _compile_regex(pattern)
        assert _regex_engine(pattern, {"heavenbase.query.regex.allow_nested_quantifiers": True}) == "re"
    # 0.1.2.2's textual nested-quantifier check still applies first.
    with pytest.raises(ValueError, match="nested quantifiers are disabled"):
        _compile_regex(r"(a+)+c")
    assert _regex_engine(r"(a+)+c", {"heavenbase.query.regex.allow_nested_quantifiers": True}) == "linear"
    # The estimate is taken at the configured text cap.
    assert _regex_engine(r"a.*a.*b", {"heavenbase.query.regex.max_text_chars": 100}) == "re"
    # sre folds U+0130 to i under IGNORECASE, so these branches overlap and the repeat is exponential on re.
    assert _regex_engine(r"(?i)^(?:\u0130x|ix)+y") == "linear"
    assert _compile_regex(r"(?i)^(?:\u0130x|ix)+y")[0].search("ix" * 40) is False


def test_regex_cost_model_folds_case_like_re():
    """Two classes overlap under IGNORECASE exactly when Python re lets one character match both."""
    parsed = _text_patterns._sre_parse.parse("(?i)a")
    model = _text_patterns._CostModel(parsed, 8192)
    cased = _text_patterns._cased()
    known = set(cased)
    outside = "".join(char for char in map(chr, range(0x110000)) if char not in known and not "\ud800" <= char <= "\udfff")
    assert re.compile("(?i)[" + re.escape(cased) + "]").search(outside) is None

    def literal(char):
        return (False, (("lit", ord(char)),))

    missed = []
    for char in cased:
        for other in re.compile("(?i)" + re.escape(char)).findall(cased):
            if not model.overlap(literal(char), literal(other)):
                missed.append((char, other))
    assert missed == []
    assert model.overlap(literal("\u0130"), literal("i"))
    assert not model.overlap(literal("\u0130"), literal("x"))
    assert not model.overlap((False, (("range", 0x10000, 0x10FFE),)), (False, (("range", 0x20000, 0x20FFE),)))


def test_regex_planning_stays_fast_on_many_wide_classes():
    """Planning compares classes by range, not by enumerating their members one by one."""
    classes = "".join("[%s-%s]*" % (chr(0x10000 + 4096 * index), chr(0x10000 + 4096 * index + 4094)) for index in range(80))
    for pattern in (classes + "x", "(?i)" + classes + "x"):
        prepared, elapsed = _timed(lambda: _compile_regex(pattern))
        assert prepared[0].engine == "linear"
        assert elapsed < 3.0, elapsed


@pytest.mark.skipif(sys.version_info < (3, 11), reason="possessive quantifiers and atomic groups need Python 3.11")
def test_regex_possessive_and_atomic_forms_stay_on_re():
    """Possessive and atomic forms are not re-entered, so only their contents count."""
    for pattern in (r"a++a*c", r"(?>a|aa)+c", r"(?:a|aa)++c", r"(?>(a|aa)+)c", r"(?>a*a*a*)c", r"a*+a*+c"):
        assert _regex_engine(pattern) == "re", pattern
    with pytest.raises(ValueError, match="atomic group"):
        _compile_regex(r"(?>(?:a|aa)+c)")


def test_regex_cost_stays_bounded_at_the_text_cap():
    """Patterns that hung 0.1.2.2 on one row answer quickly at the default 8,192-character cap."""
    predicate = hb.ext.Predicate.load("regex")
    sentence = "the quick brown fox jumps over the lazy dog "
    cases = [
        (r"a.*a.*a.*a.*b", "a" * 8192),
        (r".*foo.*bar", ("foo " * 2100)[:8192]),
        (r"the.*lazy.*dog.*cat", ("the lazy dog " * 700)[:8192]),
        (r"the.*lazy.*dog.*fox.*cat", (sentence * 200)[:8192]),
        (r"a+aa+aa+aa+c", "a" * 8192),
        (r"\d+.\d+.\d+.\d+x", "1" * 8192),
        (r"(a|aa)+c", "a" * 8192),
        (r"\d+\.?\d*$", "1" * 8191 + "x"),
        ("(a|aa)" * 30 + "c", "a" * 8192),
    ]
    for pattern, text in cases:
        prepared = predicate.compile(pattern)
        assert prepared[0].engine == "linear", pattern
        matched, elapsed = _timed(lambda: predicate.match(text, prepared))
        assert matched is False
        assert elapsed < 2.0, (pattern, elapsed)
    prepared = predicate.compile(r"the.*lazy.*dog.*cat")
    assert predicate.match(sentence * 100 + "cat", prepared)
    for copied in (copy.deepcopy(prepared), pickle.loads(pickle.dumps(prepared))):
        assert copied[0].engine == "linear"
        assert predicate.match(sentence * 100 + "cat", copied)
        assert not predicate.match(sentence * 100, copied)


def test_regex_linear_matcher_agrees_with_re():
    """The linear matcher answers exactly like re.search on random regular patterns and texts."""
    fixed = [
        (r"$", "a\n"),
        (r"a$", "a\n"),
        (r"a$", "a\n\n"),
        (r"\b", ""),
        (r"\B", ""),
        (r"\B", "a"),
        (r"\B", " "),
        (r"(?m)^b", "a\nb"),
        (r"(?m)a$", "a\nb"),
        (r"(?i)s", "ſ"),
        (r"(?i)k", "\u212a"),
        (r"(?ia)k", "\u212a"),
        (r"(?i)ß", "SS"),
        (r"", ""),
        (r"\Z", ""),
        (r"(?a:\b)é", "aé"),
        (r"\bé", "aé"),
        (r"(?:)*a", "a"),
        (r"(a*)*b", "aaab"),
        (r"(?:a|)+b", "b"),
        (r"[\]]", "]"),
        (r"[^\]-]", "-"),
        (r"(?i)[^k]", "K"),
        (r"(?x) a b ", "ab"),
        (r"\x00", "\x00"),
        (r".", "\n"),
        (r"(?s).", "\n"),
        (r"(?s:.)\n", "\n\n"),
        (r"a{2,3}b", "aab"),
        (r"a{2,3}?b", "ab"),
    ]
    for pattern, text in fixed:
        assert _linear(pattern).search(text) is (re.search(pattern, text) is not None), (pattern, text)
    rng = random.Random(20261121)
    atoms = ["a", "b", "A", ".", "[ab]", "[^a]", r"\w", r"\W", r"\s", r"\d", r"\n", "é", "_", " ", "[a-c]", "(?i:a)", "(?s:.)", r"(?a:\w)", r"\.", r"[^\n]"]
    anchors = ["^", "$", r"\A", r"\Z", r"\b", r"\B"]
    quantifiers = ["*", "+", "?", "{0,2}", "{1,3}", "{2}", "*?", "+?", "??", "{1,2}?"]
    flags = ["", "(?i)", "(?s)", "(?m)", "(?a)", "(?im)", "(?ms)"]

    def generate(depth, nested):
        roll = rng.random()
        if depth > 3 or roll < 0.35:
            return rng.choice(atoms)
        if roll < 0.45:
            return rng.choice(anchors)
        if roll < 0.65:
            return generate(depth + 1, nested) + generate(depth + 1, nested)
        if roll < 0.75:
            return "(" + generate(depth + 1, nested) + "|" + generate(depth + 1, nested) + ")"
        if roll < 0.82:
            return "(?:" + generate(depth + 1, nested) + "|" + generate(depth + 1, nested) + "|)"
        if nested >= 2:
            return rng.choice(atoms)
        body = generate(depth + 1, nested + 1)
        return body if body in anchors else "(?:" + body + ")" + rng.choice(quantifiers)

    alphabet = "aAb\n _1é.ſK"
    checked = 0
    while checked < 12_000:
        pattern = rng.choice(flags) + generate(0, 0) + (generate(0, 0) if rng.random() < 0.5 else "")
        try:
            compiled = re.compile(pattern)
        except re.error:
            continue
        linear = _linear(pattern)
        for _ in range(12):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 9)))
            assert linear.search(text) is (compiled.search(text) is not None), (pattern, text)
            checked += 1


def test_regex_linear_matcher_keeps_cpython_ascii_prefilter():
    """A scoped (?u:...) group inside an ASCII pattern stays on Python re, whose answer it keeps."""
    with pytest.raises(_text_patterns._NotRegular):
        _linear(r"(?a)(?u:\w)")
    for pattern, text in ((r"(?a)(?u:\w)", "\u00e9"), (r"(?a)(?u:\d)", "\u0663"), (r"(?a)(?u:\w)\w", "\u00e9a")):
        prepared = _compile_regex(pattern)
        assert prepared[0].engine == "re"
        assert prepared[0].search(text) is (re.search(pattern, text) is not None)
    values = {"heavenbase.query.regex.allow_nested_quantifiers": True}
    prepared = _compile_regex(r"(?a)(?u:\w).*a.*a.*a.*b", values)
    assert prepared[0].engine == "re"
    assert prepared[0].search("\u00e9aaab") is (re.search(r"(?a)(?u:\w).*a.*a.*a.*b", "\u00e9aaab") is not None)


def test_regex_linear_matcher_restarts_a_full_table(monkeypatch):
    """A text that fills the DFA table is simulated, and the next text starts a fresh table."""
    monkeypatch.setattr(_text_patterns, "_MAX_DFA_STATES", 6)
    pattern = r"a.{0,6}b.*c"
    linear = _linear(pattern)
    rng = random.Random(20261121)
    first = linear._dfa
    for _ in range(300):
        text = "".join(rng.choice("abcx") for _ in range(rng.randint(0, 30)))
        assert linear.search(text) is (re.search(pattern, text) is not None), text
    assert linear._dfa is not first


def test_regex_planning_handles_deep_nesting():
    """A deeply nested pattern that re.compile accepts is planned without RecursionError."""
    for pattern in ("(" * 250 + "a" + ")" * 250, "(" * 250 + "a*" + ")" * 250 + "b", "(?:" * 160 + "a" + ")" * 160 + "*b"):
        prepared = _compile_regex(pattern, {"heavenbase.query.regex.max_chars": 1024})
        assert prepared[0].search("xab") is (re.search(pattern, "xab") is not None)


def test_sqlite_regex_scan_on_a_long_row_finishes(tmp_path):
    """A regex the SQLite backend scans in Python must not stall the query on one long row."""
    backend = hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(tmp_path / "regex-cost.db"))
    ws = hb.HeavenBase("sqlite-regex-cost-ws", backends={"main": backend})

    class RegexDoc(hb.Entity):
        identifier = "regex-cost-doc"

        body = hb.field(hb.LongText)

    try:
        ws.register(RegexDoc)
        ws.upsert(RegexDoc, {"object_id": "long", "body": ("the lazy dog " * 700)[:8192]})
        query = ws.query(RegexDoc).where(RegexDoc.body.regex(r"the.*lazy.*dog.*cat"))
        ids, elapsed = _timed(lambda: query.execute().ids)
        assert ids == []
        assert elapsed < 10.0, elapsed
        assert ws.query(RegexDoc).where(RegexDoc.body.regex(r"lazy.*dog.*the")).execute().ids == ["long"]
    finally:
        ws.drop()
