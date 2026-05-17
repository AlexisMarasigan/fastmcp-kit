"""Placeholder e2e smoke. Real e2e suite lands in sprint 5."""

from __future__ import annotations

import pytest


@pytest.mark.e2e
def test_smoke_placeholder() -> None:
    # Pins the marker plumbing so `pytest -m e2e` returns one passing test.
    assert True
