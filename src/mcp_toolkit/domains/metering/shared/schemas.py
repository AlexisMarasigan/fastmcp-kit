"""Metering domain — shared schemas: usage events, units, rate table, config.

The usage event (spec §9.2) is the system of record for billing: every
accepted tool call, genesis, and state-rent accrual emits exactly one
`UsageEvent`, idempotent on `event_id`. Events flow to a sink (Redis
Stream by default) where the `apps/billing` consumer prices them with a
`RateTable`.

Rate table file format (JSON or YAML, loaded by `load_rate_table`):

    {
      "rates": [
        {"rate_class": "cold", "unit_type": "calls", "price": 0.001},
        {"rate_class": "genesis", "unit_type": "calls", "price": 0.01}
      ]
    }

Missing `(rate_class, unit_type)` entries price at 0.0 — shadow mode:
events are recorded and reconcilable, but bill nothing until priced.
YAML parsing needs the `[billing]` extra (pyyaml); JSON works in core.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, field_validator

from mcp_toolkit.shared.config import MeterSinkBackend
from mcp_toolkit.shared.errors import MeteringError, OptionalDependencyMissingError
from mcp_toolkit.shared.logging import get_logger

if TYPE_CHECKING:
    from mcp_toolkit.shared.config import Settings

_log = get_logger(__name__)

RateClass = Literal["genesis", "cold", "warm", "rehydration", "state_rent"]
UnitType = Literal["calls", "tokens", "gb_seconds", "custom"]

_RATE_CLASSES: frozenset[str] = frozenset(get_args(RateClass))
_UNIT_TYPES: frozenset[str] = frozenset(get_args(UnitType))

# Nullable string fields omitted from the flat stream encoding when None.
_OPTIONAL_STR_FIELDS = ("parent", "conversation_key", "end_user_id", "tool")


@dataclass(frozen=True)
class Units:
    """Return type of per-tool meter hooks (spec §10).

    A `meter=lambda result, ctx: Units(...)` hook on `@toolkit.tool`
    reports how much the call consumed and at which rate class.
    """

    amount: float
    unit_type: UnitType = "calls"
    rate_class: RateClass = "cold"


class UsageEvent(BaseModel):
    """Append-only usage record (spec §9.2). The source of truth for billing.

    `parent` is None for genesis; `tool` is None for genesis/state_rent.
    Sinks must be idempotent on `event_id` so stream redelivery is safe.
    """

    model_config = ConfigDict(frozen=True)

    v: int = 1
    event_id: str
    ts: datetime
    tenant: str
    root: str
    jti: str
    parent: str | None = None
    conversation_key: str | None = None
    end_user_id: str | None = None
    tool: str | None = None
    rate_class: RateClass
    units: float
    unit_type: UnitType
    inflight_at_admission: int | None = None
    metadata: dict[str, str] = {}

    @field_validator("ts")
    @classmethod
    def _normalize_utc(cls, value: datetime) -> datetime:
        """Pin timestamps to UTC; naive datetimes are assumed UTC."""
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def to_stream_fields(self) -> dict[str, str]:
        """Flatten to a string-only field map for Redis `XADD`.

        None-valued optionals are omitted (Redis stream fields cannot be
        null); `metadata` is JSON-encoded and omitted when empty.
        """
        fields: dict[str, str] = {
            "v": str(self.v),
            "event_id": self.event_id,
            "ts": self.ts.isoformat(),
            "tenant": self.tenant,
            "root": self.root,
            "jti": self.jti,
            "rate_class": self.rate_class,
            "units": str(self.units),
            "unit_type": self.unit_type,
        }
        for name in _OPTIONAL_STR_FIELDS:
            value: str | None = getattr(self, name)
            if value is not None:
                fields[name] = value
        if self.inflight_at_admission is not None:
            fields["inflight_at_admission"] = str(self.inflight_at_admission)
        if self.metadata:
            fields["metadata"] = json.dumps(self.metadata, sort_keys=True)
        return fields

    @classmethod
    def from_stream_fields(cls, fields: Mapping[str, str]) -> UsageEvent:
        """Inverse of `to_stream_fields`. Validates via the model schema."""
        data: dict[str, Any] = dict(fields)
        if "metadata" in data:
            data["metadata"] = json.loads(data["metadata"])
        return cls.model_validate(data)


class MeteringConfig(BaseModel):
    """Library-level metering config (spec §13). Wins over env when passed.

    Mirrors the `METER_*` environment variables; build from env with
    `MeteringConfig.from_settings(get_settings())`.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    sink: MeterSinkBackend = "redis_stream"
    dedupe_window: int = 300
    stream_key: str = "meter:events"
    jsonl_path: str = "meter-events.jsonl"
    rate_table: str = ""

    @classmethod
    def from_settings(cls, settings: Settings) -> MeteringConfig:
        """Build from the process-wide `Settings` (`METER_*` env vars)."""
        return cls(
            enabled=settings.meter_enabled,
            sink=settings.meter_sink,
            dedupe_window=settings.meter_dedupe_window,
            stream_key=settings.meter_stream_key,
            jsonl_path=settings.meter_jsonl_path,
            rate_table=settings.meter_rate_table,
        )


@dataclass(frozen=True)
class RateTable:
    """Prices per `(rate_class, unit_type)` pair.

    Missing entries price at 0.0 (shadow mode): unmatched events are
    recorded but bill nothing until the table prices them.
    """

    rates: Mapping[tuple[str, str], float] = field(default_factory=dict)

    def price_for(self, rate_class: RateClass, unit_type: UnitType) -> float:
        """Unit price for the pair; 0.0 when unpriced."""
        return self.rates.get((rate_class, unit_type), 0.0)


def _parse_rates(raw: object, *, source: str) -> dict[tuple[str, str], float]:
    """Validate the decoded file body into a rate mapping. Fail fast."""
    if not isinstance(raw, dict) or not isinstance(raw.get("rates"), list):
        raise MeteringError(f"rate table {source}: expected top-level {{'rates': [...]}}")
    rates: dict[tuple[str, str], float] = {}
    for index, entry in enumerate(raw["rates"]):
        if not isinstance(entry, dict):
            raise MeteringError(f"rate table {source}: rates[{index}] is not a mapping")
        rate_class = entry.get("rate_class")
        unit_type = entry.get("unit_type")
        price = entry.get("price")
        if rate_class not in _RATE_CLASSES:
            raise MeteringError(
                f"rate table {source}: rates[{index}].rate_class {rate_class!r} "
                f"not in {sorted(_RATE_CLASSES)}"
            )
        if unit_type not in _UNIT_TYPES:
            raise MeteringError(
                f"rate table {source}: rates[{index}].unit_type {unit_type!r} "
                f"not in {sorted(_UNIT_TYPES)}"
            )
        if isinstance(price, bool) or not isinstance(price, int | float):
            raise MeteringError(f"rate table {source}: rates[{index}].price must be a number")
        rates[(rate_class, unit_type)] = float(price)
    return rates


def load_rate_table(path: str | None) -> RateTable:
    """Load a rate table from a `.json` / `.yaml` / `.yml` file.

    An empty or None path returns an empty table (shadow mode). YAML
    requires pyyaml from the `[billing]` extra; JSON works in core.
    """
    if not path:
        return RateTable()
    file = Path(path)
    if not file.is_file():
        raise MeteringError(f"rate table not found: {path}")
    text = file.read_text(encoding="utf-8")
    suffix = file.suffix.lower()
    if suffix == ".json":
        raw: object = json.loads(text)
    elif suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as e:
            raise OptionalDependencyMissingError("pyyaml", "billing") from e
        raw = yaml.safe_load(text)
    else:
        raise MeteringError(f"rate table {path}: unsupported format {suffix!r} (use .json/.yaml)")
    rates = _parse_rates(raw, source=path)
    _log.info("rate_table_loaded", path=path, entries=len(rates))
    return RateTable(rates=rates)
