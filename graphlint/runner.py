"""
graphlint.runner — Execute a validation plan against a graph database.

The runner takes a ValidationPlan and a Backend, compiles each check
into a query, and (optionally) executes them against a live database.

Can also run in dry-run mode, outputting the queries without executing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from graphlint.parser import ValidationPlan, Check, CheckType, Severity
from graphlint.backends import Backend


# ─── Report types ────────────────────────────────────────────────────

@dataclass
class ViolatingNode:
    node_id: str
    labels: list[str]
    extra: dict = field(default_factory=dict)


@dataclass
class CheckResult:
    check_id: str
    check_type: str
    severity: str
    message: str
    shape: Optional[str]
    target_label: str
    passed: bool
    vacuous: bool = False
    violating_nodes: list[ViolatingNode] = field(default_factory=list)
    query: str = ""

    @property
    def violation_count(self) -> int:
        return len(self.violating_nodes)


@dataclass
class ValidationReport:
    conforms: bool
    generated_at: str
    schema_source: str
    backend: str
    target: Optional[str]
    summary: dict
    results: list[CheckResult]

    def to_dict(self) -> dict:
        return {
            "conforms": self.conforms,
            "generated_at": self.generated_at,
            "schema_source": self.schema_source,
            "backend": self.backend,
            "target": self.target,
            "summary": self.summary,
            "results": [
                {
                    "check_id": r.check_id,
                    "check_type": r.check_type,
                    "severity": r.severity,
                    "message": r.message,
                    "shape": r.shape,
                    "target_label": r.target_label,
                    "passed": r.passed,
                    "vacuous": r.vacuous,
                    "violation_count": r.violation_count,
                    "violating_nodes": [
                        {"node_id": vn.node_id, "labels": vn.labels, **vn.extra}
                        for vn in r.violating_nodes
                    ],
                    "query": r.query,
                }
                for r in self.results
            ],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def print_table(self) -> str:
        """Format report as a human-readable table."""
        lines = []

        # Header
        lines.append(f"graphlint validation report")
        lines.append(f"  schema: {self.schema_source}")
        lines.append(f"  backend: {self.backend}")
        if self.target:
            lines.append(f"  target: {self.target}")
        lines.append(f"  generated: {self.generated_at}")
        lines.append("")

        # Summary
        s = self.summary
        status = "✓ CONFORMS" if self.conforms else "✗ DOES NOT CONFORM"
        lines.append(f"  {status}")
        vacuous = s.get('checks_vacuous', 0)
        vacuous_str = f"  {vacuous} skipped (no data)" if vacuous else ""
        lines.append(
            f"  {s['checks_passed']}/{s['checks_total']} checks passed  |  "
            f"{s['violations']} violations  {s['warnings']} warnings  {s['info']} info{vacuous_str}"
        )
        lines.append("")

        # Failed checks
        failed = [r for r in self.results if not r.passed]
        if failed:
            lines.append("  VIOLATIONS:")
            lines.append("")
            for r in failed:
                icon = {"violation": "✗", "warning": "⚠", "info": "ℹ"}.get(r.severity, "?")
                lines.append(f"  {icon} [{r.severity.upper()}] {r.check_id}")
                lines.append(f"    {r.message}")
                lines.append(f"    {r.violation_count} node(s) affected")
                for vn in r.violating_nodes[:5]:  # show first 5
                    extra_str = ""
                    if vn.extra:
                        extra_str = "  " + "  ".join(f"{k}={v}" for k, v in vn.extra.items())
                    lines.append(f"      → {vn.node_id} {vn.labels}{extra_str}")
                if r.violation_count > 5:
                    lines.append(f"      ... and {r.violation_count - 5} more")
                lines.append("")

        return "\n".join(lines)


# ─── Compile (dry-run) ───────────────────────────────────────────────

def compile_plan(
    plan: ValidationPlan,
    backend: Backend,
) -> list[tuple[Check, str]]:
    """Compile all checks into queries without executing them.

    Returns list of (Check, query_string) tuples.
    """
    results = []
    for check in plan.checks:
        query = backend.compile_check(check)
        results.append((check, query))
    return results


def dry_run(plan: ValidationPlan, backend: Backend) -> str:
    """Return all compiled queries as a formatted string."""
    compiled = compile_plan(plan, backend)
    lines = []
    for check, query in compiled:
        lines.append(f"-- [{check.severity.value.upper()}] {check.id}")
        lines.append(f"-- {check.message}")
        lines.append(query)
        lines.append("")
    return "\n".join(lines)


# ─── Execute against Neo4j ───────────────────────────────────────────

def execute_plan(
    plan: ValidationPlan,
    backend: Backend,
    driver,  # neo4j.Driver (not type-hinted to avoid hard dependency)
    database: Optional[str] = None,
    target_uri: Optional[str] = None,
) -> ValidationReport:
    """Execute all checks against a live Neo4j database and produce a report."""

    compiled = compile_plan(plan, backend)
    results: list[CheckResult] = []
    violations_total = 0
    warnings_total = 0
    info_total = 0
    passed_total = 0
    vacuous_total = 0

    # Pre-flight: count instances per declared label to detect vacuous checks
    declared_labels = {
        plan.mapping.label_for(iri) for iri in plan.shapes
    }
    empty_labels: set[str] = set()

    # Check types where "pass" is meaningless if no nodes have the property
    _PROPERTY_VACUOUS_TYPES = {
        CheckType.PROPERTY_TYPE,
        CheckType.PROPERTY_VALUE_IN,
        CheckType.PROPERTY_PATTERN,
        CheckType.PROPERTY_STRING_LENGTH,
        CheckType.PROPERTY_RANGE,
        CheckType.PROPERTY_PAIR,
    }

    # Collect (label, property) pairs that need property-level vacancy checks
    label_props: set[tuple[str, str]] = set()
    for check, _ in compiled:
        if check.type in _PROPERTY_VACUOUS_TYPES and check.property:
            label_props.add((check.target_label, check.property))

    empty_props: set[tuple[str, str]] = set()

    with driver.session(database=database) as session:
        for label in declared_labels:
            count_result = session.run(
                f"MATCH (n:{label}) RETURN count(n) AS cnt"
            ).single()
            if count_result and count_result["cnt"] == 0:
                empty_labels.add(label)

        # Check property-level vacancy (only for labels that have nodes)
        for label, prop in label_props:
            if label in empty_labels:
                continue
            prop_result = session.run(
                f"MATCH (n:{label}) WHERE n.{prop} IS NOT NULL "
                f"RETURN count(n) AS cnt"
            ).single()
            if prop_result and prop_result["cnt"] == 0:
                empty_props.add((label, prop))

        for check, query in compiled:
            # Determine if this check is vacuous
            is_vacuous = (
                check.target_label in empty_labels
                and check.type != CheckType.EMPTY_SHAPE
            ) or (
                check.type in _PROPERTY_VACUOUS_TYPES
                and check.property
                and (check.target_label, check.property) in empty_props
            )

            # Skip no-op checks
            if query.strip().startswith("//"):
                if is_vacuous:
                    vacuous_total += 1
                else:
                    passed_total += 1
                results.append(CheckResult(
                    check_id=check.id,
                    check_type=check.type.value,
                    severity=check.severity.value,
                    message=check.message,
                    shape=check.shape,
                    target_label=check.target_label,
                    passed=not is_vacuous,
                    vacuous=is_vacuous,
                    query=query,
                ))
                continue

            # Vacuous: skip execution, no nodes to check against
            if is_vacuous:
                vacuous_total += 1
                results.append(CheckResult(
                    check_id=check.id,
                    check_type=check.type.value,
                    severity=check.severity.value,
                    message=check.message,
                    shape=check.shape,
                    target_label=check.target_label,
                    passed=False,
                    vacuous=True,
                    query=query,
                ))
                continue

            try:
                records = session.run(query).data()
            except Exception as e:
                # Query failed — report as a result with error
                results.append(CheckResult(
                    check_id=check.id,
                    check_type=check.type.value,
                    severity=check.severity.value,
                    message=f"Query execution failed: {e}",
                    shape=check.shape,
                    target_label=check.target_label,
                    passed=False,
                    query=query,
                ))
                violations_total += 1
                continue

            violating_nodes = []
            for record in records:
                node_id = str(record.get("node_id", record.get("rel_id", "unknown")))
                labels_raw = record.get("labels", record.get("source_labels", []))
                labels = list(labels_raw) if labels_raw else []
                # Collect extra fields
                extra = {
                    k: v for k, v in record.items()
                    if k not in ("node_id", "rel_id", "labels", "check_id",
                                 "source_labels", "target_labels")
                }
                violating_nodes.append(ViolatingNode(
                    node_id=node_id,
                    labels=labels,
                    extra=extra,
                ))

            passed = len(violating_nodes) == 0
            if passed:
                passed_total += 1
            else:
                if check.severity == Severity.VIOLATION:
                    violations_total += 1
                elif check.severity == Severity.WARNING:
                    warnings_total += 1
                else:
                    info_total += 1

            results.append(CheckResult(
                check_id=check.id,
                check_type=check.type.value,
                severity=check.severity.value,
                message=check.message,
                shape=check.shape,
                target_label=check.target_label,
                passed=passed,
                violating_nodes=violating_nodes,
                query=query,
            ))

    conforms = violations_total == 0

    return ValidationReport(
        conforms=conforms,
        generated_at=datetime.now(timezone.utc).isoformat(),
        schema_source=plan.schema_source,
        backend=backend.name,
        target=target_uri,
        summary={
            "violations": violations_total,
            "warnings": warnings_total,
            "info": info_total,
            "checks_passed": passed_total,
            "checks_vacuous": vacuous_total,
            "checks_total": len(compiled),
        },
        results=results,
    )
