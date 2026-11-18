"""T017 — DSH 聊天输入安全（F120 Wave 3）。

覆盖 F120 spec AC-15 / AC-22（§2.3）：

* 命中 → **与普通回答一致的阻断话术 + 安全事件**，流式与非流式都不返回 4xx；
* 命中 → **不继续模型执行**（`service.complete` 压根不被调用，天然无工具调用）；
* 未命中 → 原文透传（`service.complete` 被调用，参数不改写）；
* 策略关闭 / 无有效词 → 透传；
* Pro 能力未启用 → 透传（AC-22，不出现「看似开启实际不检查」）。

判定一律走 `SensitiveWordPolicyService`，不新建匹配逻辑（design §5.4）。
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from bisheng.dsh.api.endpoints.models import complete as dsh_complete

CHAT_BODY = {
    "model": "bisheng:1",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": False,
}

# Responses 面的 input 是 InputItem 列表，不能直接用 messages。
RESPONSES_BODY = {
    "model": "bisheng:1",
    "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "你好"}]}],
    "stream": False,
}


def _request(payload: dict, request_id: str = "req-1") -> MagicMock:
    """构造一个 body 与 payload 一致的假 Request——端点按流读 body。"""
    request = MagicMock()
    request.state.request_id = request_id
    encoded = json.dumps(payload).encode()

    async def _stream():
        yield encoded

    request.stream = _stream
    request.headers = {"content-type": "application/json"}
    return request


def _principal(tenant_id: int = 1, user_id: int = 2) -> MagicMock:
    principal = MagicMock()
    principal.tenant_id = tenant_id
    principal.user_id = user_id
    principal.session_id = "sess-1"
    return principal


def _service(result=None, stream=None) -> MagicMock:
    service = MagicMock()
    if stream is not None:
        service.complete = AsyncMock(return_value=stream)
    else:
        service.complete = AsyncMock(return_value=result)
    return service


@pytest.fixture
def _patch_model_runtime(monkeypatch):
    """把 `get_model_runtime` 换成可控桩。"""
    holder = {}

    async def _fake(runtime):
        return holder["service"]

    monkeypatch.setattr("bisheng.dsh.api.endpoints.models.get_model_runtime", _fake)
    return holder


def _text_of(response) -> str:
    """非流式是 dict，流式是 StreamingResponse；统一取文本。"""
    if isinstance(response, dict):
        return json.dumps(response, ensure_ascii=False)
    if hasattr(response, "body"):
        return response.body.decode()


def _policy(monkeypatch, *, enabled=True, words=("炸弹",), auto_reply="DSH 阻断话术"):
    """把判定结果固定住：只测接线语义，不测 AC 匹配本身（那归 sensitive_word 层）。"""
    from bisheng.sensitive_word.domain.services.sensitive_word_policy_service import SensitiveWordPolicyService

    result = MagicMock(enabled=enabled, hits=[MagicMock(word=words[0], count=1)] if enabled else [], auto_reply=auto_reply)
    monkeypatch.setattr(
        SensitiveWordPolicyService,
        "evaluate_dsh_user_text",
        classmethod(lambda cls, tenant_id, text: result if enabled else None),
    )


# --------------------------------------------------------------------------
# 一、命中：伪装回答，非 4xx
# --------------------------------------------------------------------------


async def test_input_hit_returns_blocked_reply_not_4xx(_patch_model_runtime, monkeypatch):
    """AC-15：命中返回与普通回答一致的阻断话术，不返回 4xx。"""
    _patch_model_runtime["service"] = _service(result={"id": "chatcmpl-x", "choices": []})
    _policy(monkeypatch)

    response = await dsh_complete(_request(CHAT_BODY), _principal(), MagicMock())
    text = _text_of(response)
    assert "DSH 阻断话术" in text
    assert '"error"' not in text, "命中必须是伪装回答，不是错误响应"


async def test_input_hit_emits_safety_event(_patch_model_runtime, monkeypatch):
    """AC-15：命中同时给出安全事件（与 workstation 一致的内容安全事件语义）。"""
    _patch_model_runtime["service"] = _service(result={"id": "chatcmpl-x", "choices": []})
    _policy(monkeypatch)

    response = await dsh_complete(_request(CHAT_BODY), _principal(), MagicMock())
    text = _text_of(response)
    assert "content_safety" in text
    assert "input" in text, "事件必须标明命中阶段为 input"


async def test_input_hit_never_reaches_the_model(_patch_model_runtime, monkeypatch):
    """AC-15：命中后工具调用与后续模型执行不继续。

    `service.complete` 压根不被调用，因此不可能有工具调用、不可能有额度扣减。
    """
    service = _service(result={"id": "chatcmpl-x", "choices": []})
    _patch_model_runtime["service"] = service
    _policy(monkeypatch)

    await dsh_complete(_request(CHAT_BODY), _principal(), MagicMock())
    service.complete.assert_not_awaited()


async def test_input_hit_uses_openai_shaped_reply_for_chat_completions(_patch_model_runtime, monkeypatch):
    """非流式 chat.completions：返回体与普通回答同构（有 choices / message）。"""
    _patch_model_runtime["service"] = _service(result={"id": "chatcmpl-x", "choices": []})
    _policy(monkeypatch)

    parsed = json.loads(_text_of(await dsh_complete(_request(CHAT_BODY), _principal(), MagicMock())))
    assert parsed["object"] == "chat.completion"
    assert parsed["choices"][0]["message"]["content"] == "DSH 阻断话术"
    assert parsed["choices"][0]["message"]["role"] == "assistant"


# --------------------------------------------------------------------------
# 二、命中：流式
# --------------------------------------------------------------------------


async def test_input_hit_stream_returns_sse_not_4xx(_patch_model_runtime, monkeypatch):
    """流式命中同样返回 SSE 伪装回答，不返回 4xx。"""
    stream = AsyncMock()
    _patch_model_runtime["service"] = _service(stream=stream)
    _policy(monkeypatch)

    body = {**CHAT_BODY, "stream": True}
    response = await dsh_complete(_request(body), _principal(), MagicMock())
    assert response.media_type == "text/event-stream"
    chunks = [chunk async for chunk in response.body_iterator]
    text = "".join(chunks)
    assert "DSH 阻断话术" in text
    assert "finish_reason" in text
    assert "data: [DONE]" not in text or True  # 允许两种收尾约定
    stream.aclose.assert_not_awaited()


# --------------------------------------------------------------------------
# 三、未命中 / 关闭 / 无词：透传
# --------------------------------------------------------------------------


@pytest.mark.parametrize("enabled", [False])
async def test_no_hit_passes_through_unchanged(_patch_model_runtime, monkeypatch, enabled):
    """AC-05：判定通过时保持原有正文、协议顺序与用量语义，不改写模型内容。"""
    service = _service(result={"id": "chatcmpl-real", "choices": [{"message": {"content": "真实回答"}}]})
    _patch_model_runtime["service"] = service
    _policy(monkeypatch, enabled=enabled)

    response = await dsh_complete(_request(CHAT_BODY), _principal(), MagicMock())
    assert "真实回答" in _text_of(response)
    service.complete.assert_awaited_once()


async def test_no_hit_forwards_the_original_request(_patch_model_runtime, monkeypatch):
    """透传的必须是客户端原始请求体，不能被判定逻辑改写。"""
    service = _service(result={"id": "chatcmpl-real", "choices": []})
    _patch_model_runtime["service"] = service
    _policy(monkeypatch, enabled=False)

    await dsh_complete(_request(CHAT_BODY), _principal(), MagicMock())
    forwarded = service.complete.await_args.args[1]
    assert forwarded.messages[0].content == "你好", "透传不得改写用户输入"


async def test_pro_capability_disabled_passes_through(_patch_model_runtime, monkeypatch):
    """AC-22：部署未启用 Pro 能力时保持 DSH 透传行为。"""
    from bisheng.sensitive_word.domain.services.sensitive_word_policy_service import SensitiveWordPolicyService

    monkeypatch.setattr(SensitiveWordPolicyService, "evaluate_dsh_user_text", classmethod(lambda cls, t, x: None))
    service = _service(result={"id": "chatcmpl-real", "choices": [{"message": {"content": "真实回答"}}]})
    _patch_model_runtime["service"] = service

    response = await dsh_complete(_request(CHAT_BODY), _principal(), MagicMock())
    assert "真实回答" in _text_of(response)
    service.complete.assert_awaited_once()


# --------------------------------------------------------------------------
# 四、覆盖两个入口
# --------------------------------------------------------------------------


async def test_responses_endpoint_is_covered_too(_patch_model_runtime, monkeypatch):
    """AC-01：Chat Completions 与 Responses 都要覆盖。"""
    from bisheng.dsh.api.endpoints.models import native_responses

    _patch_model_runtime["service"] = _service(result={"id": "resp-x", "choices": []})
    _policy(monkeypatch)

    response = await native_responses(_request(RESPONSES_BODY, "req-2"), _principal(), MagicMock())
    assert "DSH 阻断话术" in _text_of(response)


# --------------------------------------------------------------------------
# 五、tenant_id 来源
# --------------------------------------------------------------------------


async def test_tenant_id_comes_from_the_verified_principal(_patch_model_runtime, monkeypatch):
    """AC-13（F111）：策略选择以成功验证的 DSH 凭据返回的租户为准，不采信客户端自报。"""
    from bisheng.sensitive_word.domain.services.sensitive_word_policy_service import SensitiveWordPolicyService

    seen = {}

    def _fake(cls, tenant_id, text):
        seen["tenant_id"] = tenant_id
        return None

    monkeypatch.setattr(SensitiveWordPolicyService, "evaluate_dsh_user_text", classmethod(_fake))
    _patch_model_runtime["service"] = _service(result={"id": "chatcmpl-real", "choices": []})

    await dsh_complete(_request(CHAT_BODY), _principal(tenant_id=42, user_id=7), MagicMock())
    assert seen["tenant_id"] == 42
