"""T011 — License 准入门（F120 Wave 2）。

表驱动覆盖 design 决策 6 / §6.2 的全部裁决规则：

* 四种授权状态（缺失 / 已过期 / 未生效 / 有效）× 五种面（SSO 登录入口、商业管理面、
  授权状态查询、开源面、DSH 面）→ 放行或 26603；
* DSH 面与授权状态查询在四种状态下**全部放行**（AC-35：授权过期后横幅仍须能展示
  真实状态，查询端点不能被自己的门关掉）；
* 仅 `etl` 行有效即放行（`license_codes` 默认 `[etl, gateway]`）；
* 只读：不触发 ETL 刷新、不写 `license_info`；
* `BISHENG_PRO` 不参与裁决。

有效期一律用 Owner 的 `compute_display_state` / `compute_days_remaining` 重算
（`aggregator.py:44-45` 同样每次读取重算），**不信任缓存行的 `display_state`**——
那正是旧 Java gateway 把 `display_state` 当既成事实、导致过期后仍放行的 bug 形态。
"""

from datetime import date, datetime, timedelta

import pytest

from bisheng.commercial_license.domain.models.license_info import LicenseInfo
from bisheng.common.errcode.gateway import (
    GatewayOAuthAuthorizeError,
    GatewayRateLimitError,  # noqa: F401  (band sanity)
)
from bisheng.gateway.domain.services.license_gate import (
    CACHE_TTL_SECONDS,
    COMMERCIAL_FACE,
    DENY_EXPIRED,
    DENY_GATE_DISABLED,
    DENY_MISSING,
    DENY_NOT_EFFECTIVE,
    DENY_STORAGE,
    DSH_FACE,
    OPEN_FACE,
    STATUS_FACE,
    LicenseGate,
    classify_path,
    decide_path,
    evaluate_license,
)

TODAY = date(2026, 11, 17)

# 五种面的代表路径。前两个是商业面（受门约束），后三个不受门约束。
SSO_LOGIN_PATH = "/api/v1/gateway/oauth2/authorize"
COMMERCIAL_PATH = "/api/v1/gateway/sensitive/policies"
STATUS_PATH = "/api/v1/gateway/license/status"
OPEN_PATH = "/api/v1/chat/completions"
DSH_PATH = "/api/v1/dsh/workspace/list"

LICENSE_FACES = [
    "/api/v1/gateway/oauth2/",
    "/api/v1/gateway/sensitive/",
    "/api/v1/gateway/rate-rules",
    "/api/v1/gateway/block-records",
]


def _row(
    code: str = "etl",
    *,
    expire_date: date | None = None,
    days_remaining: int | None = None,
    display_state: str = "normal",
    extra: dict | None = None,
) -> LicenseInfo:
    """一行 license_info。`display_state` 故意可被传成「过期」而日期却是未来——
    门必须重算而不是信这一列（见模块 docstring）。"""
    return LicenseInfo(
        license_code=code,
        expire_date=expire_date,
        days_remaining=days_remaining,
        display_state=display_state,
        source_status=None,
        checked_at=datetime(2026, 11, 17, 10, 0, 0),
        extra=extra if extra is not None else {},
    )


def _valid_etl_row() -> LicenseInfo:
    return _row("etl", expire_date=date(2027, 1, 1), days_remaining=45, display_state="normal")


def _expired_etl_row() -> LicenseInfo:
    return _row("etl", expire_date=date(2026, 10, 1), days_remaining=-47, display_state="expired")


def _not_effective_etl_row() -> LicenseInfo:
    # 日期有效，但 ETL 明确说这份授权当前不可用（usable=false / disabled=true）。
    return _row(
        "etl",
        expire_date=date(2027, 1, 1),
        days_remaining=45,
        display_state="normal",
        extra={"usable": False, "disabled": True},
    )


STATES = {
    "缺失": [],
    "已过期": [_expired_etl_row()],
    "未生效": [_not_effective_etl_row()],
    "有效": [_valid_etl_row()],
}

# 面 → 期望放行的授权状态集合。SSO 登录入口与商业管理面只在「有效」放行；
# 授权状态查询、开源面、DSH 面在四种状态下全部放行。
FACE_EXPECTATIONS = [
    (SSO_LOGIN_PATH, {"有效"}),
    (COMMERCIAL_PATH, {"有效"}),
    (STATUS_PATH, set(STATES)),
    (OPEN_PATH, set(STATES)),
    (DSH_PATH, set(STATES)),
]


# --------------------------------------------------------------------------
# 一、面分类（classify_path）
# --------------------------------------------------------------------------


def test_classify_path_routes_every_face():
    assert classify_path(SSO_LOGIN_PATH, license_faces=LICENSE_FACES) == COMMERCIAL_FACE
    assert classify_path(COMMERCIAL_PATH, license_faces=LICENSE_FACES) == COMMERCIAL_FACE
    assert classify_path("/api/v1/gateway/rate-rules/42", license_faces=LICENSE_FACES) == COMMERCIAL_FACE
    assert classify_path(STATUS_PATH, license_faces=LICENSE_FACES) == STATUS_FACE
    assert classify_path("/api/v1/gateway/license/anything", license_faces=LICENSE_FACES) == STATUS_FACE
    assert classify_path(OPEN_PATH, license_faces=LICENSE_FACES) == OPEN_FACE
    assert classify_path(DSH_PATH, license_faces=LICENSE_FACES) == DSH_FACE


def test_classify_path_dsh_and_status_beat_config():
    """把 /api/v1/dsh/ 或 /api/v1/gateway/license/ 写进 license_faces 也不生效——
    这两个排除是无条件的（INV-37 / AC-35），不能靠配置收敛掉。"""
    hostile = [*LICENSE_FACES, "/api/v1/dsh/", "/api/v1/gateway/license/"]
    assert classify_path(DSH_PATH, license_faces=hostile) == DSH_FACE
    assert classify_path(STATUS_PATH, license_faces=hostile) == STATUS_FACE


def test_classify_path_exact_prefix_without_trailing_slash():
    assert classify_path("/api/v1/gateway/rate-rules", license_faces=LICENSE_FACES) == COMMERCIAL_FACE
    assert classify_path("/api/v1/gateway/rate-rulesx", license_faces=LICENSE_FACES) == OPEN_FACE


def test_classify_path_with_empty_config_gates_nothing():
    for path in (SSO_LOGIN_PATH, COMMERCIAL_PATH, STATUS_PATH, DSH_PATH):
        assert classify_path(path, license_faces=[]) != COMMERCIAL_FACE


# --------------------------------------------------------------------------
# 二、纯函数表驱动：四种授权状态 × 五种面
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state_name,rows", list(STATES.items()))
@pytest.mark.parametrize("path,allowed_states", FACE_EXPECTATIONS)
def test_state_x_face_matrix(state_name, rows, path, allowed_states):
    decision = decide_path(
        path,
        rows,
        license_codes=("etl", "gateway"),
        license_faces=LICENSE_FACES,
        today=TODAY,
    )
    if state_name in allowed_states:
        assert decision.allowed, f"{state_name} 应放行 {path}，却被 {decision.reason} 拒绝"
        assert decision.reason is None
    else:
        assert not decision.allowed, f"{state_name} 应拒绝 {path}"
        assert decision.reason in {DENY_MISSING, DENY_EXPIRED, DENY_NOT_EFFECTIVE}
        assert decision.errcode == 26603


def test_commercial_faces_map_to_26603():
    decision = decide_path(
        SSO_LOGIN_PATH,
        [],
        license_codes=("etl", "gateway"),
        license_faces=LICENSE_FACES,
        today=TODAY,
    )
    assert decision.errcode == 26603
    assert decision.reason == DENY_MISSING


def test_dsh_face_is_excluded_from_the_commercial_set():
    """DSH 面不是商业面：design §6.2 2026-11-10 裁决，INV-37 不以许可证为前提。"""
    for state_name, rows in STATES.items():
        decision = decide_path(
            DSH_PATH,
            rows,
            license_codes=("etl", "gateway"),
            license_faces=LICENSE_FACES,
            today=TODAY,
        )
        assert decision.face == DSH_FACE, state_name
        assert decision.allowed, state_name


def test_status_face_is_excluded_so_the_banner_can_explain_the_lock():
    """AC-35：授权过期后前端横幅仍须展示真实状态，查询端点不能被自己的门关掉。"""
    for state_name, rows in STATES.items():
        decision = decide_path(
            STATUS_PATH,
            rows,
            license_codes=("etl", "gateway"),
            license_faces=LICENSE_FACES,
            today=TODAY,
        )
        assert decision.face == STATUS_FACE, state_name
        assert decision.allowed, state_name


def test_open_face_is_excluded():
    decision = decide_path(
        OPEN_PATH,
        [],
        license_codes=("etl", "gateway"),
        license_faces=LICENSE_FACES,
        today=TODAY,
    )
    assert decision.face == OPEN_FACE
    assert decision.allowed


def test_all_configured_commercial_prefixes_are_gated():
    """license_faces 里每一个前缀都必须真的受门约束——漏一个就是静默开洞。"""
    for prefix in LICENSE_FACES:
        path = prefix if prefix.endswith("/") else prefix + "/1"
        decision = decide_path(
            path,
            [],
            license_codes=("etl", "gateway"),
            license_faces=LICENSE_FACES,
            today=TODAY,
        )
        assert not decision.allowed, f"{prefix} 未受门约束"
        assert decision.errcode == 26603


def test_commercial_prefix_matches_on_path_segment_not_string_prefix():
    """`/api/v1/gateway/sensitivex` 不是 `/api/v1/gateway/sensitive/` 下的路径。"""
    decision = decide_path(
        "/api/v1/gateway/sensitivex",
        [],
        license_codes=("etl", "gateway"),
        license_faces=LICENSE_FACES,
        today=TODAY,
    )
    assert decision.allowed, "前缀匹配必须落在路径段边界上"


# --------------------------------------------------------------------------
# 三、有效期重算，不信缓存列
# --------------------------------------------------------------------------


def test_recomputes_validity_instead_of_trusting_display_state():
    """缓存行写 `display_state='expired'` 但日期有效 → 必须放行。

    旧 Java gateway 把 `display_state` 当既成事实；自研按 Owner 的
    `compute_display_state` 每次读取重算（`aggregator.py:44-45` 同做法）。
    """
    row = _row("etl", expire_date=date(2027, 1, 1), days_remaining=45, display_state="expired")
    decision = decide_path(
        COMMERCIAL_PATH,
        [row],
        license_codes=("etl", "gateway"),
        license_faces=LICENSE_FACES,
        today=TODAY,
    )
    assert decision.allowed


def test_expired_row_is_denied_even_when_display_state_says_normal():
    row = _row("etl", expire_date=date(2026, 10, 1), days_remaining=-47, display_state="normal")
    decision = decide_path(
        COMMERCIAL_PATH,
        [row],
        license_codes=("etl", "gateway"),
        license_faces=LICENSE_FACES,
        today=TODAY,
    )
    assert not decision.allowed
    assert decision.reason == DENY_EXPIRED


def test_days_remaining_is_recomputed_not_read_from_the_column():
    # 列里写 99 天，但按 expire_date 重算应为 45 天。
    row = _row("etl", expire_date=date(2027, 1, 1), days_remaining=99)
    decision = evaluate_license([row], license_codes=("etl", "gateway"), today=TODAY)
    assert decision.allowed
    assert decision.days_remaining == 45


def test_row_without_any_expiry_information_is_allowed():
    """perpetual/execution 授权没有过期信息（`mappers.map_etl_payload` 置 None），
    Owner 的 compute_display_state 对 None 返回 'normal'，门必须跟随。"""
    row = _row("etl", expire_date=None, days_remaining=None)
    decision = decide_path(
        COMMERCIAL_PATH,
        [row],
        license_codes=("etl", "gateway"),
        license_faces=LICENSE_FACES,
        today=TODAY,
    )
    assert decision.allowed


# --------------------------------------------------------------------------
# 四、license_codes 范围
# --------------------------------------------------------------------------


def test_any_code_in_license_codes_being_valid_opens_the_gate():
    for code in ("etl", "gateway"):
        row = _row(code, expire_date=date(2027, 1, 1), days_remaining=45)
        decision = decide_path(
            COMMERCIAL_PATH,
            [row],
            license_codes=("etl", "gateway"),
            license_faces=LICENSE_FACES,
            today=TODAY,
        )
        assert decision.allowed, f"{code} 行有效却未放行"


def test_only_etl_row_is_enough():
    """design 决策 6：自研部署只有 ETL 一份商业授权，仅 etl 行有效即放行。"""
    decision = decide_path(
        COMMERCIAL_PATH,
        [_valid_etl_row()],
        license_codes=("etl", "gateway"),
        license_faces=LICENSE_FACES,
        today=TODAY,
    )
    assert decision.allowed


def test_code_outside_license_codes_does_not_open_the_gate():
    row = _row("dashboard", expire_date=date(2027, 1, 1), days_remaining=45)
    decision = decide_path(
        COMMERCIAL_PATH,
        [row],
        license_codes=("etl", "gateway"),
        license_faces=LICENSE_FACES,
        today=TODAY,
    )
    assert not decision.allowed
    assert decision.reason == DENY_MISSING


def test_one_expired_row_does_not_mask_another_valid_one():
    decision = decide_path(
        COMMERCIAL_PATH,
        [_expired_etl_row(), _valid_etl_row()],
        license_codes=("etl", "gateway"),
        license_faces=LICENSE_FACES,
        today=TODAY,
    )
    assert decision.allowed


def test_all_candidate_rows_expired_denies():
    decision = decide_path(
        COMMERCIAL_PATH,
        [
            _row("etl", expire_date=date(2026, 10, 1), days_remaining=-47),
            _row("gateway", expire_date=date(2026, 9, 1), days_remaining=-77),
        ],
        license_codes=("etl", "gateway"),
        license_faces=LICENSE_FACES,
        today=TODAY,
    )
    assert not decision.allowed
    assert decision.reason == DENY_EXPIRED


def test_not_effective_flags_deny():
    """ETL 的 usable=false / disabled=true / expired=true 都表示「有行但不可用」。"""
    for flag in ({"usable": False}, {"disabled": True}, {"expired": True}):
        row = _row("etl", expire_date=date(2027, 1, 1), days_remaining=45, extra=flag)
        decision = decide_path(
            COMMERCIAL_PATH,
            [row],
            license_codes=("etl", "gateway"),
            license_faces=LICENSE_FACES,
            today=TODAY,
        )
        assert not decision.allowed, f"{flag} 未导致拒绝"
        assert decision.reason == DENY_NOT_EFFECTIVE


def test_truthy_usable_flag_does_not_deny():
    row = _row("etl", expire_date=date(2027, 1, 1), days_remaining=45, extra={"usable": True})
    decision = decide_path(
        COMMERCIAL_PATH,
        [row],
        license_codes=("etl", "gateway"),
        license_faces=LICENSE_FACES,
        today=TODAY,
    )
    assert decision.allowed


# --------------------------------------------------------------------------
# 五、门关闭
# --------------------------------------------------------------------------


def test_disabled_gate_allows_everything():
    """`gateway.license_gate=false` 时不设门——灰度开关，不是硬编码 true。"""
    decision = decide_path(
        COMMERCIAL_PATH,
        [],
        license_codes=("etl", "gateway"),
        license_faces=LICENSE_FACES,
        today=TODAY,
        gate_enabled=False,
    )
    assert decision.allowed
    assert decision.reason == DENY_GATE_DISABLED


# --------------------------------------------------------------------------
# 六、服务层：60s 缓存、fail-closed、只读
# --------------------------------------------------------------------------


class _FailingRepo:
    """存储不可用：get_all 直接抛。门必须 fail-closed，不得把异常吞成放行。"""

    def __init__(self, exc):
        self.exc = exc

    async def get_all(self):
        raise self.exc


class _FakeRepo:
    """只记录被调了几次——不写任何东西。"""

    def __init__(self, rows):
        self._rows = rows
        self.calls = 0

    async def get_all(self):
        self.calls += 1
        return list(self._rows)


class _FakeClock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now


def _gate(rows, *, clock=None, ttl=CACHE_TTL_SECONDS):
    repo = _FakeRepo(rows)
    clock = clock or _FakeClock(datetime(2026, 11, 17, 10, 0, 0))
    gate = LicenseGate(repo=repo, license_codes=("etl", "gateway"), clock=clock, ttl_seconds=ttl)
    return gate, repo, clock


@pytest.mark.asyncio
async def test_cache_serves_repeated_calls_within_ttl():
    gate, repo, _ = _gate([_valid_etl_row()])
    for _ in range(5):
        assert (await gate.decide(COMMERCIAL_PATH, license_faces=LICENSE_FACES)).allowed
    assert repo.calls == 1, "60s 缓存期内不应反复读库"


@pytest.mark.asyncio
async def test_cache_refetches_after_ttl():
    clock = _FakeClock(datetime(2026, 11, 17, 10, 0, 0))
    gate, repo, _ = _gate([_valid_etl_row()], clock=clock)
    assert (await gate.decide(COMMERCIAL_PATH, license_faces=LICENSE_FACES)).allowed
    clock.now = datetime(2026, 11, 17, 10, 0, 0) + timedelta(seconds=CACHE_TTL_SECONDS + 1)
    assert (await gate.decide(COMMERCIAL_PATH, license_faces=LICENSE_FACES)).allowed
    assert repo.calls == 2


@pytest.mark.asyncio
async def test_expiry_after_a_cached_allow_is_not_served_as_allow():
    """过期后不返回缓发放行：缓存的是**行**不是**决定**，日期一旦跨过过期点，
    同一次缓存内的后续请求也必须被拒。"""
    # 授权在 2026-11-18 过期。缓存在 11-17 拉取并放行；随后日期跨过过期点，
    # 但缓存行**未刷新**（TTTL 故意设成 1e6 秒，把「重读」这个变量消掉），
    # 此时仍必须拒绝——证明门缓存的是行、每次按当天日期重算决定，
    # 而不是缓存了一个「已放行」的结论。
    clock = _FakeClock(datetime(2026, 11, 17, 10, 0, 0))
    gate, repo, _ = _gate(
        [_row("etl", expire_date=date(2026, 11, 18), days_remaining=1)],
        clock=clock,
        ttl=1_000_000,
    )
    assert (await gate.decide(COMMERCIAL_PATH, license_faces=LICENSE_FACES)).allowed
    clock.now = datetime(2026, 11, 18, 10, 0, 30)  # 跨过过期点
    decision = await gate.decide(COMMERCIAL_PATH, license_faces=LICENSE_FACES)
    assert not decision.allowed, "过期后不得返回缓发放行"
    assert decision.reason == DENY_EXPIRED
    assert repo.calls == 1, "缓存未过期，不应触发重读——决定是按日期重算的"

    # 反向：TTL 正常时，跨过过期点后缓存自己也会刷新，结论同样是拒绝。
    clock2 = _FakeClock(datetime(2026, 11, 17, 10, 0, 0))
    gate2, repo2, _ = _gate(
        [_row("etl", expire_date=date(2026, 11, 18), days_remaining=1)], clock=clock2
    )
    assert (await gate2.decide(COMMERCIAL_PATH, license_faces=LICENSE_FACES)).allowed
    clock2.now = datetime(2026, 11, 18, 10, 5, 0)  # 超过 60s TTL
    decision2 = await gate2.decide(COMMERCIAL_PATH, license_faces=LICENSE_FACES)
    assert not decision2.allowed
    assert repo2.calls == 2


@pytest.mark.asyncio
async def test_storage_unavailable_fails_closed_on_commercial_faces():
    """K4：存储不可用时 license 门必须 fail-closed，与限流面 fail-open 相反。"""
    gate = LicenseGate(
        repo=_FailingRepo(RuntimeError("mysql is down")),
        license_codes=("etl", "gateway"),
        clock=_FakeClock(datetime(2026, 11, 17, 10, 0, 0)),
    )
    for path in (SSO_LOGIN_PATH, COMMERCIAL_PATH):
        decision = await gate.decide(path, license_faces=LICENSE_FACES)
        assert not decision.allowed, f"{path} 在存储不可用时被放行"
        assert decision.reason == DENY_STORAGE
        assert decision.errcode == 26603


@pytest.mark.asyncio
async def test_storage_unavailable_leaves_excluded_faces_open():
    """存储不可用只影响受门约束的面；核心 / 开放 / DSH / 授权状态查询不受影响。"""
    gate = LicenseGate(
        repo=_FailingRepo(RuntimeError("mysql is down")),
        license_codes=("etl", "gateway"),
        clock=_FakeClock(datetime(2026, 11, 17, 10, 0, 0)),
    )
    for path in (STATUS_PATH, OPEN_PATH, DSH_PATH):
        decision = await gate.decide(path, license_faces=LICENSE_FACES)
        assert decision.allowed, f"{path} 在存储不可用时被拒绝"


@pytest.mark.asyncio
async def test_sync_never_produced_a_row_fails_closed():
    """ETL 从未成功同步过（表空）→ 商业面拒绝，而不是静默放行。"""
    gate, _, _ = _gate([])
    decision = await gate.decide(SSO_LOGIN_PATH, license_faces=LICENSE_FACES)
    assert not decision.allowed
    assert decision.reason == DENY_MISSING


@pytest.mark.asyncio
async def test_gate_is_read_only_no_write_paths():
    """决策 6：F120 只读 license_info。仓库上不得存在任何写方法调用。"""
    gate, repo, _ = _gate([_valid_etl_row()])
    await gate.decide(COMMERCIAL_PATH, license_faces=LICENSE_FACES)
    writable = [name for name in dir(repo) if name in {"upsert", "add", "commit", "delete"}]
    assert not writable, f"门不得接触写路径: {writable}"


@pytest.mark.asyncio
async def test_gate_does_not_trigger_etl_refresh(monkeypatch):
    """不得发起 ETL HTTP 刷新——那会引入新的写入路径，超出 F120 只读红线。"""
    import bisheng.gateway.domain.services.license_gate as mod

    async def _boom(*args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("门不得触发 ETL 刷新")

    monkeypatch.setattr(mod, "sync_etl_license", _boom, raising=False)
    gate, _, _ = _gate([])
    decision = await gate.decide(SSO_LOGIN_PATH, license_faces=LICENSE_FACES)
    assert not decision.allowed


@pytest.mark.asyncio
async def test_bisheng_pro_env_var_does_not_open_the_gate(monkeypatch):
    """`BISHENG_PRO` 是进程级开关，与授权事实漂移（K4），不作为授权依据。"""
    monkeypatch.setenv("BISHENG_PRO", "true")
    gate, _, _ = _gate([])
    decision = await gate.decide(SSO_LOGIN_PATH, license_faces=LICENSE_FACES)
    assert not decision.allowed


@pytest.mark.asyncio
async def test_default_settings_gate_disabled_paths_are_all_allowed():
    """默认 `license_faces` 之外的路径一律不受门约束——新部署不会被误伤。"""
    gate, _, _ = _gate([])
    decision = await gate.decide("/api/v1/skill/group", license_faces=[])
    assert decision.allowed
    assert decision.reason is None


def test_gateway_errcode_band_is_used_by_the_gate():
    """门拒绝的错误码必须落在 F120 的 266 段（26603 授权）。"""
    decision = decide_path(
        COMMERCIAL_PATH,
        [],
        license_codes=("etl", "gateway"),
        license_faces=LICENSE_FACES,
        today=TODAY,
    )
    assert decision.errcode == GatewayOAuthAuthorizeError.Code
