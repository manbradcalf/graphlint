"""
graphlint.shacl_parser — Parse SHACL/Turtle schemas into a validation plan (IR).

Takes a SHACL schema in Turtle syntax, parses it via rdflib, then walks
the graph to produce the same Check objects as the ShExC parser.
"""

from __future__ import annotations

import warnings
from typing import Optional

from rdflib import Graph, URIRef, Literal as RDFLiteral, RDF
from rdflib.collection import Collection
from rdflib.namespace import SH

from graphlint.parser import (
    Check,
    CheckType,
    Severity,
    RelationshipTarget,
    ValidationPlan,
    Mapping,
    XSD_TO_LPG_TYPE,
    _generate_strict_checks,
    _rel_cardinality_message,
)


def parse_shacl_to_plan(
    turtle: str,
    mapping: Optional[Mapping] = None,
    source: str = "<string>",
    strict: bool = False,
) -> ValidationPlan:
    """Parse a SHACL/Turtle schema string and produce a ValidationPlan."""

    if mapping is None:
        mapping = Mapping()

    g = Graph()
    g.parse(data=turtle, format="turtle")

    checks: list[Check] = []
    # Store class IRIs (not shape IRIs) so _generate_strict_checks produces
    # correct LPG labels via mapping.label_for().
    class_iris: list[str] = []

    for shape_node in g.subjects(RDF.type, SH.NodeShape):
        shape_iri = str(shape_node)

        # Resolve target label from sh:targetClass
        target_classes = list(g.objects(shape_node, SH.targetClass))
        if not target_classes:
            # Fallback: use the shape IRI itself as the class
            target_classes = [shape_node]

        for target_class in target_classes:
            class_iri = str(target_class)
            class_iris.append(class_iri)
            label = mapping.label_for(class_iri)

            # Process each sh:property block
            for prop_node in g.objects(shape_node, SH.property):
                checks.extend(
                    _process_property_shape(g, prop_node, shape_iri, label, mapping)
                )

    if strict:
        checks.extend(_generate_strict_checks(class_iris, checks, mapping))

    return ValidationPlan(
        schema_source=source,
        checks=checks,
        shapes=class_iris,
        mapping=mapping,
    )


def _process_property_shape(
    g: Graph,
    prop_node,
    shape_iri: str,
    label: str,
    mapping: Mapping,
) -> list[Check]:
    """Process a single sh:property shape into one or more Checks."""

    # Extract path (required)
    path = g.value(prop_node, SH.path)
    if path is None:
        return []
    if not isinstance(path, URIRef):
        warnings.warn(
            f"Complex sh:path in shape {shape_iri} is not yet supported, skipping."
        )
        return []

    predicate_iri = str(path)

    # Cardinality — SHACL defaults: minCount=0, maxCount=unbounded
    min_count_lit = g.value(prop_node, SH.minCount)
    max_count_lit = g.value(prop_node, SH.maxCount)
    min_count = int(min_count_lit) if min_count_lit is not None else 0
    max_count = int(max_count_lit) if max_count_lit is not None else None

    # Severity
    sev_iri = g.value(prop_node, SH.severity)
    severity = _shacl_severity(sev_iri)

    # Distinguish property vs relationship
    node_kind = g.value(prop_node, SH.nodeKind)
    sh_node = g.value(prop_node, SH.node)
    sh_class = g.value(prop_node, SH["class"])
    sh_datatype = g.value(prop_node, SH.datatype)

    if _is_relationship_constraint(node_kind, sh_node, sh_class, sh_datatype):
        return _relationship_checks(
            g, prop_node, shape_iri, label, predicate_iri,
            min_count, max_count, severity, sh_node, sh_class, mapping,
        )
    else:
        return _property_checks(
            g, prop_node, shape_iri, label, predicate_iri,
            min_count, max_count, severity, sh_datatype, mapping,
        )


def _is_relationship_constraint(node_kind, sh_node, sh_class, sh_datatype) -> bool:
    """Determine if a property shape describes a relationship (not a property)."""
    if node_kind is not None:
        return str(node_kind) in (str(SH.IRI), str(SH.BlankNodeOrIRI))
    if sh_node is not None or sh_class is not None:
        return True
    if sh_datatype is not None:
        return False
    return False


def _property_checks(
    g: Graph,
    prop_node,
    shape_iri: str,
    label: str,
    predicate_iri: str,
    min_count: int,
    max_count: Optional[int],
    severity: Severity,
    sh_datatype,
    mapping: Mapping,
) -> list[Check]:
    """Generate property checks (EXISTS, TYPE, VALUE_IN)."""

    checks: list[Check] = []
    prop_name = mapping.property_for(predicate_iri)
    is_optional = min_count == 0

    # Existence check
    if not is_optional:
        checks.append(Check(
            id=f"{label.lower()}-{prop_name}-exists",
            type=CheckType.PROPERTY_EXISTS,
            shape=shape_iri,
            target_label=label,
            severity=severity,
            message=f"{label} node missing required '{prop_name}' property",
            property=prop_name,
        ))

    # Datatype check
    if sh_datatype is not None:
        dt_str = str(sh_datatype)
        lpg_type = XSD_TO_LPG_TYPE.get(dt_str, Mapping._local_name(dt_str))
        checks.append(Check(
            id=f"{label.lower()}-{prop_name}-type",
            type=CheckType.PROPERTY_TYPE,
            shape=shape_iri,
            target_label=label,
            severity=severity,
            message=f"{label}.{prop_name} must be of type {lpg_type}",
            property=prop_name,
            expected_type=lpg_type,
            only_if_exists=is_optional,
        ))

    # Value set — sh:in is an RDF list
    sh_in = g.value(prop_node, SH["in"])
    if sh_in is not None:
        allowed = _extract_rdf_list(g, sh_in)
        checks.append(Check(
            id=f"{label.lower()}-{prop_name}-values",
            type=CheckType.PROPERTY_VALUE_IN,
            shape=shape_iri,
            target_label=label,
            severity=severity,
            message=f"{label}.{prop_name} must be one of: {allowed}",
            property=prop_name,
            allowed_values=allowed,
            only_if_exists=is_optional,
        ))

    return checks


def _relationship_checks(
    g: Graph,
    prop_node,
    shape_iri: str,
    label: str,
    predicate_iri: str,
    min_count: int,
    max_count: Optional[int],
    severity: Severity,
    sh_node,
    sh_class,
    mapping: Mapping,
) -> list[Check]:
    """Generate relationship cardinality checks."""

    checks: list[Check] = []
    rel_type = mapping.relationship_for(predicate_iri)

    # Resolve target label
    if sh_node is not None:
        # sh:node points to another NodeShape; get its targetClass
        target_class = g.value(sh_node, SH.targetClass)
        if target_class is not None:
            target_label = mapping.label_for(str(target_class))
        else:
            target_label = mapping.label_for(str(sh_node))
    elif sh_class is not None:
        target_label = mapping.label_for(str(sh_class))
    else:
        target_label = "Unknown"

    checks.append(Check(
        id=f"{label.lower()}-{rel_type.lower()}-cardinality",
        type=CheckType.RELATIONSHIP_CARDINALITY,
        shape=shape_iri,
        target_label=label,
        severity=severity,
        message=_rel_cardinality_message(label, rel_type, target_label, min_count, max_count),
        relationship=RelationshipTarget(
            type=rel_type,
            direction="outgoing",
            target_label=target_label,
        ),
        min_count=min_count,
        max_count=max_count,
    ))

    return checks


def _extract_rdf_list(g: Graph, list_node) -> list:
    """Extract values from an RDF list (for sh:in)."""
    result = []
    for item in Collection(g, list_node):
        if isinstance(item, RDFLiteral):
            val = item.toPython()
            # rdflib may return Decimal for xsd:decimal — coerce to float
            if hasattr(val, "as_integer_ratio") and not isinstance(val, (int, float)):
                val = float(val)
            result.append(val)
        else:
            result.append(str(item))
    return result


def _shacl_severity(sev_iri) -> Severity:
    """Map SHACL severity IRI to graphlint Severity enum."""
    if sev_iri is None:
        return Severity.VIOLATION  # SHACL default
    sev_str = str(sev_iri)
    if sev_str == str(SH.Warning):
        return Severity.WARNING
    if sev_str == str(SH.Info):
        return Severity.INFO
    return Severity.VIOLATION
