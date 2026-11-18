"""T057 — 开放 API 模型协议面的内容安全（F120，AC-43 / AC-44）。

这一面的命中契约与 DSH / workstation **相反**，本文件的每一条断言都在钉这个差别：

* 输入命中 → 调用模型**之前**以该面既有错误语义拒绝（`ModelFaceContentBlockedError`，
  OpenAI 形状的 error body），**不是**伪装回答。API 客户端按错误码编程，伪装回答
  会被当成正常输出（AC-43）。
* 流式命中 → 中断响应、保留已交付前缀、保持事件顺序，末帧是同一个错误语义的
  SSE error 事件（200 头早已发出，没有状态码可设）（AC-44）。
* 命中内容**不回传**：error body 里只有策略话术和错误码，没有命中的词。
* 未命中 / 策略关闭 → 原文透传，与 F120 之前逐字节一致。

匹配逻辑不在这里重测——那归 `sensitive_word` 层；这里只钉接线语义，判定用
`SensitiveWordPolicyService` 的桩固定住。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, AIMessageChunk

from test.open_api.model_gateway_fixtures import (
    FakeBishengLLM,
    build_model_face_app,
    capture_records,
    install_catalog,
    install_fake_llm,
    model_row,
    server_row,
    service_account_principal,
    text_chunk,
    usage_chunk,
)

CHAT_PATH = "/api/v2/model/v1/chat/completions"
SENSITIVE = "炸弹"
BLOCKED_REPLY = "本轮回答触发了租户安全策略，已停止。"


@pytest.fixture
def admitted(monkeypatch):
    """认证、目录、LLM 三个桩就位；返回 (records, fake)。"""

    monkeypatch.setattr(
        "bisheng.open_api.api.dependencies.validate_bearer",
        AsyncMock(return_value=service_account_principal()),
    )
    install_catalog(monkeypatch, [server_row(1, "azure-openai")], [model_row(10, 1, "gpt-4o")])
    return capture_records(monkeypatch)


def _input_policy(monkeypatch, *, hits: list | None = None, auto_reply: str = BLOCKED_REPLY):
    """固定输入判定。``hits`` 为 ``[(word, count), ...]``；空列表表示未命中。

    用真的 ``SensitiveWordHit`` 而不是 SimpleNamespace：生产代码会 ``model_dump()``
    它，桩换成鸭子类型会让这条断言在真实对象上失效。
    """

    from bisheng.sensitive_word.domain.schemas import SensitiveWordHit
    from bisheng.sensitive_word.domain.services.sensitive_word_policy_service import (
        SensitiveWordPolicyService,
    )

    hit_objects = [SensitiveWordHit(word=word, count=count) for word, count in (hits or [])]
    result = SimpleNamespace(enabled=bool(hit_objects), hits=hit_objects, auto_reply=auto_reply)
    monkeypatch.setattr(
        SensitiveWordPolicyService,
        "is_open_api_content_safety_active",
        classmethod(lambda cls, tenant_id: True),
    )
    monkeypatch.setattr(
        SensitiveWordPolicyService,
        "evaluate_open_api_user_text",
        classmethod(lambda cls, tenant_id, text: result if hit_objects else None),
    )
    return result


def _output_policy(monkeypatch, *, words: tuple[str, ...] = (SENSITIVE,)):
    """让输出扫描走真的 ``StreamContentSafetyScanner``，只把匹配结果固定住。

    刻意不整块替换扫描器：AC-44 的接线（每个 delta 都查、命中即停、finish 兜底）
    正是要测的东西，换成一个返回命中的桩就什么都证明不了了。
    """

    from bisheng.sensitive_word.domain.schemas import SensitiveWordBusinessType
    from bisheng.sensitive_word.domain.services.sensitive_word_policy_service import (
        SensitiveWordPolicyService,
    )

    def _check(cls, tenant_id, business_type, text, scope_type=None, scope_id=None):
        found = [word for word in words if word in text]
        return SimpleNamespace(
            enabled=bool(found),
            hits=[SimpleNamespace(word=word, count=1) for word in found],
            auto_reply=BLOCKED_REPLY,
        )

    monkeypatch.setattr(SensitiveWordPolicyService, "is_open_api_content_safety_active", classmethod(lambda cls, tenant_id: True))
    monkeypatch.setattr(SensitiveWordPolicyService, "check_text", classmethod(_check))
    # 输入侧放行：否则每个输出用例都会先被输入判定拦掉
    monkeypatch.setattr(SensitiveWordPolicyService, "evaluate_open_api_user_text", classmethod(lambda cls, tenant_id, text: None))
    assert SensitiveWordBusinessType.OPEN_API_CHAT.value == "open_api_chat"


def _inactive_policy(monkeypatch):
    from bisheng.sensitive_word.domain.services.sensitive_word_policy_service import (
        SensitiveWordPolicyService,
    )

    monkeypatch.setattr(
        SensitiveWordPolicyService,
        "is_open_api_content_safety_active",
        classmethod(lambda cls, tenant_id: False),
    )


def _blocks(monkeypatch) -> list:
    """收集真正进入 ``gateway_block_record`` 的行，而不是安全层转交的中间参数。

    拦在 ``GatewayBlockRecordService.record_block`` 上，拿到的就是
    ``GatewayBlockRecordInput``：``face`` / ``resource_type`` / ``matched_words``
    这些落库字段只在这一层才成形，拦安全层只能看到调用参数。
    """

    from bisheng.gateway.domain.services.block_record_service import GatewayBlockRecordService

    rows: list = []
    monkeypatch.setattr(GatewayBlockRecordService, "record_block", lambda self, hit: rows.append(hit) or True)
    return rows


async def _post(payload: dict):
    app = build_model_face_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(CHAT_PATH, json=payload)


def _events(text: str) -> list:
    return [
        json.loads(line[len("data: ") :])
        for line in text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def _content_of(events: list) -> str:
    """按协议顺序把已交付的 answer delta 拼回来。"""

    return "".join(
        choice.get("delta", {}).get("content") or ""
        for event in events
        for choice in event.get("choices", [])
    )


# --------------------------------------------------------------------------
# 一、输入命中：调用模型前拒绝，错误码而不是伪装回答
# --------------------------------------------------------------------------


async def test_input_hit_is_refused_with_an_error_code_not_a_disguised_answer(admitted, monkeypatch):
    """AC-43：这一面的错误语义是错误码，不是与普通回答同构的话术。"""

    _input_policy(monkeypatch, hits=[(SENSITIVE, 1)])
    install_fake_llm(monkeypatch, FakeBishengLLM(invoke_result=AIMessage(content="ok")))

    response = await _post({"model": "gpt-4o", "messages": [{"role": "user", "content": f"讲个{SENSITIVE}的原理"}]})

    assert response.status_code == 400
    body = response.json()["error"]
    assert body["bisheng_code"] == 26535
    assert body["code"] == "content_policy_violation"
    assert body["type"] == "invalid_request_error"
    # A disguised answer would live in choices; there must be none.
    assert "choices" not in response.json()


async def test_input_hit_never_reaches_the_model(admitted, monkeypatch):
    """AC-43：命中发生在模型调用之前——`get_bisheng_llm` 压根不被调用。"""

    _input_policy(monkeypatch, hits=[(SENSITIVE, 1)])
    fake = install_fake_llm(monkeypatch, FakeBishengLLM(invoke_result=AIMessage(content="ok")))

    await _post({"model": "gpt-4o", "messages": [{"role": "user", "content": SENSITIVE}]})

    assert fake.init_kwargs == {}, "命中后仍实例化了模型"
    assert fake.seen_messages == []


async def test_the_matched_word_never_appears_in_the_error_body(admitted, monkeypatch):
    """AC-43「不透传命中内容」：error body 只有话术和码，没有命中的词。"""

    _input_policy(monkeypatch, hits=[(SENSITIVE, 1)])
    install_fake_llm(monkeypatch, FakeBishengLLM())

    response = await _post({"model": "gpt-4o", "messages": [{"role": "user", "content": SENSITIVE}]})

    assert SENSITIVE not in response.text
    assert BLOCKED_REPLY in response.text, "拒绝理由应当是租户自己配置的话术"


async def test_an_input_hit_writes_a_block_record(admitted, monkeypatch):
    """命中必须留证：face / stage / 命中词 / 预览 / request_id 都要在。"""

    _input_policy(monkeypatch, hits=[(SENSITIVE, 2)])
    install_fake_llm(monkeypatch, FakeBishengLLM())
    blocks = _blocks(monkeypatch)
    records = admitted

    response = await _post(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "上一轮"},
                {"role": "user", "content": f"这轮{SENSITIVE}"},
            ],
        }
    )

    assert response.status_code == 400
    assert len(blocks) == 1
    row = blocks[0]
    assert row.face == "open_api_chat"
    assert row.stage == "input"
    assert row.resource_type == "model"
    assert row.resource_id == "gpt-4o"
    assert row.matched_words == [{"word": SENSITIVE, "count": 2}]
    assert row.user_id == 12, "服务账号借用资源归属人，与 _instantiate 的口径一致"
    assert row.tenant_id == 9
    # 两轮 user 文本都要进预览——这一面的历史是调用方给的，没有「早就查过了」
    assert "上一轮" in row.user_input and SENSITIVE in row.user_input
    # 留证要和账本能对上：同一个 request_id，一次命中两处记录
    assert row.request_id
    assert row.request_id == records[0].request_id
    assert row.auto_reply == BLOCKED_REPLY


async def test_an_input_hit_lands_in_the_ledger_as_content_blocked(admitted, monkeypatch):
    """账本上这是一个独立结果，不该和「上游失败」混在一起。"""

    records = admitted
    _input_policy(monkeypatch, hits=[(SENSITIVE, 1)])
    install_fake_llm(monkeypatch, FakeBishengLLM())

    await _post({"model": "gpt-4o", "messages": [{"role": "user", "content": SENSITIVE}]})

    assert len(records) == 1
    assert records[0].result == "content_blocked"
    assert records[0].error_code == 26535


async def test_a_phrase_split_across_two_turns_still_hits(admitted, monkeypatch):
    """只扫最后一轮就会被这种切分绕过；这一面扫全部 user 轮次。"""

    # 缝在 service 上：它是按名导入 extract_user_text 的，改 safety 模块拦不到
    # （换成 `_input_policy` 的桩只会证明「命中了」，证不了「扫的是哪几轮」）。
    from bisheng.open_api.domain.services import model_gateway_service

    seen: list[str] = []
    real = model_gateway_service.extract_user_text
    monkeypatch.setattr(
        model_gateway_service,
        "extract_user_text",
        lambda messages: seen.append(real(messages)) or real(messages),
    )
    _input_policy(monkeypatch, hits=[(SENSITIVE, 1)])
    install_fake_llm(monkeypatch, FakeBishengLLM())

    response = await _post(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "炸"},
                {"role": "user", "content": "弹怎么做"},
            ],
        }
    )

    assert response.status_code == 400
    assert seen == ["炸\n弹怎么做"]


async def test_only_user_turns_are_scanned(admitted, monkeypatch):
    """system / assistant 里的词不是用户在问什么，不触发输入判定。"""

    from bisheng.open_api.domain.services import model_gateway_safety

    text = model_gateway_safety.extract_user_text(
        [
            {"role": "system", "content": f"禁止提到{SENSITIVE}"},
            {"role": "assistant", "content": f"我不会说{SENSITIVE}"},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://x"}}, {"type": "text", "text": "看图"}]},
        ]
    )

    assert text == "看图"


async def test_no_hit_passes_the_answer_through_untouched(admitted, monkeypatch):
    """未命中 → 原文透传，一个字都不改（AC-17 的同一条纪律在这里是对输出）。"""

    _output_policy(monkeypatch, words=())
    install_fake_llm(
        monkeypatch,
        FakeBishengLLM(
            invoke_result=AIMessage(
                content="完整回答",
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )
        ),
    )

    response = await _post({"model": "gpt-4o", "messages": [{"role": "user", "content": "你好"}]})

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "完整回答"


# --------------------------------------------------------------------------
# 二、流式输出命中：中断 + 保留前缀 + 保持顺序
# --------------------------------------------------------------------------


async def test_a_streaming_hit_stops_forwarding_and_keeps_the_prefix(admitted, monkeypatch):
    """AC-44：命中即中断，已交付的前原样保留，后面的内容不透传。"""

    _output_policy(monkeypatch)
    install_fake_llm(
        monkeypatch,
        FakeBishengLLM(stream_script=[text_chunk("前半段安全"), text_chunk(f"中间{SENSITIVE}"), text_chunk("后半段不该出现")]),
    )

    response = await _post({"model": "gpt-4o", "messages": [{"role": "user", "content": "讲"}], "stream": True})

    events = _events(response.text)
    delivered = _content_of(events)
    assert delivered == "前半段安全", "命中词所在的那个 delta 也不能出去"
    assert "后半段不该出现" not in delivered
    assert SENSITIVE not in response.text
    # 前缀是前缀，顺序没变
    assert delivered.startswith("前半段安全")
    assert response.text.endswith("data: [DONE]\n\n")


async def test_a_streaming_hit_ends_in_the_error_event_and_sends_no_finish_reason(admitted, monkeypatch):
    """中断之后没有 finish_reason，也没有 usage 帧——那会是「正常结束」的假象。"""

    _output_policy(monkeypatch)
    install_fake_llm(
        monkeypatch,
        FakeBishengLLM(stream_script=[text_chunk("安全"), text_chunk(f"{SENSITIVE}")]),
    )

    response = await _post({"model": "gpt-4o", "messages": [{"role": "user", "content": "讲"}], "stream": True})

    events = _events(response.text)
    assert events[-1]["error"]["bisheng_code"] == 26535
    assert events[-1]["error"]["code"] == "content_policy_violation"
    assert not any("finish_reason" in choice for event in events[:-1] for choice in event.get("choices", []))
    assert not any("usage" in event for event in events[:-1])
    # 阻断理由仍然给的是租户话术，不是命中的词
    assert BLOCKED_REPLY in events[-1]["error"]["message"]
    assert SENSITIVE not in response.text


async def test_a_streaming_hit_drains_upstream_rather_than_abandoning_it(admitted, monkeypatch):
    """命中后继续抽干上游：这一次调用确实发生了，用量要能落到账本上。

    只看「fake 被调用过」证明不了什么——不抽干也会调用。这里数的是上游生成器
    被拉取到第几个 chunk，以及它有没有跑到自然结束。
    """

    _output_policy(monkeypatch)
    fake = install_fake_llm(
        monkeypatch,
        FakeBishengLLM(
            stream_script=[
                text_chunk("安全"),
                text_chunk(f"{SENSITIVE}"),
                text_chunk("后面"),
                usage_chunk(prompt=11, completion=7),
            ]
        ),
    )
    pulled: list[int] = []
    real_astream = fake.astream

    async def counting_astream(messages, **kwargs):
        index = 0
        async for chunk in real_astream(messages, **kwargs):
            index += 1
            pulled.append(index)
            yield chunk
        pulled.append(0)  # 哨兵：生成器跑到了自然结束

    fake.astream = counting_astream
    records = admitted

    response = await _post({"model": "gpt-4o", "messages": [{"role": "user", "content": "讲"}], "stream": True})

    assert "data: [DONE]" in response.text
    assert 0 in pulled, "上游生成器没有被抽干——连接是半途扔下的"
    assert pulled.count(0) == 1
    # 第四个 chunk（usage）也被读到了，所以账本上的用量不是猜的
    assert records[0].prompt_tokens == 11
    assert records[0].completion_tokens == 7
    assert records[0].total_tokens == 18
    assert records[0].result == "content_blocked"


async def test_a_streaming_hit_writes_a_block_record_holding_the_delivered_prefix(admitted, monkeypatch):
    """流式留证的 user_input 是已交付前缀，运营看到的不是从半个词开始的碎片。"""

    _output_policy(monkeypatch)
    install_fake_llm(
        monkeypatch,
        FakeBishengLLM(stream_script=[text_chunk("安全前半"), text_chunk(f"{SENSITIVE}尾巴")]),
    )
    blocks = _blocks(monkeypatch)
    records = admitted

    await _post({"model": "gpt-4o", "messages": [{"role": "user", "content": "讲"}], "stream": True})

    assert len(blocks) == 1
    row = blocks[0]
    assert row.stage == "output"
    assert row.face == "open_api_chat"
    assert row.user_input == f"安全前半{SENSITIVE}尾巴"
    assert records[0].result == "content_blocked"


async def test_the_word_that_hits_is_the_delta_that_stops(admitted, monkeypatch):
    """命中词所在的那个 delta 本身就出不去，不是等下一个间隔才拦。

    这是 ``check_interval=1`` 的直接后果，也是 AC-44「不透传命中内容」在流式下
    能被满足的唯一方式：间隔一大，短回答在第一次检查之前就已经交付完了。
    """

    _output_policy(monkeypatch)
    install_fake_llm(monkeypatch, FakeBishengLLM(stream_script=[text_chunk("安全安全安全"), text_chunk(f"{SENSITIVE}"), text_chunk("之后的不出现")]))

    response = await _post({"model": "gpt-4o", "messages": [{"role": "user", "content": "讲"}], "stream": True})

    events = _events(response.text)
    assert _content_of(events) == "安全安全安全"
    assert SENSITIVE not in response.text
    assert events[-1]["error"]["bisheng_code"] == 26535


async def test_reasoning_text_is_never_scanned(admitted, monkeypatch):
    """思考链里出现敏感词不是对外披露，扫的是最终回答。"""

    from bisheng.open_api.domain.services import openai_codec

    assert openai_codec.answer_delta_text(
        AIMessageChunk(content="", additional_kwargs={"reasoning_content": f"我在想{SENSITIVE}"})
    ) == ""
    assert openai_codec.answer_delta_text(AIMessageChunk(content="回答")) == "回答"


# --------------------------------------------------------------------------
# 三、非流式输出命中
# --------------------------------------------------------------------------


async def test_a_non_streaming_hit_is_a_plain_refusal(admitted, monkeypatch):
    """非流式整篇在手，命中就是一次普通拒绝——什么都还没交付出去。"""

    _output_policy(monkeypatch)
    install_fake_llm(monkeypatch, FakeBishengLLM(invoke_result=AIMessage(content=f"回答里带{SENSITIVE}")))
    blocks = _blocks(monkeypatch)

    response = await _post({"model": "gpt-4o", "messages": [{"role": "user", "content": "讲"}]})

    assert response.status_code == 400
    assert response.json()["error"]["bisheng_code"] == 26535
    assert SENSITIVE not in response.text
    assert blocks[0].stage == "output"


# --------------------------------------------------------------------------
# 四、策略关闭 / 未启用：与 F120 之前一致
# --------------------------------------------------------------------------


async def test_an_inactive_policy_builds_no_scanner_and_changes_nothing(admitted, monkeypatch):
    """AC-22：未启用就不该有「看似开启实际不检查」——这一步是结构性的，不是配置碰巧。"""

    # 缝在 service 上而不是 safety 模块上：service 是按名导入的，那才是真正
    # 决定「这一轮要不要构造扫描器」的地方。
    from bisheng.open_api.domain.services import model_gateway_service

    _inactive_policy(monkeypatch)
    install_fake_llm(
        monkeypatch,
        FakeBishengLLM(stream_script=[text_chunk(f"完全没{SENSITIVE}也没事"), usage_chunk(prompt=1, completion=1)]),
    )
    built: list = []
    real = model_gateway_service.make_output_scanner
    monkeypatch.setattr(
        model_gateway_service,
        "make_output_scanner",
        lambda tenant_id: built.append(real(tenant_id)) or built[-1],
    )

    response = await _post({"model": "gpt-4o", "messages": [{"role": "user", "content": "讲"}], "stream": True})

    assert built == [None], "未启用时不该构造扫描器"
    events = _events(response.text)
    assert _content_of(events) == f"完全没{SENSITIVE}也没事"
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


async def test_a_clean_stream_is_byte_for_byte_what_it_was_before_f120(admitted, monkeypatch):
    """未命中的流式响应：帧序列与关掉整个安全层时完全相同。

    比的是帧结构而不是原始字节：``id`` 和 ``created`` 每次请求都不同，那是请求
    自己的标识，不是安全层引入的差异。
    """

    script = [text_chunk("你好"), text_chunk("世界"), usage_chunk(prompt=2, completion=2)]
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": True}

    def _frames(text: str) -> list:
        frames = []
        for event in _events(text):
            frames.append({k: v for k, v in event.items() if k not in ("id", "created")})
        return frames

    _output_policy(monkeypatch, words=())
    install_fake_llm(monkeypatch, FakeBishengLLM(stream_script=list(script)))
    with_scanner = await _post(body)

    # 同一条请求，把这一面的策略整体关掉
    _inactive_policy(monkeypatch)
    install_fake_llm(monkeypatch, FakeBishengLLM(stream_script=list(script)))
    without_scanner = await _post(body)

    assert _frames(with_scanner.text) == _frames(without_scanner.text)
    assert with_scanner.text.endswith("data: [DONE]\n\n")
    assert without_scanner.text.endswith("data: [DONE]\n\n")
