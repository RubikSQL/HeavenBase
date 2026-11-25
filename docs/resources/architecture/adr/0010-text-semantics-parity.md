# ADR 0010 - Portable text semantics and SQL parity

Status: accepted; correctness gate, SQLite/MySQL proof slices, and unsafe query policy implemented

Date: 2026-07-16

Tracking: Issue #54

Boundary register: [Text and scalar semantic boundaries](../text-semantics-boundaries.md)

## Problem

HeavenBase currently treats the portable Python predicates as the result oracle,
but the generic SQL handler pushes most scalar text operations into databases
whose collation, padding, pattern, and regular-expression rules may differ. A
single provider collation setting cannot prove all of these operations.

The current mismatch is observable beyond ordering:

- equality, membership, identity lookup, uniqueness, grouping, and distinctness
  inherit provider collation and padding rules;
- `contains` is case-sensitive in Python but currently shares the SQL
  case-insensitive `match` lowering;
- wrapped literal searches do not escape SQL `%` and `_` metacharacters;
- SQL `LIKE`/`ILIKE`, wildcard dialects, and provider regex engines do not in
  general implement the Python pattern contracts; and
- generated DDL cannot prove the semantics of existing or externally managed
  columns.

This makes the missing SQL Server collation change a genuine issue, but pinning
one collation would be an incomplete fix. SQL Server pads ordinary string
comparisons, MySQL distinguishes `PAD SPACE` and `NO PAD` collations, and Oracle
currently stores a zero-length character value as null. These facts also affect
`object_id` and unique constraints, where a query-time scan cannot repair a
physical identity collision.

## Canonical assumptions

The existing logical Python behavior remains authoritative. This proposal does
not add normalization or a user-selectable text semantic mode. The later
query-scoped `unsafe()` execution policy may bypass proof explicitly
without redefining the portable operations below.

- Canonical text is Python `str`; equality compares the exact code-point
  sequence. Case, accents, normalization form, and trailing spaces are
  significant. This is *Python string parity*, not a generic claim of byte
  equality.
- `match` is a literal substring after Python `.lower()` on both operands.
  `contains`, `starts_with`, and `ends_with` are case-sensitive literal
  operations.
- `like` and `ilike` keep their whole-value SQL semantics (`%` any run, `_`
  one character, everything else literal); the scan matches the `%`-separated
  segments left to right in time linear in the value times the pattern rather
  than through one backtracking regex. Wildcard keeps `fnmatchcase`; regex
  keeps guarded Python `re.search` answers, and a regular pattern whose
  estimated backtracking is too high runs on a linear-time matcher instead.
- Empty string and null handling, including optional fields, must be frozen by
  conformance tests before any native route is enabled. Query operands must pass
  through logical-type canonicalization so native and scan routes receive the
  same value.
- Python scan is an operator oracle only after physical storage has losslessly
  round-tripped the canonical value. This slice does not prove or repair that
  storage invariant. A provider that changes encoding, normalization, padding,
  or empty-string/null identity remains unsupported for the affected value
  domain even though native pushdown is disabled.

Native execution is an optimization. If parity is not proved for the concrete
operation and physical column, the route must use the existing Python scan and
say why in `explain()`.

## Decision

### 1. Separate compiler availability from semantic proof

Add an internal `native_text_ops` allowlist to `SQLDialectSpec`, defaulting to
empty, alongside the existing `native_ops`. `native_ops` continues to mean that
a compiler exists. `native_text_ops` means the provider can potentially prove
the operation against the portable text oracle.

Add `stores_as_text(dtype)` beside `stores_as_integer(dtype)`. The generic SQL
filter compiler permits a text native route only when all of these are true:

1. the operation has a registered compiler;
2. the provider declares it in `native_text_ops`; and
3. live evidence for the concrete physical column proves it.

Unknown SQL-family providers keep ordinary non-text scalar defaults but get no
native text operations by default. This is internal provider metadata, not a new
workspace or field option.

Aggregate proof is a separate contract. `native_text_key_ops` uses the purposes
`group`, `distinct`, and `order`, with its own concrete-field evidence resolver.
Scalar `eq` or `lt` proof never authorizes an aggregate. A provider text-key
expression must change comparison semantics without changing the projected
canonical value or SQL result type; a binary cast or composite guard that cannot
meet that rule remains scan/fold-only.

### 2. Keep live physical evidence separate

Introduce a small internal text-parity evidence resolver for SQL columns. It
returns the operation names proved for a concrete column after considering the
database engine/version, encoding, data type, collation, padding behavior, and
whether empty string is preserved. The eligible operations are the intersection
of this evidence and `SQLDialectSpec.native_text_ops`. Failed inspection
withholds permission instead of failing planning or guessing.

Provider code owns introspection and exact expression lowering. The generic
router consumes proof tokens; it does not branch on provider names or parse
collation strings. A provider-specific expression may make equality independent
of a column's default collation, but it must still prove lossless storage.

Evidence may be cached only when it follows from an immutable, enforced physical
contract. Scanning the current rows and caching that observation is not proof:
an external write can immediately invalidate it. Reflected, legacy, and
externally managed columns remain unproved unless the provider can verify an
enforced schema/engine contract. The current resolver runs during planning, so
even an uncached row scan has a race before execution. A future snapshot audit
would require one provider-owned proof-and-execute transaction, not this
resolver. Changing a database default never retroactively marks existing
columns safe.

### 3. Keep physical DDL provider-owned

A provider-resolved text DDL contract belongs to the concrete SQL backend
package and is applied uniformly through generic physical-column construction
hooks. The first implementation lives in `backends/sql/parity/sqlite.py`: new
managed text columns use explicit `BINARY` collation and an enforced
text-storage-class check. Evidence reflects that check structurally, matches the
complete named expression, and verifies that SQLite check enforcement is active;
text found only in comments or unrelated DDL is not proof. Provider policy must
also account for character encoding, server version, text width, and index
support; a collation name by itself is not a semantic capability.

That setting is physical policy only. It must never by itself add entries to
`native_text_ops`, never certify an existing schema, and never trigger an
automatic migration. If a provider cannot preserve the canonical value domain,
its evidence remains empty even when a deterministic collation exists.

### 4. Compile text operators independently

Split `contains` from the case-insensitive `match` lowering. Give literal,
affix, `like`, `ilike`, wildcard, and regex operations independent compilers and
guards. Literal compilers must escape provider pattern metacharacters. Regex is
scan-only by default because POSIX, PCRE, ICU, RE2, Java, and Python `re` are not
interchangeable.

Provider-specific exact comparison expressions follow the existing registered
handler/dialect-expression seam. There is no central provider switch. For
example, SQL Server exact equality would require a suitable BIN2 expression and
a length check because BIN2 does not disable ANSI padding by itself.

### 5. Reuse the same proof everywhere

Text grouping and distinctness require equality proof. Text ordering and
`min`/`max` require ordering proof. Native candidate-ID prefilters require
Identifier membership proof because collation false positives can consume a
bounded nearest-neighbour result. Identity lookup and unique constraints need
lossless storage plus equality/constraint proof; query-time expression parity is
not sufficient. These paths must eventually consume the same evidence instead
of maintaining separate provider allowlists.

Until identity and constraint parity is proved, SQL Server trailing-space keys
and Oracle empty-string keys remain explicit unsupported edge cases. Resolving
them may require a hidden binary comparison key or a separately approved public
Identifier restriction; this proposal does not silently change Identifier
semantics.

## Delivery status

1. **Implemented - correctness gate.** Freeze the portable corpus, canonicalize text filter
   operands, add `stores_as_text`, add empty-default `native_text_ops` and
   `native_text_key_ops`, and make `explain()` report
   `text_semantics_not_proven`. Text aggregates use their independent key-proof
   gate.
2. **Implemented - independent compilers.** Separate literal/case/pattern operations and fix
   `contains`/`match` conflation and escaping. Compiler availability remains
   distinct from portable proof.
3. **Partially implemented - physical evidence.** Candidate operations and live
   evidence are separate provider-owned callables, and resolver failure safely
   returns no proof. Managed SQLite proves exact comparisons, membership,
   case-sensitive literal containment/affixes, and aggregate keys through
   provider-owned DDL/evidence/expressions. MySQL proves
   equality, inequality, and membership only for live fields that pass narrow
   official-engine, `utf8mb4` session/column, base-table, generation, type,
   collation, character-width, and byte-width checks; known MySQL-compatible
   lookalikes are rejected. Managed MySQL `LongText` storage budgets four bytes
   per logical character.
   Other built-in providers remain conservative until their own storage-domain
   and expression evidence exists.
4. **Pending - migration tooling.** Add an explicit audit and provider-owned migration or
   index rebuild path for existing columns. Never migrate automatically during
   query execution or workspace startup.
5. **Pending - incremental optimization.** Enable pattern, regex-subset, grouping, and
   ordering operations provider by provider only when the full conformance suite
   proves parity.
6. **Implemented - explicit unsafe execution policy.** Python callers use
   `.unsafe()` and JSON/agent callers use `"unsafe": true` to
   bypass portable text proof and static collation/case guards for available
   compilers. Portable routing remains the default, and `explain()` separates
   authorization from actual `portable_proof` while reporting
   `unsafe_reasons` whenever the relaxation is consumed.

The implemented gate also covers string-backed `Categorical` and `Date`, because
both use physical text storage. SQL SideTable Array/HyperG predicates remain on
the scan path: their owner joins depend on Identifier equality evidence for both
physical columns. Text group/count-distinct consume equality proof; text
`min`/`max` and stable grouped output consume ordering proof. Without provider
evidence, those aggregate shapes fold. PgVector does not advertise SQL
candidate-ID prefiltering until Identifier membership parity can be proved.
Json-path predicates over dynamic string values also scan because proof for a
declared text column does not prove the comparison semantics of a provider JSON
extraction expression; proved non-text scalar paths can still compile natively.
Scan routing does not repair a SideTable owner association already corrupted by
loose physical Identifier equality; that remains part of the explicit storage
identity boundary below.

Safe SQL pattern and regex operations remain scan-only. Their provider compiler
implementations may remain registered as compiler availability, but availability
alone never authorizes portable routing.

## Verification matrix

Every enabled operation compares a native route with a forced Python scan over:

- `A`/`a`, accented text, composed/decomposed Unicode, and non-BMP characters;
- empty string/null, trailing spaces, newline, pattern escape characters, and
  embedded NUL where the logical type accepts it;
- `%`, `_`, escape characters, wildcard brackets, anchors, and regex features;
- `object_id` lookup, `get_many`, delete, grouping, `min`/`max`, and unique
  constraints; and
- newly generated columns versus legacy columns with a nonconforming collation.

Routing tests cover every built-in SQL provider plus an unknown registered SQL
backend. A failed or absent proof must produce a scan route with the same reason
in execution and `explain()`.

## Provider constraints already established

- SQL Server ordinary comparisons pad strings to equal length; BIN2 provides a
  code-point ordering but does not remove that comparison rule. Database-level
  collation changes do not alter existing column-level collations.
- MySQL `utf8mb4_bin` is `PAD SPACE`, while `utf8mb4_0900_bin` is `NO PAD`; the
  latter cannot be assumed for MariaDB or OceanBase.
- PostgreSQL deterministic collation is necessary for some equality/index
  behaviors but is not by itself proof of Python pattern or regex parity.
- Oracle currently treats zero-length character values as null, so all text
  behavior involving canonical empty strings is unsupported without a separate
  storage encoding design; Python scan cannot reconstruct the lost distinction.

References: [SQL Server string comparison](https://learn.microsoft.com/en-us/sql/t-sql/language-elements/string-comparison-assignment?view=sql-server-ver17),
[SQL Server collation and Unicode](https://learn.microsoft.com/en-us/sql/relational-databases/collations/collation-and-unicode-support?view=sql-server-ver17),
[MySQL binary collations](https://dev.mysql.com/doc/refman/8.4/en/charset-binary-collations.html),
[PostgreSQL `CREATE COLLATION`](https://www.postgresql.org/docs/current/sql-createcollation.html), and
[Oracle null semantics](https://docs.oracle.com/en/database/oracle/oracle-database/26/sqlrf/Nulls.html).

## Remaining approval gates

The accepted decision authorizes the safe routing/evidence implementation above,
the provider-owned managed SQLite DDL contract, and the explicit query-scoped
`unsafe()` execution policy. It does not authorize automatic legacy
schema migration, Identifier domain restrictions, or provider-dependent defaults. Primary-key,
point-lookup, delete-before-insert, and unique-constraint collisions under loose
provider equality remain an explicit unsupported physical boundary until a
provider storage/constraint design is approved. Lossless canonical-text
round-trip enforcement, rejection, audit, or encoding is likewise a separate
storage contract; fallback routing must not be described as repairing values a
provider has already changed.

A new safe proof for backend-native regex or pattern behavior, and any broader
workspace-level unsafe policy, require separate approval. Safe mode continues to use the
operation-defined semantics in this ADR and falls back rather than tolerating a
known mismatch. The boundary register records the exact unsafe surface,
fallback estimate, and remaining deliberate limitations.
