"""
Test the SHACL pipeline: SHACL/Turtle -> IR -> Cypher/GQL
"""

from graphlint.shacl_parser import parse_shacl_to_plan
from graphlint.parser import parse_schema, CheckType, Severity
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

    # hasDirector: sh:minCount 1, no sh:maxCount -> 1..*
    hd = [c for c in rel_checks if c.relationship.type == "HAS_DIRECTOR"][0]
    assert hd.min_count == 1
    assert hd.max_count is None

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
        assert "MATCH" in query or "OPTIONAL MATCH" in query, f"Query for {check.id} missing MATCH"
        assert "RETURN" in query, f"Query for {check.id} missing RETURN"


def test_shacl_gql_backend(movies_shacl):
    """Verify GQL backend uses id() not elementId()."""
    plan = parse_shacl_to_plan(movies_shacl)
    compiled = compile_plan(plan, GQLBackend())

    for check, query in compiled:
        if query.startswith("//"):
            continue
        assert "id(n)" in query or "id(startNode" in query, f"GQL query for {check.id} missing id()"
        assert "elementId" not in query


def test_shacl_dry_run(movies_shacl):
    """dry_run produces readable output."""
    plan = parse_shacl_to_plan(movies_shacl)
    output = dry_run(plan, CypherBackend())

    assert len(output) > 0
    assert "--" in output
    assert "MATCH" in output


def test_parse_schema_shacl(movies_shacl):
    """parse_schema parses SHACL format."""
    plan = parse_schema(movies_shacl, source="movies.shacl.ttl")
    assert len(plan.shapes) == 4


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


# ─── Tier 1: New SHACL Core constraints ──────────────────────────────


def test_shacl_pattern():
    """sh:pattern produces PROPERTY_PATTERN check."""
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
            sh:pattern "^[A-Z]" ;
            sh:flags "i" ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    pattern_checks = [c for c in plan.checks if c.type == CheckType.PROPERTY_PATTERN]
    assert len(pattern_checks) == 1
    assert pattern_checks[0].pattern == "^[A-Z]"
    assert pattern_checks[0].pattern_flags == "i"

    # Verify Cypher compilation
    query = CypherBackend().compile_check(pattern_checks[0])
    assert "=~" in query
    assert "(?i)" in query

    # Verify GQL compilation
    query_gql = GQLBackend().compile_check(pattern_checks[0])
    assert "=~" in query_gql
    assert "id(n)" in query_gql


def test_shacl_pattern_no_flags():
    """sh:pattern without sh:flags works correctly."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:property [
            sh:path ex:code ;
            sh:pattern "^[A-Z]{3}$" ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    pattern_checks = [c for c in plan.checks if c.type == CheckType.PROPERTY_PATTERN]
    assert len(pattern_checks) == 1
    assert pattern_checks[0].pattern_flags is None

    query = CypherBackend().compile_check(pattern_checks[0])
    assert "(?i)" not in query


def test_shacl_string_length():
    """sh:minLength/maxLength produces PROPERTY_STRING_LENGTH check."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:property [
            sh:path ex:bio ;
            sh:datatype xsd:string ;
            sh:minLength 10 ;
            sh:maxLength 500 ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    len_checks = [c for c in plan.checks if c.type == CheckType.PROPERTY_STRING_LENGTH]
    assert len(len_checks) == 1
    assert len_checks[0].min_length == 10
    assert len_checks[0].max_length == 500

    query = CypherBackend().compile_check(len_checks[0])
    assert "size(n.bio)" in query
    assert "< 10" in query
    assert "> 500" in query


def test_shacl_string_length_min_only():
    """sh:minLength alone works."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:property [
            sh:path ex:name ;
            sh:minLength 1 ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    len_checks = [c for c in plan.checks if c.type == CheckType.PROPERTY_STRING_LENGTH]
    assert len(len_checks) == 1
    assert len_checks[0].min_length == 1
    assert len_checks[0].max_length is None


def test_shacl_range():
    """sh:minInclusive/maxInclusive produces PROPERTY_RANGE check."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:property [
            sh:path ex:year ;
            sh:datatype xsd:integer ;
            sh:minInclusive 1888 ;
            sh:maxInclusive 2100 ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    range_checks = [c for c in plan.checks if c.type == CheckType.PROPERTY_RANGE]
    assert len(range_checks) == 1
    assert range_checks[0].min_inclusive == 1888.0
    assert range_checks[0].max_inclusive == 2100.0

    query = CypherBackend().compile_check(range_checks[0])
    assert "< 1888.0" in query
    assert "> 2100.0" in query


def test_shacl_range_exclusive():
    """sh:minExclusive/maxExclusive produces correct conditions."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:property [
            sh:path ex:score ;
            sh:datatype xsd:float ;
            sh:minExclusive 0.0 ;
            sh:maxExclusive 10.0 ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    range_checks = [c for c in plan.checks if c.type == CheckType.PROPERTY_RANGE]
    assert len(range_checks) == 1
    assert range_checks[0].min_exclusive == 0.0
    assert range_checks[0].max_exclusive == 10.0

    query = CypherBackend().compile_check(range_checks[0])
    assert "<= 0.0" in query
    assert ">= 10.0" in query


def test_shacl_has_value():
    """sh:hasValue reuses PROPERTY_VALUE_IN with single value."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:property [
            sh:path ex:status ;
            sh:hasValue "active" ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    value_checks = [
        c for c in plan.checks
        if c.type == CheckType.PROPERTY_VALUE_IN and "hasvalue" in c.id
    ]
    assert len(value_checks) == 1
    assert value_checks[0].allowed_values == ["active"]


def test_shacl_closed_shape():
    """sh:closed with sh:ignoredProperties produces UNDECLARED_PROPERTIES check."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:closed true ;
        sh:ignoredProperties ( ex:internalId ) ;
        sh:property [
            sh:path ex:name ;
            sh:datatype xsd:string ;
            sh:minCount 1 ;
        ] ;
        sh:property [
            sh:path ex:age ;
            sh:datatype xsd:integer ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    closed_checks = [
        c for c in plan.checks if c.type == CheckType.UNDECLARED_PROPERTIES
    ]
    assert len(closed_checks) == 1
    assert "name" in closed_checks[0].allowed_properties
    assert "age" in closed_checks[0].allowed_properties
    assert "internalId" in closed_checks[0].allowed_properties

    query = CypherBackend().compile_check(closed_checks[0])
    assert "keys(n)" in query


def test_shacl_closed_false_no_check():
    """sh:closed false does not generate undeclared properties check."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:closed false ;
        sh:property [
            sh:path ex:name ;
            sh:minCount 1 ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    closed_checks = [
        c for c in plan.checks if c.type == CheckType.UNDECLARED_PROPERTIES
    ]
    assert len(closed_checks) == 0


def test_shacl_annotation_properties():
    """sh:defaultValue and sh:order are parsed as metadata."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:property [
            sh:path ex:status ;
            sh:datatype xsd:string ;
            sh:defaultValue "draft" ;
            sh:order 1 ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    # The type check should have annotation metadata
    type_checks = [c for c in plan.checks if c.type == CheckType.PROPERTY_TYPE]
    assert len(type_checks) == 1
    assert type_checks[0].default_value == "draft"
    assert type_checks[0].display_order == 1


# ─── Tier 2: SHACL-Unique features ──────────────────────────────────


def test_shacl_property_pair_less_than():
    """sh:lessThan produces PROPERTY_PAIR check."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:property [
            sh:path ex:startDate ;
            sh:datatype xsd:integer ;
            sh:lessThan ex:endDate ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    pair_checks = [c for c in plan.checks if c.type == CheckType.PROPERTY_PAIR]
    assert len(pair_checks) == 1
    assert pair_checks[0].property == "startDate"
    assert pair_checks[0].compare_property == "endDate"
    assert pair_checks[0].comparison_type == "lessThan"

    query = CypherBackend().compile_check(pair_checks[0])
    assert "n.startDate" in query
    assert "n.endDate" in query
    assert "NOT (n.startDate < n.endDate)" in query


def test_shacl_property_pair_equals():
    """sh:equals produces PROPERTY_PAIR check."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:property [
            sh:path ex:email ;
            sh:equals ex:primaryEmail ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    pair_checks = [c for c in plan.checks if c.type == CheckType.PROPERTY_PAIR]
    assert len(pair_checks) == 1
    assert pair_checks[0].comparison_type == "equals"

    query = CypherBackend().compile_check(pair_checks[0])
    assert "<>" in query


def test_shacl_property_pair_disjoint():
    """sh:disjoint produces PROPERTY_PAIR check."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:property [
            sh:path ex:name ;
            sh:disjoint ex:nickname ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    pair_checks = [c for c in plan.checks if c.type == CheckType.PROPERTY_PAIR]
    assert len(pair_checks) == 1
    assert pair_checks[0].comparison_type == "disjoint"

    query = CypherBackend().compile_check(pair_checks[0])
    assert "n.name = n.nickname" in query


def test_shacl_inverse_path():
    """sh:inversePath sets relationship direction to incoming."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .

    ex:PersonShape
        a sh:NodeShape ;
        sh:targetClass ex:Person ;
        sh:property [
            sh:path [ sh:inversePath ex:hasActor ] ;
            sh:nodeKind sh:IRI ;
            sh:class ex:Movie ;
            sh:minCount 1 ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    rel_checks = [c for c in plan.checks if c.type == CheckType.RELATIONSHIP_CARDINALITY]
    assert len(rel_checks) == 1
    assert rel_checks[0].relationship.direction == "incoming"
    assert rel_checks[0].relationship.type == "HAS_ACTOR"
    assert rel_checks[0].relationship.target_label == "Movie"

    query = CypherBackend().compile_check(rel_checks[0])
    assert "<-[" in query  # incoming direction


def test_shacl_class_hierarchy():
    """rdfs:subClassOf creates acceptable_labels for relationship checks."""
    turtle = """\
    @prefix sh:   <http://www.w3.org/ns/shacl#> .
    @prefix ex:   <http://example.org/test#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

    ex:Dog rdfs:subClassOf ex:Animal .
    ex:Cat rdfs:subClassOf ex:Animal .

    ex:PersonShape
        a sh:NodeShape ;
        sh:targetClass ex:Person ;
        sh:property [
            sh:path ex:hasPet ;
            sh:nodeKind sh:IRI ;
            sh:class ex:Animal ;
            sh:minCount 1 ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    rel_checks = [c for c in plan.checks if c.type == CheckType.RELATIONSHIP_CARDINALITY]
    assert len(rel_checks) == 1

    # Should have acceptable_labels including Animal + its subclasses
    assert rel_checks[0].acceptable_labels is not None
    assert "Animal" in rel_checks[0].acceptable_labels
    assert "Dog" in rel_checks[0].acceptable_labels
    assert "Cat" in rel_checks[0].acceptable_labels

    query = CypherBackend().compile_check(rel_checks[0])
    assert "labels(t)" in query
    assert "'Animal'" in query


# ─── Tier 3: Complex features ───────────────────────────────────────


def test_shacl_qualified_cardinality():
    """sh:qualifiedValueShape with sh:qualifiedMinCount produces QUALIFIED_CARDINALITY check."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:property [
            sh:path ex:score ;
            sh:qualifiedValueShape [
                sh:datatype xsd:integer ;
            ] ;
            sh:qualifiedMinCount 1 ;
            sh:qualifiedMaxCount 5 ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    qc_checks = [c for c in plan.checks if c.type == CheckType.QUALIFIED_CARDINALITY]
    assert len(qc_checks) == 1
    assert qc_checks[0].qualified_min == 1
    assert qc_checks[0].qualified_max == 5
    assert qc_checks[0].qualified_filter is not None
    assert qc_checks[0].qualified_filter.expected_type == "integer"

    query = CypherBackend().compile_check(qc_checks[0])
    assert "qcount" in query


def test_shacl_logical_not():
    """sh:not produces LOGICAL_NOT check."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:not [
            sh:property [
                sh:path ex:status ;
                sh:pattern "^deleted" ;
            ] ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    not_checks = [c for c in plan.checks if c.type == CheckType.LOGICAL_NOT]
    assert len(not_checks) == 1
    assert len(not_checks[0].sub_checks) == 1

    query = CypherBackend().compile_check(not_checks[0])
    assert "MATCH" in query
    assert "=~" in query


def test_shacl_logical_or():
    """sh:or produces LOGICAL_OR check."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:or (
            [
                sh:property [
                    sh:path ex:email ;
                    sh:minCount 1 ;
                ] ;
            ]
            [
                sh:property [
                    sh:path ex:phone ;
                    sh:minCount 1 ;
                ] ;
            ]
        ) .
    """
    plan = parse_shacl_to_plan(turtle)
    or_checks = [c for c in plan.checks if c.type == CheckType.LOGICAL_OR]
    assert len(or_checks) == 1
    assert len(or_checks[0].sub_checks) == 2

    query = CypherBackend().compile_check(or_checks[0])
    assert "AND" in query  # sh:or violations use AND (all must fail)


def test_shacl_logical_and():
    """sh:and produces LOGICAL_AND check."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:and (
            [
                sh:property [
                    sh:path ex:name ;
                    sh:minCount 1 ;
                ] ;
            ]
            [
                sh:property [
                    sh:path ex:age ;
                    sh:minCount 1 ;
                ] ;
            ]
        ) .
    """
    plan = parse_shacl_to_plan(turtle)
    and_checks = [c for c in plan.checks if c.type == CheckType.LOGICAL_AND]
    assert len(and_checks) == 1
    assert len(and_checks[0].sub_checks) == 2

    query = CypherBackend().compile_check(and_checks[0])
    assert "OR" in query  # sh:and violations use OR (any failure)


def test_shacl_logical_xone():
    """sh:xone produces LOGICAL_XONE check."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:xone (
            [
                sh:property [
                    sh:path ex:email ;
                    sh:minCount 1 ;
                ] ;
            ]
            [
                sh:property [
                    sh:path ex:phone ;
                    sh:minCount 1 ;
                ] ;
            ]
        ) .
    """
    plan = parse_shacl_to_plan(turtle)
    xone_checks = [c for c in plan.checks if c.type == CheckType.LOGICAL_XONE]
    assert len(xone_checks) == 1
    assert len(xone_checks[0].sub_checks) == 2

    query = CypherBackend().compile_check(xone_checks[0])
    assert "satisfied_count" in query
    assert "<> 1" in query


def test_shacl_unique_lang():
    """sh:uniqueLang emits INFO-level check and warning."""
    turtle = """\
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    @prefix ex: <http://example.org/test#> .

    ex:TestShape
        a sh:NodeShape ;
        sh:targetClass ex:Test ;
        sh:property [
            sh:path ex:label ;
            sh:uniqueLang true ;
        ] .
    """
    import warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        plan = parse_shacl_to_plan(turtle)

    lang_checks = [c for c in plan.checks if c.type == CheckType.UNIQUE_LANG]
    assert len(lang_checks) == 1
    assert lang_checks[0].severity == Severity.INFO

    # Should have emitted a warning
    assert any("uniqueLang" in str(warning.message) for warning in w)

    # Backend produces comment
    query = CypherBackend().compile_check(lang_checks[0])
    assert query.startswith("//")


# ─── Backend negative tests ──────────────────────────────────────────


def test_shacl_new_checks_gql_compilation():
    """Verify all SHACL-unique checks compile to GQL with element_id()."""
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
            sh:pattern "^[A-Z]" ;
            sh:minLength 1 ;
            sh:maxLength 100 ;
        ] ;
        sh:property [
            sh:path ex:score ;
            sh:datatype xsd:float ;
            sh:minInclusive 0.0 ;
            sh:maxInclusive 100.0 ;
            sh:lessThan ex:maxScore ;
        ] .
    """
    plan = parse_shacl_to_plan(turtle)
    gql = GQLBackend()

    for check in plan.checks:
        query = gql.compile_check(check)
        if query.startswith("//"):
            continue
        assert "id(n)" in query or "id(startNode" in query, f"GQL query for {check.id} missing id()"
        assert "elementId" not in query, f"GQL query for {check.id} has Cypher's elementId"


def test_movies_shacl_new_constraints(movies_shacl):
    """Updated movies.shacl.ttl produces checks for new constraint types."""
    plan = parse_shacl_to_plan(movies_shacl)

    type_counts = {}
    for c in plan.checks:
        type_counts[c.type] = type_counts.get(c.type, 0) + 1

    # Released has sh:minInclusive/maxInclusive, score has sh:minExclusive/maxExclusive
    assert type_counts.get(CheckType.PROPERTY_RANGE, 0) >= 2
    # Tagline has sh:maxLength, Person.name has sh:minLength
    assert type_counts.get(CheckType.PROPERTY_STRING_LENGTH, 0) >= 2
    # Genre is sh:closed
    assert type_counts.get(CheckType.UNDECLARED_PROPERTIES, 0) >= 1
    # announcedYear sh:lessThan released
    assert type_counts.get(CheckType.PROPERTY_PAIR, 0) >= 1
    # Person: born OR nationality
    assert type_counts.get(CheckType.LOGICAL_OR, 0) >= 1
    # Movie: tagline must not be "TBD"
    assert type_counts.get(CheckType.LOGICAL_NOT, 0) >= 1
