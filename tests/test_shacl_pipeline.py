"""
Test the SHACL pipeline: SHACL/Turtle -> IR -> Cypher/GQL

Mirrors test_pipeline.py structure. Includes cross-format parity tests
to verify SHACL and ShExC produce semantically equivalent plans.
"""

from graphlint.shacl_parser import parse_shacl_to_plan
from graphlint.parser import parse_shexc_to_plan, parse_schema, CheckType, Severity
from graphlint.backends.cypher import CypherBackend
from graphlint.backends.gql import GQLBackend
from graphlint.runner import compile_plan, dry_run


def test_shacl_parser_shapes(movies_shacl):
    """Parse movies.shacl.ttl and verify we get the expected shapes."""
    plan = parse_shacl_to_plan(movies_shacl, source="movies.shacl.ttl")

    assert len(plan.shapes) == 4
    labels = {plan.mapping.label_for(s) for s in plan.shapes}
    assert labels == {"Movie", "Person", "Genre", "Review"}


def test_shacl_check_types(movies_shacl):
    """Verify all expected check types are generated."""
    plan = parse_shacl_to_plan(movies_shacl)

    type_counts = {}
    for c in plan.checks:
        type_counts[c.type] = type_counts.get(c.type, 0) + 1

    assert type_counts.get(CheckType.PROPERTY_EXISTS, 0) > 0
    assert type_counts.get(CheckType.PROPERTY_TYPE, 0) > 0
    assert type_counts.get(CheckType.PROPERTY_VALUE_IN, 0) > 0
    assert type_counts.get(CheckType.RELATIONSHIP_CARDINALITY, 0) > 0


def test_shacl_optional_property(movies_shacl):
    """Optional properties (no sh:minCount) skip existence check but still type-check."""
    plan = parse_shacl_to_plan(movies_shacl)

    # Person.born is optional (no sh:minCount)
    born_checks = [c for c in plan.checks if c.property == "born"]
    exists_checks = [c for c in born_checks if c.type == CheckType.PROPERTY_EXISTS]
    type_checks = [c for c in born_checks if c.type == CheckType.PROPERTY_TYPE]

    assert len(exists_checks) == 0, "Optional property should not have existence check"
    assert len(type_checks) == 1, "Should have type check for optional property"
    assert type_checks[0].only_if_exists is True

    # Movie.title is required (sh:minCount 1)
    title_checks = [
        c for c in plan.checks
        if c.property == "title" and c.target_label == "Movie"
    ]
    title_exists = [c for c in title_checks if c.type == CheckType.PROPERTY_EXISTS]
    assert len(title_exists) == 1, "Required property should have existence check"


def test_shacl_value_set(movies_shacl):
    """sh:in produces PROPERTY_VALUE_IN check with correct allowed values."""
    plan = parse_shacl_to_plan(movies_shacl)

    rating_checks = [
        c for c in plan.checks
        if c.property == "rating" and c.type == CheckType.PROPERTY_VALUE_IN
    ]

    assert len(rating_checks) == 1
    assert rating_checks[0].only_if_exists is True
    assert "G" in rating_checks[0].allowed_values
    assert "R" in rating_checks[0].allowed_values
    assert "NC-17" in rating_checks[0].allowed_values


def test_shacl_cardinality_variations(movies_shacl):
    """Verify cardinality: 1..*, 1..1, 0..1."""
    plan = parse_shacl_to_plan(movies_shacl)

    rel_checks = [c for c in plan.checks if c.type == CheckType.RELATIONSHIP_CARDINALITY]

    # hasActor: sh:minCount 1, no sh:maxCount -> 1..*
    ha = [c for c in rel_checks if c.relationship.type == "HAS_ACTOR"][0]
    assert ha.min_count == 1
    assert ha.max_count is None

    # hasDirector: sh:minCount 1, sh:maxCount 1 -> 1..1
    hd = [c for c in rel_checks if c.relationship.type == "HAS_DIRECTOR"][0]
    assert hd.min_count == 1
    assert hd.max_count == 1

    # writtenBy: no sh:minCount, sh:maxCount 1 -> 0..1
    wb = [c for c in rel_checks if c.relationship.type == "WRITTEN_BY"][0]
    assert wb.min_count == 0
    assert wb.max_count == 1


def test_shacl_strict_mode(movies_shacl):
    """strict=True adds undeclared labels, rel types, per-label props, and empty shapes."""
    plan = parse_shacl_to_plan(movies_shacl, strict=True)

    strict_checks = [c for c in plan.checks if c.id.startswith("strict-")]
    # 1 labels + 1 rels + 4 per-label props + 4 empty shapes
    assert len(strict_checks) == 10, f"Expected 10 strict checks, got {len(strict_checks)}"

    label_checks = [c for c in strict_checks if c.type == CheckType.UNDECLARED_LABELS]
    assert len(label_checks) == 1
    assert set(label_checks[0].allowed_values) == {"Movie", "Person", "Genre", "Review"}


def test_shacl_strict_mode_off_by_default(movies_shacl):
    """Without strict=True, no coverage checks are generated."""
    plan = parse_shacl_to_plan(movies_shacl)
    strict_checks = [c for c in plan.checks if c.id.startswith("strict-")]
    assert len(strict_checks) == 0


def test_shacl_cypher_backend(movies_shacl):
    """Verify each SHACL check compiles to valid Cypher."""
    plan = parse_shacl_to_plan(movies_shacl)
    compiled = compile_plan(plan, CypherBackend())

    for check, query in compiled:
        if query.startswith("//"):
            continue
        assert "MATCH" in query, f"Query for {check.id} missing MATCH"
        assert "RETURN" in query, f"Query for {check.id} missing RETURN"


def test_shacl_gql_backend(movies_shacl):
    """Verify GQL backend uses element_id() not elementId()."""
    plan = parse_shacl_to_plan(movies_shacl)
    compiled = compile_plan(plan, GQLBackend())

    for check, query in compiled:
        if query.startswith("//"):
            continue
        assert "element_id" in query
        assert "elementId" not in query


def test_shacl_dry_run(movies_shacl):
    """dry_run produces readable output."""
    plan = parse_shacl_to_plan(movies_shacl)
    output = dry_run(plan, CypherBackend())

    assert len(output) > 0
    assert "--" in output
    assert "MATCH" in output


def test_auto_detection_shexc(movies_shex):
    """parse_schema auto-detects ShExC format."""
    plan = parse_schema(movies_shex, source="movies.shex")
    assert len(plan.shapes) == 4


def test_auto_detection_shacl(movies_shacl):
    """parse_schema auto-detects SHACL format."""
    plan = parse_schema(movies_shacl, source="movies.shacl.ttl")
    assert len(plan.shapes) == 4


def test_parity_check_counts(movies_shex, movies_shacl):
    """Both formats produce the same number of checks per type."""
    shexc_plan = parse_shexc_to_plan(movies_shex)
    shacl_plan = parse_shacl_to_plan(movies_shacl)

    def count_by_type(plan):
        counts = {}
        for c in plan.checks:
            counts[c.type] = counts.get(c.type, 0) + 1
        return counts

    shexc_counts = count_by_type(shexc_plan)
    shacl_counts = count_by_type(shacl_plan)

    for check_type in CheckType:
        shexc_n = shexc_counts.get(check_type, 0)
        shacl_n = shacl_counts.get(check_type, 0)
        assert shexc_n == shacl_n, (
            f"{check_type.value}: ShExC has {shexc_n}, SHACL has {shacl_n}"
        )


def test_shacl_severity_mapping():
    """sh:severity sh:Warning maps to Severity.WARNING."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:property [
            sh:path ex:name ;
            sh:datatype xsd:string ;
            sh:minCount 1 ;
            sh:severity sh:Warning ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    assert len(plan.checks) > 0
    for c in plan.checks:
        assert c.severity == Severity.WARNING


def test_shacl_malformed_no_path():
    """Property shape with no sh:path is silently skipped."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:property [
            sh:datatype xsd:string ;
            sh:minCount 1 ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    assert len(plan.checks) == 0
