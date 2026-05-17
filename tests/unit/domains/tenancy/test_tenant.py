"""Unit tests for `Tenant` dataclass + access-layer semantics."""

from __future__ import annotations

from mcp_toolkit.domains.tenancy.shared import Tenant


def test_tenant_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    t = Tenant(tenant_id="acme")
    try:
        t.tenant_id = "other"  # type: ignore[misc]
    except FrozenInstanceError:
        return
    raise AssertionError("expected FrozenInstanceError")


def test_default_access_layers_empty() -> None:
    t = Tenant(tenant_id="acme")
    assert t.access_layers == frozenset()


def test_access_layers_carried_through() -> None:
    t = Tenant(
        tenant_id="acme",
        display_name="ACME",
        access_layers=frozenset({"weather", "billing"}),
    )
    assert "weather" in t.access_layers
    assert "billing" in t.access_layers
    assert "admin" not in t.access_layers
