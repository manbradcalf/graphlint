"""
Test the full graphlint pipeline: ShExC -> IR -> Cypher/GQL

No live database needed — we just verify the parser produces the right
checks and the backends produce valid-looking queries.

Run with: uv run pytest -s    (the -s flag shows print output)

TODO: Add validation-level tests that assert exact linter behavior:
  - exact check counts for a given schema (not just "more than zero")
  - required prop missing -> property_exists violation
  - wrong type -> property_type violation
  - value outside enum -> property_value_in violation
  - too few / too many relationships -> cardinality violation
  - optional (?) props skip existence check but still type-check
  These tests should use small focused schemas, not movies.shex.
"""

from graphlint.parser import parse_shexc_to_plan, Mapping, CheckType, Severity
from graphlint.backends.cypher import CypherBackend
from graphlint.backends.gql import GQLBackend
from graphlint.runner import compile_plan, dry_run


def test_parser(movies_shex):
    """Parse movies.shex and verify we get the expected shapes and check types."""
    plan = parse_shexc_to_plan(movies_shex, source="movies.shex")

    print(f"\nParsed {len(plan.shapes)} shapes from movies.shex:")
    for s in plan.shapes:
        print(f"  {s}")

    assert len(plan.shapes) == 4, f"Expected 4 shapes, got {len(plan.shapes)}"

    type_counts = {}
    for c in plan.checks:
        type_counts[c.type] = type_counts.get(c.type, 0) + 1

    print(f"\nGenerated {len(plan.checks)} validation checks:")
    for t, count in sorted(type_counts.items(), key=lambda x: x[0].value):
        print(f"  {t.value}: {count}")

    assert type_counts.get(CheckType.PROPERTY_EXISTS, 0) > 0
    assert type_counts.get(CheckType.PROPERTY_TYPE, 0) > 0
    assert type_counts.get(CheckType.PROPERTY_VALUE_IN, 0) > 0
    assert type_counts.get(CheckType.RELATIONSHIP_CARDINALITY, 0) > 0


def test_mapping():
    """Verify URI-to-graph convention: local names for labels/props, UPPER_SNAKE for rels."""
    m = Mapping()

    print("\nURI -> node label (strip namespace, keep local name):")
    for uri, expected in [
        ("http://example.org/movies#Movie", "Movie"),
        ("http://example.org/movies#Person", "Person"),
    ]:
        result = m.label_for(uri)
        print(f"  {uri} -> {result}")
        assert result == expected

    print("\nURI -> property name (strip namespace):")
    for uri, expected in [
        ("http://example.org/movies#title", "title"),
        ("http://example.org/movies#released", "released"),
    ]:
        result = m.property_for(uri)
        print(f"  {uri} -> {result}")
        assert result == expected

    print("\nURI -> relationship type (camelCase -> UPPER_SNAKE_CASE):")
    for uri, expected in [
        ("http://example.org/movies#hasActor", "HAS_ACTOR"),
        ("http://example.org/movies#hasDirector", "HAS_DIRECTOR"),
        ("http://example.org/movies#inGenre", "IN_GENRE"),
    ]:
        result = m.relationship_for(uri)
        print(f"  {uri} -> {result}")
        assert result == expected

    print("\nCustom override (hasActor -> ACTED_IN):")
    m2 = Mapping(
        predicates_to_relationships={
            "http://example.org/movies#hasActor": "ACTED_IN"
        }
    )
    assert m2.relationship_for("http://example.org/movies#hasActor") == "ACTED_IN"
    assert m2.relationship_for("http://example.org/movies#hasDirector") == "HAS_DIRECTOR"
    print("  hasActor -> ACTED_IN (overridden)")
    print("  hasDirector -> HAS_DIRECTOR (still uses convention)")


# ── Strict mode tests ────────────────────────────────────────────────


def test_strict_mode_off_by_default(movies_shex):
    """Without strict=True, no coverage checks are generated."""
    plan = parse_shexc_to_plan(movies_shex)
    strict_checks = [c for c in plan.checks if c.id.startswith("strict-")]
    assert len(strict_checks) == 0, "Strict checks should not appear by default"


def test_strict_mode_generates_coverage_checks(movies_shex):
    """strict=True adds undeclared labels, rel types, and per-label property checks."""
    plan = parse_shexc_to_plan(movies_shex, strict=True)

    strict_checks = [c for c in plan.checks if c.id.startswith("strict-")]
    # 1 labels + 1 rels + 4 per-label props + 4 empty shapes (Movie, Person, Genre, Review)
    assert len(strict_checks) == 10, f"Expected 10 strict checks, got {len(strict_checks)}"

    label_checks = [c for c in strict_checks if c.type == CheckType.UNDECLARED_LABELS]
    assert len(label_checks) == 1
    assert set(label_checks[0].allowed_values) == {"Movie", "Person", "Genre", "Review"}

    rel_checks = [c for c in strict_checks if c.type == CheckType.UNDECLARED_RELATIONSHIP_TYPES]
    assert len(rel_checks) == 1
    assert "HAS_ACTOR" in rel_checks[0].allowed_relationships
    assert "HAS_DIRECTOR" in rel_checks[0].allowed_relationships

    prop_checks = [c for c in strict_checks if c.type == CheckType.UNDECLARED_PROPERTIES]
    assert len(prop_checks) == 4  # one per shape


def test_strict_undeclared_properties_per_label(movies_shex):
    """Each label gets its own undeclared-props check with correct allowed properties."""
    plan = parse_shexc_to_plan(movies_shex, strict=True)

    prop_checks = {
        c.target_label: c
        for c in plan.checks
        if c.type == CheckType.UNDECLARED_PROPERTIES
    }

    assert "title" in prop_checks["Movie"].allowed_properties
    assert "released" in prop_checks["Movie"].allowed_properties
    assert "tagline" in prop_checks["Movie"].allowed_properties

    assert "name" in prop_checks["Person"].allowed_properties
    assert "born" in prop_checks["Person"].allowed_properties

    assert "name" in prop_checks["Genre"].allowed_properties
    assert "rating" in prop_checks["Genre"].allowed_properties

    assert "score" in prop_checks["Review"].allowed_properties
    assert "summary" in prop_checks["Review"].allowed_properties


def test_strict_empty_shape_checks(movies_shex):
    """Strict mode warns when declared shapes have zero instances."""
    plan = parse_shexc_to_plan(movies_shex, strict=True)

    empty_checks = [c for c in plan.checks if c.type == CheckType.EMPTY_SHAPE]
    assert len(empty_checks) == 4  # one per shape

    labels = {c.target_label for c in empty_checks}
    assert labels == {"Movie", "Person", "Genre", "Review"}

    for c in empty_checks:
        assert c.severity == Severity.WARNING, "Empty shape checks should be warnings, not violations"


def test_strict_mode_cypher_compilation(movies_shex):
    """Strict checks compile to valid Cypher with expected keywords."""
    plan = parse_shexc_to_plan(movies_shex, strict=True)
    compiled = compile_plan(plan, CypherBackend())

    strict_compiled = [(c, q) for c, q in compiled if c.id.startswith("strict-")]

    for check, query in strict_compiled:
        print(f"  {check.id}")
        print(f"    {query[:120]}...")

    # Find specific check types
    label_query = next(q for c, q in strict_compiled if c.type == CheckType.UNDECLARED_LABELS)
    assert "db.labels()" in label_query

    rel_query = next(q for c, q in strict_compiled if c.type == CheckType.UNDECLARED_RELATIONSHIP_TYPES)
    assert "db.relationshipTypes()" in rel_query

    prop_queries = [q for c, q in strict_compiled if c.type == CheckType.UNDECLARED_PROPERTIES]
    assert len(prop_queries) == 4
    for q in prop_queries:
        assert "keys(n)" in q


def test_cypher_backend(movies_shex):
    """Verify each check compiles to a Cypher query with MATCH, RETURN, and a node ID."""
    plan = parse_shexc_to_plan(movies_shex, source="movies.shex")
    compiled = compile_plan(plan, CypherBackend())

    print(f"\nCompiled {len(compiled)} Cypher queries:")
    for check, query in compiled:
        if query.startswith("//"):
            print(f"  {check.id} -> (skipped, no-op)")
            continue
        print(f"  {check.id}")
        print(f"    {query[:120]}...")
        assert "MATCH" in query, f"Query for {check.id} missing MATCH"
        assert "RETURN" in query, f"Query for {check.id} missing RETURN"
        assert "elementId" in query or "element_id" in query or "rel_id" in query, (
            f"Query for {check.id} not returning node/rel ID"
        )


def test_gql_backend(movies_shex):
    """Verify GQL backend uses element_id() (ISO standard) not elementId() (Neo4j legacy)."""
    plan = parse_shexc_to_plan(movies_shex, source="movies.shex")
    compiled = compile_plan(plan, GQLBackend())

    print(f"\nCompiled {len(compiled)} GQL queries:")
    for check, query in compiled:
        if query.startswith("//"):
            print(f"  {check.id} -> (skipped, no-op)")
            continue
        print(f"  {check.id} -> uses element_id()")
        assert "element_id" in query, (
            f"GQL query for {check.id} should use element_id()"
        )
        assert "elementId" not in query, (
            f"GQL query for {check.id} should NOT use elementId()"
        )


def test_dry_run(movies_shex):
    """Verify dry_run produces a readable SQL-like script without hitting a database."""
    plan = parse_shexc_to_plan(movies_shex, source="movies.shex")
    output = dry_run(plan, CypherBackend())

    print(f"\nDry run output: {len(output)} chars, {output.count('MATCH')} queries")
    print("First 500 chars:")
    print(output[:500])

    assert len(output) > 0
    assert "--" in output
    assert "MATCH" in output


def test_optional_property_handling(movies_shex):
    """ShExC '?' means optional: skip existence check, but still type-check if present."""
    plan = parse_shexc_to_plan(movies_shex)

    # Person.born is optional
    born_checks = [c for c in plan.checks if c.property == "born"]
    exists_checks = [c for c in born_checks if c.type == CheckType.PROPERTY_EXISTS]
    type_checks = [c for c in born_checks if c.type == CheckType.PROPERTY_TYPE]

    print("\nPerson.born is marked optional (?):")
    print(f"  existence checks: {len(exists_checks)} (should be 0 — don't require it)")
    print(f"  type checks:      {len(type_checks)} (should be 1 — validate IF present)")
    if type_checks:
        print(f"  only_if_exists:   {type_checks[0].only_if_exists} (should be True)")

    assert len(exists_checks) == 0, "Optional property should not have existence check"
    assert len(type_checks) == 1, "Should have type check for optional property"
    assert type_checks[0].only_if_exists is True, "Type check should be only_if_exists"

    # Movie.title is required
    title_checks = [
        c
        for c in plan.checks
        if c.property == "title" and c.target_label == "Movie"
    ]
    title_exists = [c for c in title_checks if c.type == CheckType.PROPERTY_EXISTS]

    print("\nMovie.title is required (no ?):")
    print(f"  existence checks: {len(title_exists)} (should be 1 — enforce it)")

    assert len(title_exists) == 1, "Required property should have existence check"


def test_value_set_handling(movies_shex):
    """ShExC [val1 val2 ...] means enum: only these values are allowed."""
    plan = parse_shexc_to_plan(movies_shex)

    rating_checks = [
        c
        for c in plan.checks
        if c.property == "rating" and c.type == CheckType.PROPERTY_VALUE_IN
    ]

    print(f'\nGenre.rating uses ["G" "PG" "PG-13" "R" "NC-17"]:')
    print(f"  value_in checks: {len(rating_checks)} (should be 1)")
    if rating_checks:
        print(f"  allowed values:  {rating_checks[0].allowed_values}")
        print(f"  only_if_exists:  {rating_checks[0].only_if_exists} (should be True)")

    assert len(rating_checks) == 1, "Should have value_in check for rating"
    assert rating_checks[0].only_if_exists is True
    assert "G" in rating_checks[0].allowed_values
    assert "R" in rating_checks[0].allowed_values
    assert "NC-17" in rating_checks[0].allowed_values


def test_cardinality_variations(movies_shex):
    """ShExC cardinality: + = 1..*, ? = 0..1, bare = exactly 1."""
    plan = parse_shexc_to_plan(movies_shex)

    rel_checks = [
        c for c in plan.checks if c.type == CheckType.RELATIONSHIP_CARDINALITY
    ]

    print(f"\nRelationship cardinality checks ({len(rel_checks)} total):")
    for c in rel_checks:
        max_str = str(c.max_count) if c.max_count is not None else "*"
        print(f"  (:{c.target_label})-[:{c.relationship.type}]->(:{c.relationship.target_label})  [{c.min_count}..{max_str}]")

    # hasActor + -> 1..*
    ha = [c for c in rel_checks if c.relationship.type == "HAS_ACTOR"][0]
    assert ha.min_count == 1
    assert ha.max_count is None  # unbounded

    # hasDirector bare -> 1..1
    hd = [c for c in rel_checks if c.relationship.type == "HAS_DIRECTOR"][0]
    assert hd.min_count == 1
    assert hd.max_count == 1

    # writtenBy ? -> 0..1
    wb = [c for c in rel_checks if c.relationship.type == "WRITTEN_BY"][0]
    assert wb.min_count == 0
    assert wb.max_count == 1

    print("\nVerified:")
    print("  HAS_ACTOR    + -> [1..*]  (at least one)")
    print("  HAS_DIRECTOR bare -> [1..1]  (exactly one)")
    print("  WRITTEN_BY   ? -> [0..1]  (optional)")
