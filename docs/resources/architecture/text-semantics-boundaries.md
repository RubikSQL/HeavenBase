# Text and scalar semantic boundary register

Status: current boundary register; future changes require the approval gates below

Date: 2026-07-21

Owner: [ADR 0010](adr/0010-text-semantics-parity.md), Issue #54

## Purpose

This file records deliberate limitations of the portable text-semantics work so
that a safe fallback is not mistaken for complete storage or native-execution
parity. It also separates implemented behavior from research candidates. The
default contract remains portable Python semantics. This slice adds managed
SQLite text DDL, conditional MySQL evidence, and a query-scoped unsafe execution
policy; it does not add migrations, identity repair, or strict scalar
coercion.

## Current coverage snapshot

As of 2026-07-21, **2 of 12 registered SQL dialect identifiers (16.7%)** declare
a built-in text-operation candidate backed by provider-owned physical evidence:
`sqlite` and `mysql`. SQLite also declares text-key evidence for group,
distinct, and order. The complete identifier set is `duckdb`, `mssql`, `mysql`, `oceanbase`,
`oracle`, `pgvector`, `postgres`, `postgresql`, `sql`, `sqlite`, `starrocks`,
and `trino`. Some are aliases or specializations, so this is registration
coverage rather than a count of independent database engines.

New HeavenBase-managed SQLite text columns qualify by construction. SQLite
evidence reflects the named check as a complete structured constraint and
withholds proof while check enforcement is disabled; comments or other DDL
substrings are not evidence. MySQL is conditional per live field and qualifies
only when official-engine identity, client/connection/result and column
`utf8mb4` character sets, base-table/non-generated shape, text type, character
and byte width, and the expected binary-collation contract pass inspection. A
legacy SQLite field or a MySQL field that misses one condition remains unproved.

This does not mean that every query performs a full Python scan. Non-text
scalar operations retain their native routes, and a mixed query may reduce its
candidate set before evaluating an unproved text predicate. It does mean that
an unproved text predicate or text aggregate key cannot be called physically
portable merely because a SQL compiler exists.

The repository does not yet measure production workload fallback. The
[SQL text fallback estimate](../reports/text-fallback-estimate.md) records an
explicit operation/backend model: 21% fallback for managed SQLite, 54% for an
evidence-qualified MySQL field, and 53.6% for one illustrative mixed-backend
workload. These are operation-weighted design estimates, not observed queries,
rows, latency, or market share.

## Implemented user-visible contract

- Portable semantics remain the default.
- `match(value)` is a case-insensitive literal substring operation implemented
  by applying Python `str.lower()` to both operands.
- `contains(value)` is a case-sensitive literal substring operation.
- `field == value` is case-sensitive exact equality; `like` is the portable
  case-sensitive wildcard operation; Python `regex` is case-sensitive unless
  the pattern requests otherwise, for example with `(?i)`.
- A Python-scan `like`/`ilike` costs time linear in the stored value times
  the pattern length, however many `%` wildcards the pattern has.
- A Python-scan `regex` answers exactly as Python `re.search`. When the filter
  is prepared, HeavenBase estimates the worst-case backtracking of Python `re`
  on a text of `heavenbase.query.regex.max_text_chars` characters (8,192 by
  default). It multiplies the work after a quantifier by its iteration
  counts only when something after the quantifier can fail and can start
  with a character the quantifier also matches; a `\b` after a quantifier
  over word characters lets only a non-word character follow. It multiplies
  by the branches of an alternation only when they can match the same text,
  with case folded as Python `re` folds it under `(?i)`. It compounds
  choices repeated inside a quantifier. It adds, rather than multiplies, the
  work inside atomic groups, possessive quantifiers and lookarounds, which
  are never re-entered once they finish. A backreference compares at most
  the width of its group; when the character just before it cannot belong to
  the group, as in `<(\w+)>.*</\1>`, its comparisons together read the text
  about once per capture. A search starts at every position, except that a
  pattern led by a multiline `^` that cannot consume a line break stays
  within one line per start, and a class followed by a quantifier over a
  disjoint class, as in `<\w+`, gives each start its own run.
  - Within 10^9 estimated steps the pattern runs on Python `re`. At the default
    cap that admits patterns such as `foo.*bar`, `\w+\d+`, `(\w+)\s+\1`,
    `(["']).*?\1` and `<(\w+)[^>]*>.*?</\1>`, which can still take up to a few
    seconds on one adversarial 8,192-character value.
  - Above it, a pattern made only of literals, classes, groups, alternation,
    quantifiers, inline flags and the anchors `^ $ \A \Z \b \B` runs on a
    built-in matcher (a lazily built DFA). That covers `.*foo.*bar`,
    `the.*lazy.*dog.*cat`, `(a|aa)+c` and `\w+\s*\w+x`, on which Python `re`
    takes from seconds to hours per value at that cap, and the matcher about a
    millisecond. Its work is at most proportional to the text length times the
    expanded pattern size, which is limited to 4,096 nodes. A value that needs
    more than 4,096 DFA states, which a large bounded repeat after a variable
    prefix can cause (for example `a.{0,2000}b.*c.*d`), is matched without the
    state table, and that costs seconds per 8,192-character value; the next
    value starts a fresh table.
  - A pattern above the budget that uses a backreference, lookaround,
    conditional group, atomic group or possessive quantifier, expands past
    4,096 nodes, or scopes `(?u:...)` inside an ASCII pattern (Python `re`
    tests the leading class of such a group as ASCII), stays on Python `re` up
    to 3·10^9 estimated steps, where one adversarial value can take several
    seconds. At the default cap any estimate that grows with the cube of the
    text length is far above that, for example `(a+)\1x`. Above it the filter
    raises `ValueError` ("Unsafe regex pattern ...") unless
    `heavenbase.query.regex.allow_nested_quantifiers` is `true`.
  - With that key `false` (the default), 0.1.2.2's textual check also still
    rejects a group containing `+` or `*` that is itself quantified, such as
    `(a+)+`. The estimate is a static model, not a measured bound.
- If physical text parity is not proved, the affected text operation scans or
  folds and `explain()` reports `text_semantics_not_proven`.
- New managed SQLite columns prove exact comparison, membership, range,
  case-sensitive `contains`/affix, and aggregate-key semantics through explicit
  binary expressions plus an exactly reflected, actively enforced
  text-storage-class constraint.
- Evidence-qualified MySQL columns prove exact equality, inequality, and
  membership through `utf8mb4_bin` comparison plus byte-length guards.
  Managed MySQL `LongText` uses a text family sized for the full four-byte
  `utf8mb4` logical character budget.
- `.unsafe()` and JSON `"unsafe": true` authorize documented
  result-relaxing query optimizations. The current implementation may use an
  available native compiler without portable proof. This is frozen execution
  policy, not a new semantic universe or a validation/security bypass.
  `explain()` reports the authorization, `portable_proof`, and any applied
  `unsafe_reasons`.

## Logical-type canonicalization boundary

"Canonical" below means an explicit HeavenBase logical-type contract, not an
industry-wide representation. A logical type may accept several input shapes
only when it declares how they map to one stored value and rejects ambiguous or
lossy inputs. Query operands and writes must use the same contract.

| Logical type | Current input normalization | Boundary decision |
| --- | --- | --- |
| `Date` | ISO string, `date`, or `datetime` to validated UTC+0 `YYYY-MM-DD` text; numeric and `bool` inputs are rejected | Deliberate repository contract, not arbitrary stringification. The `datetime` projection intentionally discards time-of-day; strict-input design must decide whether callers should make that projection explicitly. |
| `Array` | List, tuple, or array-like input to a Python list, with per-item validation when typed | Deliberate repository contract. Arrays are not converted to strings. Bare strings are rejected. |
| Text types | Most text validators use `str(value)`; pattern operands also use `str(value)` | Compatibility concern. Values such as numbers and containers can silently become text with representation-dependent meaning. Research strict text inputs and migration impact. |
| `Categorical[string]` | Uses string coercion before membership validation | Compatibility concern inherited from text coercion, although membership limits the resulting domain. |
| `Integer` | Uses `int(value)` | Known lossy boundary: non-integral floats can be truncated and `bool` can be accepted. A strict contract should reject values that are not exact integers. |
| `Float` | Uses `float(value)` | Known precision boundary: large integers and decimal-like inputs can be rounded to binary floating point; non-finite values also need an explicit policy. |

The next scalar-type design must decide accepted Python input domains,
round-trip requirements, non-finite handling, and compatibility/migration
behavior. The preferred invariant is to reject an implicit conversion when it
cannot preserve the declared logical value. An explicit conversion API may be
considered for callers that intentionally accept rounding or formatting.

Do not make filter validation stricter in isolation: doing so would allow a
value to be written but make an equivalent lookup invalid. Strictness belongs
in the logical type's shared validation/encoding contract and must be applied to
writes, defaults, computed values, query operands, and serialized JSON specs.

## Deliberate limitation register

| ID | Current limitation | Consequence | Boundary / future work |
| --- | --- | --- | --- |
| T01 | Only SQLite and MySQL have a first provider-owned physical text-evidence slice | Text-heavy workloads on other providers still scan or fold frequently | Add evidence incrementally; keep the safe default |
| T02 | Python scan starts after storage has already round-tripped a value | Encoding, normalization, padding, truncation, or empty-string loss cannot be repaired at query time | Prove lossless storage or reject/encode the unsupported value domain |
| T03 | Query-time comparison proof does not prove physical identity or uniqueness | Primary-key, unique-index, `get`, and delete-before-insert collisions can occur before a scan helps | Design provider-owned constraint/storage keys and migration tooling |
| T04 | Legacy, reflected, and externally managed columns have no managed creation contract | A database default or newly chosen collation cannot certify existing data | Require immutable schema introspection plus an explicit audit/migration |
| T05 | Side-table owner joins depend on physical Identifier equality | Loose physical equality can mis-associate rows even when the final predicate scans | Prove both join columns or use a separately approved encoded key |
| T06 | JSON string paths are dynamically typed provider expressions | Proof for a declared text column does not prove JSON extraction semantics | Keep string-path comparisons on scan until expression-specific proof exists |
| T07 | Equality, ordering, grouping, distinctness, and `min`/`max` are separate capabilities | Scalar equality evidence cannot authorize ordering or aggregate keys | Maintain independent evidence purposes and conformance corpora |
| T08 | Bounded candidate filters, including vector top-k prefilters, amplify parity errors | A false exclusion before top-k cannot be recovered later | Require Identifier membership proof, or an explicit provider-semantics policy |
| T09 | SQL wildcard and regex engines are not Python `fnmatchcase`/`re` | Native pattern behavior can differ by escaping, Unicode, flags, or regex dialect | Keep portable pattern operations scan-only until an operation-specific subset is proved |
| T10 | Runtime inspection can fail, race, or become stale | Observed rows or mutable defaults are not durable evidence | Cache only immutable enforced schema/engine fingerprints; otherwise withhold proof |
| T11 | Numeric scalar parity is outside ADR 0010's text proof gate | Float rounding, NaN, infinity, signed zero, and numeric affinity can diverge across providers | Create a separate numeric semantics ADR and corpus |
| T12 | Text, integer, and float validation still permits broad implicit coercion | User intent can be changed before either native or scan execution | Design strict shared logical-type validation with compatibility analysis |
| T13 | No route telemetry measures real fallback frequency | Registration coverage and the modelled estimate cannot answer production workload cost | Add aggregate route counters and representative benchmarks without logging values |
| T14 | `unsafe()` authorizes documented result-relaxing query optimizations | Current native text execution may change equality, padding, pattern, regex, grouping, or bounded-candidate results; future approximate accelerators may introduce other ambiguity | Keep authorization query-scoped and off by default; every applied relaxation must emit an `unsafe_reasons` entry in `explain()` |
| T15 | Oracle currently maps zero-length character values to null | The portable distinction between `""` and `None` is physically lost | Use a reversible storage encoding or declare a restricted domain; scan is insufficient |
| T16 | Case-insensitive `match` uses Python `str.lower()`, not Unicode `str.casefold()` | Some caseless equivalents, such as `"ß"` and `"ss"`, do not match | Keep the frozen contract unless a separately approved, versioned semantic change replaces it |
| T17 | Direct Short/Medium/LongText validation maps `None` to `""` | Caller mistakes can collapse absence into an empty value before storage | Review alongside optional-field handling in the strict-input ADR; preserve compatibility until approved |
| T18 | SQLite proof applies only to new/equivalent enforced columns; no automatic legacy migration exists | Existing SQLite tables retain scan fallback even when their current rows happen to be text | Add an explicit audit/migration workflow before changing legacy DDL |
| T19 | MySQL proof covers query predicates, not physical key identity or uniqueness | Loose key collation may still collide before query routing runs | Design provider-owned key/constraint storage separately |

## Provider evidence research tracks

Evidence should be implemented provider by provider, not as a central list of
collation names. Each opt-in needs: a managed-column DDL contract; immutable
engine/version, encoding, type, collation, padding, and empty-string evidence;
provider-owned comparison expressions; and native-versus-forced-scan tests over
the verification corpus in ADR 0010.

| Provider family | Candidate first slice | Required proof or blocker |
| --- | --- | --- |
| PostgreSQL / PgVector | Exact equality and membership on managed text columns | Deterministic collation, database encoding, concrete column type/collation, lossless storage, and expression tests. Embedded NUL is outside PostgreSQL `text`, so the full Python-string domain needs an explicit boundary first. |
| MySQL | **Implemented:** equality, inequality, and membership on evidence-qualified `utf8mb4` fields | `utf8mb4_bin` plus byte length repairs PAD SPACE equality. Live evidence positively identifies official MySQL, rejects known lookalikes, and requires UTF-8 session/column paths, a base non-generated text column, the expected collation contract, and both character and four-byte capacity. Managed `LongText` uses `MEDIUMTEXT` at the default logical limit. Ordering and key identity remain separate. |
| SQL Server | Exact equality with a BIN2 comparison expression | Unicode storage, column/type evidence, and an explicit length/data-length guard because ordinary comparison pads trailing spaces. Constraint parity is separate. |
| SQLite | **Implemented:** exact comparisons, membership, case-sensitive literal containment/affixes, group, distinct, and aggregate ordering on managed text columns | New fields use explicit `BINARY` text types and enforced `typeof(...) IN ('text','null')` checks. Evidence uses structured constraint reflection, requires the exact named expression, and checks active enforcement; lexical DDL matches fail closed. `instr` handles literal containment; byte-preserving `substr` handles affixes, including embedded NUL. Legacy fields without the equivalent constraint remain unproved. Case-insensitive match and patterns remain separate. |
| DuckDB | Exact equality prototype | Freeze engine version, text type and collation behavior, then run the full Unicode/storage corpus before declaring evidence. |
| OceanBase | Independent prototype | Treat its version, mode, collation inventory, and padding behavior independently rather than inheriting MySQL evidence. |
| Oracle | No full-domain text opt-in yet | Empty string/null collapse blocks the current canonical domain. Research reversible encoding or an explicitly restricted Identifier/text domain first. |
| Trino / StarRocks | Defer until direct providers mature | Connector/source behavior and distributed expression semantics require provider- and connector-specific proof. |
| Generic `sql` | Never enabled by default | An unknown SQL-family backend cannot inherit another engine's physical contract. |

Primary-source constraints include PostgreSQL's distinction between deterministic
and nondeterministic comparisons, MySQL's `PAD SPACE` versus `NO PAD`
collations, SQL Server's ANSI trailing-space padding, SQLite's dynamic typing,
and Oracle's empty-string/null collapse. These sources identify candidates;
only repository conformance tests can establish HeavenBase parity.

## Unsafe query execution policy

There is a valid efficiency use case for deliberately accepting result
ambiguity when exact portable proof is unavailable. The public authorization is
intentionally generic so future result-relaxing accelerators, such as
approximate bloom or hash prefilters, do not require parallel query modes. The
specific relaxation remains auditable through `explain()` rather than being
encoded in the public method name.

The policy belongs to the frozen request, not to one clause and not only to the
terminal `execute()` call. It may change selected routes and returned results,
so it must participate in serialization, temporary build-artifact cache
identity, planning, `explain()`, `execute()`, terminal `count()`, aggregate
routing, and near routing as one value. Query results themselves are never
cached automatically. An `execute(unsafe=True)` argument would allow the
explained and executed request to diverge and would leave JSON/agent callers
without the same request identity.

The implemented query-scoped surface is:

```python
rows = ws.query(Entity).where(expr).unsafe().execute().rows()
```

The agent/JSON form is `{"filter": ..., "unsafe": true}`. Omission or
`false` keeps safe portable routing. `unsafe()` returns a derived immutable
`Query`, and `QuerySpec.unsafe` freezes the policy.

Unsafe query policy:

- currently bypasses portable text-safety gates—concrete physical proof and
  static collation/case guards—for an available native compiler;
- retains logical-type validation, parameterization, resource guards, and all
  security checks;
- is explicit in the frozen query spec and `explain()`, which separately reports
  whether the selected native operation had portable proof and lists
  `unsafe_reasons` when a relaxation was actually used;
- applies only to query execution, never silently changes writes, DDL, migrations,
  primary-key identity, or unique constraints; and
- explicitly authorizes provider-dependent result differences, including
  candidate-filter/top-k differences when such a route is used.

There is no hidden workspace-wide default. A compiler that does not exist is not
created by the policy, and ordinary unsupported/resource guards still apply.
Setting `unsafe` does not mean a relaxation was used: an absent
`unsafe_reasons` field means execution stayed within proved behavior.
Conversely, adding a documented unsafe optimization in a future release may
change the route or results of an existing unsafe query; this evolution is part
of the unsafe contract and never affects the safe default.

New unsafe optimizations must keep their provider-specific implementation in the
owning backend/strategy, expose an explicit reason through fragment metadata,
and add differential tests that bound the admitted ambiguity. Do not use this
flag for type-validation bypasses, SQL safety, authorization, resource-limit
avoidance, writes, durability, or schema/identity changes. Do not add
per-optimization public flags until real tuning pressure demonstrates that the
single authorization is insufficient.

## Prioritized future slices

1. Add privacy-preserving route telemetry and benchmark text-heavy query shapes;
   replace the current explicit estimate with measurements.
2. Write a strict scalar-input ADR covering text, integer, float, `Date` input
   projections, defaults, optional values, and migrations before changing
   coercion behavior.
3. Add SQL Server Unicode storage/equality evidence and resolve PostgreSQL's NUL
   domain boundary in separate provider modules with forced-scan differential tests.
4. Add audit/migration support for legacy SQLite and other provider columns;
   never infer proof from a database default.
5. Measure the query-scoped `unsafe()` policy and its reason distribution before
   considering finer-grained or workspace-level tuning.
6. Defer regex/pattern subsets, broader portable ordering, Oracle encoding, and
   connector-mediated engines until equality evidence is stable.

Stop and request a new design approval before changing logical value domains,
automatic legacy migration behavior, Identifier encoding, or identity/unique
constraint semantics.

## Verification and references

Coverage is derived from the registered `SQLDialectSpec` values and their
`native_text_ops` / `native_text_key_ops`; it must be refreshed whenever a
provider opts in. Each implementation must run the provider matrix through
`bash scripts/test.bash` and compare its native route with forced Python scan.

Primary references:

- [PostgreSQL `CREATE COLLATION`](https://www.postgresql.org/docs/current/sql-createcollation.html)
- [MySQL Unicode collations and pad attributes](https://dev.mysql.com/doc/refman/8.4/en/charset-unicode-sets.html)
- [SQL Server string comparison and trailing-space padding](https://learn.microsoft.com/en-us/sql/t-sql/language-elements/string-comparison-assignment?view=sql-server-ver17)
- [SQL Server collation and Unicode support](https://learn.microsoft.com/en-us/sql/relational-databases/collations/collation-and-unicode-support?view=sql-server-ver17)
- [SQLite data types and collations](https://www.sqlite.org/datatype3.html)
- [SQLite `STRICT` tables](https://www.sqlite.org/stricttables.html)
- [Oracle null and empty-string behavior](https://docs.oracle.com/en/database/oracle/oracle-database/26/sqlrf/Nulls.html)
