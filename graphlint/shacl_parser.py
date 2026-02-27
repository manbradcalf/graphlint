"""
graphlint.shacl_parser — Parse SHACL/Turtle schemas into a validation plan (IR).

Takes a SHACL schema in Turtle syntax, parses it via rdflib, then walks
the graph to produce Check objects for validation.
"""

from __future__ import annotations

import warnings
from typing import Optional

from rdflib import Graph, URIRef, Literal as RDFLiteral, RDF, RDFS, BNode
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

    # Build rdfs:subClassOf hierarchy (transitive closure)
    class_hierarchy = _build_class_hierarchy(g, mapping)

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

            # Collect declared property paths for sh:closed
            declared_paths: list[str] = []

            # Process each sh:property block
            for prop_node in g.objects(shape_node, SH.property):
                path = g.value(prop_node, SH.path)
                if path is not None and isinstance(path, URIRef):
                    declared_paths.append(str(path))
                checks.extend(
                    _process_property_shape(
                        g, prop_node, shape_iri, label, mapping, class_hierarchy
                    )
                )

            # sh:closed — emit UNDECLARED_PROPERTIES check
            sh_closed = g.value(shape_node, SH.closed)
            if sh_closed is not None and sh_closed.toPython() is True:
                ignored_node = g.value(shape_node, SH.ignoredProperties)
                ignored_iris: list[str] = []
                if ignored_node is not None:
                    for item in Collection(g, ignored_node):
                        ignored_iris.append(str(item))

                all_allowed = declared_paths + ignored_iris
                allowed_props = [mapping.property_for(iri) for iri in all_allowed]

                checks.append(Check(
                    id=f"{label.lower()}-closed-undeclared-props",
                    type=CheckType.UNDECLARED_PROPERTIES,
                    shape=shape_iri,
                    target_label=label,
                    severity=Severity.VIOLATION,
                    message=f"{label} is a closed shape; only declared properties are allowed: {allowed_props}",
                    allowed_properties=allowed_props,
                ))

            # sh:not, sh:and, sh:or, sh:xone — shape-level logical constraints
            checks.extend(
                _logical_constraints(g, shape_node, shape_iri, label, mapping)
            )

    if strict:
        checks.extend(_generate_strict_checks(class_iris, checks, mapping))

    return ValidationPlan(
        schema_source=source,
        checks=checks,
        shapes=class_iris,
        mapping=mapping,
    )


# ── Class hierarchy ──────────────────────────────────────────────


def _build_class_hierarchy(
    g: Graph, mapping: Mapping
) -> dict[str, list[str]]:
    """Build transitive closure of rdfs:subClassOf -> acceptable LPG labels.

    Returns a dict mapping each class IRI to the list of LPG labels
    that are acceptable (the class itself + all subclasses).
    """
    # Collect direct subclass relationships
    children: dict[str, list[str]] = {}
    for sub, _, sup in g.triples((None, RDFS.subClassOf, None)):
        sup_str = str(sup)
        sub_str = str(sub)
        children.setdefault(sup_str, []).append(sub_str)

    if not children:
        return {}

    # Compute transitive closure for each class that has subclasses
    hierarchy: dict[str, list[str]] = {}
    all_classes = set(children.keys())
    for child_list in children.values():
        all_classes.update(child_list)

    for cls in all_classes:
        descendants = _collect_descendants(cls, children)
        labels = [mapping.label_for(c) for c in descendants]
        hierarchy[cls] = labels

    return hierarchy


def _collect_descendants(cls: str, children: dict[str, list[str]]) -> list[str]:
    """Collect class + all transitive subclasses."""
    result = [cls]
    for child in children.get(cls, []):
        result.extend(_collect_descendants(child, children))
    return result


# ── Property shape processing ────────────────────────────────────


def _process_property_shape(
    g: Graph,
    prop_node,
    shape_iri: str,
    label: str,
    mapping: Mapping,
    class_hierarchy: dict[str, list[str]],
) -> list[Check]:
    """Process a single sh:property shape into one or more Checks."""

    # Extract path (required)
    path = g.value(prop_node, SH.path)
    if path is None:
        return []

    # Handle sh:inversePath
    direction = "outgoing"
    if isinstance(path, BNode):
        inverse = g.value(path, SH.inversePath)
        if inverse is not None and isinstance(inverse, URIRef):
            path = inverse
            direction = "incoming"
        else:
            warnings.warn(
                f"Complex sh:path in shape {shape_iri} is not yet supported, skipping."
            )
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
            direction, class_hierarchy,
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
    """Generate property checks (EXISTS, TYPE, VALUE_IN, PATTERN, etc.)."""

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

    # sh:hasValue — reuses PROPERTY_VALUE_IN with single value
    sh_has_value = g.value(prop_node, SH.hasValue)
    if sh_has_value is not None:
        if isinstance(sh_has_value, RDFLiteral):
            val = sh_has_value.toPython()
        else:
            val = str(sh_has_value)
        checks.append(Check(
            id=f"{label.lower()}-{prop_name}-hasvalue",
            type=CheckType.PROPERTY_VALUE_IN,
            shape=shape_iri,
            target_label=label,
            severity=severity,
            message=f"{label}.{prop_name} must have value {val}",
            property=prop_name,
            allowed_values=[val],
            only_if_exists=is_optional,
        ))

    # sh:pattern — regex constraint
    sh_pattern = g.value(prop_node, SH.pattern)
    if sh_pattern is not None:
        pattern_str = str(sh_pattern)
        sh_flags = g.value(prop_node, SH.flags)
        flags_str = str(sh_flags) if sh_flags is not None else None
        checks.append(Check(
            id=f"{label.lower()}-{prop_name}-pattern",
            type=CheckType.PROPERTY_PATTERN,
            shape=shape_iri,
            target_label=label,
            severity=severity,
            message=f"{label}.{prop_name} must match pattern '{pattern_str}'",
            property=prop_name,
            pattern=pattern_str,
            pattern_flags=flags_str,
            only_if_exists=is_optional,
        ))

    # sh:minLength / sh:maxLength — string length constraint
    sh_min_len = g.value(prop_node, SH.minLength)
    sh_max_len = g.value(prop_node, SH.maxLength)
    if sh_min_len is not None or sh_max_len is not None:
        min_len = int(sh_min_len) if sh_min_len is not None else None
        max_len = int(sh_max_len) if sh_max_len is not None else None
        msg_parts = []
        if min_len is not None:
            msg_parts.append(f"at least {min_len}")
        if max_len is not None:
            msg_parts.append(f"at most {max_len}")
        checks.append(Check(
            id=f"{label.lower()}-{prop_name}-strlen",
            type=CheckType.PROPERTY_STRING_LENGTH,
            shape=shape_iri,
            target_label=label,
            severity=severity,
            message=f"{label}.{prop_name} length must be {' and '.join(msg_parts)} characters",
            property=prop_name,
            min_length=min_len,
            max_length=max_len,
            only_if_exists=is_optional,
        ))

    # sh:minInclusive / sh:maxInclusive / sh:minExclusive / sh:maxExclusive
    sh_min_inc = g.value(prop_node, SH.minInclusive)
    sh_max_inc = g.value(prop_node, SH.maxInclusive)
    sh_min_exc = g.value(prop_node, SH.minExclusive)
    sh_max_exc = g.value(prop_node, SH.maxExclusive)
    if any(v is not None for v in (sh_min_inc, sh_max_inc, sh_min_exc, sh_max_exc)):
        min_inc = float(sh_min_inc.toPython()) if sh_min_inc is not None else None
        max_inc = float(sh_max_inc.toPython()) if sh_max_inc is not None else None
        min_exc = float(sh_min_exc.toPython()) if sh_min_exc is not None else None
        max_exc = float(sh_max_exc.toPython()) if sh_max_exc is not None else None
        msg_parts = []
        if min_inc is not None:
            msg_parts.append(f">= {min_inc}")
        if max_inc is not None:
            msg_parts.append(f"<= {max_inc}")
        if min_exc is not None:
            msg_parts.append(f"> {min_exc}")
        if max_exc is not None:
            msg_parts.append(f"< {max_exc}")
        checks.append(Check(
            id=f"{label.lower()}-{prop_name}-range",
            type=CheckType.PROPERTY_RANGE,
            shape=shape_iri,
            target_label=label,
            severity=severity,
            message=f"{label}.{prop_name} must be {', '.join(msg_parts)}",
            property=prop_name,
            min_inclusive=min_inc,
            max_inclusive=max_inc,
            min_exclusive=min_exc,
            max_exclusive=max_exc,
            only_if_exists=is_optional,
        ))

    # Property pair constraints: sh:equals, sh:disjoint, sh:lessThan, sh:lessThanOrEquals
    for pred, comp_type in [
        (SH.equals, "equals"),
        (SH.disjoint, "disjoint"),
        (SH.lessThan, "lessThan"),
        (SH.lessThanOrEquals, "lessThanOrEquals"),
    ]:
        comp_val = g.value(prop_node, pred)
        if comp_val is not None:
            comp_prop = mapping.property_for(str(comp_val))
            checks.append(Check(
                id=f"{label.lower()}-{prop_name}-{comp_type.lower()}",
                type=CheckType.PROPERTY_PAIR,
                shape=shape_iri,
                target_label=label,
                severity=severity,
                message=f"{label}.{prop_name} must be {comp_type} {label}.{comp_prop}",
                property=prop_name,
                compare_property=comp_prop,
                comparison_type=comp_type,
                only_if_exists=is_optional,
            ))

    # sh:uniqueLang — not applicable to LPG
    sh_unique_lang = g.value(prop_node, SH.uniqueLang)
    if sh_unique_lang is not None and sh_unique_lang.toPython() is True:
        warnings.warn(
            f"sh:uniqueLang on {predicate_iri} in {shape_iri}: "
            "LPG has no native language tags; constraint acknowledged but cannot be enforced."
        )
        checks.append(Check(
            id=f"{label.lower()}-{prop_name}-uniquelang",
            type=CheckType.UNIQUE_LANG,
            shape=shape_iri,
            target_label=label,
            severity=Severity.INFO,
            message=(
                f"sh:uniqueLang on {label}.{prop_name} — "
                "LPG has no native language tags; constraint acknowledged but not enforced"
            ),
            property=prop_name,
        ))

    # Annotation properties — metadata only, no queries
    sh_default = g.value(prop_node, SH.defaultValue)
    sh_order = g.value(prop_node, SH.order)
    default_val = None
    order_val = None
    if sh_default is not None:
        default_val = sh_default.toPython() if isinstance(sh_default, RDFLiteral) else str(sh_default)
    if sh_order is not None:
        order_val = int(sh_order)

    # Apply annotation fields to the last check emitted for this property, or create metadata check
    if default_val is not None or order_val is not None:
        if checks:
            # Attach to last check for this property
            last = checks[-1]
            if last.property == prop_name:
                last.default_value = default_val
                last.display_order = order_val

    # sh:qualifiedValueShape — qualified cardinality
    qvs = g.value(prop_node, SH.qualifiedValueShape)
    if qvs is not None:
        q_min_lit = g.value(prop_node, SH.qualifiedMinCount)
        q_max_lit = g.value(prop_node, SH.qualifiedMaxCount)
        q_min = int(q_min_lit) if q_min_lit is not None else None
        q_max = int(q_max_lit) if q_max_lit is not None else None

        q_filter = _parse_qualified_filter(g, qvs, shape_iri, label, prop_name, mapping)
        if q_filter is not None:
            msg_parts = []
            if q_min is not None:
                msg_parts.append(f"at least {q_min}")
            if q_max is not None:
                msg_parts.append(f"at most {q_max}")
            checks.append(Check(
                id=f"{label.lower()}-{prop_name}-qualified",
                type=CheckType.QUALIFIED_CARDINALITY,
                shape=shape_iri,
                target_label=label,
                severity=severity,
                message=f"{label}.{prop_name} must have {' and '.join(msg_parts)} values matching qualified shape",
                property=prop_name,
                qualified_filter=q_filter,
                qualified_min=q_min,
                qualified_max=q_max,
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
    direction: str = "outgoing",
    class_hierarchy: dict[str, list[str]] | None = None,
) -> list[Check]:
    """Generate relationship cardinality checks."""

    checks: list[Check] = []
    rel_type = mapping.relationship_for(predicate_iri)

    # Resolve target label
    target_class_iri = None
    if sh_node is not None:
        # sh:node points to another NodeShape; get its targetClass
        target_class = g.value(sh_node, SH.targetClass)
        if target_class is not None:
            target_label = mapping.label_for(str(target_class))
            target_class_iri = str(target_class)
        else:
            target_label = mapping.label_for(str(sh_node))
            target_class_iri = str(sh_node)
    elif sh_class is not None:
        target_label = mapping.label_for(str(sh_class))
        target_class_iri = str(sh_class)
    else:
        target_label = "Unknown"

    # Check class hierarchy for acceptable labels
    acceptable = None
    if class_hierarchy and target_class_iri and target_class_iri in class_hierarchy:
        acceptable = class_hierarchy[target_class_iri]

    checks.append(Check(
        id=f"{label.lower()}-{rel_type.lower()}-cardinality",
        type=CheckType.RELATIONSHIP_CARDINALITY,
        shape=shape_iri,
        target_label=label,
        severity=severity,
        message=_rel_cardinality_message(label, rel_type, target_label, min_count, max_count),
        relationship=RelationshipTarget(
            type=rel_type,
            direction=direction,
            target_label=target_label,
        ),
        min_count=min_count,
        max_count=max_count,
        acceptable_labels=acceptable,
    ))

    return checks


# ── Qualified cardinality ────────────────────────────────────────


def _parse_qualified_filter(
    g: Graph, qvs, shape_iri: str, label: str, prop_name: str, mapping: Mapping,
) -> Optional[Check]:
    """Parse a sh:qualifiedValueShape into a filter Check.

    Supports inner sh:datatype, sh:class, and sh:in constraints.
    """
    # Inner datatype
    inner_dt = g.value(qvs, SH.datatype)
    if inner_dt is not None:
        dt_str = str(inner_dt)
        lpg_type = XSD_TO_LPG_TYPE.get(dt_str, Mapping._local_name(dt_str))
        return Check(
            id=f"{label.lower()}-{prop_name}-qfilter-type",
            type=CheckType.PROPERTY_TYPE,
            shape=shape_iri,
            target_label=label,
            severity=Severity.VIOLATION,
            message=f"Qualified filter: type must be {lpg_type}",
            property=prop_name,
            expected_type=lpg_type,
        )

    # Inner sh:class
    inner_class = g.value(qvs, SH["class"])
    if inner_class is not None:
        inner_label = mapping.label_for(str(inner_class))
        return Check(
            id=f"{label.lower()}-{prop_name}-qfilter-class",
            type=CheckType.PROPERTY_TYPE,
            shape=shape_iri,
            target_label=inner_label,
            severity=Severity.VIOLATION,
            message=f"Qualified filter: must be {inner_label}",
            property=prop_name,
            expected_type=inner_label,
        )

    # Inner sh:in
    inner_in = g.value(qvs, SH["in"])
    if inner_in is not None:
        allowed = _extract_rdf_list(g, inner_in)
        return Check(
            id=f"{label.lower()}-{prop_name}-qfilter-values",
            type=CheckType.PROPERTY_VALUE_IN,
            shape=shape_iri,
            target_label=label,
            severity=Severity.VIOLATION,
            message=f"Qualified filter: value must be one of {allowed}",
            property=prop_name,
            allowed_values=allowed,
        )

    warnings.warn(
        f"sh:qualifiedValueShape in {shape_iri} for {prop_name}: "
        "inner shape type not supported, skipping."
    )
    return None


# ── Logical constraints ──────────────────────────────────────────


def _logical_constraints(
    g: Graph,
    shape_node,
    shape_iri: str,
    label: str,
    mapping: Mapping,
) -> list[Check]:
    """Parse sh:not, sh:and, sh:or, sh:xone on a shape node."""
    checks: list[Check] = []

    # sh:not — can appear multiple times
    for not_shape in g.objects(shape_node, SH["not"]):
        sub = _parse_logical_inner(g, not_shape, shape_iri, label, mapping)
        if sub:
            checks.append(Check(
                id=f"{label.lower()}-logical-not",
                type=CheckType.LOGICAL_NOT,
                shape=shape_iri,
                target_label=label,
                severity=Severity.VIOLATION,
                message=f"{label} must NOT satisfy: {sub[0].message}",
                sub_checks=sub,
            ))

    # sh:and, sh:or, sh:xone — RDF lists of shapes
    for pred, check_type, op_name in [
        (SH["and"], CheckType.LOGICAL_AND, "AND"),
        (SH["or"], CheckType.LOGICAL_OR, "OR"),
        (SH["xone"], CheckType.LOGICAL_XONE, "XONE"),
    ]:
        list_node = g.value(shape_node, pred)
        if list_node is not None:
            subs = []
            for inner_shape in Collection(g, list_node):
                inner = _parse_logical_inner(g, inner_shape, shape_iri, label, mapping)
                subs.extend(inner)
            if subs:
                checks.append(Check(
                    id=f"{label.lower()}-logical-{op_name.lower()}",
                    type=check_type,
                    shape=shape_iri,
                    target_label=label,
                    severity=Severity.VIOLATION,
                    message=f"{label} must satisfy {op_name} of {len(subs)} conditions",
                    sub_checks=subs,
                ))

    return checks


def _parse_logical_inner(
    g: Graph,
    inner_node,
    shape_iri: str,
    label: str,
    mapping: Mapping,
) -> list[Check]:
    """Parse an inner shape reference for logical constraints.

    Extracts simple property constraints from the inner shape.
    """
    checks: list[Check] = []

    # Inner shape may have sh:property blocks
    for prop_node in g.objects(inner_node, SH.property):
        path = g.value(prop_node, SH.path)
        if path is None or not isinstance(path, URIRef):
            continue

        prop_name = mapping.property_for(str(path))

        # Extract simple constraints from inner property shape
        sh_datatype = g.value(prop_node, SH.datatype)
        if sh_datatype is not None:
            dt_str = str(sh_datatype)
            lpg_type = XSD_TO_LPG_TYPE.get(dt_str, Mapping._local_name(dt_str))
            checks.append(Check(
                id=f"{label.lower()}-{prop_name}-inner-type",
                type=CheckType.PROPERTY_TYPE,
                shape=shape_iri,
                target_label=label,
                severity=Severity.VIOLATION,
                message=f"{label}.{prop_name} must be of type {lpg_type}",
                property=prop_name,
                expected_type=lpg_type,
            ))

        sh_min_inc = g.value(prop_node, SH.minInclusive)
        sh_max_inc = g.value(prop_node, SH.maxInclusive)
        if sh_min_inc is not None or sh_max_inc is not None:
            min_inc = float(sh_min_inc.toPython()) if sh_min_inc is not None else None
            max_inc = float(sh_max_inc.toPython()) if sh_max_inc is not None else None
            checks.append(Check(
                id=f"{label.lower()}-{prop_name}-inner-range",
                type=CheckType.PROPERTY_RANGE,
                shape=shape_iri,
                target_label=label,
                severity=Severity.VIOLATION,
                message=f"{label}.{prop_name} range constraint",
                property=prop_name,
                min_inclusive=min_inc,
                max_inclusive=max_inc,
            ))

        sh_pattern = g.value(prop_node, SH.pattern)
        if sh_pattern is not None:
            checks.append(Check(
                id=f"{label.lower()}-{prop_name}-inner-pattern",
                type=CheckType.PROPERTY_PATTERN,
                shape=shape_iri,
                target_label=label,
                severity=Severity.VIOLATION,
                message=f"{label}.{prop_name} must match pattern '{str(sh_pattern)}'",
                property=prop_name,
                pattern=str(sh_pattern),
            ))

        sh_has_value = g.value(prop_node, SH.hasValue)
        if sh_has_value is not None:
            val = sh_has_value.toPython() if isinstance(sh_has_value, RDFLiteral) else str(sh_has_value)
            checks.append(Check(
                id=f"{label.lower()}-{prop_name}-inner-hasvalue",
                type=CheckType.PROPERTY_VALUE_IN,
                shape=shape_iri,
                target_label=label,
                severity=Severity.VIOLATION,
                message=f"{label}.{prop_name} must equal '{val}'",
                property=prop_name,
                allowed_values=[val],
                only_if_exists=True,
            ))

        sh_min_count = g.value(prop_node, SH.minCount)
        if sh_min_count is not None and int(sh_min_count) > 0:
            checks.append(Check(
                id=f"{label.lower()}-{prop_name}-inner-exists",
                type=CheckType.PROPERTY_EXISTS,
                shape=shape_iri,
                target_label=label,
                severity=Severity.VIOLATION,
                message=f"{label} node must have '{prop_name}' property",
                property=prop_name,
            ))

    return checks


# ── Helpers ──────────────────────────────────────────────────────


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
