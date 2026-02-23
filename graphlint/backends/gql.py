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
            CheckType.PROPERTY_PATTERN: self._property_pattern,
            CheckType.PROPERTY_STRING_LENGTH: self._property_string_length,
            CheckType.PROPERTY_RANGE: self._property_range,
            CheckType.PROPERTY_PAIR: self._property_pair,
            CheckType.RELATIONSHIP_CARDINALITY: self._relationship_cardinality,
            CheckType.RELATIONSHIP_ENDPOINT: self._relationship_endpoint,
            CheckType.UNDECLARED_LABELS: self._undeclared_labels,
            CheckType.UNDECLARED_RELATIONSHIP_TYPES: self._undeclared_relationship_types,
            CheckType.UNDECLARED_PROPERTIES: self._undeclared_properties,
            CheckType.EMPTY_SHAPE: self._empty_shape,
            CheckType.QUALIFIED_CARDINALITY: self._qualified_cardinality,
            CheckType.LOGICAL_NOT: self._logical_not,
            CheckType.LOGICAL_AND: self._logical_and,
            CheckType.LOGICAL_OR: self._logical_or,
            CheckType.LOGICAL_XONE: self._logical_xone,
            CheckType.UNIQUE_LANG: self._unique_lang,
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

    def _property_pattern(self, check: Check) -> str:
        pattern = check.pattern.replace("'", "\\'")
        if check.pattern_flags and "i" in check.pattern_flags:
            regex = f"(?i){pattern}"
        else:
            regex = pattern

        return (
            f"MATCH (n:{check.target_label})\n"
            f"WHERE n.{check.property} IS NOT NULL AND NOT n.{check.property} =~ '{regex}'\n"
            f"RETURN element_id(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       n.{check.property} AS actual_value,\n"
            f"       '{check.id}' AS check_id"
        )

    def _property_string_length(self, check: Check) -> str:
        conditions = []
        if check.min_length is not None:
            conditions.append(f"size(n.{check.property}) < {check.min_length}")
        if check.max_length is not None:
            conditions.append(f"size(n.{check.property}) > {check.max_length}")

        where_clause = " OR ".join(conditions)

        return (
            f"MATCH (n:{check.target_label})\n"
            f"WHERE n.{check.property} IS NOT NULL AND ({where_clause})\n"
            f"RETURN element_id(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       n.{check.property} AS actual_value,\n"
            f"       size(n.{check.property}) AS actual_length,\n"
            f"       '{check.id}' AS check_id"
        )

    def _property_range(self, check: Check) -> str:
        conditions = []
        if check.min_inclusive is not None:
            conditions.append(f"n.{check.property} < {check.min_inclusive}")
        if check.max_inclusive is not None:
            conditions.append(f"n.{check.property} > {check.max_inclusive}")
        if check.min_exclusive is not None:
            conditions.append(f"n.{check.property} <= {check.min_exclusive}")
        if check.max_exclusive is not None:
            conditions.append(f"n.{check.property} >= {check.max_exclusive}")

        where_clause = " OR ".join(conditions)

        return (
            f"MATCH (n:{check.target_label})\n"
            f"WHERE n.{check.property} IS NOT NULL AND ({where_clause})\n"
            f"RETURN element_id(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       n.{check.property} AS actual_value,\n"
            f"       '{check.id}' AS check_id"
        )

    def _property_pair(self, check: Check) -> str:
        prop1 = check.property
        prop2 = check.compare_property
        comp = check.comparison_type

        op_map = {
            "equals": f"n.{prop1} <> n.{prop2}",
            "disjoint": f"n.{prop1} = n.{prop2}",
            "lessThan": f"NOT (n.{prop1} < n.{prop2})",
            "lessThanOrEquals": f"NOT (n.{prop1} <= n.{prop2})",
        }
        condition = op_map[comp]

        return (
            f"MATCH (n:{check.target_label})\n"
            f"WHERE n.{prop1} IS NOT NULL AND n.{prop2} IS NOT NULL\n"
            f"  AND {condition}\n"
            f"RETURN element_id(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       n.{prop1} AS value1,\n"
            f"       n.{prop2} AS value2,\n"
            f"       '{check.id}' AS check_id"
        )

    def _relationship_cardinality(self, check: Check) -> str:
        rel = check.relationship
        min_c = check.min_count if check.min_count is not None else 0
        max_c = check.max_count

        if check.acceptable_labels:
            label_list = _gql_list_literal(check.acceptable_labels)
            if rel.direction == "outgoing":
                pattern = f"(n)-[r:{rel.type}]->(t)"
                target_filter = f"WHERE any(lbl IN labels(t) WHERE lbl IN {label_list})"
            else:
                pattern = f"(n)<-[r:{rel.type}]-(t)"
                target_filter = f"WHERE any(lbl IN labels(t) WHERE lbl IN {label_list})"
        else:
            if rel.direction == "outgoing":
                pattern = f"(n)-[r:{rel.type}]->(t:{rel.target_label})"
            else:
                pattern = f"(n)<-[r:{rel.type}]-(t:{rel.target_label})"
            target_filter = None

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

        if target_filter:
            return (
                f"MATCH (n:{check.target_label})\n"
                f"OPTIONAL MATCH {pattern}\n"
                f"{target_filter}\n"
                f"WITH n, count(r) AS rel_count\n"
                f"WHERE {where}\n"
                f"RETURN element_id(n) AS node_id,\n"
                f"       labels(n) AS labels,\n"
                f"       rel_count AS actual_count,\n"
                f"       '{check.id}' AS check_id"
            )

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

    # ── Qualified cardinality ────────────────────────────────────

    def _qualified_cardinality(self, check: Check) -> str:
        qf = check.qualified_filter
        filter_cond = self._compile_condition(qf, "n")

        conditions = []
        if check.qualified_min is not None:
            conditions.append(f"qcount < {check.qualified_min}")
        if check.qualified_max is not None:
            conditions.append(f"qcount > {check.qualified_max}")

        if not conditions:
            return (
                f"// Check {check.id}: qualified cardinality with no bounds\n"
                f"// This check always passes — skipped"
            )

        where = " OR ".join(conditions)

        return (
            f"MATCH (n:{check.target_label})\n"
            f"WITH n, size([x IN CASE WHEN n.{check.property} IS NOT NULL THEN\n"
            f"  CASE WHEN {filter_cond} THEN [1] ELSE [] END\n"
            f"  ELSE [] END | x]) AS qcount\n"
            f"WHERE {where}\n"
            f"RETURN element_id(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       qcount AS qualified_count,\n"
            f"       '{check.id}' AS check_id"
        )

    # ── Logical constraints ──────────────────────────────────────

    def _logical_not(self, check: Check) -> str:
        if not check.sub_checks:
            return f"// Check {check.id}: sh:not with no inner checks — skipped"

        inner = check.sub_checks[0]
        cond = self._compile_condition(inner, "n")

        return (
            f"MATCH (n:{check.target_label})\n"
            f"WHERE {cond}\n"
            f"RETURN element_id(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       '{check.id}' AS check_id"
        )

    def _logical_and(self, check: Check) -> str:
        if not check.sub_checks:
            return f"// Check {check.id}: sh:and with no inner checks — skipped"

        conditions = []
        for sc in check.sub_checks:
            cond = self._compile_condition(sc, "n")
            conditions.append(f"NOT ({cond})")

        where = " OR ".join(conditions)

        return (
            f"MATCH (n:{check.target_label})\n"
            f"WHERE {where}\n"
            f"RETURN element_id(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       '{check.id}' AS check_id"
        )

    def _logical_or(self, check: Check) -> str:
        if not check.sub_checks:
            return f"// Check {check.id}: sh:or with no inner checks — skipped"

        conditions = []
        for sc in check.sub_checks:
            cond = self._compile_condition(sc, "n")
            conditions.append(f"NOT ({cond})")

        where = " AND ".join(conditions)

        return (
            f"MATCH (n:{check.target_label})\n"
            f"WHERE {where}\n"
            f"RETURN element_id(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       '{check.id}' AS check_id"
        )

    def _logical_xone(self, check: Check) -> str:
        if not check.sub_checks:
            return f"// Check {check.id}: sh:xone with no inner checks — skipped"

        case_parts = []
        for sc in check.sub_checks:
            cond = self._compile_condition(sc, "n")
            case_parts.append(f"CASE WHEN {cond} THEN 1 ELSE 0 END")

        sum_expr = " + ".join(case_parts)

        return (
            f"MATCH (n:{check.target_label})\n"
            f"WITH n, ({sum_expr}) AS satisfied_count\n"
            f"WHERE satisfied_count <> 1\n"
            f"RETURN element_id(n) AS node_id,\n"
            f"       labels(n) AS labels,\n"
            f"       satisfied_count,\n"
            f"       '{check.id}' AS check_id"
        )

    def _compile_condition(self, check: Check, node_var: str) -> str:
        """Compile a Check into a WHERE-clause fragment."""
        if check.type == CheckType.PROPERTY_EXISTS:
            return f"{node_var}.{check.property} IS NOT NULL"
        elif check.type == CheckType.PROPERTY_TYPE:
            gql_type = _gql_type_check(check.property, check.expected_type)
            return f"{node_var}.{check.property} IS NOT NULL AND NOT ({gql_type})"
        elif check.type == CheckType.PROPERTY_VALUE_IN:
            values_str = _gql_list_literal(check.allowed_values)
            return f"{node_var}.{check.property} IN {values_str}"
        elif check.type == CheckType.PROPERTY_PATTERN:
            pattern = check.pattern.replace("'", "\\'")
            if check.pattern_flags and "i" in check.pattern_flags:
                regex = f"(?i){pattern}"
            else:
                regex = pattern
            return f"{node_var}.{check.property} =~ '{regex}'"
        elif check.type == CheckType.PROPERTY_RANGE:
            conds = []
            if check.min_inclusive is not None:
                conds.append(f"{node_var}.{check.property} >= {check.min_inclusive}")
            if check.max_inclusive is not None:
                conds.append(f"{node_var}.{check.property} <= {check.max_inclusive}")
            if check.min_exclusive is not None:
                conds.append(f"{node_var}.{check.property} > {check.min_exclusive}")
            if check.max_exclusive is not None:
                conds.append(f"{node_var}.{check.property} < {check.max_exclusive}")
            return " AND ".join(conds) if conds else "TRUE"
        else:
            return "TRUE"

    # ── Unique language ──────────────────────────────────────────

    def _unique_lang(self, check: Check) -> str:
        return (
            f"// Check {check.id}: sh:uniqueLang not applicable to LPG\n"
            f"// Neo4j properties have no language tags — constraint acknowledged"
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
