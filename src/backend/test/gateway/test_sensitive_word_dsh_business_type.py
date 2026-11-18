"""T005 — ``dsh_chat`` as a first-class sensitive-word business type.

F120 reuses the F111 sensitive-word engine for DSH chat, so DSH needs its own
``business_type`` row. The risk in "just add an enum member" is that the row
namespace silently collides with, or bleeds into, the existing three types —
the unique key is ``(tenant_id, business_type, scope_type, scope_id,
logic_delete)``, so a wrong key ordering would make a DSH policy overwrite a
workbench one.

What this file pins:

1. the new value exists and is accepted wherever the existing three are;
2. the scope/cache key namespace keeps the four business types *distinct* —
   a DSH policy never answers for workbench chat and vice versa;
3. the existing three types behave exactly as before (same defaults, same
   cache keys, same checks) — a pure addition, no behaviour drift.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bisheng.sensitive_word.domain.models.sensitive_word_policy import (
    SensitiveWordPolicy,
    SensitiveWordPolicyDao,
)
from bisheng.sensitive_word.domain.schemas import (
    SensitiveWordBusinessType,
    SensitiveWordPolicyResp,
    SensitiveWordScopeType,
)
from bisheng.sensitive_word.domain.services.sensitive_word_policy_service import (
    DEFAULT_AUTO_REPLY,
    SensitiveWordPolicyService,
)

EXISTING_TYPES = [
    SensitiveWordBusinessType.KNOWLEDGE_SPACE_FILE_PARSE,
    SensitiveWordBusinessType.CHANNEL_ARTICLE,
    SensitiveWordBusinessType.WORKBENCH_CHAT,
]


def test_dsh_chat_value_exists_and_is_stable():
    assert SensitiveWordBusinessType.DSH_CHAT.value == "dsh_chat"


def test_all_five_types_are_accepted_by_the_enum():
    values = {item.value for item in SensitiveWordBusinessType}
    assert {
        "knowledge_space_file_parse",
        "channel_article",
        "workbench_chat",
        "dsh_chat",
        "open_api_chat",
    } == values


def test_scope_statement_separates_dsh_chat_from_the_existing_three():
    """The DAO's scope statement is the only place the row is looked up; if
    ``business_type`` were dropped from it, a DSH policy would answer for every
    business type."""
    stmts = {
        business_type.value: SensitiveWordPolicyDao._scope_stmt(
            tenant_id=1,
            business_type=business_type.value,
        )
        for business_type in [*EXISTING_TYPES, SensitiveWordBusinessType.DSH_CHAT]
    }
    compiled = {name: str(stmt.compile(compile_kwargs={"literal_binds": True})) for name, stmt in stmts.items()}
    # Every statement names its own business_type.
    for name, sql in compiled.items():
        assert f"'{name}'" in sql, f"{name} scope statement lost its business_type filter"
    # And no two are the same query.
    assert len(set(compiled.values())) == 4, compiled


def test_cache_key_separates_dsh_chat_from_the_existing_three():
    """A shared automaton cache keyed without ``business_type`` would hand a DSH
    chat the workbench chat's word list (and its enabled flag)."""
    keys = {}
    for business_type in [*EXISTING_TYPES, SensitiveWordBusinessType.DSH_CHAT]:
        policy = SensitiveWordPolicy(
            tenant_id=1,
            business_type=business_type.value,
            scope_type="tenant",
            scope_id="1",
            enabled=True,
            words_types=["custom"],
        )
        keys[business_type.value] = SensitiveWordPolicyService._build_cache_key(
            1,
            business_type.value,
            "tenant",
            "1",
            policy,
            ["词A"],
            case_sensitive=False,
        )
    assert len({key[1] for key in keys.values()}) == 4, keys
    assert len(set(keys.values())) == 4, "two business types produced the same cache key"


def test_default_response_shape_is_identical_across_types():
    """The DSH face reuses the same response schema; adding the type must not
    change the default (disabled, no hits, non-empty auto_reply)."""
    for business_type in [*EXISTING_TYPES, SensitiveWordBusinessType.DSH_CHAT]:
        resp = SensitiveWordPolicyService.default_response(1, business_type.value)
        assert isinstance(resp, SensitiveWordPolicyResp)
        assert resp.business_type == business_type
        assert resp.scope_type == SensitiveWordScopeType.TENANT
        assert resp.enabled is False
        assert resp.auto_reply


def test_existing_types_still_check_with_their_own_words(monkeypatch):
    """Behaviour of the three existing types is unchanged: same hit counts,
    same auto_reply, same enabled flag."""
    seen: dict[str, str] = {}

    def _get_policy(**kwargs):
        seen[kwargs["business_type"]] = kwargs.get("scope_id")
        return SimpleNamespace(
            enabled=True,
            words_types=["custom"],
            custom_words="敏感词",
            auto_reply="命中",
            extra_config={},
        )

    monkeypatch.setattr(
        "bisheng.sensitive_word.domain.models.sensitive_word_policy.SensitiveWordPolicyDao.get_policy",
        _get_policy,
    )

    for business_type in EXISTING_TYPES:
        result = SensitiveWordPolicyService.check_text(
            tenant_id=1,
            business_type=business_type,
            text="这里有敏感词",
        )
        assert result.enabled is True
        assert [(hit.word, hit.count) for hit in result.hits] == [("敏感词", 1)]
        assert result.auto_reply == "命中"

    assert set(seen) == {item.value for item in EXISTING_TYPES}
    assert set(seen.values()) == {"1"}


def test_dsh_chat_checks_with_its_own_words(monkeypatch):
    monkeypatch.setattr(
        "bisheng.sensitive_word.domain.models.sensitive_word_policy.SensitiveWordPolicyDao.get_policy",
        lambda **kwargs: SimpleNamespace(
            enabled=True,
            words_types=["custom"],
            custom_words="违规词",
            auto_reply="回答已被拦截",
            extra_config={},
        ),
    )

    result = SensitiveWordPolicyService.check_text(
        tenant_id=1,
        business_type=SensitiveWordBusinessType.DSH_CHAT,
        text="这里违规词出现",
    )
    assert result.enabled is True
    assert [(hit.word, hit.count) for hit in result.hits] == [("违规词", 1)]
    assert result.auto_reply == "回答已被拦截"


def test_disabled_dsh_chat_policy_passes_text_through(monkeypatch):
    monkeypatch.setattr(
        "bisheng.sensitive_word.domain.models.sensitive_word_policy.SensitiveWordPolicyDao.get_policy",
        lambda **kwargs: SimpleNamespace(
            enabled=False,
            words_types=["custom"],
            custom_words="违规词",
            auto_reply="",
            extra_config={},
        ),
    )

    result = SensitiveWordPolicyService.check_text(
        tenant_id=1,
        business_type=SensitiveWordBusinessType.DSH_CHAT,
        text="这里违规词出现",
    )
    assert result.enabled is False
    assert result.hits == []


def test_dsh_chat_without_policy_row_is_inert(monkeypatch):
    """No row at all → nothing to enforce. The gateway must treat this as
    pass-through, not as fail-closed."""
    monkeypatch.setattr(
        "bisheng.sensitive_word.domain.models.sensitive_word_policy.SensitiveWordPolicyDao.get_policy",
        lambda **kwargs: None,
    )

    result = SensitiveWordPolicyService.check_text(
        tenant_id=1,
        business_type=SensitiveWordBusinessType.DSH_CHAT,
        text="任意文本",
    )
    assert result.enabled is False
    assert result.hits == []


def _stored_policy(business_type: SensitiveWordBusinessType, *, auto_reply: str = ""):
    return SimpleNamespace(
        tenant_id=1,
        business_type=business_type.value,
        scope_type="tenant",
        scope_id="1",
        enabled=True,
        words_types=["custom"],
        custom_words="词",
        auto_reply=auto_reply,
        extra_config={},
    )


def test_workbench_chat_validation_still_guards_its_own_fields():
    """F111's workbench rules (words_types required, auto_reply required and
    ≤500 chars, only when enabling) must keep rejecting the same payloads —
    the DSH type must not have loosened them."""
    from bisheng.sensitive_word.domain.schemas import SensitiveWordPolicyPayload

    base = {"words_types": [], "custom_words": "", "auto_reply": "", "extra_config": {}}

    with pytest.raises(ValueError):
        SensitiveWordPolicyService._validate_enabled_workbench_payload(
            SensitiveWordPolicyPayload(enabled=True, **base),
            [],
        )

    with pytest.raises(ValueError):
        SensitiveWordPolicyService._validate_enabled_workbench_payload(
            SensitiveWordPolicyPayload(enabled=True, **{**base, "words_types": ["custom"], "auto_reply": ""}),
            ["custom"],
        )

    with pytest.raises(ValueError):
        SensitiveWordPolicyService._validate_enabled_workbench_payload(
            SensitiveWordPolicyPayload(enabled=True, **{**base, "auto_reply": "x" * 501}),
            ["custom"],
        )

    # Disabled payloads are exempt — unchanged behaviour.
    SensitiveWordPolicyService._validate_enabled_workbench_payload(
        SensitiveWordPolicyPayload(enabled=False, **base),
        [],
    )


@pytest.mark.asyncio
async def test_dsh_chat_upsert_skips_workbench_only_validation(monkeypatch):
    """DSH is a different face: it must not inherit workbench's required-field
    rules, and an empty auto_reply falls back to the generic default."""
    from bisheng.sensitive_word.domain.schemas import SensitiveWordPolicyPayload

    captured: dict = {}

    async def _aupsert(**kwargs):
        captured.update(kwargs)
        return _stored_policy(SensitiveWordBusinessType.DSH_CHAT, auto_reply=kwargs["auto_reply"])

    monkeypatch.setattr(SensitiveWordPolicyDao, "aupsert_policy", _aupsert)
    monkeypatch.setattr(SensitiveWordPolicyService, "clear_cache", classmethod(lambda cls: None))

    login_user = SimpleNamespace(user_id=7, tenant_id=1)
    resp = await SensitiveWordPolicyService.aupsert_policy(
        login_user,
        SensitiveWordBusinessType.DSH_CHAT,
        SensitiveWordPolicyPayload(enabled=True, words_types=["custom"], custom_words="违规词"),
    )

    assert captured["business_type"] == "dsh_chat"
    assert captured["enabled"] is True
    # Empty auto_reply → the generic default, not workbench's chat wording.
    assert captured["auto_reply"] == DEFAULT_AUTO_REPLY
    assert resp.business_type == SensitiveWordBusinessType.DSH_CHAT
    assert resp.auto_reply == DEFAULT_AUTO_REPLY


@pytest.mark.asyncio
async def test_workbench_chat_upsert_still_uses_its_own_default(monkeypatch):
    """The workbench face keeps its own auto_reply wording and its own
    validation — proof that adding DSH did not re-route the existing type."""
    from bisheng.sensitive_word.domain.schemas import SensitiveWordPolicyPayload

    captured: dict = {}

    async def _aupsert(**kwargs):
        captured.update(kwargs)
        return _stored_policy(SensitiveWordBusinessType.WORKBENCH_CHAT, auto_reply=kwargs["auto_reply"])

    monkeypatch.setattr(SensitiveWordPolicyDao, "aupsert_policy", _aupsert)
    monkeypatch.setattr(SensitiveWordPolicyService, "clear_cache", classmethod(lambda cls: None))

    login_user = SimpleNamespace(user_id=7, tenant_id=1)
    await SensitiveWordPolicyService.aupsert_policy(
        login_user,
        SensitiveWordBusinessType.WORKBENCH_CHAT,
        SensitiveWordPolicyPayload(
            enabled=True,
            words_types=["custom"],
            custom_words="违规词",
            auto_reply="  对话内容不合规  ",
        ),
    )

    # Stripped, not padded with the generic default.
    assert captured["auto_reply"] == "对话内容不合规"

    with pytest.raises(ValueError):
        await SensitiveWordPolicyService.aupsert_policy(
            login_user,
            SensitiveWordBusinessType.WORKBENCH_CHAT,
            SensitiveWordPolicyPayload(enabled=True, words_types=[], custom_words="违规词"),
        )
