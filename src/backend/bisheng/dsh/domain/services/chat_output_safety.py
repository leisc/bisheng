"""DSH 输出安全：命中即阻断（F120 T020，AC-16）。

**判定不在这里**：匹配逻辑复用 `StreamContentSafetyScanner`（同一份
`SensitiveWordPolicyService`），DSH 只负责「什么时候喂、命中后怎么收尾」
（design §5.4 不重复造匹配）。

**为什么是守卫对象而不是包一层的 generator**：chat 面与 responses 面的上游
形状本来就不同——chat 的 `adapter.stream()` 吐 delta dict，responses 的
`execution.stream()` 吐 event dict，由各自调用方拼帧。让调用方继续持有帧结构，
守卫只回答「这段文本命中了吗」，命中后由调用方按自己的协议收尾。这样
AC-16 的「不改协议事件结构」是结构上成立的，而不是靠守卫尽量模仿。

**AC-16 的三个约束如何满足**：
1. *在完整词交付前阻断* —— 每个 delta 交付前先喂 scanner；命中则该 delta
   不转发，上游后续分片也不再转发。
2. *保留已交付前缀* —— 已发出的帧不撤回，只停止后续。
3. *保持原协议事件顺序* —— 不注入新事件类型，收尾帧沿用调用方自己的形状
   （chat 是 finish_reason=stop → [DONE]，responses 是 response.completed）。

只扫描最终回答 delta。thinking / 工具文本绝不喂进来。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from bisheng.sensitive_word.domain.schemas import SensitiveWordBusinessType
from bisheng.sensitive_word.domain.services.stream_scanner import StreamContentSafetyScanner

# 与输入侧共用同一组额外元数据，日志/审计可按 stage 区分。
CONTENT_SAFETY_EXTRA = {"content_safety": True, "stage": "output"}


class DshOutputGuard:
    """DSH 流式输出守卫。

    ``feed`` 返回 ``None`` 表示放行；返回字符串表示命中，该字符串即阻断话术，
    调用方应立即收尾并停止读取上游。
    """

    def __init__(self, tenant_id: int) -> None:
        self.tenant_id = tenant_id
        self._scanner = StreamContentSafetyScanner(
            tenant_id, SensitiveWordBusinessType.DSH_CHAT, check_interval=1
        )
        self._blocked: str | None = None

    @property
    def blocked(self) -> str | None:
        return self._blocked

    def feed(self, text: str | None) -> str | None:
        if self._blocked is not None or not text:
            return None
        result = self._scanner.feed(text)
        if result is not None:
            self._blocked = result.auto_reply or ""
            return self._blocked
        return None

    def finish(self) -> str | None:
        """上游结束后再查一次：短于 check_interval 的尾部也要覆盖。"""
        if self._blocked is not None:
            return self._blocked
        result = self._scanner.finish()
        if result is not None:
            self._blocked = result.auto_reply or ""
            return self._blocked
        return None


def make_output_guard(tenant_id: int) -> DshOutputGuard | None:
    """Pro 未启用或策略未启用时返回 None，调用方整体透传。"""
    from bisheng.sensitive_word.domain.services.sensitive_word_policy_service import (
        SensitiveWordPolicyService,
    )

    if not SensitiveWordPolicyService.is_dsh_content_safety_active(tenant_id):
        return None
    return DshOutputGuard(tenant_id)


def blocked_chat_delta(auto_reply: str) -> dict:
    """命中后的 chat delta：一个内容 delta，由调用方拼进既有帧结构。"""
    return {"content": auto_reply}


def blocked_responses_frames(auto_reply: str) -> list[str]:
    """命中后的 responses 收尾帧：delta + done + completed，顺序与上游一致。"""
    item_id = "msg_" + str(id(auto_reply))
    completed = {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "created_at": int(datetime.now(UTC).timestamp()),
            "output": [
                {
                    "id": item_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": auto_reply, "annotations": []}],
                }
            ],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "content_safety": dict(CONTENT_SAFETY_EXTRA),
        },
    }
    frames = [
        "event: response.output_text.delta\n"
        + _sse(
            {
                "type": "response.output_text.delta",
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "delta": auto_reply,
            }
        ),
        "event: response.output_text.done\n"
        + _sse(
            {
                "type": "response.output_text.done",
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "text": auto_reply,
            }
        ),
        "event: response.completed\n" + _sse(completed),
    ]
    return frames


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"


async def guard_chat_completion(payload: dict, *, tenant_id: int) -> dict:
    """非流式 chat.completion：命中则整体替换为阻断话术，否则原对象返回。"""
    if not isinstance(payload, dict):
        return payload
    guard = make_output_guard(tenant_id)
    if guard is None:
        return payload
    message = (payload.get("choices") or [{}])[0].get("message") or {}
    content = message.get("content")
    if not content:
        return payload
    reply = guard.feed(content) or guard.finish()
    if reply is None:
        return payload
    return {
        **payload,
        "choices": [
            {
                **payload["choices"][0],
                "message": {**message, "content": reply, "content_safety": dict(CONTENT_SAFETY_EXTRA)},
            }
        ],
    }


async def guard_responses_completion(payload: dict, *, tenant_id: int) -> dict:
    """非流式 /responses：命中则替换 output 文本，否则原对象返回。"""
    if not isinstance(payload, dict):
        return payload
    guard = make_output_guard(tenant_id)
    if guard is None:
        return payload
    outputs = payload.get("output")
    if not isinstance(outputs, list) or not outputs:
        return payload
    texts = [
        part.get("text", "")
        for item in outputs
        if isinstance(item, dict) and item.get("type") == "message"
        for part in item.get("content") or []
        if isinstance(part, dict) and part.get("type") == "output_text"
    ]
    if not any(texts):
        return payload
    reply = guard.feed("".join(texts)) or guard.finish()
    if reply is None:
        return payload
    return {
        **payload,
        "output": [
            {
                **item,
                "content": [
                    {**part, "text": reply, "content_safety": dict(CONTENT_SAFETY_EXTRA)}
                    if isinstance(part, dict) and part.get("type") == "output_text"
                    else part
                    for part in (item.get("content") or [])
                ],
            }
            if isinstance(item, dict)
            else item
            for item in outputs
        ],
    }
