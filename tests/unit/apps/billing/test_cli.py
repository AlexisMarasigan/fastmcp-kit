"""Unit tests for the `mcp-toolkit-billing` CLI.

`invoice` and `verify` run end-to-end through `main([...])` against tmp
JSONL/rate-table files; `consume` is covered at the argument-parsing and
config-guard level only (the loop itself is `BillingConsumer`'s suite).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ulid import ULID

from mcp_toolkit.apps.billing.cli import build_parser, cmd_consume, main
from mcp_toolkit.domains.metering.shared.schemas import UsageEvent

_RATES_JSON = json.dumps(
    {
        "rates": [
            {"rate_class": "genesis", "unit_type": "calls", "price": 0.01},
            {"rate_class": "cold", "unit_type": "calls", "price": 0.001},
        ]
    }
)


def _event(
    *,
    root: str,
    jti: str | None = None,
    parent: str | None,
    rate_class: str = "cold",
    units: float = 1.0,
) -> UsageEvent:
    jti = jti if jti is not None else str(ULID())
    return UsageEvent.model_validate(
        {
            "event_id": f"sha256:{jti}",
            "ts": datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC),
            "tenant": "ten_acme",
            "root": root,
            "jti": jti,
            "parent": parent,
            "rate_class": rate_class,
            "units": units,
            "unit_type": "calls",
        }
    )


def _write_events(path: Path, events: list[UsageEvent]) -> None:
    path.write_text("".join(e.model_dump_json() + "\n" for e in events), encoding="utf-8")


def _clean_root_events() -> list[UsageEvent]:
    root = str(ULID())
    genesis = _event(root=root, jti=root, parent=None, rate_class="genesis")
    return [genesis, _event(root=root, parent=root, units=3.0)]


# ---------------------------------------------------------------- invoice


def test_invoice_prints_invoice_json_per_tenant(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    events_file = tmp_path / "events.jsonl"
    rates_file = tmp_path / "rates.json"
    _write_events(events_file, _clean_root_events())
    rates_file.write_text(_RATES_JSON, encoding="utf-8")

    rc = main(["invoice", "--events", str(events_file), "--rates", str(rates_file)])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"ten_acme"}
    invoice = payload["ten_acme"]
    assert invoice["tenant"] == "ten_acme"
    assert invoice["total"] == pytest.approx(0.01 + 3.0 * 0.001)
    assert {line["rate_class"] for line in invoice["lines"]} == {"genesis", "cold"}


def test_invoice_missing_events_file_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rates_file = tmp_path / "rates.json"
    rates_file.write_text(_RATES_JSON, encoding="utf-8")

    rc = main(["invoice", "--events", str(tmp_path / "absent.jsonl"), "--rates", str(rates_file)])

    assert rc == 1
    assert "absent.jsonl" in capsys.readouterr().err


def test_invoice_malformed_event_line_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    events_file = tmp_path / "events.jsonl"
    rates_file = tmp_path / "rates.json"
    events_file.write_text('{"not": "an event"}\n', encoding="utf-8")
    rates_file.write_text(_RATES_JSON, encoding="utf-8")

    rc = main(["invoice", "--events", str(events_file), "--rates", str(rates_file)])

    assert rc == 1
    assert "line 1" in capsys.readouterr().err


# ----------------------------------------------------------------- verify


def test_verify_clean_log_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    events_file = tmp_path / "events.jsonl"
    _write_events(events_file, _clean_root_events())

    rc = main(["verify", "--events", str(events_file)])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"roots": 1, "violations": {}}


def test_verify_violations_exit_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = str(ULID())
    orphan = _event(root=root, parent=str(ULID()))  # no genesis, unknown parent
    events_file = tmp_path / "events.jsonl"
    _write_events(events_file, [orphan])

    rc = main(["verify", "--events", str(events_file)])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["roots"] == 1
    assert root in payload["violations"]
    assert payload["violations"][root]  # at least one violation string


# ---------------------------------------------------------------- consume


def test_consume_parses_flags() -> None:
    args = build_parser().parse_args(
        ["consume", "--once", "--group", "g2", "--consumer", "c9", "--count", "5"]
    )
    assert args.func is cmd_consume
    assert args.once is True
    assert args.group == "g2"
    assert args.consumer == "c9"
    assert args.count == 5
    assert args.block_ms == 5000  # default


def test_consume_rejects_redis_stream_sink(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The consumer reads FROM the stream; pointing its sink back at the
    # stream would loop events forever.
    monkeypatch.setenv("METER_SINK", "redis_stream")

    rc = main(["consume", "--once"])

    assert rc == 2
    assert "METER_SINK" in capsys.readouterr().err


def test_consume_requires_upstash_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("METER_SINK", "jsonl")
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)

    rc = main(["consume", "--once"])

    assert rc == 2
    assert "UPSTASH" in capsys.readouterr().err


def test_no_subcommand_exits_with_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
