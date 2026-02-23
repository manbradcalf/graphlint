"""
graphlint.parser — Parse graph schemas into a validation plan (IR).

Supports ShExC (via pyshexc) and SHACL/Turtle (via rdflib). The unified
entry point is parse_schema(), which auto-detects the format.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional
from pyshexc.parser_impl.generate_shexj import parse as parse_shexc


# ─── Data types ──────────────────────────────────────────────────────


class Severity(str, Enum):
    VIOLATION = "violation"
    WARNING = "warning"
    INFO = "info"


class CheckType(str, Enum):
    PROPERTY_EXISTS = "property_exists"
    PROPERTY_TYPE = "property_type"
    PROPERTY_VALUE_IN = "property_value_in"
    RELATIONSHIP_CARDINALITY = "relationship_cardinality"
    RELATIONSHIP_ENDPOINT = "relationship_endpoint"
    CLOSED_SHAPE = "closed_shape"
    UNDECLARED_LABELS = "undeclared_labels"
    UNDECLARED_RELATIONSHIP_TYPES = "undeclared_relationship_types"
    UNDECLARED_PROPERTIES = "undeclared_properties"
    EMPTY_SHAPE = "empty_shape"


@dataclass
class RelationshipTarget:
    type: str  # relationship type in LPG (e.g. "HAS_COMPONENT")
    direction: str  # "outgoing" or "incoming"
    target_label: str  # target node label


@dataclass
class Check:
    id: str
    type: CheckType
    shape: Optional[str]
    target_label: str
    severity: Severity
    message: str
    # Property checks
    property: Optional[str] = None
    expected_type: Optional[str] = None
    allowed_values: Optional[list] = None
    only_if_exists: bool = False
    # Relationship checks
    relationship: Optional[RelationshipTarget] = None
    min_count: Optional[int] = None
    max_count: Optional[int] = None
    # Closed shape
    allowed_properties: Optional[list[str]] = None
    allowed_relationships: Optional[list[str]] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        d["severity"] = self.severity.value
        return {k: v for k, v in d.items() if v is not None}


# ─── Mapping ─────────────────────────────────────────────────────────


@dataclass
class Mapping:
    """Maps between RDF IRIs and LPG names.

    Uses convention-based defaults: the local name (fragment after # or
    last path segment) of an IRI becomes the LPG label/type/property.

    Users can override with explicit mappings.
    """

    classes_to_labels: dict[str, str] = field(default_factory=dict)
    predicates_to_relationships: dict[str, str] = field(default_factory=dict)
    predicates_to_properties: dict[str, str] = field(default_factory=dict)

    def label_for(self, iri: str) -> str:
        if iri in self.classes_to_labels:
            return self.classes_to_labels[iri]
        return self._local_name(iri)

    def property_for(self, iri: str) -> str:
        if iri in self.predicates_to_properties:
            return self.predicates_to_properties[iri]
        return self._local_name(iri)

    def relationship_for(self, iri: str) -> str:
        if iri in self.predicates_to_relationships:
            return self.predicates_to_relationships[iri]
        # Convention: camelCase → UPPER_SNAKE_CASE
        local = self._local_name(iri)
        return self._to_upper_snake(local)

    @staticmethod
    def _local_name(iri: str) -> str:
        if "#" in iri:
            return iri.rsplit("#", 1)[1]
        return iri.rsplit("/", 1)[-1]

    @staticmethod
    def _to_upper_snake(name: str) -> str:
        """Convert camelCase to UPPER_SNAKE_CASE."""
        result = []
        for i, ch in enumerate(name):
            if ch.isupper() and i > 0:
                result.append("_")
            result.append(ch.upper())
        return "".join(result)


# ─── XSD type mapping ────────────────────────────────────────────────

XSD = "http://www.w3.org/2001/XMLSchema#"

XSD_TO_LPG_TYPE = {
    f"{XSD}string": "string",
    f"{XSD}integer": "integer",
    f"{XSD}int": "integer",
    f"{XSD}long": "integer",
    f"{XSD}float": "float",
    f"{XSD}double": "float",
    f"{XSD}decimal": "float",
    f"{XSD}boolean": "boolean",
    f"{XSD}date": "date",
    f"{XSD}dateTime": "datetime",
}


# ─── Validation Plan ─────────────────────────────────────────────────


@dataclass
class ValidationPlan:
    schema_source: str
    checks: list[Check]
    shapes: list[str]  # shape IRIs found in schema
    mapping: Mapping

    def to_dict(self) -> dict:
        return {
            "schema_source": self.schema_source,
            "shapes": self.shapes,
            "checks": [c.to_dict() for c in self.checks],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ─── Parser ──────────────────────────────────────────────────────────


def parse_shexc_to_plan(
    shexc: str,
    mapping: Optional[Mapping] = None,
    source: str = "<string>",
    strict: bool = False,
) -> ValidationPlan:
    """Parse a ShExC schema string and produce a ValidationPlan."""

    if mapping is None:
        mapping = Mapping()

    schema = parse_shexc(shexc)
    # schema is a ShExJSG Schema object; get the JSON representation
    schema_json = json.loads(schema._as_json)

    checks: list[Check] = []
    shape_iris: list[str] = []

    for shape in schema_json.get("shapes", []):
        shape_id = shape.get("id", "")
        shape_iris.append(shape_id)
        label = mapping.label_for(shape_id)

        expression = shape.get("expression")
        if expression is None:
            continue

        shape_checks = _process_expression(expression, shape_id, label, mapping)
        checks.extend(shape_checks)

    if strict:
        checks.extend(_generate_strict_checks(shape_iris, checks, mapping))

    return ValidationPlan(
        schema_source=source,
        checks=checks,
        shapes=shape_iris,
        mapping=mapping,
    )


def _process_expression(
    expr: dict,
    shape_id: str,
    label: str,
    mapping: Mapping,
) -> list[Check]:
    """Recursively process a ShExJ expression into checks."""

    checks = []
    expr_type = expr.get("type")

    if expr_type == "EachOf":
        for sub_expr in expr.get("expressions", []):
            checks.extend(_process_expression(sub_expr, shape_id, label, mapping))

    elif expr_type == "TripleConstraint":
        checks.extend(_process_triple_constraint(expr, shape_id, label, mapping))

    return checks


def _process_triple_constraint(
    tc: dict,
    shape_id: str,
    label: str,
    mapping: Mapping,
) -> list[Check]:
    """Convert a single TripleConstraint into one or more Checks."""

    checks = []
    predicate = tc.get("predicate", "")
    value_expr = tc.get("valueExpr")
    min_card = tc.get("min", 1)  # ShExC default is 1
    max_card = tc.get("max", 1)  # ShExC default is 1 (-1 = unbounded)

    if max_card == -1:
        max_card = None  # unbounded

    is_optional = min_card == 0

    # Determine if this is a property constraint or a relationship constraint
    is_relationship = _is_shape_reference(value_expr)

    if is_relationship:
        # Relationship constraint
        target_shape_iri = (
            value_expr if isinstance(value_expr, str) else value_expr.get("id", "")
        )
        target_label = mapping.label_for(target_shape_iri)
        rel_type = mapping.relationship_for(predicate)

        check_id = f"{label.lower()}-{rel_type.lower()}-cardinality"
        checks.append(
            Check(
                id=check_id,
                type=CheckType.RELATIONSHIP_CARDINALITY,
                shape=shape_id,
                target_label=label,
                severity=Severity.VIOLATION,
                message=_rel_cardinality_message(
                    label, rel_type, target_label, min_card, max_card
                ),
                relationship=RelationshipTarget(
                    type=rel_type,
                    direction="outgoing",
                    target_label=target_label,
                ),
                min_count=min_card,
                max_count=max_card,
            )
        )

    else:
        # Property constraint
        prop_name = mapping.property_for(predicate)

        if not is_optional:
            # Required property — existence check
            check_id = f"{label.lower()}-{prop_name}-exists"
            checks.append(
                Check(
                    id=check_id,
                    type=CheckType.PROPERTY_EXISTS,
                    shape=shape_id,
                    target_label=label,
                    severity=Severity.VIOLATION,
                    message=f"{label} node missing required '{prop_name}' property",
                    property=prop_name,
                )
            )

        if isinstance(value_expr, dict):
            # Datatype constraint
            datatype = value_expr.get("datatype")
            if datatype:
                lpg_type = XSD_TO_LPG_TYPE.get(datatype, datatype)
                check_id = f"{label.lower()}-{prop_name}-type"
                checks.append(
                    Check(
                        id=check_id,
                        type=CheckType.PROPERTY_TYPE,
                        shape=shape_id,
                        target_label=label,
                        severity=Severity.VIOLATION,
                        message=f"{label}.{prop_name} must be of type {lpg_type}",
                        property=prop_name,
                        expected_type=lpg_type,
                        only_if_exists=is_optional,
                    )
                )

            # Value set constraint
            values = value_expr.get("values")
            if values:
                allowed = _extract_value_set(values)
                check_id = f"{label.lower()}-{prop_name}-values"
                checks.append(
                    Check(
                        id=check_id,
                        type=CheckType.PROPERTY_VALUE_IN,
                        shape=shape_id,
                        target_label=label,
                        severity=Severity.VIOLATION,
                        message=f"{label}.{prop_name} must be one of: {allowed}",
                        property=prop_name,
                        allowed_values=allowed,
                        only_if_exists=is_optional,
                    )
                )

    return checks


def _is_shape_reference(value_expr) -> bool:
    """Check if a valueExpr is a reference to another shape (relationship)
    vs a node constraint (property)."""
    if isinstance(value_expr, str):
        # A bare string IRI = shape reference
        return True
    if isinstance(value_expr, dict):
        # NodeConstraint with datatype or values = property
        if value_expr.get("type") == "NodeConstraint":
            return False
        # ShapeRef or shape ID = relationship
        if value_expr.get("type") in ("ShapeRef", "Shape"):
            return True
    return False


def _extract_value_set(values: list) -> list:
    """Extract allowed values from a ShExJ value set."""
    result = []
    for v in values:
        if isinstance(v, dict):
            val = v.get("value")
            vtype = v.get("type", "")
            # Try to coerce to numeric if it looks like one
            if vtype and ("integer" in vtype or "int" in vtype):
                try:
                    val = int(val)
                except (ValueError, TypeError):
                    pass
            elif vtype and (
                "float" in vtype or "decimal" in vtype or "double" in vtype
            ):
                try:
                    val = float(val)
                except (ValueError, TypeError):
                    pass
            result.append(val)
        else:
            result.append(v)
    return result


def _rel_cardinality_message(
    source: str,
    rel_type: str,
    target: str,
    min_c: int,
    max_c: Optional[int],
) -> str:
    if min_c == 0 and max_c == 1:
        return f"{source} may have at most one {rel_type} relationship to {target}"
    elif min_c == 1 and max_c == 1:
        return f"{source} must have exactly one {rel_type} relationship to {target}"
    elif min_c == 1 and max_c is None:
        return f"{source} must have at least one {rel_type} relationship to {target}"
    elif min_c == 0 and max_c is None:
        return f"{source} may have zero or more {rel_type} relationships to {target}"
    else:
        max_str = str(max_c) if max_c is not None else "∞"
        return f"{source} must have {min_c}..{max_str} {rel_type} relationships to {target}"


# ─── Strict mode checks ──────────────────────────────────────────────


def _generate_strict_checks(
    shape_iris: list[str],
    existing_checks: list[Check],
    mapping: Mapping,
) -> list[Check]:
    """Generate closed-world coverage checks from already-parsed shapes."""

    strict_checks: list[Check] = []

    # Collect declared LPG labels
    declared_labels = [mapping.label_for(iri) for iri in shape_iris]

    # Collect declared relationship types across all shapes
    declared_rels = sorted({
        c.relationship.type
        for c in existing_checks
        if c.relationship is not None
    })

    # Collect declared properties per label
    props_by_label: dict[str, list[str]] = {}
    for c in existing_checks:
        if c.property and c.target_label:
            props_by_label.setdefault(c.target_label, [])
            if c.property not in props_by_label[c.target_label]:
                props_by_label[c.target_label].append(c.property)

    # 1. Undeclared node labels
    strict_checks.append(Check(
        id="strict-undeclared-labels",
        type=CheckType.UNDECLARED_LABELS,
        shape=None,
        target_label="*",
        severity=Severity.WARNING,
        message=f"Database contains node labels not declared in schema. Declared: {declared_labels}",
        allowed_values=declared_labels,
    ))

    # 2. Undeclared relationship types
    strict_checks.append(Check(
        id="strict-undeclared-rel-types",
        type=CheckType.UNDECLARED_RELATIONSHIP_TYPES,
        shape=None,
        target_label="*",
        severity=Severity.WARNING,
        message=f"Database contains relationship types not declared in schema. Declared: {declared_rels}",
        allowed_relationships=declared_rels,
    ))

    # 3. Undeclared properties (one check per declared label)
    for label in declared_labels:
        props = props_by_label.get(label, [])
        strict_checks.append(Check(
            id=f"strict-{label.lower()}-undeclared-props",
            type=CheckType.UNDECLARED_PROPERTIES,
            shape=None,
            target_label=label,
            severity=Severity.WARNING,
            message=f"{label} nodes have properties not declared in schema. Declared: {props}",
            allowed_properties=props,
        ))

    # 4. Empty shapes — warn when a declared label has zero instances
    for label in declared_labels:
        strict_checks.append(Check(
            id=f"strict-{label.lower()}-empty",
            type=CheckType.EMPTY_SHAPE,
            shape=None,
            target_label=label,
            severity=Severity.WARNING,
            message=f"Schema declares {label} but no {label} nodes exist in the database",
        ))

    return strict_checks


# ─── Unified entry point ────────────────────────────────────────────


def _detect_schema_format(text: str) -> str:
    """Detect schema format: 'shexc' or 'shacl'."""
    shacl_signals = [
        "sh:NodeShape",
        "sh:targetClass",
        "sh:property",
        "<http://www.w3.org/ns/shacl#",
    ]
    for signal in shacl_signals:
        if signal in text:
            return "shacl"
    return "shexc"


def parse_schema(
    schema: str,
    mapping: Optional[Mapping] = None,
    source: str = "<string>",
    strict: bool = False,
    format: Optional[str] = None,
) -> ValidationPlan:
    """Parse a schema string (ShExC or SHACL/Turtle) into a ValidationPlan.

    If format is None, auto-detects based on content.
    """
    fmt = format or _detect_schema_format(schema)
    if fmt == "shacl":
        from graphlint.shacl_parser import parse_shacl_to_plan
        return parse_shacl_to_plan(schema, mapping=mapping, source=source, strict=strict)
    else:
        return parse_shexc_to_plan(schema, mapping=mapping, source=source, strict=strict)
