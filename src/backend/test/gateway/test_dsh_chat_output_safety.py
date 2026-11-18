"""T019 — DSH 流式输出安全（F120 Wave 3）。

覆盖 F120 spec AC-16（§2.3）。断言对象是**客户端真正收到的字节**：测试直接驱动
`DshModelService._stream` / `_stream_responses`（T020 改的就是这两个方法），
策略层打桩、守卫与 scanner 走真实代码路径。

* 命中词**跨分片 / 单个 delta / 长前缀之后**出现时，仍在完整词交付前阻断；
* **保留已交付前缀**（不撤回已发出的分片）；
* **协议事件顺序不变**——不注入新事件类型，收尾仍是 finish_reason/stop → [DONE]；
* 非流式同样覆盖；
* 未命中 / 策略关闭 / Pro 未启用 → 原样透传。

判定复用 `StreamContentSafetyScanner`（不新建匹配逻辑，design §5.4）。
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from bisheng.dsh.domain.schemas.chat import DshChatRequest
from bisheng.dsh.domain.schemas.responses import DshResponsesRequest
from bisheng.dsh.domain.services.access import DshPrincipal
from bisheng.dsh.domain.services.model import DshModelService

TENANT_ID = "2"
MODEL = "bisheng:42"
REQUEST_ID = "req-out-1"
HIT_WORD = "炸弹"
AUTO_REPLY = "DSH 输出阻断话术"

# 命中词到达端侧的三种形态：被拆成多个 delta / 整个词落在一个 delta 里 /
# 前面已经输出了很长一段（越过 workstation 那个 100 字符检查间隔）才出现命中词。
# 线上文本里的 \uXXXX 由 SSE 编解码层还原成真实字符，不需要扫描器自己解码，
# 所以不把「转义序列」单列一种形态。
SPLIT_PLAIN = ["这是一个", "炸", "弹", "演示"]
SPLIT_SINGLE = ["这是一个", "炸弹", "演示"]
LONG_PREFIX = ["铺垫" * 60, "炸", "弹", "演示"]

RESPONSES_TERMINAL = {
    "type": "response.completed",
    "response": {
        "id": "resp_1",
        "status": "completed",
        "output": [
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "这是一个炸弹演示", "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
    },
}


def _principal():
    return DshPrincipal(tenant_id=TENANT_ID, user_id="20", session_id="session")


class FakeExecution:
    """只实现 _stream 用到的面。"""

    def __init__(self, items, *, status="SUCCEEDED", finish_reason="stop"):
        self._items = items
        self.status = status
        self.finish_reason = finish_reason
        self.usage = SimpleNamespace(input_tokens=1, output_tokens=2, total_tokens=3)
        self.provider_request_id = None
        self.failure_code = None
        self.closed = False

    async def stream(self):
        try:
            for item in self._items:
                yield item
        finally:
            self.closed = True


def _event():
    return SimpleNamespace(request_id=REQUEST_ID, started_at=datetime(2026, 9, 9, tzinfo=UTC), usage=None)


@pytest.fixture
def service(monkeypatch):
    """构造一个只跑流式路径的 DshModelService；记账打成空操作。"""

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(DshModelService, "_settle", _noop)
    return DshModelService(
        policy_reader=None,
        model_loader=None,
        llm_builder=None,
        capabilities_for=None,
        usage=None,
        now=lambda: datetime(2026, 9, 9, tzinfo=UTC),
    )


def _patch_policy(monkeypatch, *, active=True, words=(HIT_WORD,)):
    """把策略层打桩：部署开关 + 真匹配。

    单独桩 `check_text` 不足以驱动守卫——`is_dsh_content_safety_active` 是另一条
    调用路径，未启用 Pro 时会先短路成透传。桩必须真匹配：无脑返回 hit 会让第一个
    分片就被拦，从而掩盖「保留已交付前缀」的语义。
    """
    from bisheng.sensitive_word.domain.services import sensitive_word_policy_service as svc_mod
    from bisheng.sensitive_word.domain.services import stream_scanner

    def _check(cls, *, tenant_id, business_type, text):
        matched = any(word in text for word in words)
        return SimpleNamespace(
            enabled=True,
            hits=(HIT_WORD,) if matched else (),
            auto_reply=AUTO_REPLY if matched else "",
        )

    monkeypatch.setattr(stream_scanner.SensitiveWordPolicyService, "check_text", classmethod(_check))
    monkeypatch.setattr(
        svc_mod.SensitiveWordPolicyService,
        "is_dsh_content_safety_active",
        classmethod(lambda cls, tenant_id: active),
    )


def _chat_deltas(chunks):
    return [{"content": chunk} for chunk in chunks]


def _responses_events(chunks):
    events = [{"type": "response.created", "response": {"id": "resp_1"}}]
    events += [{"type": "response.output_text.delta", "item_id": "msg_1", "delta": chunk} for chunk in chunks]
    events.append(RESPONSES_TERMINAL)
    return events


def _chat_text(frames):
    out = []
    for frame in frames:
        if not frame.startswith("data: "):
            continue
        raw = frame[len("data: ") :].strip()
        if raw == "[DONE]":
            continue
        payload = json.loads(raw)
        for choice in payload.get("choices", []):
            out.append(choice.get("delta", {}).get("content", "") or "")
    return "".join(out)


def _responses_text(frames):
    out = []
    for frame in frames:
        if "data: " not in frame:
            continue
        payload = json.loads(frame.split("data: ", 1)[1])
        if payload.get("type") == "response.output_text.delta":
            out.append(payload.get("delta", ""))
    return "".join(out)


# --------------------------------------------------------------------------
# 一、命中：完整词交付前阻断
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chunks",
    [SPLIT_PLAIN, SPLIT_SINGLE, LONG_PREFIX],
    ids=["split-across-deltas", "single-delta", "long-prefix"],
)
async def test_chat_hit_is_blocked_before_the_complete_word_is_delivered(monkeypatch, service, chunks):
    """AC-16：命中词跨分片 / 单 delta / 转义序列，仍在完整词交付前阻断。"""
    _patch_policy(monkeypatch)
    execution = FakeExecution(_chat_deltas(chunks))
    frames = [
        frame
        async for frame in service._stream(
            _principal(), DshChatRequest(model=MODEL, messages=[{"role": "user", "content": "hi"}], stream=True),
            execution,
            _event(),
        )
    ]
    assert HIT_WORD not in _chat_text(frames), f"完整命中词不得交付给客户端：{_chat_text(frames)!r}"


async def test_chat_already_delivered_prefix_is_preserved(monkeypatch, service):
    """AC-16：保留已交付前缀——不撤回已发出的分片。"""
    _patch_policy(monkeypatch)
    execution = FakeExecution(_chat_deltas(SPLIT_PLAIN))
    frames = [
        frame
        async for frame in service._stream(
            _principal(), DshChatRequest(model=MODEL, messages=[{"role": "user", "content": "hi"}], stream=True),
            execution,
            _event(),
        )
    ]
    text = _chat_text(frames)
    assert text.startswith("这是一个"), f"已交付前缀必须保留，实际：{text!r}"


async def test_chat_blocked_reply_replaces_the_remaining_output(monkeypatch, service):
    """命中后以 auto_reply 收尾，客户端拿到的是阻断话术而非模型原文。"""
    _patch_policy(monkeypatch)
    execution = FakeExecution(_chat_deltas(SPLIT_PLAIN))
    frames = [
        frame
        async for frame in service._stream(
            _principal(), DshChatRequest(model=MODEL, messages=[{"role": "user", "content": "hi"}], stream=True),
            execution,
            _event(),
        )
    ]
    assert AUTO_REPLY in _chat_text(frames)


async def test_chat_protocol_event_order_is_unchanged(monkeypatch, service):
    """AC-16：保持原协议事件顺序——role chunk 在前，finish + [DONE] 收尾。"""
    _patch_policy(monkeypatch)
    execution = FakeExecution(_chat_deltas(SPLIT_PLAIN))
    frames = [
        frame
        async for frame in service._stream(
            _principal(), DshChatRequest(model=MODEL, messages=[{"role": "user", "content": "hi"}], stream=True),
            execution,
            _event(),
        )
    ]
    parsed = [json.loads(f[len("data: ") :].strip()) for f in frames if f.startswith("data: ") and "[DONE]" not in f]
    assert "role" in parsed[0]["choices"][0]["delta"], "首个 chunk 必须带 role（与未拦截时同一拼法）"
    assert parsed[-1]["choices"][0]["finish_reason"] == "stop", "收尾必须是 finish_reason=stop"
    assert frames[-1] == "data: [DONE]\n\n", "最后一帧必须是 [DONE]"
    # 不注入新事件类型：所有帧都还是 chat.completion.chunk。
    assert all(p["object"] == "chat.completion.chunk" for p in parsed)


async def test_chat_upstream_frames_after_the_hit_are_dropped(monkeypatch, service):
    """命中后上游剩余分片不再转发。"""
    _patch_policy(monkeypatch)
    execution = FakeExecution(_chat_deltas([*SPLIT_PLAIN, "后续", "内容", "不该", "出现"]))
    frames = [
        frame
        async for frame in service._stream(
            _principal(), DshChatRequest(model=MODEL, messages=[{"role": "user", "content": "hi"}], stream=True),
            execution,
            _event(),
        )
    ]
    text = _chat_text(frames)
    assert "不该" not in text and "出现" not in text


async def test_chat_upstream_is_drained_so_usage_still_settles(monkeypatch, service):
    """命中后仍把上游读干净：模型已生成的 token 照实记账，不变成 usage_unknown。"""
    _patch_policy(monkeypatch)
    execution = FakeExecution(_chat_deltas([*SPLIT_PLAIN, "后续", "内容"]))
    frames = [
        frame
        async for frame in service._stream(
            _principal(), DshChatRequest(model=MODEL, messages=[{"role": "user", "content": "hi"}], stream=True),
            execution,
            _event(),
        )
    ]
    assert execution.closed, "上游必须被读完再关闭"
    assert frames[-1] == "data: [DONE]\n\n"


# --------------------------------------------------------------------------
# 二、未命中 / 关闭 / 无 Pro：透传
# --------------------------------------------------------------------------


async def test_chat_clean_stream_passes_through_unchanged(monkeypatch, service):
    _patch_policy(monkeypatch, words=())
    chunks = ["正常", "回答", "内容"]
    execution = FakeExecution(_chat_deltas(chunks))
    frames = [
        frame
        async for frame in service._stream(
            _principal(), DshChatRequest(model=MODEL, messages=[{"role": "user", "content": "hi"}], stream=True),
            execution,
            _event(),
        )
    ]
    assert _chat_text(frames) == "".join(chunks)
    assert frames[-1] == "data: [DONE]\n\n"
    # 透传时不得改写帧数：role 并入首个内容 chunk，故为 len(chunks) + finish + [DONE]。
    assert len(frames) == len(chunks) + 2


@pytest.mark.parametrize("active", [False], ids=["pro-disabled"])
async def test_chat_pro_disabled_passes_through(monkeypatch, service, active):
    _patch_policy(monkeypatch, active=active)
    chunks = SPLIT_PLAIN
    execution = FakeExecution(_chat_deltas(chunks))
    frames = [
        frame
        async for frame in service._stream(
            _principal(), DshChatRequest(model=MODEL, messages=[{"role": "user", "content": "hi"}], stream=True),
            execution,
            _event(),
        )
    ]
    assert _chat_text(frames) == "".join(chunks)
    assert HIT_WORD in _chat_text(frames), "Pro 未启用时必须透传，包括命中词本身"


# --------------------------------------------------------------------------
# 三、非流式同样覆盖
# --------------------------------------------------------------------------


async def test_non_stream_chat_hit_is_blocked(monkeypatch):
    """AC-16：非流式同样覆盖。"""
    from bisheng.dsh.domain.services.chat_output_safety import guard_chat_completion

    _patch_policy(monkeypatch)
    result = {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "这是一个炸弹演示"}, "finish_reason": "stop"}
        ],
    }
    guarded = await guard_chat_completion(result, tenant_id=int(TENANT_ID))
    assert HIT_WORD not in guarded["choices"][0]["message"]["content"]
    assert guarded["choices"][0]["message"]["content"] == AUTO_REPLY


async def test_non_stream_chat_clean_passes_through(monkeypatch):
    from bisheng.dsh.domain.services.chat_output_safety import guard_chat_completion

    _patch_policy(monkeypatch, words=())
    result = {"choices": [{"message": {"content": "正常回答"}}]}
    guarded = await guard_chat_completion(result, tenant_id=int(TENANT_ID))
    assert guarded is result, "未命中必须原对象返回，不复制"


async def test_non_stream_responses_hit_is_blocked(monkeypatch):
    from bisheng.dsh.domain.services.chat_output_safety import guard_responses_completion

    _patch_policy(monkeypatch)
    guarded = await guard_responses_completion(RESPONSES_TERMINAL["response"], tenant_id=int(TENANT_ID))
    text = guarded["output"][0]["content"][0]["text"]
    assert HIT_WORD not in text
    assert text == AUTO_REPLY


async def test_non_stream_responses_clean_passes_through(monkeypatch):
    from bisheng.dsh.domain.services.chat_output_safety import guard_responses_completion

    _patch_policy(monkeypatch, words=())
    guarded = await guard_responses_completion(RESPONSES_TERMINAL["response"], tenant_id=int(TENANT_ID))
    assert guarded is RESPONSES_TERMINAL["response"], "未命中必须原对象返回，不复制"


# --------------------------------------------------------------------------
# 四、Responses 面流式
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chunks",
    [SPLIT_PLAIN, SPLIT_SINGLE, LONG_PREFIX],
    ids=["split-across-deltas", "single-delta", "long-prefix"],
)
async def test_responses_stream_hit_is_blocked(monkeypatch, service, chunks):
    _patch_policy(monkeypatch)
    execution = FakeExecution(_responses_events(chunks))
    frames = [
        frame
        async for frame in service._stream_responses(
            execution,
            _event(),
            _principal(),
            DshResponsesRequest(
                model=MODEL, input=[{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
            ),
        )
    ]
    text = _responses_text(frames)
    assert HIT_WORD not in text, f"完整命中词不得交付给客户端：{text!r}"
    assert text.startswith(chunks[0]), f"已交付前缀必须保留，实际前 20 字符：{text[:20]!r}"
    assert AUTO_REPLY in text


async def test_responses_stream_event_order_is_unchanged(monkeypatch, service):
    """AC-16：事件顺序不变——response.created 在前，completed 收尾且只有一个。"""
    _patch_policy(monkeypatch)
    execution = FakeExecution(_responses_events(SPLIT_PLAIN))
    frames = [
        frame
        async for frame in service._stream_responses(
            execution,
            _event(),
            _principal(),
            DshResponsesRequest(
                model=MODEL, input=[{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
            ),
        )
    ]
    names = [f.split("\n", 1)[0][len("event: ") :].strip() for f in frames if f.startswith("event: ")]
    assert names[0] == "response.created", "首个事件必须是 response.created"
    assert names[-1] == "response.completed", "收尾必须是 response.completed"
    assert names.count("response.completed") == 1, "命中时不得重复发送上游终态帧"
    # 收尾 completed 里带的 output 也必须是阻断话术，不能漏改。
    final = json.loads(frames[-1].split("data: ", 1)[1])
    assert HIT_WORD not in final["response"]["output"][0]["content"][0]["text"]


async def test_responses_stream_clean_passes_through(monkeypatch, service):
    _patch_policy(monkeypatch, words=())
    chunks = ["正常", "回答"]
    execution = FakeExecution(_responses_events(chunks))
    frames = [
        frame
        async for frame in service._stream_responses(
            execution,
            _event(),
            _principal(),
            DshResponsesRequest(
                model=MODEL, input=[{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
            ),
        )
    ]
    assert _responses_text(frames) == "".join(chunks)
    assert frames[-1].startswith("event: response.completed")
    assert len(frames) == len(_responses_events(chunks)), "透传不得增减帧"
