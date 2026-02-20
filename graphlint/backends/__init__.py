"""
graphlint.backends.base — Backend protocol for query generation.

Each backend translates Check objects into executable queries
for a specific graph query language.
"""

from __future__ import annotations

from typing import Protocol, Optional
from graphlint.parser import Check, CheckType


class Backend(Protocol):
    """Interface that every query-language backend must implement."""

    name: str

    def compile_check(self, check: Check) -> str:
        """Compile a single Check into an executable query string.

        The query MUST return rows only for violations (nodes that
        fail the check). Zero rows = check passed.

        Each row MUST include at minimum:
            - node_id: the element ID of the violating node
            - labels: the labels on the violating node
        """
        ...
