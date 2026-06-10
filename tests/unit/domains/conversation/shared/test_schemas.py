"""Unit tests for conversation shared schemas (spec §2, §5, §6.4, §9.1, §13)."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from mcp_toolkit.domains.conversation.shared import (
    ConversationConfig,
    ConversationRecord,
    SessionBlobClaims,
    canonical_json,
    compute_event_id,
    sanitize_conversation_key,
    validate_end_user_id,
)
from mcp_toolkit.shared.config import Settings
from mcp_toolkit.shared.errors import ConversationError

# --- ConversationConfig ------------------------------------------------------


class TestConversationConfig:
    def test_defaults(self) -> None:
        cfg = ConversationConfig()
        assert cfg.enabled is False
        assert cfg.key_sources == ("meta", "header", "session")
        assert cfg.header == "X-Conversation-Key"
        assert cfg.end_user_header == "X-End-User-Id"
        assert cfg.ttl_header == "X-Conversation-Ttl"
        assert cfg.ttl_default == 86_400
        assert cfg.ttl_max == 604_800
        assert cfg.root_max_age == 604_800
        assert cfg.inflight_max == 16
        assert cfg.signing_key == ""
        assert cfg.signing_kid == "k1"
        assert cfg.signing_key_previous == ""
        assert cfg.signing_kid_previous == ""
        assert cfg.blob_ttl == 3_600
        assert cfg.genesis_rate_limit == 0
        assert cfg.store == "memory"
        assert cfg.jwks_path == "/.well-known/mcp-toolkit-jwks.json"
        assert cfg.meta_key == "ai.mcp-toolkit.conversation_key"

    def test_frozen(self) -> None:
        cfg = ConversationConfig()
        with pytest.raises(ValidationError):
            cfg.enabled = True

    def test_store_rejects_unknown_backend(self) -> None:
        with pytest.raises(ValidationError):
            ConversationConfig(store="dynamodb")  # type: ignore[arg-type]

    def test_from_settings_defaults_round_trip(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        cfg = ConversationConfig.from_settings(settings)
        assert cfg == ConversationConfig()

    def test_from_settings_maps_all_conv_fields(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            conv_enabled=True,
            conv_key_sources="header, session",
            conv_header="X-Thread",
            conv_end_user_header="X-User",
            conv_ttl_header="X-Ttl",
            conv_ttl_default=120,
            conv_ttl_max=240,
            conv_root_max_age=360,
            conv_inflight_max=4,
            conv_signing_key="c2VlZA==",
            conv_signing_kid="k2",
            conv_signing_key_previous="b2xk",
            conv_signing_kid_previous="k1",
            conv_blob_ttl=60,
            conv_genesis_rate_limit=10,
            conv_store="upstash",
            conv_jwks_path="/jwks.json",
            conv_meta_key="x.custom.conversation_key",
        )
        cfg = ConversationConfig.from_settings(settings)
        assert cfg.enabled is True
        assert cfg.key_sources == ("header", "session")  # comma-split, stripped
        assert cfg.header == "X-Thread"
        assert cfg.end_user_header == "X-User"
        assert cfg.ttl_header == "X-Ttl"
        assert cfg.ttl_default == 120
        assert cfg.ttl_max == 240
        assert cfg.root_max_age == 360
        assert cfg.inflight_max == 4
        assert cfg.signing_key == "c2VlZA=="
        assert cfg.signing_kid == "k2"
        assert cfg.signing_key_previous == "b2xk"
        assert cfg.signing_kid_previous == "k1"
        assert cfg.blob_ttl == 60
        assert cfg.genesis_rate_limit == 10
        assert cfg.store == "upstash"
        assert cfg.jwks_path == "/jwks.json"
        assert cfg.meta_key == "x.custom.conversation_key"

    def test_from_settings_drops_blank_key_sources(self) -> None:
        settings = Settings(_env_file=None, conv_key_sources="meta,,header,")  # type: ignore[call-arg]
        cfg = ConversationConfig.from_settings(settings)
        assert cfg.key_sources == ("meta", "header")


# --- SessionBlobClaims -------------------------------------------------------


class TestSessionBlobClaims:
    def test_construct_and_v_default(self) -> None:
        claims = SessionBlobClaims(
            iss="mcp-toolkit",
            sub="ten_acme",
            root="01JX0000000000000000000000",
            root_iat=1_749_470_000,
            iat=1_749_470_000,
            exp=1_749_473_600,
        )
        assert claims.iss == "mcp-toolkit"
        assert claims.sub == "ten_acme"
        assert claims.root == "01JX0000000000000000000000"
        assert claims.v == 1

    def test_int_fields_validated(self) -> None:
        with pytest.raises(ValidationError):
            SessionBlobClaims(
                iss="mcp-toolkit",
                sub="ten_acme",
                root="r",
                root_iat="not-an-int",  # type: ignore[arg-type]
                iat=0,
                exp=0,
            )


# --- ConversationRecord ------------------------------------------------------


class TestConversationRecord:
    def test_defaults(self) -> None:
        rec = ConversationRecord(
            tenant="ten_acme",
            root="01JX0000000000000000000000",
            key_hash="ab" * 32,
            key_label="thread_8f3a",
            root_iat=1_749_470_000,
            ttl=86_400,
        )
        assert rec.tip is None
        assert rec.state_bytes == 0
        assert rec.last_rent_ts is None
        assert rec.end_user_id is None
        assert rec.metadata == {}

    def test_keyless_genesis_allows_none_key(self) -> None:
        rec = ConversationRecord(
            tenant="ten_acme",
            root="01JX0000000000000000000000",
            key_hash=None,
            key_label=None,
            root_iat=1,
            ttl=60,
        )
        assert rec.key_hash is None
        assert rec.key_label is None

    def test_metadata_default_not_shared(self) -> None:
        kwargs = {
            "tenant": "t",
            "root": "r",
            "key_hash": None,
            "key_label": None,
            "root_iat": 1,
            "ttl": 60,
        }
        a = ConversationRecord(**kwargs)  # type: ignore[arg-type]
        b = ConversationRecord(**kwargs)  # type: ignore[arg-type]
        a.metadata["env"] = "prod"
        assert b.metadata == {}


# --- sanitize_conversation_key -----------------------------------------------


class TestSanitizeConversationKey:
    def test_accepts_full_charset(self) -> None:
        key = "Az09._:-thread"
        key_hash, label = sanitize_conversation_key("ten_acme", key)
        assert label == key
        expected = hashlib.sha256(
            hashlib.sha256(b"ten_acme").digest() + hashlib.sha256(key.encode()).digest()
        ).hexdigest()
        assert key_hash == expected

    def test_accepts_max_length(self) -> None:
        key = "a" * 256
        _, label = sanitize_conversation_key("t", key)
        assert label == key

    def test_rejects_over_max_length(self) -> None:
        with pytest.raises(ConversationError) as exc_info:
            sanitize_conversation_key("t", "a" * 257)
        assert exc_info.value.code == "invalid_conversation_key"

    @pytest.mark.parametrize(
        "bad_key",
        ["has space", "slash/", "at@sign", "unié", "semi;colon", "", "tab\tkey", "新键"],
    )
    def test_rejects_bad_charset(self, bad_key: str) -> None:
        with pytest.raises(ConversationError) as exc_info:
            sanitize_conversation_key("t", bad_key)
        assert exc_info.value.code == "invalid_conversation_key"

    def test_deterministic(self) -> None:
        h1, _ = sanitize_conversation_key("ten_acme", "thread_1")
        h2, _ = sanitize_conversation_key("ten_acme", "thread_1")
        assert h1 == h2

    def test_tenant_isolation(self) -> None:
        h_a, _ = sanitize_conversation_key("ten_a", "thread_1")
        h_b, _ = sanitize_conversation_key("ten_b", "thread_1")
        assert h_a != h_b

    def test_different_keys_differ(self) -> None:
        h1, _ = sanitize_conversation_key("t", "thread_1")
        h2, _ = sanitize_conversation_key("t", "thread_2")
        assert h1 != h2

    def test_hash_is_domain_separated(self) -> None:
        """sha256 over the fixed-length digests of tenant and key — no
        tenant/key boundary ambiguity for ANY tenant charset."""
        expected = hashlib.sha256(
            hashlib.sha256(b"ten_acme").digest() + hashlib.sha256(b"thread_1").digest()
        ).hexdigest()
        assert sanitize_conversation_key("ten_acme", "thread_1")[0] == expected

    def test_separator_straddling_tenant_does_not_collide(self) -> None:
        # The old flat `tenant||key` concatenation hashed ('acme|', 'bar')
        # and ('acme', '|bar') to the same digest. Tenant ids are
        # operator-supplied and unvalidated at mint time, so the
        # construction itself must be unambiguous.
        h, _ = sanitize_conversation_key("acme|", "bar")
        assert h != hashlib.sha256(b"acme|||bar").hexdigest()


# --- validate_end_user_id ----------------------------------------------------


class TestValidateEndUserId:
    def test_accepts_opaque_pseudonym(self) -> None:
        assert validate_end_user_id("u_anon_42") == "u_anon_42"

    def test_rejects_email_like_values(self) -> None:
        with pytest.raises(ConversationError) as exc_info:
            validate_end_user_id("alice@example.com")
        assert exc_info.value.code == "invalid_end_user_id"

    def test_rejects_over_max_length(self) -> None:
        with pytest.raises(ConversationError) as exc_info:
            validate_end_user_id("u" * 129)
        assert exc_info.value.code == "invalid_end_user_id"

    def test_accepts_max_length(self) -> None:
        value = "u" * 128
        assert validate_end_user_id(value) == value

    def test_rejects_empty(self) -> None:
        with pytest.raises(ConversationError) as exc_info:
            validate_end_user_id("")
        assert exc_info.value.code == "invalid_end_user_id"

    @pytest.mark.parametrize(
        "phone_like",
        ["555-867-5309", "5558675309", "555.867.5309", "1-555-867-5309", "044:123:4567"],
    )
    def test_rejects_phone_like_digit_strings(self, phone_like: str) -> None:
        with pytest.raises(ConversationError) as exc_info:
            validate_end_user_id(phone_like)
        assert exc_info.value.code == "invalid_end_user_id"

    @pytest.mark.parametrize(
        "pii_like",
        ["+1-555-867-5309", "John Smith", "uniçode", "(555)8675309", "a b", "tab\tid", "名前"],
    )
    def test_rejects_values_outside_opaque_charset(self, pii_like: str) -> None:
        with pytest.raises(ConversationError) as exc_info:
            validate_end_user_id(pii_like)
        assert exc_info.value.code == "invalid_end_user_id"

    @pytest.mark.parametrize(
        "pseudonym",
        ["550e8400-e29b-41d4-a716-446655440000", "user:1234", "1234", "a.b-c_d:e"],
    )
    def test_accepts_opaque_pseudonyms(self, pseudonym: str) -> None:
        assert validate_end_user_id(pseudonym) == pseudonym


# --- canonical_json ----------------------------------------------------------


class TestCanonicalJson:
    def test_key_order_stable(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

    def test_compact_separators_and_sorted(self) -> None:
        assert canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'

    def test_nested_keys_sorted(self) -> None:
        assert canonical_json({"z": {"b": 1, "a": 2}}) == '{"z":{"a":2,"b":1}}'

    def test_unicode_not_escaped(self) -> None:
        assert canonical_json({"k": "héllo"}) == '{"k":"héllo"}'


# --- compute_event_id --------------------------------------------------------


class TestComputeEventId:
    def test_prefix_and_shape(self) -> None:
        event_id = compute_event_id("blob_hash", "req_1", {"q": "x"})
        assert event_id.startswith("sha256:")
        assert len(event_id) == len("sha256:") + 64

    def test_same_inputs_same_id(self) -> None:
        a = compute_event_id("s", "req_1", {"q": "x", "n": 1})
        b = compute_event_id("s", "req_1", {"n": 1, "q": "x"})  # arg order irrelevant
        assert a == b

    def test_different_request_id_different_id(self) -> None:
        a = compute_event_id("s", "req_1", {"q": "x"})
        b = compute_event_id("s", "req_2", {"q": "x"})
        assert a != b

    def test_different_session_different_id(self) -> None:
        a = compute_event_id("s1", "req_1", {"q": "x"})
        b = compute_event_id("s2", "req_1", {"q": "x"})
        assert a != b

    def test_different_arguments_different_id(self) -> None:
        a = compute_event_id("s", "req_1", {"q": "x"})
        b = compute_event_id("s", "req_1", {"q": "y"})
        assert a != b

    def test_matches_spec_construction(self) -> None:
        """§7.4: sha256 over session_identity | request_id | canonical_json(args)."""
        args = {"q": "x"}
        material = "s|req_1|" + canonical_json(args)
        expected = "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
        assert compute_event_id("s", "req_1", args) == expected
