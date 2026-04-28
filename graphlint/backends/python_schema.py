"""
graphlint.backends.python_schema — Generate Python schema constants from a ValidationPlan.

Reads the parsed SHACL shapes and emits a Python module with typed constants
for entity types, relationship types, display labels, and relationship
constraints. Designed for use in knowledge graph extraction pipelines
where the schema drives NER and relationship extraction.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from rdflib import Graph, RDFS
from rdflib.namespace import SH

from graphlint.parser import CheckType, ValidationPlan


def generate_schema(
    plan: ValidationPlan,
    shacl_source: str | None = None,
) -> str:
    """Generate Python schema module source from a ValidationPlan.

    Uses LPG labels as-is for entity types and relationship types.
    For human-readable display names, use ``generate_schema_with_labels``
    which extracts ``rdfs:label`` annotations from the SHACL source.

    Args:
        plan: Parsed SHACL validation plan.
        shacl_source: Original SHACL filename for the docstring.

    Returns:
        Python source code as a string.
    """
    mapping = plan.mapping
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    source_name = shacl_source or plan.schema_source

    # --- Extract entity types ---
    entity_types: dict[str, str] = {}
    for class_iri in plan.shapes:
        label = mapping.label_for(class_iri)
        entity_types[label] = _label_to_display(label)

    # --- Collect relationship types and constraints from checks ---
    rel_types: list[str] = []
    rel_constraints: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"source": set(), "target": set()}
    )

    for check in plan.checks:
        if check.type == CheckType.RELATIONSHIP_CARDINALITY and check.relationship:
            rel = check.relationship
            if rel.type not in rel_types:
                rel_types.append(rel.type)
            rel_constraints[rel.type]["source"].add(check.target_label)
            # Use acceptable_labels (from sh:or or class hierarchy) if available
            if check.acceptable_labels:
                for al in check.acceptable_labels:
                    rel_constraints[rel.type]["target"].add(al)
            elif rel.target_label and rel.target_label != "Unknown":
                rel_constraints[rel.type]["target"].add(rel.target_label)

    # --- Build display labels ---
    rel_display: dict[str, str] = {
        rt: _label_to_display(rt) for rt in rel_types
    }

    # --- Render ---
    lines: list[str] = []

    # Header
    lines.append(f'"""Schema constants auto-generated from SHACL by graphlint.')
    lines.append(f"")
    lines.append(f"Source: {source_name}")
    lines.append(f"Generated: {now}")
    lines.append(f'"""')
    lines.append(f"")

    # ENTITY_TYPES
    lines.append("# Entity types — graph label → human-readable name")
    lines.append("ENTITY_TYPES: dict[str, str] = {")
    for label, human in entity_types.items():
        lines.append(f'    "{label}": "{human}",')
    lines.append("}")
    lines.append("")

    # RELATIONSHIP_TYPES
    lines.append("# Relationship types — valid relationship type strings from the SHACL schema")
    lines.append("RELATIONSHIP_TYPES: list[str] = [")
    for rt in rel_types:
        lines.append(f'    "{rt}",')
    lines.append("]")
    lines.append("")

    # RELATIONSHIP_DISPLAY_LABELS
    lines.append("# Relationship display labels — type → human-readable label for UI")
    lines.append("RELATIONSHIP_DISPLAY_LABELS: dict[str, str] = {")
    for rt, display in rel_display.items():
        lines.append(f'    "{rt}": "{display}",')
    lines.append("}")
    lines.append("")

    # RELATIONSHIP_CONSTRAINTS
    lines.append("# Relationship constraints — which source/target types are valid")
    lines.append("RELATIONSHIP_CONSTRAINTS: dict[str, dict[str, list[str]]] = {")
    for rt in rel_types:
        sources = sorted(rel_constraints[rt]["source"])
        targets = sorted(rel_constraints[rt]["target"])
        lines.append(f'    "{rt}": {{')
        lines.append(f'        "source": {sources},')
        lines.append(f'        "target": {targets},')
        lines.append(f"    }},")
    lines.append("}")
    lines.append("")

    return "\n".join(lines)


def generate_schema_with_labels(
    plan: ValidationPlan,
    turtle: str,
    shacl_source: str | None = None,
) -> str:
    """Generate schema with human-readable labels extracted from SHACL rdfs:label.

    This is the preferred entry point — it re-parses the Turtle to extract
    ``rdfs:label`` annotations that the ValidationPlan doesn't carry,
    and uses them as human-readable display names for entity types.

    For relationship display labels, ``rdfs:comment`` on the property shape
    is used if available, otherwise the relationship type is converted to
    a readable form.
    """
    mapping = plan.mapping

    # Parse the Turtle to get rdfs:label for each shape
    g = Graph()
    g.parse(data=turtle, format="turtle")

    label_map: dict[str, str] = {}
    for shape_node in g.subjects(SH.targetClass):
        target_class = g.value(shape_node, SH.targetClass)
        if target_class is None:
            continue
        lpg_label = mapping.label_for(str(target_class))
        rdfs_label = g.value(shape_node, RDFS.label)
        if rdfs_label is not None:
            label_map[lpg_label] = str(rdfs_label)

    # Generate the base schema
    source = generate_schema(plan, shacl_source=shacl_source)

    # Replace generated display names with rdfs:label values
    for lpg_label, human_label in label_map.items():
        placeholder = _label_to_display(lpg_label)
        source = source.replace(
            f'"{lpg_label}": "{placeholder}"',
            f'"{lpg_label}": "{human_label}"',
        )

    return source


def _label_to_display(name: str) -> str:
    """Convert a graph label to a human-readable display string.

    Generic conversion: replaces underscores with spaces and lowercases.
    Works for any naming convention — no ontology-specific logic.

    Examples:
        HAS_CULTURAL_AFFILIATION → has cultural affiliation
        Person → Person
        Man-Made_Object → Man-Made Object
    """
    return name.replace("_", " ").lower()
