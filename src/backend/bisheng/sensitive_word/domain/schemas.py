from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class SensitiveWordBusinessType(str, Enum):
    KNOWLEDGE_SPACE_FILE_PARSE = "knowledge_space_file_parse"
    CHANNEL_ARTICLE = "channel_article"
    WORKBENCH_CHAT = "workbench_chat"
    #: F120 native gateway — DSH (client-side) chat. Reuses the F111 engine; the
    #: row namespace is separate from the three above because the unique key is
    #: (tenant_id, business_type, scope_type, scope_id, logic_delete) — sharing a
    #: business_type would make one face's policy overwrite another's.
    DSH_CHAT = "dsh_chat"


class SensitiveWordScopeType(str, Enum):
    TENANT = "tenant"


SensitiveWordType = Literal["builtin", "custom"]


class SensitiveWordPolicyPayload(BaseModel):
    enabled: bool = False
    words_types: list[SensitiveWordType] = Field(default_factory=list)
    custom_words: str = ""
    auto_reply: str = ""
    extra_config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("words_types")
    @classmethod
    def dedupe_words_types(cls, value: list[str]) -> list[str]:
        allowed = {"builtin", "custom"}
        result: list[str] = []
        for item in value or []:
            if item in allowed and item not in result:
                result.append(item)
        return result


class SensitiveWordPolicyResp(SensitiveWordPolicyPayload):
    tenant_id: int
    business_type: SensitiveWordBusinessType
    scope_type: SensitiveWordScopeType = SensitiveWordScopeType.TENANT
    scope_id: str


class SensitiveWordHit(BaseModel):
    word: str
    count: int


class SensitiveWordCheckResult(BaseModel):
    enabled: bool = False
    hits: list[SensitiveWordHit] = Field(default_factory=list)
    auto_reply: str | None = None
