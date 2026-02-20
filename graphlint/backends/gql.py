"""
graphlint.backends.gql — Compile validation checks into GQL queries.

GQL (ISO/IEC 39075) is syntactically close to Cypher for basic pattern
matching. This backend produces GQL-compliant queries where possible,
falling back to annotations where GQL diverges from Cypher.

NOTE: GQL is still early in adoption. This backend tracks the ISO
standard as implementations mature. Currently, the main differences
from the Cypher backend are:
  - element_id() instead of elementId()
  - Slightly different function naming conventions
  - GQL uses VALUE TYPE instead of valueType() in some contexts
"""

from __future__ import annotations

from graphlint.parser import Check, CheckType


class GQLBackend:
    name = "gql"

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
            raise NotImplementedError(f"Check type {check.type} not implemented for GQL backend")
        return handler(check)

    def _property_exists(self, check: Check) -> str:
        return (
            f"MATCH (n:{check.target_label})\n"
            f"WHERE n.{check.property} IS NULL\n"
            f"RETURN element_id(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       '{check.id}' AS check_id"
        )

    def _property_type(self, check: Check) -> str:
        type_check = _gql_type_check(check.property, check.expected_type)

        if check.only_if_exists:
            where = f"WHERE n.{check.property} IS NOT NULL AND {type_check}"
        else:
            where = f"WHERE n.{check.property} IS NOT NULL AND {type_check}"

        return (
            f"MATCH (n:{check.target_label})\n"
            f"{where}\n"
            f"RETURN element_id(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       n.{check.property} AS actual_value,\n"
            f"       '{check.id}' AS check_id"
        )

    def _property_value_in(self, check: Check) -> str:
        values_str = _gql_list_literal(check.allowed_values)

        if check.only_if_exists:
            where = f"WHERE n.{check.property} IS NOT NULL AND NOT n.{check.property} IN {values_str}"
        else:
            where = f"WHERE NOT n.{check.property} IN {values_str}"

        return (
            f"MATCH (n:{check.target_label})\n"
            f"{where}\n"
            f"RETURN element_id(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       n.{check.property} AS actual_value,\n"
            f"       '{check.id}' AS check_id"
        )

    def _relationship_cardinality(self, check: Check) -> str:
        rel = check.relationship
        min_c = check.min_count if check.min_count is not None else 0
        max_c = check.max_count

        if rel.direction == "outgoing":
            pattern = f"(n)-[r:{rel.type}]->(t:{rel.target_label})"
        else:
            pattern = f"(n)<-[r:{rel.type}]-(t:{rel.target_label})"

        conditions = []
        if min_c > 0:
            conditions.append(f"rel_count < {min_c}")
        if max_c is not None:
            conditions.append(f"rel_count > {max_c}")

        if not conditions:
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
            f"RETURN element_id(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       rel_count AS actual_count,\n"
            f"       '{check.id}' AS check_id"
        )

    def _relationship_endpoint(self, check: Check) -> str:
        return (
            f"MATCH (s)-[r:{check.relationship.type}]->(t)\n"
            f"WHERE NOT (s:{check.target_label} AND t:{check.relationship.target_label})\n"
            f"RETURN element_id(r) AS rel_id,\n"
            f"       type(r) AS rel_type,\n"
            f"       labels(s) AS source_labels,\n"
            f"       labels(t) AS target_labels,\n"
            f"       '{check.id}' AS check_id"
        )

    # ── Strict mode checks ────────────────────────────────────────

    def _undeclared_labels(self, check: Check) -> str:
        labels_str = _gql_list_literal(check.allowed_values)
        return (
            f"CALL db.labels() YIELD label\n"
            f"WHERE NOT label IN {labels_str}\n"
            f"WITH label\n"
            f"MATCH (n) WHERE label IN labels(n)\n"
            f"WITH n, label LIMIT 1\n"
            f"RETURN element_id(n) AS node_id,\n"
            f"       [label] AS labels,\n"
            f"       label AS undeclared_label,\n"
            f"       '{check.id}' AS check_id"
        )

    def _undeclared_relationship_types(self, check: Check) -> str:
        rels_str = _gql_list_literal(check.allowed_relationships)
        return (
            f"CALL db.relationshipTypes() YIELD relationshipType\n"
            f"WHERE NOT relationshipType IN {rels_str}\n"
            f"WITH relationshipType\n"
            f"MATCH ()-[r]->() WHERE type(r) = relationshipType\n"
            f"WITH r, relationshipType LIMIT 1\n"
            f"RETURN element_id(startNode(r)) AS node_id,\n"
            f"       labels(startNode(r)) AS labels,\n"
            f"       relationshipType AS undeclared_type,\n"
            f"       '{check.id}' AS check_id"
        )

    def _undeclared_properties(self, check: Check) -> str:
        props_str = _gql_list_literal(check.allowed_properties)
        return (
            f"MATCH (n:{check.target_label})\n"
            f"WITH n, [k IN keys(n) WHERE NOT k IN {props_str}] AS extra\n"
            f"WHERE size(extra) > 0\n"
            f"UNWIND extra AS undeclared_key\n"
            f"RETURN element_id(n) AS node_id,\n"
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


def _gql_type_check(prop: str, expected_type: str) -> str:
    type_map = {
        "string": "STRING",
        "integer": "INTEGER",
        "float": "FLOAT",
        "boolean": "BOOLEAN",
        "date": "DATE",
        "datetime": "TIMESTAMP",
    }
    gql_type = type_map.get(expected_type, expected_type.upper())
    # GQL uses value_type() function
    return f"NOT value_type(n.{prop}) STARTS WITH '{gql_type}'"


def _gql_list_literal(values: list) -> str:
    parts = []
    for v in values:
        if isinstance(v, str):
            escaped = v.replace("'", "\\'")
            parts.append(f"'{escaped}'")
        elif isinstance(v, bool):
            parts.append("TRUE" if v else "FALSE")
        elif isinstance(v, (int, float)):
            parts.append(str(v))
        else:
            parts.append(repr(v))
    return "[" + ", ".join(parts) + "]"
