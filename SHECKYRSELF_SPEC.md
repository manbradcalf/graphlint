# graphlint check — Cypher Query Validator

## Problem

AI agents (via MCP servers like neo4jmcp) hallucinate graph schemas when writing
Cypher to Neo4j and Memgraph. They create nodes with labels, relationships, and
properties that don't exist in the intended schema.

## Solution

A CLI command that validates a Cypher write query against a SHACL schema by
executing it in a transaction, running graphlint's existing validation pipeline
against the resulting state, and committing only if it conforms.

## Architecture

The key insight: **no Cypher parsing needed.** The database itself is the parser.
We execute the query, let graphlint validate the resulting graph state using its
existing engine, then commit or rollback.

### Transaction Flow

```
0. Read subgraph sample   ─┐
1. Execute write query      ├─ single transaction, no commit yet
2. Run validation checks   ─┘
3. Conforms? → commit. Violations? → rollback + report.
```

**Step 0 — Get subgraph sample:**
Run the schema's `@subgraph` query to scope what part of the graph we care about.
This defines the validation boundary — we don't scan the entire database.

**Step 1 — Execute write query:**
Run the agent's Cypher write query inside the same transaction. The new/modified
nodes are now visible within the transaction but not committed.

**Step 2 — Run validation checks:**
Run graphlint's compiled validation queries (from `compile_plan`) within the same
transaction. These queries see the uncommitted writes. The validation is scoped
to the subgraph defined in step 0.

**Step 3 — Commit or rollback:**
- Zero violations → `tx.commit()` → query takes effect
- Any violations → `tx.rollback()` → no changes, return violation report

### What already exists (no new code needed)

- SHACL parser → `ValidationPlan` with full constraint model
- `compile_plan()` → Cypher validation queries for all check types
- `ValidationReport` with structured output (JSON, table)
- Cypher backend with Neo4j and Memgraph dialect support
- All constraint types: property existence, types, ranges, patterns,
  cardinality, closed shapes, logical operators, etc.

### What's new

1. **Frontmatter convention** for SHACL `.ttl` files
2. **Transaction-scoped execution** (modify `execute_plan` to accept a transaction)
3. **CLI entry point** (`graphlint check`)
4. **Subgraph scoping** (validation queries filtered to subgraph boundary)

---

## SHACL Frontmatter Convention

Schema files use comment-based frontmatter for metadata. This keeps the file
valid Turtle while making schemas discoverable by LLMs.

```turtle
# @name: Movies
# @description: Schema for the movies graph — Movie, Person, Genre, Review
# @database: neo4j://localhost:7687/neo4j
# @labels: Movie, Person, Genre, Review
# @subgraph: MATCH (m:Movie)-[r]->(n) RETURN m, r, n

@prefix ex:   <http://example.org/movies#> .
@prefix sh:   <http://www.w3.org/ns/shacl#> .
...
```

### Frontmatter Fields

| Field          | Required | Description                                                  |
|----------------|----------|--------------------------------------------------------------|
| `@name`        | yes      | Human-readable schema name                                   |
| `@description` | yes      | What this schema covers. LLMs use this to find the right schema. |
| `@database`    | no       | Default connection URI                                       |
| `@labels`      | yes      | Comma-separated node labels this schema declares             |
| `@subgraph`    | yes      | Cypher query defining the graph neighborhood to validate     |

**One subgraph per file.** A schema file may contain many SHACL shapes (Movie,
Person, Genre, Review), but they share a single subgraph query that captures
the full neighborhood the schema describes.

### Parsing

Frontmatter lines start with `# @key:` and appear before the first non-comment
line. Simple key-value extraction — no YAML/JSON dependency.

---

## CLI Interface

```
graphlint check \
  --query "CREATE (m:Movie {title: 'Inception', released: 2010})" \
  --schema examples/movies.shacl.ttl \
  --db bolt://localhost:7687 \
  [--strict] \
  [--dialect neo4j|memgraph] \
  [--database neo4j] \
  [--format json|table|quiet]
```

### Arguments

| Flag         | Required | Default    | Description                            |
|--------------|----------|------------|----------------------------------------|
| `--query`    | yes      |            | Cypher write query to validate         |
| `--schema`   | yes      |            | Path to SHACL `.ttl` file              |
| `--db`       | yes      |            | Database connection URI (bolt://)      |
| `--strict`   | no       | false      | Enable strict mode (undeclared labels/props/rels) |
| `--dialect`  | no       | neo4j      | Cypher dialect (neo4j or memgraph)     |
| `--database` | no       |            | Database name (Neo4j multi-db)         |
| `--format`   | no       | table      | Output format                          |

### Exit Codes

| Code | Meaning                      |
|------|------------------------------|
| 0    | Query conforms, committed    |
| 1    | Violations found, rolled back|
| 2    | Error (connection, parse, etc)|

### Output Examples

**Success (table format):**
```
graphlint check — PASSED ✓
  schema: movies.shacl.ttl
  query committed to bolt://localhost:7687

  12/12 checks passed | 0 violations
```

**Failure (table format):**
```
graphlint check — FAILED ✗
  schema: movies.shacl.ttl
  query rolled back (not committed)

  10/12 checks passed | 2 violations

  VIOLATIONS:

  ✗ [VIOLATION] movie-released-type
    Movie.released must be integer
    1 node(s) affected
      → 4:abc123 [Movie]  actual_value="not-a-number"

  ✗ [VIOLATION] movie-has_actor-cardinality
    Movie must have ≥1 HAS_ACTOR→Person relationship
    1 node(s) affected
      → 4:abc123 [Movie]  actual_count=0
```

**JSON format:** Uses existing `ValidationReport.to_json()` with added fields:
```json
{
  "action": "rolled_back",
  "query": "CREATE ...",
  "conforms": false,
  "results": [ ... ]
}
```

---

## Runner Modifications

The current `execute_plan` creates its own session internally. The new feature
needs all queries (write + validation) to share one transaction.

### New function: `check_query`

```python
def check_query(
    query: str,
    plan: ValidationPlan,
    backend: Backend,
    driver,
    database: str | None = None,
    target_uri: str | None = None,
) -> tuple[ValidationReport, bool]:
    """
    Execute a Cypher query in a transaction, validate against the plan,
    commit if valid, rollback if not.

    Returns (report, committed).
    """
```

**Implementation sketch:**

```python
with driver.session(database=database) as session:
    tx = session.begin_transaction()
    try:
        # Step 0+1: execute the write query
        tx.run(query)

        # Step 2: run validation queries in same transaction
        compiled = compile_plan(plan, backend)
        # ... run each check via tx.run(check_query) ...
        # ... build ValidationReport same as execute_plan ...

        # Step 3: commit or rollback
        if report.conforms:
            tx.commit()
            return report, True
        else:
            tx.rollback()
            return report, False
    except Exception:
        tx.rollback()
        raise
```

The validation loop inside the transaction is largely identical to the existing
`execute_plan` — same vacuous detection, same result collection, same report
building. The difference is using `tx.run()` instead of `session.run()`.

---

## Subgraph Scoping

The `@subgraph` query defines the validation boundary. Validation queries need
to be scoped to this boundary so pre-existing violations elsewhere don't cause
false failures.

### Approach

The subgraph query is executed first in the transaction to establish the
neighborhood. Validation queries are then filtered to nodes within that
neighborhood (e.g., by collecting node IDs from the subgraph result and adding
`WHERE elementId(n) IN [...]` clauses, or by using the subgraph pattern as a
base MATCH).

**Detail to resolve during implementation:** The exact scoping mechanism depends
on query size and performance characteristics. Options:
- Collect node IDs from subgraph → filter validation queries by ID set
- Use `WITH` clauses to chain subgraph pattern into validation queries
- Use Neo4j's `CALL {} IN TRANSACTIONS` for large subgraphs

---

## Memgraph Compatibility

- Same bolt protocol, same Python driver
- Transaction control (`begin_transaction`, `commit`, `rollback`) works identically
- Existing `CypherBackend(dialect="memgraph")` handles query differences
  (`id()` vs `elementId()`, type checking workarounds)
- `--dialect memgraph` flag selects the right backend

---

## Agent Workflow (Full Scenario)

```
1. User: "Add the movie Inception to the database"

2. LLM recognizes this as a graph write operation

3. LLM searches for relevant SHACL schemas:
   - Scans .ttl files for frontmatter
   - Matches based on @name, @description, @labels

4. LLM presents options:
   "I found these schemas that may apply:
    [1] Movies (movies.shacl.ttl) — Movie, Person, Genre, Review
    [2] None of these — create a new schema
   Which should I validate against?"

5. User selects "Movies"

6. LLM constructs Cypher and validates:
   $ graphlint check \
       --query "CREATE (m:Movie {title: 'Inception', released: 2010})" \
       --schema examples/movies.shacl.ttl \
       --db bolt://localhost:7687

7a. If PASSED → query was committed, LLM reports success
7b. If FAILED → query was rolled back, LLM shows violations and asks
    the user to clarify (e.g., "The schema requires at least one
    HAS_ACTOR relationship. Who acted in Inception?")
```

---

## Implementation Order

1. **Frontmatter parser** — extract metadata from `.ttl` comment headers
2. **`check_query` function** — transaction-scoped validation in runner.py
3. **Subgraph scoping** — filter validation to `@subgraph` boundary
4. **CLI entry point** — argparse-based `graphlint check` command
5. **Update movies example** — add frontmatter to `movies.shacl.ttl`
6. **Tests** — transaction rollback behavior, frontmatter parsing, CLI integration

---

## Open Questions

- **Query piping:** Should the CLI accept queries via stdin for multi-line Cypher?
  (e.g., `cat query.cypher | graphlint check --schema ... --db ...`)
- **Multiple queries:** Should `--query` accept semicolon-separated statements?
- **Dry-run mode:** Should there be a `--dry-run` that validates but always rolls
  back, even on success? Useful for testing.
- **Auth:** How to pass Neo4j/Memgraph credentials? Env vars
  (`NEO4J_USERNAME`, `NEO4J_PASSWORD`) are standard.
