"""Conversation domain — shared schemas and pure helpers.

Value objects for the conversation identity layer (spec §2, §5, §9.1):
`ConversationConfig` (library-level config mirroring the `conv_*` Settings
fields), `SessionBlobClaims` (the JWS payload riding in `Mcp-Session-Id`),
and `ConversationRecord` (the per-root Redis record at `conv:rec:{root}`).

Plus the pure functions used at the gateway boundary: conversation-key
sanitization (§6.4), end-user-id PII rejection (§6.2), canonical JSON, and
the request-identity dedupe key (§7.4). No I/O here — stores and middleware
live in `server/`.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from mcp_toolkit.shared.config import ConversationStoreBackend
from mcp_toolkit.shared.errors import ConversationError

if TYPE_CHECKING:
    from mcp_toolkit.shared.config import Settings

KEY_MAX_LENGTH = 256
END_USER_ID_MAX_LENGTH = 128
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
# End-user ids share the conversation-key allowlist: it excludes `@`
# (emails), whitespace (full names), `+`/`()` (phone prefixes) — §6.2.
_END_USER_SEPARATORS = re.compile(r"[._:-]")
# Separator-padded digit strings at/above this many digits look like
# phone numbers or national-id numbers — obvious PII, rejected (§6.2).
PHONE_LIKE_MIN_DIGITS = 7


class ConversationConfig(BaseModel):
    """Library-level conversation config (spec §10, §13).

    Passed to `MCPToolkit(conversation=...)`; wins over env. Use
    `from_settings` to build the env-derived equivalent.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    key_sources: tuple[str, ...] = ("meta", "header", "session")
    header: str = "X-Conversation-Key"
    end_user_header: str = "X-End-User-Id"
    ttl_header: str = "X-Conversation-Ttl"
    ttl_default: int = 86_400
    ttl_max: int = 604_800
    root_max_age: int = 604_800
    inflight_max: int = 16
    # Base64-encoded 32-byte Ed25519 private seed. Empty = ephemeral dev key.
    signing_key: str = ""
    signing_kid: str = "k1"
    # Previous key for rotation overlap. Verified, never signed with.
    signing_key_previous: str = ""
    signing_kid_previous: str = ""
    blob_ttl: int = 3_600
    # Genesis events per tenant per hour. 0 = unlimited.
    genesis_rate_limit: int = 0
    store: ConversationStoreBackend = "memory"
    jwks_path: str = "/.well-known/mcp-toolkit-jwks.json"
    # `_meta` field consulted on `tools/call` (waterfall source "meta", §6.1).
    meta_key: str = "ai.mcp-toolkit.conversation_key"

    @classmethod
    def from_settings(cls, settings: Settings) -> ConversationConfig:
        """Build a config from the process `Settings` (`conv_*` fields, §13)."""
        sources = tuple(s.strip() for s in settings.conv_key_sources.split(",") if s.strip())
        return cls(
            enabled=settings.conv_enabled,
            key_sources=sources,
            header=settings.conv_header,
            end_user_header=settings.conv_end_user_header,
            ttl_header=settings.conv_ttl_header,
            ttl_default=settings.conv_ttl_default,
            ttl_max=settings.conv_ttl_max,
            root_max_age=settings.conv_root_max_age,
            inflight_max=settings.conv_inflight_max,
            signing_key=settings.conv_signing_key,
            signing_kid=settings.conv_signing_kid,
            signing_key_previous=settings.conv_signing_key_previous,
            signing_kid_previous=settings.conv_signing_kid_previous,
            blob_ttl=settings.conv_blob_ttl,
            genesis_rate_limit=settings.conv_genesis_rate_limit,
            store=settings.conv_store,
            jwks_path=settings.conv_jwks_path,
            meta_key=settings.conv_meta_key,
        )


class SessionBlobClaims(BaseModel):
    """JWS claims carried in `Mcp-Session-Id` (spec §5.2).

    Identity only, never application state. `kid` rides in the JWS
    protected header, not here. Statelessly verifiable with the public key.
    """

    iss: str
    sub: str  # tenant_id
    root: str  # root jti (ULID) — the billing aggregation key
    root_iat: int  # genesis timestamp; enforces the hard age cap (§8.2)
    iat: int
    exp: int
    v: int = 1


class ConversationRecord(BaseModel):
    """Per-root conversation record persisted at `conv:rec:{root}` (spec §9.1)."""

    tenant: str
    root: str
    key_hash: str | None  # sha256(sha256(tenant)+sha256(key)); None in keyless mode (§6.3)
    key_label: str | None  # raw builder key — invoice display only, never a lookup
    root_iat: int
    tip: str | None = None  # last completed jti — best-effort DAG parent (§7.2)
    state_bytes: int = 0  # accrual basis for state rent (§8.3)
    last_rent_ts: int | None = None
    ttl: int = 0
    end_user_id: str | None = None
    metadata: dict[str, str] = {}


def sanitize_conversation_key(tenant_id: str, key: str) -> tuple[str, str]:
    """Validate a builder-supplied conversation key (spec §6.4).

    Returns `(key_hash, key_label)`: the sha256 hex over the fixed-length
    digests of tenant and key (`sha256(sha256(tenant_id) + sha256(key))`)
    used as the storage key (tenant-isolated by construction), and the raw
    key kept only as an invoice display label.

    Raises `ConversationError("invalid_conversation_key")` when the key
    exceeds 256 chars or strays outside `[A-Za-z0-9._:-]`.
    """
    if len(key) > KEY_MAX_LENGTH:
        raise ConversationError(
            "invalid_conversation_key",
            f"conversation key exceeds {KEY_MAX_LENGTH} characters",
        )
    if not _KEY_PATTERN.fullmatch(key):
        raise ConversationError(
            "invalid_conversation_key",
            "conversation key must be 1-256 characters from [A-Za-z0-9._:-]",
        )
    # Hash tenant and key to fixed-length digests before the outer hash.
    # A flat `tenant||key` concatenation is ambiguous when an
    # operator-minted tenant id contains the separator (`acme|`+`bar` vs
    # `acme`+`|bar`), which would collide two tenants' storage keys.
    tenant_digest = hashlib.sha256(tenant_id.encode()).digest()
    key_digest = hashlib.sha256(key.encode()).digest()
    key_hash = hashlib.sha256(tenant_digest + key_digest).hexdigest()
    return key_hash, key


def validate_end_user_id(value: str) -> str:
    """Validate an `X-End-User-Id` value (spec §6.2).

    Must be an opaque pseudonym: non-empty, ≤ 128 chars, drawn from the
    conversation-key allowlist `[A-Za-z0-9._:-]` (rejects emails, names
    with spaces, `+`-prefixed phone numbers), and not a separator-padded
    digit string of ≥ `PHONE_LIKE_MIN_DIGITS` digits (phone/national-id
    shaped) — obvious PII is rejected at the gateway.

    Raises `ConversationError("invalid_end_user_id")` otherwise.
    """
    if not value:
        raise ConversationError("invalid_end_user_id", "end-user id must be non-empty")
    if len(value) > END_USER_ID_MAX_LENGTH:
        raise ConversationError(
            "invalid_end_user_id",
            f"end-user id exceeds {END_USER_ID_MAX_LENGTH} characters",
        )
    if not _KEY_PATTERN.fullmatch(value):
        raise ConversationError(
            "invalid_end_user_id",
            "end-user id must be an opaque pseudonym from [A-Za-z0-9._:-] — "
            "emails and other real-world identity data are rejected",
        )
    digits = _END_USER_SEPARATORS.sub("", value)
    if digits.isdigit() and len(digits) >= PHONE_LIKE_MIN_DIGITS:
        raise ConversationError(
            "invalid_end_user_id",
            "end-user id looks like a phone number or numeric identity data; "
            "supply an opaque pseudonym",
        )
    return value


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, raw unicode."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_event_id(session_identity: str, request_id: str, arguments: Any) -> str:
    """Request-identity dedupe key (spec §7.4).

    `sha256:` + sha256 over `session_identity | request_id |
    canonical_json(arguments)`. Transport retries reproduce the same id
    (bill once); genuinely parallel identical calls differ in `request_id`
    (bill twice).
    """
    material = f"{session_identity}|{request_id}|{canonical_json(arguments)}"
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
