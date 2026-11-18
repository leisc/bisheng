"""Content-safety block record contracts (F120 T024, design §5.4).

The record is **evidence**, so the write path and the read path are deliberately
asymmetric:

* the write path takes an input object that is already truncated, because it
  runs on the request hot path and must not become a second copy of user
  content (``user_input`` is a 500-char preview, never the full input);
* the read path returns a projection that carries nothing the query page does
  not show, so a schema change cannot silently widen what an administrator can
  pull out of the table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from bisheng.common.schemas.api import PageInfiniteCursorData
from bisheng.gateway.domain.models.block_record import GatewayBlockFace, GatewayBlockStage

GatewayBlockFaceName = Literal[
    'workstation_chat',
    'dsh_chat',
    'channel_article',
    'knowledge_file',
    'open_api_chat',
    'open_api_model',
]
GatewayBlockStageName = Literal['input', 'output']

#: Keep at most this many matched words per record. The Aho-Corasick automaton
#: returns every occurrence; a long input hitting a broad policy can produce
#: hundreds, and the point of the column is "what matched", not a full census.
MAX_MATCHED_WORDS = 50
#: Preview ceiling for the rejected input. Matches the column width.
MAX_PREVIEW_CHARS = 500
#: The blocked-reply copy is stored verbatim so the query page can show exactly
#: what the requester saw. It comes from the policy, so it is already bounded —
#: this is a belt-and-braces cap against a hand-edited policy row.
MAX_AUTO_REPLY_CHARS = 500


class GatewayBlockRecordInput(BaseModel):
    """What a hit point hands to :meth:`GatewayBlockRecordService.record_block`.

    ``matched_words`` accepts either ``SensitiveWordHit``-shaped dicts or plain
    strings, because the two existing check entry points differ: the input path
    has the automaton's ``hits``, the streaming path only knows the word the
    scanner stopped on.
    """

    face: GatewayBlockFaceName
    stage: GatewayBlockStageName
    tenant_id: int
    user_id: int = 0
    resource_type: str = ''
    resource_id: str = ''
    matched_words: list = Field(default_factory=list)
    user_input: str = ''
    auto_reply: str = ''
    request_id: str = ''


class GatewayBlockRecordItem(BaseModel):
    id: int
    tenant_id: int
    user_id: int
    face: GatewayBlockFaceName
    stage: GatewayBlockStageName
    resource_type: str
    resource_id: str
    matched_words: list
    user_input: str
    auto_reply: str
    request_id: str
    create_time: datetime


class GatewayBlockRecordPage(PageInfiniteCursorData[GatewayBlockRecordItem]):
    """Newest-first keyset page.

    ``PageInfiniteCursorData`` rather than ``PageData`` on purpose: this table is
    the one F120 table expected to reach millions of rows, and a ``total`` count
    means scanning every page the administrator ever scrolled past.
    """


__all__ = [
    'MAX_AUTO_REPLY_CHARS',
    'MAX_MATCHED_WORDS',
    'MAX_PREVIEW_CHARS',
    'GatewayBlockFace',
    'GatewayBlockRecordInput',
    'GatewayBlockRecordItem',
    'GatewayBlockRecordPage',
    'GatewayBlockStage',
]
