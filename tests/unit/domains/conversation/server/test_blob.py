"""Unit tests for the session blob JWS signer/verifier (spec §5.2, §8.2)."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_toolkit.domains.conversation.server.blob import SessionBlobSigner
from mcp_toolkit.domains.conversation.shared.schemas import (
    ConversationConfig,
    SessionBlobClaims,
)
from mcp_toolkit.shared.errors import ConversationError

# Deterministic 32-byte Ed25519 seeds for tests.
SEED_A = base64.b64encode(bytes(range(32))).decode("ascii")
SEED_B = base64.b64encode(bytes(range(32, 64))).decode("ascii")

NOW = 1_749_470_000
TENANT = "ten_acme"
ROOT = "01JXAW3F8M9QZC5T2V7B4N6KDH"  # ULID-shaped


def make_config(**overrides: Any) -> ConversationConfig:
    defaults: dict[str, Any] = {"signing_key": SEED_A, "signing_kid": "k1"}
    defaults.update(overrides)
    return ConversationConfig(**defaults)


def make_signer(**overrides: Any) -> SessionBlobSigner:
    return SessionBlobSigner(make_config(**overrides))


def tamper_payload(blob: str, **changes: Any) -> str:
    """Rewrite payload claims without re-signing — signature must break."""
    header_b64, payload_b64, sig_b64 = blob.split(".")
    pad = "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
    payload.update(changes)
    forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode("ascii")
    return ".".join([header_b64, forged, sig_b64])


# --- mint / verify round trip ------------------------------------------------


class TestMintVerifyRoundTrip:
    def test_round_trip_returns_claims(self) -> None:
        signer = make_signer()
        blob = signer.mint(TENANT, ROOT, NOW, now=NOW)
        claims = signer.verify(blob, now=NOW)
        assert isinstance(claims, SessionBlobClaims)
        assert claims.iss == "mcp-toolkit"
        assert claims.sub == TENANT
        assert claims.root == ROOT
        assert claims.root_iat == NOW
        assert claims.iat == NOW
        assert claims.exp == NOW + 3_600  # default blob_ttl
        assert claims.v == 1

    def test_exp_honours_configured_blob_ttl(self) -> None:
        signer = make_signer(blob_ttl=60)
        blob = signer.mint(TENANT, ROOT, NOW, now=NOW)
        claims = signer.verify(blob, now=NOW)
        assert claims.exp == claims.iat + 60

    def test_mint_defaults_to_wall_clock(self) -> None:
        signer = make_signer()
        before = int(time.time())
        blob = signer.mint(TENANT, ROOT, before)
        claims = signer.verify(blob)
        assert before <= claims.iat <= int(time.time())

    def test_kid_rides_in_protected_header_not_claims(self) -> None:
        signer = make_signer()
        blob = signer.mint(TENANT, ROOT, NOW, now=NOW)
        header = jwt.get_unverified_header(blob)
        assert header["kid"] == "k1"
        assert header["alg"] == "EdDSA"
        claims = signer.verify(blob, now=NOW)
        assert "kid" not in claims.model_dump()

    def test_blob_within_size_budget(self) -> None:
        # §5.2: size budget ≤ 1 KB even with generous identifier lengths.
        signer = make_signer(signing_kid="rotation-key-2026-06-10")
        blob = signer.mint("ten_" + "x" * 60, ROOT, NOW, now=NOW)
        assert len(blob.encode("utf-8")) <= 1024


# --- verification failures ----------------------------------------------------


class TestVerifyRejections:
    def test_tampered_blob_rejected(self) -> None:
        signer = make_signer()
        blob = signer.mint(TENANT, ROOT, NOW, now=NOW)
        tampered = tamper_payload(blob, root="forged-root")
        with pytest.raises(ConversationError) as ei:
            signer.verify(tampered, now=NOW)
        assert ei.value.code == "invalid_session_blob"

    def test_garbage_blob_rejected(self) -> None:
        signer = make_signer()
        with pytest.raises(ConversationError) as ei:
            signer.verify("not-a-jws", now=NOW)
        assert ei.value.code == "invalid_session_blob"

    def test_expired_blob_rejected(self) -> None:
        signer = make_signer(blob_ttl=60)
        blob = signer.mint(TENANT, ROOT, NOW, now=NOW)
        with pytest.raises(ConversationError) as ei:
            signer.verify(blob, now=NOW + 61)
        assert ei.value.code == "invalid_session_blob"

    def test_blob_valid_just_before_expiry(self) -> None:
        signer = make_signer(blob_ttl=60)
        blob = signer.mint(TENANT, ROOT, NOW, now=NOW)
        assert signer.verify(blob, now=NOW + 59).root == ROOT

    def test_root_iat_over_max_age_is_conversation_expired(self) -> None:
        # §8.2: a fresh blob (valid exp) over an old root must still die.
        signer = make_signer(root_max_age=100)
        blob = signer.mint(TENANT, ROOT, NOW - 101, now=NOW)
        with pytest.raises(ConversationError) as ei:
            signer.verify(blob, now=NOW)
        assert ei.value.code == "conversation_expired"

    def test_root_iat_at_exact_max_age_still_valid(self) -> None:
        signer = make_signer(root_max_age=100)
        blob = signer.mint(TENANT, ROOT, NOW - 100, now=NOW)
        assert signer.verify(blob, now=NOW).root == ROOT

    def test_wrong_issuer_rejected(self) -> None:
        signer = make_signer()
        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(SEED_A))
        evil = jwt.encode(
            {
                "iss": "evil",
                "sub": TENANT,
                "root": ROOT,
                "root_iat": NOW,
                "iat": NOW,
                "exp": NOW + 3_600,
                "v": 1,
            },
            key,
            algorithm="EdDSA",
            headers={"kid": "k1"},
        )
        with pytest.raises(ConversationError) as ei:
            signer.verify(evil, now=NOW)
        assert ei.value.code == "invalid_session_blob"

    def test_missing_claims_rejected(self) -> None:
        signer = make_signer()
        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(SEED_A))
        partial = jwt.encode(
            {"iss": "mcp-toolkit", "exp": NOW + 3_600},
            key,
            algorithm="EdDSA",
            headers={"kid": "k1"},
        )
        with pytest.raises(ConversationError) as ei:
            signer.verify(partial, now=NOW)
        assert ei.value.code == "invalid_session_blob"

    def test_unsigned_alg_none_rejected(self) -> None:
        signer = make_signer()
        unsigned = jwt.encode(
            {
                "iss": "mcp-toolkit",
                "sub": TENANT,
                "root": ROOT,
                "root_iat": NOW,
                "iat": NOW,
                "exp": NOW + 3_600,
                "v": 1,
            },
            # PyJWT's NoneAlgorithm treats "" as None; typed key param disallows None.
            "",
            algorithm="none",
            headers={"kid": "k1"},
        )
        with pytest.raises(ConversationError) as ei:
            signer.verify(unsigned, now=NOW)
        assert ei.value.code == "invalid_session_blob"


# --- kid rotation (§5.2) -------------------------------------------------------


class TestKidRotation:
    def test_previous_key_blob_verifies_during_overlap(self) -> None:
        old_signer = make_signer(signing_key=SEED_A, signing_kid="k1")
        blob = old_signer.mint(TENANT, ROOT, NOW, now=NOW)
        rotated = make_signer(
            signing_key=SEED_B,
            signing_kid="k2",
            signing_key_previous=SEED_A,
            signing_kid_previous="k1",
        )
        assert rotated.verify(blob, now=NOW).root == ROOT

    def test_rotated_signer_signs_with_current_key_only(self) -> None:
        rotated = make_signer(
            signing_key=SEED_B,
            signing_kid="k2",
            signing_key_previous=SEED_A,
            signing_kid_previous="k1",
        )
        blob = rotated.mint(TENANT, ROOT, NOW, now=NOW)
        assert jwt.get_unverified_header(blob)["kid"] == "k2"

    def test_unknown_kid_rejected(self) -> None:
        # Same key material under an unrecognized kid: selection is by kid.
        foreign = make_signer(signing_key=SEED_A, signing_kid="k9")
        blob = foreign.mint(TENANT, ROOT, NOW, now=NOW)
        verifier = make_signer(signing_key=SEED_A, signing_kid="k1")
        with pytest.raises(ConversationError) as ei:
            verifier.verify(blob, now=NOW)
        assert ei.value.code == "invalid_session_blob"


# --- JWKS endpoint payload (§5.2) ----------------------------------------------


class TestJwks:
    def test_single_key_shape(self) -> None:
        signer = make_signer()
        document = signer.jwks()
        assert set(document) == {"keys"}
        (key,) = document["keys"]
        assert key["kty"] == "OKP"
        assert key["crv"] == "Ed25519"
        assert key["alg"] == "EdDSA"
        assert key["use"] == "sig"
        assert key["kid"] == "k1"

    def test_x_is_base64url_without_padding(self) -> None:
        signer = make_signer()
        (key,) = signer.jwks()["keys"]
        x = key["x"]
        assert "=" not in x
        assert "+" not in x
        assert "/" not in x
        raw = base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))
        assert len(raw) == 32

    def test_x_matches_configured_seed_public_key(self) -> None:
        signer = make_signer()
        (key,) = signer.jwks()["keys"]
        private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(SEED_A))
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        expected = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        expected_x = base64.urlsafe_b64encode(expected).rstrip(b"=").decode("ascii")
        assert key["x"] == expected_x

    def test_includes_previous_key_current_first(self) -> None:
        rotated = make_signer(
            signing_key=SEED_B,
            signing_kid="k2",
            signing_key_previous=SEED_A,
            signing_kid_previous="k1",
        )
        keys = rotated.jwks()["keys"]
        assert [k["kid"] for k in keys] == ["k2", "k1"]
        assert all(k["kty"] == "OKP" and k["use"] == "sig" for k in keys)


# --- ephemeral dev mode ---------------------------------------------------------


class TestEphemeralMode:
    def test_empty_signing_key_round_trips(self) -> None:
        signer = make_signer(signing_key="")
        blob = signer.mint(TENANT, ROOT, NOW, now=NOW)
        assert signer.verify(blob, now=NOW).root == ROOT

    def test_ephemeral_keys_do_not_cross_verify(self) -> None:
        # Two processes generate distinct keys — blobs don't verify across pods.
        signer_a = make_signer(signing_key="")
        signer_b = make_signer(signing_key="")
        blob = signer_a.mint(TENANT, ROOT, NOW, now=NOW)
        with pytest.raises(ConversationError) as ei:
            signer_b.verify(blob, now=NOW)
        assert ei.value.code == "invalid_session_blob"

    def test_ephemeral_mode_logs_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mcp_toolkit.domains.conversation.server import blob as blob_module

        events: list[str] = []

        class _StubLog:
            def warning(self, event: str, **kwargs: Any) -> None:
                events.append(event)

        monkeypatch.setattr(blob_module, "_log", _StubLog())
        make_signer(signing_key="")
        assert events == ["conversation.signing_key.ephemeral"]


# --- key material helpers --------------------------------------------------------


class TestGenerateSigningKey:
    def test_returns_base64_of_32_bytes(self) -> None:
        encoded = SessionBlobSigner.generate_signing_key()
        assert len(base64.b64decode(encoded, validate=True)) == 32

    def test_each_call_is_distinct(self) -> None:
        assert SessionBlobSigner.generate_signing_key() != SessionBlobSigner.generate_signing_key()

    def test_generated_key_is_usable(self) -> None:
        signer = make_signer(signing_key=SessionBlobSigner.generate_signing_key())
        blob = signer.mint(TENANT, ROOT, NOW, now=NOW)
        assert signer.verify(blob, now=NOW).sub == TENANT


class TestKeyConfigValidation:
    def test_invalid_base64_signing_key(self) -> None:
        with pytest.raises(ValueError, match="base64"):
            make_signer(signing_key="!!not-base64!!")

    def test_wrong_length_seed(self) -> None:
        short = base64.b64encode(b"too-short").decode("ascii")
        with pytest.raises(ValueError, match="32"):
            make_signer(signing_key=short)

    def test_previous_key_requires_previous_kid(self) -> None:
        with pytest.raises(ValueError, match="signing_kid_previous"):
            make_signer(signing_key_previous=SEED_B, signing_kid_previous="")

    def test_previous_kid_requires_previous_key(self) -> None:
        with pytest.raises(ValueError, match="signing_key_previous"):
            make_signer(signing_key_previous="", signing_kid_previous="k0")

    def test_previous_kid_must_differ_from_current(self) -> None:
        with pytest.raises(ValueError, match="differ"):
            make_signer(
                signing_key_previous=SEED_B,
                signing_kid_previous="k1",
                signing_kid="k1",
            )
