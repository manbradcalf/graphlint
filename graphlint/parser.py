"""
graphlint.parser — Parse graph schemas into a validation plan (IR).

Supports SHACL/Turtle (via rdflib). The unified entry point is
parse_schema().
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


# ─── Data types ──────────────────────────────────────────────────────


class Severity(str, Enum):
    VIOLATION = "violation"
    WARNING = "warning"
    INFO = "info"


class CheckType(str, Enum):
    PROPERTY_EXISTS = "property_exists"
    PROPERTY_TYPE = "property_type"
    PROPERTY_VALUE_IN = "property_value_in"
    PROPERTY_PATTERN = "property_pattern"
    PROPERTY_STRING_LENGTH = "property_string_length"
    PROPERTY_RANGE = "property_range"
    PROPERTY_PAIR = "property_pair"
    RELATIONSHIP_CARDINALITY = "relationship_cardinality"
    RELATIONSHIP_ENDPOINT = "relationship_endpoint"
    CLOSED_SHAPE = "closed_shape"
    UNDECLARED_LABELS = "undeclared_labels"
    UNDECLARED_RELATIONSHIP_TYPES = "undeclared_relationship_types"
    UNDECLARED_PROPERTIES = "undeclared_properties"
    EMPTY_SHAPE = "empty_shape"
    QUALIFIED_CARDINALITY = "qualified_cardinality"
    LOGICAL_NOT = "logical_not"
    LOGICAL_AND = "logical_and"
    LOGICAL_OR = "logical_or"
    LOGICAL_XONE = "logical_xone"
    UNIQUE_LANG = "unique_lang"


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
    # Pattern (sh:pattern)
    pattern: Optional[str] = None
    pattern_flags: Optional[str] = None
    # String length (sh:minLength, sh:maxLength)
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    # Numeric range (sh:minInclusive, sh:maxInclusive, sh:minExclusive, sh:maxExclusive)
    min_inclusive: Optional[float] = None
    max_inclusive: Optional[float] = None
    min_exclusive: Optional[float] = None
    max_exclusive: Optional[float] = None
    # Property pair (sh:equals, sh:disjoint, sh:lessThan, sh:lessThanOrEquals)
    compare_property: Optional[str] = None
    comparison_type: Optional[str] = None  # "equals", "disjoint", "lessThan", "lessThanOrEquals"
    # Class hierarchy (sh:class with rdfs:subClassOf)
    acceptable_labels: Optional[list[str]] = None
    # Qualified cardinality (sh:qualifiedValueShape)
    qualified_filter: Optional["Check"] = None
    qualified_min: Optional[int] = None
    qualified_max: Optional[int] = None
    # Logical constraints (sh:not, sh:and, sh:or, sh:xone)
    sub_checks: Optional[list["Check"]] = None
    # Annotation properties
    default_value: Optional[Any] = None
    display_order: Optional[int] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        d["severity"] = self.severity.value
        # Handle nested Check objects
        if self.qualified_filter is not None:
            d["qualified_filter"] = self.qualified_filter.to_dict()
        if self.sub_checks is not None:
            d["sub_checks"] = [sc.to_dict() for sc in self.sub_checks]
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


def parse_schema(
    schema: str,
    mapping: Optional[Mapping] = None,
    source: str = "<string>",
    strict: bool = False,
) -> ValidationPlan:
    """Parse a SHACL/Turtle schema string into a ValidationPlan."""
    from graphlint.shacl_parser import parse_shacl_to_plan
    return parse_shacl_to_plan(schema, mapping=mapping, source=source, strict=strict)
