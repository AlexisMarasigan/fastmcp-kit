"""Unit tests for the metering domain's shared schemas.

Covers spec §2 (rate classes), §9.2 (usage event schema + stream round-trip),
§13 (MeteringConfig env mirroring), and the rate-table loader used by the
`apps/billing` consumer (shadow-mode fallback to 0.0 prices).
"""

from __future__ import annotations

import builtins
import dataclasses
import json
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from ulid import ULID

from mcp_toolkit.domains.metering.shared.schemas import (
    MeteringConfig,
    RateTable,
    Units,
    UsageEvent,
    load_rate_table,
)
from mcp_toolkit.shared.config import Settings
from mcp_toolkit.shared.errors import MeteringError, OptionalDependencyMissingError


def _event(**overrides: object) -> UsageEvent:
    """A fully-populated valid event; override fields per test."""
    base: dict[str, object] = {
        "event_id": "sha256:" + "ab" * 32,
        "ts": datetime(2026, 6, 9, 12, 0, 0, 123000, tzinfo=UTC),
        "tenant": "ten_acme",
        "root": str(ULID()),
        "jti": str(ULID()),
        "parent": str(ULID()),
        "conversation_key": "thread_8f3a",
        "end_user_id": "u_anon_42",
        "tool": "get_weather",
        "rate_class": "cold",
        "units": 12.5,
        "unit_type": "calls",
        "inflight_at_admission": 3,
        "metadata": {"env": "prod"},
    }
    base.update(overrides)
    return UsageEvent.model_validate(base)


# ---------------------------------------------------------------- Units


def test_units_defaults_and_frozen() -> None:
    units = Units(amount=1.0)
    assert units.unit_type == "calls"
    assert units.rate_class == "cold"
    with pytest.raises(dataclasses.FrozenInstanceError):
        units.amount = 2.0  # type: ignore[misc]


# ----------------------------------------------------------- UsageEvent


def test_usage_event_stream_fields_are_flat_strings() -> None:
    fields = _event().to_stream_fields()
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in fields.items())


def test_usage_event_stream_round_trip() -> None:
    event = _event()
    assert UsageEvent.from_stream_fields(event.to_stream_fields()) == event


def test_usage_event_json_round_trip() -> None:
    event = _event()
    assert UsageEvent.model_validate_json(event.model_dump_json()) == event


def test_genesis_event_parent_none_round_trips() -> None:
    genesis = _event(
        parent=None,
        tool=None,
        rate_class="genesis",
        units=1.0,
        inflight_at_admission=None,
        metadata={},
    )
    assert genesis.v == 1
    fields = genesis.to_stream_fields()
    # None-valued optionals are omitted from the flat stream encoding.
    for absent in ("parent", "tool", "inflight_at_admission", "metadata"):
        assert absent not in fields
    restored = UsageEvent.from_stream_fields(fields)
    assert restored == genesis
    assert restored.parent is None
    assert restored.metadata == {}


def test_usage_event_ts_normalized_to_utc() -> None:
    naive = _event(ts=datetime(2026, 6, 9, 12, 0, 0))
    assert naive.ts.tzinfo == UTC

    plus_two = timezone(timedelta(hours=2))
    aware = _event(ts=datetime(2026, 6, 9, 14, 0, 0, tzinfo=plus_two))
    assert aware.ts == datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


def test_usage_event_rejects_unknown_rate_class() -> None:
    with pytest.raises(ValidationError):
        _event(rate_class="free_lunch")


def test_usage_event_is_frozen() -> None:
    event = _event()
    with pytest.raises(ValidationError):
        event.units = 999.0


# ------------------------------------------------------- MeteringConfig


def test_metering_config_defaults() -> None:
    config = MeteringConfig()
    assert config.enabled is False
    assert config.sink == "redis_stream"
    assert config.dedupe_window == 300
    assert config.stream_key == "meter:events"
    assert config.jsonl_path == "meter-events.jsonl"
    assert config.rate_table == ""


def test_metering_config_from_settings() -> None:
    settings = Settings(
        meter_enabled=True,
        meter_sink="jsonl",
        meter_dedupe_window=60,
        meter_stream_key="meter:test",
        meter_jsonl_path="var/events.jsonl",
        meter_rate_table="rates.json",
    )
    config = MeteringConfig.from_settings(settings)
    assert config.enabled is True
    assert config.sink == "jsonl"
    assert config.dedupe_window == 60
    assert config.stream_key == "meter:test"
    assert config.jsonl_path == "var/events.jsonl"
    assert config.rate_table == "rates.json"


def test_metering_config_is_frozen() -> None:
    config = MeteringConfig()
    with pytest.raises(ValidationError):
        config.enabled = True


# ------------------------------------------------------------ RateTable


def test_rate_table_price_for_known_and_fallback() -> None:
    table = RateTable(rates={("cold", "calls"): 0.001})
    assert table.price_for("cold", "calls") == 0.001
    # Missing entry → 0.0: shadow mode, never a billing surprise.
    assert table.price_for("warm", "tokens") == 0.0


def test_load_rate_table_empty_or_none_path() -> None:
    for path in (None, ""):
        table = load_rate_table(path)
        assert table.price_for("genesis", "calls") == 0.0


def test_load_rate_table_json(tmp_path: Path) -> None:
    payload = {
        "rates": [
            {"rate_class": "cold", "unit_type": "calls", "price": 0.001},
            {"rate_class": "genesis", "unit_type": "calls", "price": 0.01},
        ]
    }
    file = tmp_path / "rates.json"
    file.write_text(json.dumps(payload), encoding="utf-8")
    table = load_rate_table(str(file))
    assert table.price_for("cold", "calls") == 0.001
    assert table.price_for("genesis", "calls") == 0.01
    assert table.price_for("state_rent", "gb_seconds") == 0.0


def test_load_rate_table_yaml(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    file = tmp_path / "rates.yaml"
    file.write_text(
        "rates:\n"
        "  - {rate_class: warm, unit_type: calls, price: 0.0005}\n"
        "  - {rate_class: state_rent, unit_type: gb_seconds, price: 0.00002}\n",
        encoding="utf-8",
    )
    table = load_rate_table(str(file))
    assert table.price_for("warm", "calls") == 0.0005
    assert table.price_for("state_rent", "gb_seconds") == 0.00002


def test_load_rate_table_yaml_without_pyyaml_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "rates.yaml"
    file.write_text("rates: []\n", encoding="utf-8")

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "yaml" or name.startswith("yaml."):
            raise ImportError("simulated missing pyyaml")
        # Pass-through is correct at runtime but unprovable to mypy.
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    sys.modules.pop("yaml", None)

    with pytest.raises(OptionalDependencyMissingError) as exc:
        load_rate_table(str(file))
    msg = str(exc.value)
    assert "pyyaml" in msg
    assert "[billing]" in msg


def test_load_rate_table_missing_file() -> None:
    with pytest.raises(MeteringError):
        load_rate_table("/nonexistent/rates.json")


def test_load_rate_table_unsupported_extension(tmp_path: Path) -> None:
    file = tmp_path / "rates.toml"
    file.write_text("", encoding="utf-8")
    with pytest.raises(MeteringError):
        load_rate_table(str(file))


def test_load_rate_table_rejects_unknown_rate_class(tmp_path: Path) -> None:
    file = tmp_path / "rates.json"
    file.write_text(
        json.dumps({"rates": [{"rate_class": "nope", "unit_type": "calls", "price": 1.0}]}),
        encoding="utf-8",
    )
    with pytest.raises(MeteringError):
        load_rate_table(str(file))
