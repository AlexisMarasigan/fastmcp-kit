"""Conversation domain — root identity, key waterfall, session blob, admission.

See docs/SPEC-conversation-metering.md. Depends on auth/tenancy upstream
(resolved tenant) and shared/; never imports metering — apps/server wires
the two together through `conversation_middleware(on_genesis=...)`.
"""

from __future__ import annotations

from mcp_toolkit.domains.conversation.server import (
    ConversationContext,
    ConversationStore,
    InMemoryConversationStore,
    SessionBlobSigner,
    UpstashConversationStore,
    conversation_middleware,
    current_conversation,
)
from mcp_toolkit.domains.conversation.shared import (
    END_USER_ID_MAX_LENGTH,
    KEY_MAX_LENGTH,
    ConversationConfig,
    ConversationRecord,
    SessionBlobClaims,
    canonical_json,
    compute_event_id,
    sanitize_conversation_key,
    validate_end_user_id,
)

__all__ = [
    "END_USER_ID_MAX_LENGTH",
    "KEY_MAX_LENGTH",
    "ConversationConfig",
    "ConversationContext",
    "ConversationRecord",
    "ConversationStore",
    "InMemoryConversationStore",
    "SessionBlobClaims",
    "SessionBlobSigner",
    "UpstashConversationStore",
    "canonical_json",
    "compute_event_id",
    "conversation_middleware",
    "current_conversation",
    "sanitize_conversation_key",
    "validate_end_user_id",
]
