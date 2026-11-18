"""Content safety for the OpenAI-compatible model protocol face (F120 T058).

The opposite hit contract from the DSH face, for one reason: who is listening.

DSH answers a hit with a *disguised* completion because the desktop UI renders a
4xx as a service failure. An API client programs on error codes — it reads a
disguised answer as ordinary output and never learns the request was refused. So
a hit here surfaces as ``ModelFaceContentBlockedError``: an OpenAI-shaped error
body for a non-streaming call, and the same error inside an SSE ``error`` event
for a streaming one, because by then the 200 header is already on the wire and
there is no status left to set (AC-43 / AC-44).

Neither path passes the matched word back. The error carries the code and the
policy's blocked-reply wording; the word itself stays in the block record, which
only a tenant administrator can read.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from bisheng.common.errcode.model_face import ModelFaceContentBlockedError
from bisheng.gateway.domain.schemas.block_record import GatewayBlockRecordInput
from bisheng.gateway.domain.services.block_record_service import gateway_block_record_service
from bisheng.sensitive_word.domain.schemas import (
    SensitiveWordBusinessType,
    SensitiveWordCheckResult,
    SensitiveWordHit,
)
from bisheng.sensitive_word.domain.services.sensitive_word_policy_service import (
    SensitiveWordPolicyService,
)
from bisheng.sensitive_word.domain.services.stream_scanner import StreamContentSafetyScanner

#: ``gateway_block_record.face`` — one of the values the writer whitelists.
FACE = "open_api_chat"
RESOURCE_TYPE = "model"

#: Every delta is checked, not every 100 characters like the shared
#: scanner's default. With any larger interval a short answer is delivered in
#: full before the first check runs, which is exactly the leak AC-44 forbids.
#:
#: The cost is one policy read per delta, and it is paid only by tenants that
#: turned this face's policy on: ``make_output_scanner`` returns ``None``
#: otherwise, so the inactive path — every tenant that is not using the feature
#: — does no work at all. Same choice the DSH face made, for the same reason.
#: If the read ever shows up in a profile, the fix is to resolve the policy once
#: per stream inside the scanner, not to widen the interval.
OUTPUT_CHECK_INTERVAL = 1


def extract_user_text(messages: list[dict[str, Any]] | None) -> str:
    """Concatenate the text of every user turn.

    Unlike the workstation and DSH faces — where the platform owns the history
    and already vetted the earlier turns as they were written — this face's
    caller supplies the whole message list on every call. There is no
    "checked earlier" state to lean on, so the earlier turns are scanned too;
    otherwise a caller could split a sensitive phrase across two turns and have
    the second half sail through on a last-turn-only scan.
    """
    if not messages:
        return ""
    parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            # Multimodal parts: text only, same rule as the DSH face (F111 AC-20).
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
    return "\n".join(part for part in parts if part)


def evaluate_input(tenant_id: int, messages: list[dict[str, Any]] | None) -> SensitiveWordCheckResult | None:
    """Input determination. ``None`` when inactive or clean, so the caller can
    short-circuit with a single truthiness test."""
    return SensitiveWordPolicyService.evaluate_open_api_user_text(tenant_id, extract_user_text(messages))


def make_output_scanner(tenant_id: int) -> StreamContentSafetyScanner | None:
    """``None`` when this tenant has no active open_api policy.

    Returning ``None`` rather than a scanner that never hits keeps the streaming
    loop free of a per-delta branch on the common (inactive) path — and, more to
    the point, keeps an inactive tenant's stream byte-for-byte identical to what
    it was before F120 (AC-22's "no Pro → pass through", made structural).
    """
    if not SensitiveWordPolicyService.is_open_api_content_safety_active(tenant_id):
        return None
    return StreamContentSafetyScanner(
        tenant_id=tenant_id,
        business_type=SensitiveWordBusinessType.OPEN_API_CHAT,
        check_interval=OUTPUT_CHECK_INTERVAL,
    )


def _hit_words(result: SensitiveWordCheckResult) -> list:
    """Normalise the hits into the writer's ``matched_words`` shape.

    The writer accepts both the ``{"word", "count"}`` dict and a bare string, so
    either is fine; the dict is preferred because ``count`` is what an operator
    needs to tell a one-off from a word that fired fifty times.
    """

    return [
        hit.model_dump() if isinstance(hit, SensitiveWordHit) else {"word": str(hit), "count": 1}
        for hit in (result.hits or [])
    ]


def blocked_error(result: SensitiveWordCheckResult) -> ModelFaceContentBlockedError:
    """The one error both paths raise.

    ``auto_reply`` is the tenant's own wording for a refusal, so it is what the
    caller reads; the matched word is not included anywhere in it.
    """
    return ModelFaceContentBlockedError(msg=result.auto_reply or ModelFaceContentBlockedError.Msg)


def record_block(
    *,
    tenant_id: int,
    user_id: int,
    stage: str,
    result: SensitiveWordCheckResult,
    request_id: str,
    resource_id: str,
    user_input: str,
) -> bool:
    """Best-effort evidence write. Never raises, never touches the database.

    Called from the request path, so the same rule as the DSH face applies: a
    lost audit row must not cost the caller their answer. ``resource_id`` is the
    requested model name — the thing an operator needs to correlate the refusal
    with a model configuration.
    """
    try:
        return gateway_block_record_service.record_block(
            GatewayBlockRecordInput(
                face=FACE,
                stage=stage,
                tenant_id=tenant_id,
                user_id=user_id,
                resource_type=RESOURCE_TYPE,
                resource_id=resource_id,
                matched_words=_hit_words(result),
                user_input=user_input,
                auto_reply=result.auto_reply or "",
                request_id=request_id,
            )
        )
    except Exception:
        # record_block already swallows its own build failures; this guards the
        # call itself (a schema change, an import cycle) so a refusal still
        # reaches the caller.
        logger.opt(exception=True).error(
            "model_gateway.block_record_failed | stage={} tenant_id={} request_id={}",
            stage,
            tenant_id,
            request_id,
        )
        return False
