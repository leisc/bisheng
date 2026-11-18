"""Native-gateway error codes — module 266 (F120).

Module 266 is the next free slot in the 26x band: 261 = dsh/dsh_audit ·
262 = dsh_market · 263 = model_face · 264 = mcp_face · **266 = F120** ·
270 = commercial_license. Do not "correct" the assignment in
``docs/constitution.md`` C5 or in ``features/v3.0.0/release-contract.md``, and
do not add F120 codes to a neighbouring module.

Sub-ranges (design §4.4):

* ``26600-26609`` traffic control (rate limit / online concurrency)
* ``26610-26629`` OAuth / SSO / LDAP provider side
* ``26630-26649`` config validation
* ``26650-26699`` reserved

One code, one meaning (C5). Two traps this family is prone to:

* Re-raising a generic ``GiteeError``/``ServerError`` with a formatted message
  loses the code, and the frontend then shows a 500 for a config mistake. If a
  caller needs to distinguish "provider disabled" from "provider missing", it
  needs two codes, not one code with two messages.
* A code whose message embeds a placeholder (``{provider_type}``) must keep the
  placeholder — ``BaseErrorCode._format_msg`` leaves an unfilled ``{name}`` as
  literal text, so dropping the braces silently freezes the first value.
"""

from .base import BaseErrorCode

# ---------------------------------------------------------------------------
# 26600-26609 traffic control
# ---------------------------------------------------------------------------


class GatewayRateLimitError(BaseErrorCode):
    Code: int = 26601
    Msg: str = '请求过于频繁，请稍后重试'


class GatewayOnlineLimitError(BaseErrorCode):
    Code: int = 26602
    Msg: str = '当前在线会话数已达上限，请稍后重试'


class GatewayBlockRecordInvalidCursorError(BaseErrorCode):
    """Block-record query got a cursor that is malformed, version-incompatible,
    or issued under a different sort context.

    Same contract as ``AppInvalidCursorError`` / ``KnowledgeInvalidCursorError``
    (F027): a bad cursor is the client's mistake, not a server fault, so it gets
    a stable code the frontend can translate instead of a 500.
    """

    Code: int = 26609
    Msg: str = '分页参数已失效，请刷新后重试'


# ---------------------------------------------------------------------------
# 26610-26629 OAuth / SSO / LDAP provider side
#
# NOTE: 26603-26606 live here in *meaning* but were allocated inside the
# 26600-26609 numeric band, because that band was reserved for traffic control
# before the OAuth work was scoped. The README table (§20) is the authority and
# already lists them by meaning; do not "fix" the numbers by moving them, that
# would change the wire contract for every already-deployed frontend.
# ---------------------------------------------------------------------------


class GatewayOAuthAuthorizeError(BaseErrorCode):
    Code: int = 26603
    Msg: str = '网关授权失败，请重新登录'


class GatewayOAuthStateError(BaseErrorCode):
    Code: int = 26604
    Msg: str = '登录状态校验失败，请重新登录'


class GatewayOAuthProviderError(BaseErrorCode):
    Code: int = 26605
    Msg: str = '登录方式不可用，请联系管理员'


class GatewayOAuthTokenRefreshError(BaseErrorCode):
    Code: int = 26606
    Msg: str = '登录态已过期，请重新登录'


class GatewayLdapBindError(BaseErrorCode):
    Code: int = 26607
    Msg: str = 'LDAP 目录服务校验失败，请联系管理员'


# ---------------------------------------------------------------------------
# 26630-26649 config validation
# ---------------------------------------------------------------------------


class GatewayConfigInvalidError(BaseErrorCode):
    Code: int = 26608
    Msg: str = '网关配置不完整或格式错误，请联系管理员'
