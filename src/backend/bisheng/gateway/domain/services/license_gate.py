"""License 准入门（F120 T012，design 决策 6 / §6.2）。

**只读。** `license_info` 表的 Owner 是 `commercial_license` Feature；release-contract
第 49 行规定非 Owner 不得对其他对象产生创建/修改/删除行为。因此本模块：

* 只经 `LicenseInfoRepository.get_all()` 读快照，**不写表、不发起 ETL HTTP 刷新**；
* 有效期用 Owner 的 `compute_display_state` / `compute_days_remaining` 每次重算
  （`aggregator.py:44-45` 同做法），**不信任缓存行的 `display_state`**——那是旧
  Java gateway 把缓存列当既成事实、导致过期后仍放行的 bug 形态；
* `BISHENG_PRO` 是进程级环境变量，与授权事实漂移（K4），**不作为授权依据**。

**失败方向与限流面相反**（design §5.2）：限流面 Redis 不可用时 fail-open，license 门
必须 fail-closed（K4）——商业面请求级拒绝 26603，平台核心继续可用。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from loguru import logger

from bisheng.commercial_license.domain.mappers import compute_days_remaining, compute_display_state
from bisheng.commercial_license.domain.models.license_info import LicenseInfo
from bisheng.common.errcode.gateway import GatewayOAuthAuthorizeError

# 进程内缓存 TTL。缓存的是**行**，不是**决定**——决定每次按当天日期重算，
# 这样授权在缓存期内跨过过期点也会立即被拒（见 test_license_gate 的
# test_expiry_after_a_cached_allow_is_not_served_as_allow）。
CACHE_TTL_SECONDS = 60

# 拒绝原因分类，进结构化日志与前端横幅解释。
DENY_MISSING = "missing"  # 表里没有任何 license_codes 内的行（ETL 从未同步成功）
DENY_EXPIRED = "expired"  # 候选行全部已过期
DENY_NOT_EFFECTIVE = "not_effective"  # 行存在且未过期，但 ETL 标记为不可用
DENY_STORAGE = "storage_unavailable"  # 读库失败，fail-closed
DENY_GATE_DISABLED = "gate_disabled"  # 灰度开关关闭，不设门（放行）

# 面分类。
COMMERCIAL_FACE = "commercial"  # 受门约束（license_faces 配置）
DSH_FACE = "dsh"  # 明确排除：INV-37 开源完整功能不以许可证为前提
STATUS_FACE = "status"  # 明确排除：AC-35 授权过期后横幅仍须展示真实状态
OPEN_FACE = "open"  # 不在 license_faces 内，不受门约束

# DSH 面与授权状态查询的排除是**无条件**的，不随配置收紧——把 `/api/v1/dsh/`
# 写进 license_faces 也不生效，否则开源部署会被授权状态绑架。
DSH_PREFIXES = ("/api/v1/dsh/",)
STATUS_PREFIXES = ("/api/v1/gateway/license/",)

# ETL 行 extra 里表示「有授权但当前不可用」的开关。GATEWAY_EXTRA_KEYS 与
# ETL_EXTRA_KEYS 都含 "expired"，另两个仅 ETL 有。
_NOT_EFFECTIVE_FALSE_KEYS = ("usable",)
_NOT_EFFECTIVE_TRUE_KEYS = ("disabled", "expired")

GetRows = Callable[[], Awaitable[Sequence[LicenseInfo]]]


@dataclass(frozen=True)
class LicenseGateDecision:
    """一次门裁决。`allowed=True` 时 `reason`/`errcode` 均为 None（灰度关闭除外）。"""

    allowed: bool
    reason: str | None = None
    face: str | None = None
    errcode: int | None = None
    license_code: str | None = None
    days_remaining: int | None = None

    def __bool__(self) -> bool:  # 便于 `if await gate.decide(...):`
        return self.allowed


ALLOW = LicenseGateDecision(allowed=True)


def _matches_prefix(path: str, prefix: str) -> bool:
    """前缀匹配必须落在路径段边界上：`/api/v1/gateway/sensitive/` 不匹配
    `/api/v1/gateway/sensitivex`。"""
    if not prefix:
        return False
    if path == prefix.rstrip("/"):
        return True
    return path.startswith(prefix if prefix.endswith("/") else prefix + "/")


def classify_path(path: str, *, license_faces: Sequence[str]) -> str:
    """把请求路径归到四种面之一。DSH / 授权状态查询无条件优先于配置。"""
    for prefix in DSH_PREFIXES:
        if _matches_prefix(path, prefix):
            return DSH_FACE
    for prefix in STATUS_PREFIXES:
        if _matches_prefix(path, prefix):
            return STATUS_FACE
    for prefix in license_faces or ():
        if _matches_prefix(path, prefix):
            return COMMERCIAL_FACE
    return OPEN_FACE


def _row_is_not_effective(row: LicenseInfo) -> bool:
    extra = row.extra or {}
    for key in _NOT_EFFECTIVE_FALSE_KEYS:
        if extra.get(key) is False:
            return True
    for key in _NOT_EFFECTIVE_TRUE_KEYS:
        if extra.get(key) is True:
            return True
    return False


def _sort_key(row: LicenseInfo, today: date) -> tuple[int, int]:
    """无过期信息的 perpetual/execution 授权最宽松，排最前；否则按剩余天数降序。"""
    days = compute_days_remaining(row.expire_date, row.days_remaining, today)
    return (0, 0) if days is None else (1, -days)


def evaluate_license(
    rows: Sequence[LicenseInfo],
    *,
    license_codes: Sequence[str],
    today: date,
) -> LicenseGateDecision:
    """只看授权事实，不看路径。返回 allowed + 拒绝原因分类 + 当日重算的剩余天数。"""
    candidates = [row for row in rows if row.license_code in set(license_codes)]
    if not candidates:
        return LicenseGateDecision(allowed=False, reason=DENY_MISSING, errcode=GatewayOAuthAuthorizeError.Code)

    best: LicenseInfo | None = None
    for row in sorted(candidates, key=lambda r: _sort_key(r, today)):
        # 失效判定走 Owner 的 compute_display_state，不自己比 days<=0——
        # 阈值（>30 normal / >=1 expiring / 其余 expired）归 Owner 所有。
        if compute_display_state(row.expire_date, row.days_remaining, today) == "expired":
            continue
        if _row_is_not_effective(row):
            continue
        best = row
        break

    if best is None:
        # 区分「全部过期」与「有行但被标记不可用」：两者的处置文案不同。
        any_not_effective = any(_row_is_not_effective(row) for row in candidates)
        reason = DENY_NOT_EFFECTIVE if any_not_effective else DENY_EXPIRED
        return LicenseGateDecision(allowed=False, reason=reason, errcode=GatewayOAuthAuthorizeError.Code)

    return LicenseGateDecision(
        allowed=True,
        license_code=best.license_code,
        days_remaining=compute_days_remaining(best.expire_date, best.days_remaining, today),
    )


def decide_path(
    path: str,
    rows: Sequence[LicenseInfo],
    *,
    license_codes: Sequence[str],
    license_faces: Sequence[str],
    today: date,
    gate_enabled: bool = True,
) -> LicenseGateDecision:
    """纯函数：路径 × 授权行 → 裁决。中间件只调这一个入口。"""
    if not gate_enabled:
        face = classify_path(path, license_faces=license_faces)
        return LicenseGateDecision(allowed=True, reason=DENY_GATE_DISABLED, face=face)

    face = classify_path(path, license_faces=license_faces)
    if face != COMMERCIAL_FACE:
        return LicenseGateDecision(allowed=True, face=face)

    decision = evaluate_license(rows, license_codes=license_codes, today=today)
    return LicenseGateDecision(
        allowed=decision.allowed,
        reason=decision.reason,
        face=face,
        errcode=decision.errcode,
        license_code=decision.license_code,
        days_remaining=decision.days_remaining,
    )


class LicenseGate:
    """带 60s 进程内缓存的读入门。

    `repo` 只用到 `get_all()`，测试可注入任何异步替身；`clock` 返回 `datetime`，
    由此派生「今天」——缓存行不变而日期跨过过期点时仍会拒绝。
    """

    def __init__(
        self,
        *,
        repo,
        license_codes: Sequence[str],
        clock: Callable[[], datetime] | None = None,
        ttl_seconds: int = CACHE_TTL_SECONDS,
    ) -> None:
        self._repo = repo
        self._license_codes = tuple(license_codes)
        self._clock = clock or datetime.now
        self._ttl_seconds = ttl_seconds
        self._rows: tuple[LicenseInfo, ...] = ()
        self._fetched_at: datetime | None = None

    async def _snapshot(self) -> tuple[LicenseInfo, ...]:
        now = self._clock()
        if self._fetched_at is not None and (now - self._fetched_at).total_seconds() < self._ttl_seconds:
            return self._rows
        rows = await self._repo.get_all()
        self._rows = tuple(rows)
        self._fetched_at = now
        return self._rows

    async def decide(self, path: str, *, license_faces: Sequence[str]) -> LicenseGateDecision:
        face = classify_path(path, license_faces=license_faces)
        if face != COMMERCIAL_FACE:
            # 不受门约束的面连库都不读——DSH / 授权状态查询 / 开放面不应因授权
            # 或存储故障产生任何额外依赖。
            return LicenseGateDecision(allowed=True, face=face)

        try:
            rows = await self._snapshot()
        except Exception as exc:
            logger.warning("license gate read failed, fail-closed path={} err={}", path, exc)
            return LicenseGateDecision(
                allowed=False,
                reason=DENY_STORAGE,
                face=face,
                errcode=GatewayOAuthAuthorizeError.Code,
            )

        decision = evaluate_license(rows, license_codes=self._license_codes, today=self._clock().date())
        return LicenseGateDecision(
            allowed=decision.allowed,
            reason=decision.reason,
            face=face,
            errcode=decision.errcode,
            license_code=decision.license_code,
            days_remaining=decision.days_remaining,
        )
