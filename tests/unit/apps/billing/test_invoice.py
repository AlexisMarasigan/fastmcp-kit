"""Unit tests for invoice reconstruction + DAG verification (spec §9.3, §15).

`reconstruct` is the auditable path: any invoice line must be
reconstructible from the event log alone (§1.5). The property test
replays randomized parallel/retry schedules with seeded stdlib `random`
(no hypothesis dep) and checks the §9.3 invariant; the fraud sims pin
the amortization economics and the multiplexing anomaly signal.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest
from ulid import ULID

from mcp_toolkit.apps.billing.invoice import (
    Invoice,
    InvoiceLine,
    count_end_users,
    reconstruct,
    verify_dag,
)
from mcp_toolkit.domains.metering.shared.schemas import RateClass, RateTable, UnitType, UsageEvent

_TS = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)

RATES = RateTable(
    rates={
        ("genesis", "calls"): 0.01,
        ("cold", "calls"): 0.001,
        ("warm", "calls"): 0.0003,
        ("rehydration", "calls"): 0.0008,
        ("cold", "tokens"): 0.00002,
        ("warm", "tokens"): 0.000007,
        ("state_rent", "gb_seconds"): 0.00005,
    }
)


def _event(
    *,
    tenant: str = "ten_acme",
    root: str,
    jti: str | None = None,
    parent: str | None,
    rate_class: RateClass = "cold",
    units: float = 1.0,
    unit_type: UnitType = "calls",
    event_id: str | None = None,
    conversation_key: str | None = None,
    end_user_id: str | None = None,
) -> UsageEvent:
    jti = jti if jti is not None else str(ULID())
    return UsageEvent(
        event_id=event_id if event_id is not None else f"sha256:{jti}",
        ts=_TS,
        tenant=tenant,
        root=root,
        jti=jti,
        parent=parent,
        conversation_key=conversation_key,
        end_user_id=end_user_id,
        tool=None if rate_class in ("genesis", "state_rent") else "search",
        rate_class=rate_class,
        units=units,
        unit_type=unit_type,
    )


def _genesis(
    root: str, *, tenant: str = "ten_acme", conversation_key: str | None = None
) -> UsageEvent:
    return _event(
        tenant=tenant,
        root=root,
        jti=root,
        parent=None,
        rate_class="genesis",
        units=1.0,
        conversation_key=conversation_key,
    )


# ------------------------------------------------------------ reconstruct


def test_reconstruct_groups_by_tenant_root_and_rate() -> None:
    root = str(ULID())
    events = [
        _genesis(root, conversation_key="thread_1"),
        _event(root=root, parent=root, rate_class="cold", units=2.0),
        _event(root=root, parent=root, rate_class="cold", units=3.0),
        _event(root=root, parent=root, rate_class="warm", units=4.0),
    ]
    invoices = reconstruct(events, RATES)

    assert set(invoices) == {"ten_acme"}
    invoice = invoices["ten_acme"]
    assert isinstance(invoice, Invoice)
    by_class = {(line.rate_class, line.unit_type): line for line in invoice.lines}
    cold = by_class[("cold", "calls")]
    assert isinstance(cold, InvoiceLine)
    assert cold.root == root
    assert cold.conversation_key == "thread_1"
    assert cold.units == pytest.approx(5.0)
    assert cold.price == pytest.approx(0.001)
    assert cold.amount == pytest.approx(0.005)
    warm = by_class[("warm", "calls")]
    assert warm.amount == pytest.approx(4.0 * 0.0003)
    genesis = by_class[("genesis", "calls")]
    assert genesis.amount == pytest.approx(0.01)
    assert invoice.total == pytest.approx(sum(line.amount for line in invoice.lines))


def test_reconstruct_separates_tenants_and_roots() -> None:
    root_a, root_b, root_c = str(ULID()), str(ULID()), str(ULID())
    events = [
        _genesis(root_a, tenant="ten_a"),
        _genesis(root_b, tenant="ten_a"),
        _genesis(root_c, tenant="ten_b"),
        _event(tenant="ten_a", root=root_a, parent=root_a, units=1.0),
        _event(tenant="ten_b", root=root_c, parent=root_c, units=7.0),
    ]
    invoices = reconstruct(events, RATES)
    assert set(invoices) == {"ten_a", "ten_b"}
    assert {line.root for line in invoices["ten_a"].lines} == {root_a, root_b}
    assert invoices["ten_b"].total == pytest.approx(0.01 + 7.0 * 0.001)


def test_reconstruct_unpriced_pairs_bill_zero_shadow_mode() -> None:
    root = str(ULID())
    events = [
        _genesis(root),
        _event(root=root, parent=root, rate_class="cold", units=9.0, unit_type="custom"),
    ]
    invoices = reconstruct(events, RATES)
    lines = {(line.rate_class, line.unit_type): line for line in invoices["ten_acme"].lines}
    assert lines[("cold", "custom")].price == 0.0
    assert lines[("cold", "custom")].amount == 0.0
    assert invoices["ten_acme"].total == pytest.approx(0.01)


def test_reconstruct_empty_events_returns_empty() -> None:
    assert reconstruct([], RATES) == {}


# ------------------------------------------------------------- verify_dag


def test_verify_dag_clean_chain_and_parallel_branches() -> None:
    root = str(ULID())
    a = _event(root=root, parent=root)
    b = _event(root=root, parent=a.jti)
    c = _event(root=root, parent=a.jti)  # parallel sibling: shared parent
    rent = _event(root=root, parent=root, rate_class="state_rent", unit_type="gb_seconds")
    assert verify_dag([_genesis(root), a, b, c, rent]) == []


def test_verify_dag_flags_missing_genesis() -> None:
    root = str(ULID())
    violations = verify_dag([_event(root=root, parent=root)])
    assert any("genesis" in v for v in violations)


def test_verify_dag_flags_multiple_geneses() -> None:
    root = str(ULID())
    second = _event(root=root, jti=str(ULID()), parent=None, rate_class="genesis")
    violations = verify_dag([_genesis(root), second])
    assert any("genesis" in v for v in violations)


def test_verify_dag_flags_genesis_jti_not_root() -> None:
    root = str(ULID())
    bad_genesis = _event(root=root, jti=str(ULID()), parent=None, rate_class="genesis")
    violations = verify_dag([bad_genesis])
    assert any(root in v for v in violations)


def test_verify_dag_flags_unknown_parent() -> None:
    root = str(ULID())
    orphan = _event(root=root, parent=str(ULID()))
    violations = verify_dag([_genesis(root), orphan])
    assert any(orphan.jti in v for v in violations)


def test_verify_dag_flags_parentless_non_genesis() -> None:
    root = str(ULID())
    floating = _event(root=root, parent=None)
    violations = verify_dag([_genesis(root), floating])
    assert any(floating.jti in v for v in violations)


def test_verify_dag_flags_cycle() -> None:
    root = str(ULID())
    jti_a, jti_b = str(ULID()), str(ULID())
    a = _event(root=root, jti=jti_a, parent=jti_b)
    b = _event(root=root, jti=jti_b, parent=jti_a)
    violations = verify_dag([_genesis(root), a, b])
    assert any("cycle" in v for v in violations)


def test_verify_dag_flags_mixed_roots_and_duplicate_jti() -> None:
    root = str(ULID())
    other_root = str(ULID())
    stray = _event(root=other_root, parent=root)
    dup = _event(root=root, jti=root, parent=root)  # collides with genesis jti
    violations = verify_dag([_genesis(root), stray, dup])
    assert any("root" in v for v in violations)
    assert any("duplicate" in v for v in violations)


def test_verify_dag_empty_events_is_a_violation() -> None:
    assert verify_dag([]) != []


# --------------------------------------------- property test (§9.3, §15)


def _random_schedule(seed: int) -> tuple[list[UsageEvent], str]:
    """One randomized genesis + parallel tool calls + retries schedule.

    Mirrors the kit's behavior: parents are sampled from *completed*
    jtis (parallel calls may share a parent), and transport retries
    reuse an existing event_id — the dedupe layer drops them before
    emission, so the schedule emits nothing for a retry.
    """
    # Seeded reproducible schedule, not crypto — plain `random` is the point.
    rng = random.Random(seed)  # noqa: S311
    tenant = f"ten_{seed}"
    root = str(ULID())
    events = [_genesis(root, tenant=tenant, conversation_key=f"thread_{seed}")]
    completed = [root]
    for _ in range(rng.randint(5, 40)):
        if events[1:] and rng.random() < 0.2:
            # Retry: reuses a prior event_id; the kit's SET NX dedupe
            # absorbs it pre-emission. No event reaches the log.
            _ = rng.choice(events[1:]).event_id
            continue
        if rng.random() < 0.1:
            events.append(
                _event(
                    tenant=tenant,
                    root=root,
                    parent=root,
                    rate_class="state_rent",
                    units=round(rng.uniform(0.5, 5000.0), 3),
                    unit_type="gb_seconds",
                )
            )
            continue
        rate_classes: list[RateClass] = ["cold", "warm", "rehydration"]
        unit_types: list[UnitType] = ["calls", "tokens"]
        rate_class = rng.choice(rate_classes)
        unit_type = "calls" if rate_class == "rehydration" else rng.choice(unit_types)
        call = _event(
            tenant=tenant,
            root=root,
            parent=rng.choice(completed),
            rate_class=rate_class,
            units=round(rng.uniform(0.1, 50.0), 3),
            unit_type=unit_type,
        )
        events.append(call)
        # Only some calls have "completed" (advanced the tip) by the
        # time the next call is admitted — that is the parallelism.
        if rng.random() < 0.6:
            completed.append(call.jti)
    return events, tenant


@pytest.mark.parametrize("seed", range(20))
def test_property_invariant_over_random_schedules(seed: int) -> None:
    """§9.3: invoice total == independent sum(units × rate); DAG sound."""
    events, tenant = _random_schedule(seed)

    assert verify_dag(events) == []

    expected = sum(e.units * RATES.price_for(e.rate_class, e.unit_type) for e in events)
    invoices = reconstruct(events, RATES)
    assert set(invoices) == {tenant}
    assert invoices[tenant].total == pytest.approx(expected)
    assert sum(line.units for line in invoices[tenant].lines) == pytest.approx(
        sum(e.units for e in events)
    )


# ------------------------------------------------------- fraud sims (§15)


def test_fraud_sim_amortizer_discount_never_reaches_zero() -> None:
    """Amortization economics (P1): warm-rate billing collapses the
    attack into a *bounded* caching discount.

    The amortizer preloads once (1 cold) then freerides 30 warm calls
    while paying rent on the held state. The honest equivalent pays 31
    cold calls with no rent. With warm > 0 the amortizer must pay
    strictly more than the hypothetical free-rider world where warm
    costs nothing — i.e. the discount never reaches 100%.
    """
    root = str(ULID())
    amortizer = [
        _genesis(root),
        _event(root=root, parent=root, rate_class="cold", units=1.0),
        *(_event(root=root, parent=root, rate_class="warm", units=1.0) for _ in range(30)),
        # 3 rent accruals of 1 GB held ~1 minute each — calibrated so the
        # comparison shows BOTH economics: a real (bounded) discount and
        # a floor the attacker can never go below.
        *(
            _event(
                root=root,
                parent=root,
                rate_class="state_rent",
                units=60.0,
                unit_type="gb_seconds",
            )
            for _ in range(3)
        ),
    ]
    honest_root = str(ULID())
    honest = [
        _genesis(honest_root),
        *(
            _event(root=honest_root, parent=honest_root, rate_class="cold", units=1.0)
            for _ in range(31)
        ),
    ]
    assert verify_dag(amortizer) == []
    assert verify_dag(honest) == []

    amortizer_total = reconstruct(amortizer, RATES)["ten_acme"].total
    honest_total = reconstruct(honest, RATES)["ten_acme"].total

    free_rider_rates = RateTable(rates={**dict(RATES.rates), ("warm", "calls"): 0.0})
    free_rider_total = reconstruct(amortizer, free_rider_rates)["ten_acme"].total

    # The discount exists (caching is legitimately cheaper than cold)…
    assert amortizer_total < honest_total
    # …but never reaches zero: 30 warm units at a positive rate always
    # cost more than the warm-is-free world the attacker hopes for.
    assert amortizer_total > free_rider_total


def test_fraud_sim_multiplexer_flagged_by_end_user_count() -> None:
    """Multiplexing (§11): 40 distinct end users on one root is an
    anomaly signal, not an enforcement gate — `count_end_users` feeds it.
    """
    threshold = 10
    root = str(ULID())
    events = [
        _genesis(root),
        *(
            _event(root=root, parent=root, rate_class="cold", end_user_id=f"u_anon_{i}")
            for i in range(40)
        ),
    ]
    assert count_end_users(events) == 40
    assert count_end_users(events) > threshold
    # Events without an end_user_id never inflate the count.
    assert count_end_users([_genesis(str(ULID()))]) == 0
