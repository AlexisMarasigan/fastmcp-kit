"""Billing consumer app (the `[billing]` extra) — spec resolved decision 1.

Drains the kit's usage-event stream into a Stripe-Meters-compatible sink
and reconstructs auditable invoices from the event log alone.
"""

from __future__ import annotations

from mcp_toolkit.apps.billing.consumer import BillingConsumer
from mcp_toolkit.apps.billing.invoice import (
    Invoice,
    InvoiceLine,
    count_end_users,
    reconstruct,
    verify_dag,
)

__all__ = [
    "BillingConsumer",
    "Invoice",
    "InvoiceLine",
    "count_end_users",
    "reconstruct",
    "verify_dag",
]
