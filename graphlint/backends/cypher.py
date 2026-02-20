"""
graphlint.backends.cypher — Compile validation checks into Cypher queries.
"""

from __future__ import annotations

from graphlint.parser import Check, CheckType, Severity


class CypherBackend:
    name = "cypher"

    def compile_check(self, check: Check) -> str:
        dispatch = {
            CheckType.PROPERTY_EXISTS: self._property_exists,
            CheckType.PROPERTY_TYPE: self._property_type,
            CheckType.PROPERTY_VALUE_IN: self._property_value_in,
            CheckType.RELATIONSHIP_CARDINALITY: self._relationship_cardinality,
            CheckType.RELATIONSHIP_ENDPOINT: self._relationship_endpoint,
            CheckType.UNDECLARED_LABELS: self._undeclared_labels,
            CheckType.UNDECLARED_RELATIONSHIP_TYPES: self._undeclared_relationship_types,
            CheckType.UNDECLARED_PROPERTIES: self._undeclared_properties,
            CheckType.EMPTY_SHAPE: self._empty_shape,
        }
        handler = dispatch.get(check.type)
        if handler is None:
            raise NotImplementedError(f"Check type {check.type} not implemented for Cypher backend")
        return handler(check)

    # ── Property checks ──────────────────────────────────────────

    def _property_exists(self, check: Check) -> str:
        return (
            f"MATCH (n:{check.target_label})\n"
            f"WHERE n.{check.property} IS NULL\n"
            f"RETURN elementId(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       '{check.id}' AS check_id"
        )

    def _property_type(self, check: Check) -> str:
        type_check = _cypher_type_check(check.property, check.expected_type)

        if check.only_if_exists:
            where = f"WHERE n.{check.property} IS NOT NULL AND {type_check}"
        else:
            where = f"WHERE n.{check.property} IS NOT NULL AND {type_check}"

        return (
            f"MATCH (n:{check.target_label})\n"
            f"{where}\n"
            f"RETURN elementId(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       n.{check.property} AS actual_value,\n"
            f"       '{check.id}' AS check_id"
        )

    def _property_value_in(self, check: Check) -> str:
        values_str = _cypher_list_literal(check.allowed_values)

        if check.only_if_exists:
            where = f"WHERE n.{check.property} IS NOT NULL AND NOT n.{check.property} IN {values_str}"
        else:
            where = f"WHERE NOT n.{check.property} IN {values_str}"

        return (
            f"MATCH (n:{check.target_label})\n"
            f"{where}\n"
            f"RETURN elementId(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       n.{check.property} AS actual_value,\n"
            f"       '{check.id}' AS check_id"
        )

    # ── Relationship checks ──────────────────────────────────────

    def _relationship_cardinality(self, check: Check) -> str:
        rel = check.relationship
        min_c = check.min_count if check.min_count is not None else 0
        max_c = check.max_count  # None = unbounded

        if rel.direction == "outgoing":
            pattern = f"(n)-[r:{rel.type}]->(t:{rel.target_label})"
        else:
            pattern = f"(n)<-[r:{rel.type}]-(t:{rel.target_label})"

        # Build WHERE clause for cardinality violations
        conditions = []
        if min_c > 0:
            conditions.append(f"rel_count < {min_c}")
        if max_c is not None:
            conditions.append(f"rel_count > {max_c}")

        if not conditions:
            # min=0, max=unbounded — this check can never fail
            return (
                f"// Check {check.id}: no constraint (0..*)\n"
                f"// This check always passes — skipped"
            )

        where = " OR ".join(conditions)

        return (
            f"MATCH (n:{check.target_label})\n"
            f"OPTIONAL MATCH {pattern}\n"
            f"WITH n, count(r) AS rel_count\n"
            f"WHERE {where}\n"
            f"RETURN elementId(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       rel_count AS actual_count,\n"
            f"       '{check.id}' AS check_id"
        )

    def _relationship_endpoint(self, check: Check) -> str:
        return (
            f"MATCH (s)-[r:{check.relationship.type}]->(t)\n"
            f"WHERE NOT (s:{check.target_label} AND t:{check.relationship.target_label})\n"
            f"RETURN elementId(r) AS rel_id,\n"
            f"       type(r) AS rel_type,\n"
            f"       labels(s) AS source_labels,\n"
            f"       labels(t) AS target_labels,\n"
            f"       '{check.id}' AS check_id"
        )

    # ── Strict mode checks ────────────────────────────────────────

    def _undeclared_labels(self, check: Check) -> str:
        labels_str = _cypher_list_literal(check.allowed_values)
        return (
            f"CALL db.labels() YIELD label\n"
            f"WHERE NOT label IN {labels_str}\n"
            f"WITH label\n"
            f"MATCH (n) WHERE label IN labels(n)\n"
            f"WITH n, label LIMIT 1\n"
            f"RETURN elementId(n) AS node_id,\n"
            f"       [label] AS labels,\n"
            f"       label AS undeclared_label,\n"
            f"       '{check.id}' AS check_id"
        )

    def _undeclared_relationship_types(self, check: Check) -> str:
        rels_str = _cypher_list_literal(check.allowed_relationships)
        return (
            f"CALL db.relationshipTypes() YIELD relationshipType\n"
            f"WHERE NOT relationshipType IN {rels_str}\n"
            f"WITH relationshipType\n"
            f"MATCH ()-[r]->() WHERE type(r) = relationshipType\n"
            f"WITH r, relationshipType LIMIT 1\n"
            f"RETURN elementId(startNode(r)) AS node_id,\n"
            f"       labels(startNode(r)) AS labels,\n"
            f"       relationshipType AS undeclared_type,\n"
            f"       '{check.id}' AS check_id"
        )

    def _undeclared_properties(self, check: Check) -> str:
        props_str = _cypher_list_literal(check.allowed_properties)
        return (
            f"MATCH (n:{check.target_label})\n"
            f"WITH n, [k IN keys(n) WHERE NOT k IN {props_str}] AS extra\n"
            f"WHERE size(extra) > 0\n"
            f"UNWIND extra AS undeclared_key\n"
            f"RETURN elementId(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       undeclared_key AS undeclared_property,\n"
            f"       '{check.id}' AS check_id"
        )

    def _empty_shape(self, check: Check) -> str:
        return (
            f"OPTIONAL MATCH (n:{check.target_label})\n"
            f"WITH count(n) AS cnt\n"
            f"WHERE cnt = 0\n"
            f"RETURN 'none' AS node_id,\n"
            f"       ['{check.target_label}'] AS labels,\n"
            f"       0 AS instance_count,\n"
            f"       '{check.id}' AS check_id"
        )


# ── Helpers ──────────────────────────────────────────────────────

def _cypher_type_check(prop: str, expected_type: str) -> str:
    """Generate a Cypher expression that checks if a property is NOT the expected type."""
    type_map = {
        "string": "STRING",
        "integer": "INTEGER",
        "float": "FLOAT",
        "boolean": "BOOLEAN",
        "date": "DATE",
        "datetime": "DATETIME",
    }
    cypher_type = type_map.get(expected_type, expected_type.upper())
    return f"NOT valueType(n.{prop}) STARTS WITH '{cypher_type}'"


def _cypher_list_literal(values: list) -> str:
    """Convert a Python list to a Cypher list literal."""
    parts = []
    for v in values:
        if isinstance(v, str):
            escaped = v.replace("'", "\\'")
            parts.append(f"'{escaped}'")
        elif isinstance(v, bool):
            parts.append("true" if v else "false")
        elif isinstance(v, (int, float)):
            parts.append(str(v))
        else:
            parts.append(repr(v))
    return "[" + ", ".join(parts) + "]"
