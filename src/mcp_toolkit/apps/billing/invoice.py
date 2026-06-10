"""Invoice reconstruction + DAG verification over the usage-event log.

The event log is the system of record (P5): `reconstruct` is the
auditable path — any invoice line is reconstructible from the events
alone (§1.5), with prices applied from a `RateTable`. `verify_dag`
checks the §9.3 invariant for one root's events: a single-rooted DAG
whose sole parentless node is the genesis event (`jti == root`).

Everything here is pure: no I/O, no clock, no randomness — the same
events and rates always produce the same invoice, which is what makes
disputes resolvable from the log.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mcp_toolkit.shared.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mcp_toolkit.domains.metering.shared.schemas import (
        RateClass,
        RateTable,
        UnitType,
        UsageEvent,
    )

_log = get_logger(__name__)

_GENESIS = "genesis"


@dataclass(frozen=True)
class InvoiceLine:
    """One priced `(root, rate_class, unit_type)` aggregate."""

    root: str
    conversation_key: str | None
    rate_class: str
    unit_type: str
    units: float
    price: float
    amount: float


@dataclass(frozen=True)
class Invoice:
    """All of one tenant's lines; `total` is the sum of line amounts."""

    tenant: str
    lines: tuple[InvoiceLine, ...]
    total: float


def reconstruct(events: Iterable[UsageEvent], rates: RateTable) -> dict[str, Invoice]:
    """Rebuild per-tenant invoices from the event log alone (§1.5, P5).

    Groups `tenant → root → (rate_class, unit_type)`, sums units, and
    prices each line as `units × price`. The conversation key is a
    display label only (P4): the first non-None key seen for a root
    labels its lines; it never changes the billed sum.
    """
    # tenant → root → (rate_class, unit_type) → summed units
    sums: dict[str, dict[str, dict[tuple[RateClass, UnitType], float]]] = {}
    key_labels: dict[str, str] = {}
    for event in events:
        per_root = sums.setdefault(event.tenant, {}).setdefault(event.root, {})
        pair = (event.rate_class, event.unit_type)
        per_root[pair] = per_root.get(pair, 0.0) + event.units
        if event.conversation_key is not None and event.root not in key_labels:
            key_labels[event.root] = event.conversation_key

    invoices: dict[str, Invoice] = {}
    for tenant, per_tenant in sums.items():
        lines: list[InvoiceLine] = []
        for root in sorted(per_tenant):
            for (rate_class, unit_type), units in sorted(per_tenant[root].items()):
                price = rates.price_for(rate_class, unit_type)
                lines.append(
                    InvoiceLine(
                        root=root,
                        conversation_key=key_labels.get(root),
                        rate_class=rate_class,
                        unit_type=unit_type,
                        units=units,
                        price=price,
                        amount=units * price,
                    )
                )
        invoices[tenant] = Invoice(
            tenant=tenant,
            lines=tuple(lines),
            total=sum(line.amount for line in lines),
        )
    return invoices


def count_end_users(events: Iterable[UsageEvent]) -> int:
    """Distinct non-None `end_user_id`s — the multiplexing anomaly signal.

    Exact counterpart of the per-root HLL (§11); alerting compares this
    against a threshold, it is never an enforcement gate (decision 2).
    """
    return len({event.end_user_id for event in events if event.end_user_id is not None})


def verify_dag(events_for_one_root: Iterable[UsageEvent]) -> list[str]:
    """Check the §9.3 invariant for one root's events. [] when sound.

    Violations reported:
    - empty event set / more than one distinct `root` field
    - not exactly one `rate_class=genesis` event
    - genesis `jti` != `root` (the genesis jti IS the root, §5.1)
    - duplicate `jti`s
    - a non-genesis event with no parent, or a parent that is neither a
      known jti nor the root
    - cycles in the parent edges
    """
    events = list(events_for_one_root)
    violations: list[str] = []
    if not events:
        return ["no events: a root must have at least its genesis event"]

    roots = {event.root for event in events}
    if len(roots) > 1:
        violations.append(f"mixed root fields in one event set: {sorted(roots)}")
    root = events[0].root

    jtis: set[str] = set()
    for event in events:
        if event.jti in jtis:
            violations.append(f"duplicate jti {event.jti}")
        jtis.add(event.jti)

    geneses = [event for event in events if event.rate_class == _GENESIS]
    if len(geneses) != 1:
        violations.append(f"expected exactly one genesis event, found {len(geneses)}")
    for genesis in geneses:
        if genesis.jti != root:
            violations.append(f"genesis jti {genesis.jti} != root {root}")
        if genesis.parent is not None:
            violations.append(f"genesis {genesis.jti} must have no parent")

    parent_of: dict[str, str | None] = {}
    for event in events:
        if event.rate_class == _GENESIS:
            continue
        if event.parent is None:
            violations.append(f"non-genesis event {event.jti} has no parent")
        elif event.parent not in jtis and event.parent != root:
            violations.append(f"event {event.jti} has unknown parent {event.parent}")
        parent_of[event.jti] = event.parent

    violations.extend(_find_cycles(parent_of))
    return violations


def _find_cycles(parent_of: dict[str, str | None]) -> list[str]:
    """Detect cycles in the parent edges (each node has at most one parent).

    Chase every chain; a node revisited within its own chase is a cycle.
    Chains end at None, at an unknown parent (flagged separately by the
    caller), or at an already-cleared node.
    """
    violations: list[str] = []
    cleared: set[str] = set()
    for start in parent_of:
        if start in cleared:
            continue
        on_path: set[str] = set()
        node: str | None = start
        while node is not None and node not in cleared:
            if node in on_path:
                violations.append(f"cycle detected through jti {node}")
                break
            on_path.add(node)
            node = parent_of.get(node)
        cleared.update(on_path)
    return violations
