"""DSH 输入安全：命中时的伪装回答（F120 T018，AC-15）。

**为什么是「伪装回答」而不是错误**：DSH 是 OpenAI 兼容面，客户端按协议消费
`choices`。返回 4xx 会让客户端把「被内容安全策略拦了」当成网络/服务故障，
而工作站 / 频道 / DSH 同属人类对话面，命中语义一律是伪装回答（design §5.4
语义分界；AC-15 / AC-24）。

**为什么不碰模型执行**：判定发生在端点、`service.complete` 之前。命中即短路，
天然不发起工具调用、不消耗额度（AC-15「工具调用与后续模型执行不继续」）。

响应形状刻意复刻 `DshModelService.complete` 的成功返回：非流式是
`{id, object, created, model, choices, usage}`，流式是 OpenAI chunk 序列。
客户端不需要为「被拦」走任何特殊分支。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

# 与 workstation 一致：额外元数据落在同一处，供审计/回溯识别这是安全拦截而非模型输出。
CONTENT_SAFETY_EXTRA = {"content_safety": True, "stage": "input"}


def blocked_chat_completion(*, model: str, auto_reply: str, request_id: str) -> dict:
    """非流式 chat.completions 的命中回答。"""
    return {
        "id": "chatcmpl-" + request_id,
        "object": "chat.completion",
        "created": int(datetime.now(UTC).timestamp()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": auto_reply},
                "finish_reason": "stop",
                # 非标准字段：客户端忽略，日志与 block record 用得上。
                "content_safety": dict(CONTENT_SAFETY_EXTRA),
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def blocked_responses_completion(*, model: str, auto_reply: str, request_id: str) -> dict:
    """非流式 /responses 的命中回答（Responses 协议形态）。"""
    message_id = "msg_" + uuid.uuid4().hex
    return {
        "id": "resp_" + request_id,
        "object": "response",
        "created_at": int(datetime.now(UTC).timestamp()),
        "model": model,
        "status": "completed",
        "output": [
            {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": auto_reply, "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "content_safety": dict(CONTENT_SAFETY_EXTRA),
    }


def _sse(data: dict) -> str:
    return "data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n\n"


async def iter_blocked_chat_stream(*, model: str, auto_reply: str, request_id: str) -> AsyncIterator[str]:
    """流式 chat.completions 的命中回答：一个 role chunk + 一个内容 chunk + 收尾。"""
    common = {
        "id": "chatcmpl-" + request_id,
        "object": "chat.completion.chunk",
        "created": int(datetime.now(UTC).timestamp()),
        "model": model,
    }
    yield _sse({**common, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
    yield _sse({**common, "choices": [{"index": 0, "delta": {"content": auto_reply}, "finish_reason": None}]})
    yield _sse({**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    yield "data: [DONE]\n\n"


async def iter_blocked_responses_stream(*, model: str, auto_reply: str, request_id: str) -> AsyncIterator[str]:
    """流式 /responses 的命中回答：保持 response.output_text.delta 事件顺序。"""
    response_id = "resp_" + request_id
    item_id = "msg_" + uuid.uuid4().hex
    created_at = int(datetime.now(UTC).timestamp())

    def _event(name: str, **payload) -> str:
        body = {"type": name, "sequence_number": _event.counter, **payload}
        _event.counter += 1
        return _sse(body)

    _event.counter = 0
    yield _event("response.created", response={"id": response_id, "model": model, "created_at": created_at})
    yield _event(
        "response.output_item.added",
        output_index=0,
        item={"id": item_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []},
    )
    yield _event(
        "response.content_part.added",
        item_id=item_id,
        output_index=0,
        content_index=0,
        part={"type": "output_text", "text": "", "annotations": []},
    )
    yield _event(
        "response.output_text.delta",
        item_id=item_id,
        output_index=0,
        content_index=0,
        delta=auto_reply,
    )
    yield _event(
        "response.output_text.done",
        item_id=item_id,
        output_index=0,
        content_index=0,
        text=auto_reply,
    )
    yield _event(
        "response.content_part.done",
        item_id=item_id,
        output_index=0,
        content_index=0,
        part={"type": "output_text", "text": auto_reply, "annotations": []},
    )
    yield _event(
        "response.output_item.done",
        output_index=0,
        item={
            "id": item_id,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": auto_reply, "annotations": []}],
        },
    )
    yield _event(
        "response.completed",
        response={
            "id": response_id,
            "model": model,
            "created_at": created_at,
            "status": "completed",
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
    )
