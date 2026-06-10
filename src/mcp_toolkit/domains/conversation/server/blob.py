"""Session blob (`Mcp-Session-Id`) JWS signer/verifier (spec §5.2, §8.2).

The session blob replaces the random MCP session ID with a compact EdDSA
(Ed25519) JWS carrying `SessionBlobClaims` — identity only, never
application state. Workers verify statelessly with the public key; no KV
read on the hot path. Key rotation rides on the JWS `kid` protected
header plus a verify-only previous key, both published through `jwks()`
for the `/.well-known/mcp-toolkit-jwks.json` endpoint.

`pyjwt` + `cryptography` arrive transitively via the pinned `mcp` SDK
(spec implementation notes) — no extra needed.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from pydantic import ValidationError

from mcp_toolkit.domains.conversation.shared.schemas import (
    ConversationConfig,
    SessionBlobClaims,
)
from mcp_toolkit.shared.errors import ConversationError
from mcp_toolkit.shared.logging import get_logger

_log = get_logger(__name__)

_ISSUER = "mcp-toolkit"
_ALGORITHM = "EdDSA"
_SEED_BYTES = 32


def _b64url(raw: bytes) -> str:
    """Base64url without padding, per RFC 7517 JWK encoding."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _load_private_key(seed_b64: str, field: str) -> Ed25519PrivateKey:
    """Decode a base64-encoded 32-byte Ed25519 seed into a private key.

    Raises `ValueError` (config-time failure, not a wire error) on bad
    base64 or a seed of the wrong length.
    """
    try:
        seed = base64.b64decode(seed_b64, validate=True)
    except ValueError as e:  # binascii.Error subclasses ValueError
        raise ValueError(f"{field} is not valid base64") from e
    if len(seed) != _SEED_BYTES:
        raise ValueError(f"{field} must decode to exactly {_SEED_BYTES} bytes, got {len(seed)}")
    return Ed25519PrivateKey.from_private_bytes(seed)


class SessionBlobSigner:
    """Mints and verifies the `Mcp-Session-Id` JWS for one process.

    Built from `ConversationConfig`. The current key signs and verifies;
    the optional previous key (rotation overlap, §5.2) only verifies.
    An empty `signing_key` generates an ephemeral per-process key — dev
    mode only, since blobs then never verify across pods.
    """

    def __init__(self, config: ConversationConfig) -> None:
        self._config = config
        if config.signing_key:
            self._private_key = _load_private_key(config.signing_key, "signing_key")
        else:
            self._private_key = Ed25519PrivateKey.generate()
            _log.warning(
                "conversation.signing_key.ephemeral",
                detail="no signing key configured; generated an ephemeral per-process key — "
                "session blobs will not verify across pods (dev mode only)",
            )
        # kid → public key. Insertion order is the JWKS order: current first.
        self._public_keys: dict[str, Ed25519PublicKey] = {
            config.signing_kid: self._private_key.public_key()
        }
        if bool(config.signing_key_previous) != bool(config.signing_kid_previous):
            raise ValueError("signing_key_previous and signing_kid_previous must be set together")
        if config.signing_key_previous:
            if config.signing_kid_previous == config.signing_kid:
                raise ValueError("signing_kid_previous must differ from signing_kid")
            previous = _load_private_key(config.signing_key_previous, "signing_key_previous")
            self._public_keys[config.signing_kid_previous] = previous.public_key()

    def mint(self, tenant: str, root: str, root_iat: int, *, now: int | None = None) -> str:
        """Issue a compact JWS session blob binding `tenant` to `root` (§5.2).

        `exp = iat + blob_ttl`; `kid` rides in the protected header. `now`
        is injectable for tests and defaults to the wall clock.
        """
        iat = int(time.time()) if now is None else now
        claims = SessionBlobClaims(
            iss=_ISSUER,
            sub=tenant,
            root=root,
            root_iat=root_iat,
            iat=iat,
            exp=iat + self._config.blob_ttl,
        )
        return jwt.encode(
            claims.model_dump(),
            self._private_key,
            algorithm=_ALGORITHM,
            headers={"kid": self._config.signing_kid},
        )

    def verify(self, blob: str, *, now: int | None = None) -> SessionBlobClaims:
        """Statelessly verify a session blob and return its claims.

        Raises `ConversationError("invalid_session_blob")` on any
        signature/format/expiry failure, and
        `ConversationError("conversation_expired")` when the root exceeds
        the hard age cap (§8.2) even if the blob itself is fresh.
        """
        ts = int(time.time()) if now is None else now
        try:
            header: dict[str, Any] = jwt.get_unverified_header(blob)
        except jwt.InvalidTokenError as e:
            raise ConversationError("invalid_session_blob", "session blob is malformed") from e
        kid = header.get("kid")
        public_key = self._public_keys.get(kid) if isinstance(kid, str) else None
        if public_key is None:
            raise ConversationError(
                "invalid_session_blob", "session blob signed with an unknown key id"
            )
        try:
            payload = jwt.decode(
                blob,
                public_key,
                algorithms=[_ALGORITHM],
                issuer=_ISSUER,
                # pyjwt checks `exp` against the real clock only; disable it
                # and enforce against the injectable `ts` below instead.
                options={"verify_exp": False},
            )
        except jwt.InvalidTokenError as e:
            raise ConversationError(
                "invalid_session_blob", "session blob failed verification"
            ) from e
        try:
            claims = SessionBlobClaims.model_validate(payload)
        except ValidationError as e:
            raise ConversationError(
                "invalid_session_blob", "session blob claims are malformed"
            ) from e
        if claims.exp <= ts:
            raise ConversationError("invalid_session_blob", "session blob has expired")
        if ts - claims.root_iat > self._config.root_max_age:
            raise ConversationError(
                "conversation_expired",
                "conversation exceeded its maximum age; re-initialize to start a new one",
            )
        return claims

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        """Public JWKS document for the current (+ previous) keys (§5.2).

        OKP/Ed25519 JWKs per RFC 8037; `x` is base64url without padding.
        Served at `config.jwks_path` so external verifiers can validate
        blobs through rotations.
        """
        keys = [
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": _b64url(public.public_bytes(Encoding.Raw, PublicFormat.Raw)),
                "kid": kid,
                "alg": _ALGORITHM,
                "use": "sig",
            }
            for kid, public in self._public_keys.items()
        ]
        return {"keys": keys}

    @staticmethod
    def generate_signing_key() -> str:
        """Operator helper: mint a fresh base64-encoded Ed25519 seed.

        Paste the value into `CONV_SIGNING_KEY` /
        `ConversationConfig.signing_key`.
        """
        seed = Ed25519PrivateKey.generate().private_bytes(
            Encoding.Raw, PrivateFormat.Raw, NoEncryption()
        )
        return base64.b64encode(seed).decode("ascii")
