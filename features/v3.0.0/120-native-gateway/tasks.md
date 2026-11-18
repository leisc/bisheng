# F120 任务

**关联规格**: [spec.md](./spec.md)（48 条 AC，✅ 已评审 2026-11-10）· [design.md](./design.md)（8 项决策 + 15 条已知坑，接手第一入口）
**版本**: v3.0.0 · **创建日期**: 2026-11-10

## 状态

| 步骤 | 状态 | 备注 |
|------|------|------|
| spec.md | ✅ 已评审 | 48 条 AC；2026-11-10 `/sdd-review` 问题已按用户三条裁决修订，用户当日确认 |
| design.md | ✅ 已评审 | 2026-11-10 `/sdd-review design` 报 3 high（RSA 私钥明文违 C6 / 新表未登记租户过滤违 C3 / license 门与 spec 矛盾），已按用户裁决修订（Fernet 密文、补齐登记、SSO 纳入授权门并同步 spec AC-02/33/34/47），当日确认 |
| tasks.md | 🔲 已拆解 | 6 Wave / 66 任务；Wave 0–1 为前置与底座，可先行 |
| 实现 | 🔲 未开始 | 0 / 66 完成 |

> **开工前置条件**：spec 与 design 未经用户 ★ 确认前，**不得**进入 Wave 2 及以后的业务实现；Wave 0（纯重构、行为等价）与 Wave 1（底座、无行为变更）可在确认期内并行准备，但同样不得合入。

## 路径和执行约定

`BE=src/backend/bisheng`、`BT=src/backend/test/gateway`、`CFG=docker/bisheng/config`、`FE=src/frontend/platform/src`、`FEC=src/frontend/client/src`、`LOC=src/frontend/packages/locales/src`、`DOC=docs/architecture`。

- 测试先写并观察失败（红→绿）；Lua 脚本与中间件测试在 CI 起真实 Redis，禁 mock 掉时间/时钟语义。DB 迁移测试不依赖本地中间件（SDD-Guide §6）。
- 每行任务自包含；设计论证指向 design §X，不复制。共享文件（`main.py`、`http_middleware.py`、`sensitive_word/domain/schemas.py`、`release-contract.md`）只改本任务落点，保留上游后续增强。
- 错误码统一走 `BE/common/errcode/gateway.py` 的 26600–26699 段，禁散落；文案三语一次一文件小编辑后跑生成器（T065，2026-11-10 从 T008 拆出）。
- **License 只读红线**：F120 不得写 `license_info`、不得改 `commercial_license/` 下任何文件（`etl_sync.py:42` 无 source 参数；release-contract 第 49 行只允许非 Owner 读取/调用 Owner Service）；授权门只读表 + Owner 的 `compute_display_state` 重算有效期，`license_codes` 默认 `[etl, gateway]`，`BISHENG_PRO` 不是授权依据（design 决策 6；T011/T012/T015/T016）。
- **新增路由必须在 `main.py` 模块顶层导入**（不能放进 `create_app` 内）：`register_tenant_filter_events` 在 DatabaseManager 初始化时才跑（`core/database/manager.py:62`），迟导入的模型进不了 `_discover_tenant_aware_tables` 的发现集（`tenant_filter.py:187`），租户过滤静默失效（C3）。
- 新表一律 `dialect_helpers`（`JsonType`/`LargeText`）+ 租户列 + `logic_delete`（追加型表除外）；迁移 `down_revision` 取当日 `uv run alembic heads`（现 head `f114_app_authorization`，文件名前缀不代表拓扑顺序，见 backend alembic AGENTS.md）。
- **租户过滤登记**：新租户感知模型必须同步加入 `core/database/tenant_filter.py:42` 的 `_TENANT_AWARE_MODEL_MODULES`，否则 `tenant_id` 自动填充与跨租户查询过滤静默失效（design §5.7、C3；T061/T062）。
- 中间件相关任务必须验证注册顺序：`GatewayMiddleware` 比 `CustomMiddleware` 更内层（design §5.2、坑 #11）。
- `sensitive_word` 的 Pro 门控（`sensitive_word_policy_service.py:350`）意味着 e2e 必须 `BISHENG_PRO=true`（坑 #9）。
- 前端任务为手动验证；每个前端任务附打开路径、操作、预期与错误场景。

---

## Wave 0：前置重构（行为等价，无新功能）

| 状态 | ID/类型 | 文件与自包含任务 | 依赖/配对 |
| --- | --- | --- | --- |
| [x] | T001 测试/基础设施 | `BT/test_hmac_signature_parity.py`：同一 `METHOD\nPATH\nBODY` 输入下，`common/utils/hmac_signature.py` 与 `app_runtime/domain/services/hmac_auth.py`、`app_runtime/domain/services/orchestrator_client.py` 的现行实现输出字节一致；header 名 `X-Signature` 一致。覆盖 AC: AC-41 | 无；T002 |

**T001 实测记录（2026-11-17，13 tests passed）**：三处实现公式确认为同一 `METHOD\\nPATH\\nBODY` → hex(sha256)；`sso_sync/domain/services/hmac_auth.py` 的 `compute_signature` 已是 `compute_hmac_signature` 的薄封装（无需收敛，但已纳入断言）。断言口径见文件内 `test_third_copy_sso_sync_is_a_thin_alias`。

| [x] | T002 基础设施 | `BE/common/utils/hmac_signature.py`、`BE/app_runtime/domain/services/hmac_auth.py`、`orchestrator_client.py`：把两份重写收敛为调用规范实现；不改签名公式、不改 header、不改错误路径（design 决策 7、坑 #2） | T001 |

**T002 实测记录（2026-11-17）**：两处 `compute_signature` 均改为委托 `compute_hmac_signature`（保留函数名与签名，`hmac`/`hashlib` 导入按使用情况清理）；docstring 标注"不得再次就地重写"。验证方式：改动前后对 `test/app_runtime/test_internal_app_proxy_api.py`、`test_preview_entry.py`、`test_orchestrator_client.py`、`test/dsh/test_content_safety_{hmac,runtime}.py` 跑同一选择集，失败集合逐条一致（38 项均为本地缺中间件的既有失败，如 `service_identity_not_configured`），即零新增失败。

| [x] | T003 测试/基础设施 | `BT/test_current_subject_context.py`：带合法 token 经 `CustomMiddleware` 后 `current_subject` 可读且与 request 身份一致；无 token/ exempt 路径为 None；上下文在请求结束后 reset。覆盖 AC: AC-03 | 无；T004 |

**T003 实测记录（2026-11-17，6 tests）**：断言口径 = 与中间件自身 `_decode_jwt_subject(token)` 结果逐字段相等（防止第二次解码漂移）；exempt 路径（`/api/v1/user/login`）必须为 None。

| [x] | T004 基础设施 | `BE/core/context/user.py`（新增 `current_subject` ContextVar + set/reset helper）、`BE/utils/http_middleware.py`：在既有 `decoded_subject` 处 `set()`，`finally` 中 reset；不改变任何既有判定分支（design §5.2、坑 #11） | T003 |

**T004 实测记录（2026-11-17，6 tests passed）**：`current_subject` 默认 None；`set()` 放在 `is_exempt` 计算之后（T003 的 exempt 用例证明放在 `decoded_subject` 处会泄漏登录态），exempt 与 DSH 自有凭据路径显式置 None；`finally` 中 reset。验证方式：`create_app()` 正常构建（middleware 数 6，未新增层）；test/gateway 19 passed；与基线同一选择集失败集合逐条一致。
| [x] | T005 测试/Domain（含 1 行枚举实现） | `BT/test_sensitive_word_dsh_business_type.py`：枚举新增 `dsh_chat` 后，策略创建/读取/缓存键按 (tenant, business_type, scope) 唯一；既有三种 business_type 行为不变。实现落点 `BE/sensitive_word/domain/schemas.py`：`SensitiveWordBusinessType` 增补 `DSH_CHAT = "dsh_chat"`（承接 F111 引擎，行命名空间独立；`business_type` 是 `String(64)` 普通列、唯一键 `uk_sensitive_policy_scope` 含该列，**无需 DDL**）。测试与实现同任务：改动只有 1 行且测试即其回归网，单独占一个任务号会与既有 T006（`test_gateway_models.py`）撞号。覆盖 AC: AC-17 | 无 |

**T005 实测记录（2026-11-17，先红后绿 11 tests）**：断言口径 = `_scope_stmt` 编译后 SQL 各自含自身 business_type 且四条互不相同；`_build_cache_key` 四类型互异；`aupsert_policy` 对 DSH 不走 workbench 的必填校验且空 auto_reply 回落 `DEFAULT_AUTO_REPLY`，workbench 仍走自身裁剪与校验。先写测试确认 4 处 `AttributeError: DSH_CHAT`（红）后再加枚举。验证方式：test/gateway + test/sensitive_word 共 55 passed；与基线同一选择集失败集合逐条一致。`GET/PUT /api/v1/sensitive-word-policies/{business_type}` 的路径参数由该枚举校验，`dsh_chat` 自动成为合法面（管理面前端注册见 T022/T065）。


## Wave 1：数据、错误码与配置底座（无行为变更）

| 状态 | ID/类型 | 文件与自包含任务 | 依赖/配对 |
| --- | --- | --- | --- |
| [x] | T006 测试/Domain | `BT/test_gateway_models.py`：三表 ORM round-trip（`gateway_oauth_provider`/`gateway_rate_rule`/`gateway_block_record`）、唯一键冲突、`logic_delete` 语义、租户列非空。覆盖 AC: AC-40。**2026-11-17 实施记录**：19 条用例全绿（`test/gateway` 目录合计 49 条，含 Wave 0 的 30 条）。实现期追加三处原任务文本未覆盖的断言——① `_TENANT_AWARE_MODEL_MODULES` 与 `_discover_tenant_aware_tables()` 含三表（C3：漏登记的模块会静默绕过租户过滤，这是 `tenant_filter.py` 头注里点名的历史事故模式）；② `gateway_block_record` 列集合**恰好**等于 design §5.7 的 12 列（append-only 表无 `logic_delete`/`update_time`，用列集合断言而非逐字段，防止后人补一列就破坏只增语义）；③ alembic 单 head 断言（从版本文件静态推导，不连库）。**测试隔离偏差**：本文件加 `_no_tenant_context` autouse fixture 置 `_bypass_tenant_filter=True`——`register_tenant_filter_events` 是**进程级全局**监听器，任何先构建真实 `CustomMiddleware` 的测试模块都会替整个 pytest 进程注册它，此后无租户上下文的 SELECT 会被解析成 `DEFAULT_TENANT_ID`，Repository 在 INSERT 后发的 `refresh()` 因此查不到 `tenant_id=3` 的行（现象 `Could not refresh instance`）。生产环境请求恒有租户上下文，故这不是代码缺陷；过滤本身的读写行为归 T061 集成测试 | 无；T007 |
| [x] | T007 Domain/基础设施 | `BE/gateway/domain/models/{oauth_provider,rate_rule,block_record}.py` + 各 Repository classmethod（get/aget/create/update/delete + 规则解析查询）+ `BE/core/database/alembic/versions/<date>_f120_gateway_tables.py`（`down_revision` 取当日 heads）+ `features/v3.0.0/release-contract.md` 表 1 追加三表 Owner=F120，**并登记 `sensitive_word_policy` 的 `dsh_chat` 策略管理面 Owner=F120（承接 F111 的 Gateway 侧前提，修订记录见 F111 spec）**。**回滚方案**：三表无外部依赖方（只有本 Feature 读写），`downgrade` 整批 `drop_table` 即可；`release-contract.md` 的归属登记随 F120 下线同步删除（登记本身不改运行时行为）。**不写 `license_info`**（Owner 是 `commercial_license`，release-contract 第 49 行只允许读取/调用）。覆盖 AC: AC-40。**2026-11-17 实施记录**：三模型 + 三 Repository 落地；迁移文件 `v3_0_0_f120_gateway_tables.py`（`down_revision='f114_app_authorization'`，取自当日 `uv run alembic heads` 实跑输出，非规划期快照）；`uv run alembic heads` 复核为单 head。release-contract 表 1 追加 4 行（三表 Owner=F120 + `dsh_chat` 管理面 Owner=F120），未新增 `license_info` 行。三模块已登记进 `_TENANT_AWARE_MODEL_MODULES`。**实现期决策**：① 唯一键均含 `logic_delete`——软删后同一业务键必须可再次插入，否则租户删除过的 provider/规则永远无法重建（T006 用用例锁住）；② `GatewayBlockRecord.id` 用 `BigInteger().with_variant(Integer,'sqlite')`——SQLite 下 `BIGINT PRIMARY KEY` 不享 rowid 别名、不自增，而 `test_gateway_models.py` 要 round-trip；MySQL/DM8 仍为 BIGINT；③ Repository 的读全部显式带 `tenant_id` 谓词，不依赖监听器（T061 另证过滤）；`GatewayOAuthProviderRepository.redact_config` 把 `client_secret` 等 6 类机密键掩码为 `***`，管理面只回该副本（C6）；④ `resolve_rule` 用 ≤4 次索引查询按候选序取首个命中，同作用域内 resource_id 精确优先于类型全量——未用 SQL `CASE` 排序，候选列表短且可读性优先 | T006 |
| [x] | T008 测试/基础设施 | `BT/test_gateway_errcode.py`（码值唯一、区间 26600–26699、继承 BaseErrorCode）；`BE/common/errcode/gateway.py`
  · 26600–26699 段经全模块扫描确认为空（26x 已用 261 dsh/dsh_audit · 262 dsh_market · 263 model_face · 264 mcp_face · 265 app_publish · 270 commercial_license），未撞任何既有码；`README.md` 补第 20 节「原生网关模块 266」
  · 测试实现时发现 `model_face.ModelFaceError` 是无 `Code` 的抽象基类，`_classes()` 加 `hasattr(obj, 'Code')` 过滤，否则跨模块碰撞检查会误报
  · 8 个码（26601 限流 / 26602 在线超限 / 26603 授权 / 26604 state / 26605 provider / 26606 换 token / 26607 LDAP / 26608 配置非法）在 `test_documented_codes_exist_with_the_documented_meaning` 中逐码钉死，改名/改号即红（前端 switch 契约）
  · 2026-11-17 实现，6 tests passed | 无；T065
| [x] | T065 基础设施 | `LOC/api_errors/{zh-Hans,en,ja}.json` 三语各一次编辑（F120 模块段文案）+ 跑 i18n 生成器更新派生文件 + 过 `pnpm --filter @bisheng/locales check`
  · 8 个码三语各一次编辑（26601–26608），与 `BE/common/errcode/gateway.py` 的 `Msg` 一一对应；`zh-Hans` 用后端同款中文文案，`en`/`ja` 另写
  · 只追加 8 个键，无重排（JSON 顺序即可读性，diff 只有 +8 行）
  · `pnpm --filter @bisheng/locales build` 生成 6 个派生文件（platform 3 + client 3），`--check` 退出码 0
  · 2026-11-17 实现 | 无；T008
| [x] | T009 测试/基础设施 | `BT/test_gateway_settings.py`：`gateway.enabled=false` 时中间件与路由均不装配；`license_faces`/`filter_url`/`default_qps` 解析与默认值
  · 13 tests passed（先红后绿：`ModuleNotFoundError: No module named 'bisheng.core.config.gateway'` × 11 + `AttributeError: 'Settings' object has no attribute 'gateway'`）
  · 默认 `license_faces` 恰为四个商业面前缀，且显式断言不含 `/api/v1/dsh/` 与 `/api/v1/gateway/license/`（AC-35）
  · `license_codes` 默认 `[etl, gateway]`（决策 6）；`filter_url` 默认空（新部署不会被限流）
  · frozen（`ValidationError` on assignment）+ `extra='forbid'` 拒绝未知键（`lisence_faces` 拼错必须是启动失败，不能看起来像门没开）
  · 2026-11-17 实现 | 无；T010
| [x] | T010 基础设施 | `BE/core/config/gateway.py`（`GatewaySettings`，frozen + strict 仿 `core/config/dsh.py`）+ `BE/core/config/settings.py` 挂载 + `CFG/config/config.yaml` 增加 `gateway:` 段（默认全关）
  · `GatewaySettings` / `GatewayRateLimitSettings` / `GatewayOnlineSettings` / `GatewayOAuthSettings` 四层，全部 `extra='forbid', strict=True, frozen=True`
  · `settings.py` 挂载 `gateway: GatewaySettings`；**同时把 `gateway` 加入 `validate_lists` 的 `{'dev','dsh'}` 例外集**——该 validator 会把所有 falsy 顶层值改成 `[]`，漏掉这一行会让 `gateway:` 空段变成 `[]` 并被 pydantic 拒掉（这正是 `dsh` 在例外集里的原因）
  · 提交前发现 `src/backend/bisheng/config.yaml` 被 `src/backend/bisheng/.gitignore` 忽略——改它不会进版本库，T009 原本对着它断言等于对着一个没人提交的文件测试，fresh clone 必红
  · 改为同时改**两个已跟踪模板** `docker/bisheng/config/config.yaml`（compose 挂载源）与 `docker/helm/bisheng/files/config.yaml`；T009 的 yaml 测试随之改为遍历这两个模板，并新增 `test_local_dev_config_yaml_stays_git_ignored` 钉住「dev 副本永远不承担测试语义」这个前提
  · 本地 dev 副本仍保留该段（方便本机排查），但测试不再依赖它
  · 2026-11-17 实现；`create_app()` 冒烟通过：6 层中间件 / 885 路由（网关全关，未新增任何一层）| 无；T009 |
| [x] | T061 测试/基础设施 | `BT/test_gateway_tenant_filter.py`：断言 `_TENANT_AWARE_MODEL_MODULES` 含 `bisheng.gateway.domain.models.*`；子租户写入自动填 `tenant_id`、跨租户读取被过滤
  · 8 tests passed（先红：`bisheng.sensitive_word.domain.models.sensitive_word_policy missing from _TENANT_AWARE_MODEL_MODULES`）
  · `_force_import_all_models()` 进 fixture——白名单是靠 `import` 副作用登记的，不强制导入时 `_discover_tenant_aware_tables()` 为空，测试会假绿
  · `_tenant_events` fixture 显式调 `register_tenant_filter_events()`：`do_orm_execute` 监听器是**进程级**的，依赖别的测试模块先注册属于顺序耦合
  · 子租户 `tenant_id` 自动填走 `before_flush`；读裁剪走 `build_tenant_filter_clause`；root 上下文可见全部
  · 2026-11-17 实现 | 无；T062
| [x] | T062 基础设施 | `BE/core/database/tenant_filter.py`：把 `bisheng.gateway.domain.models`（三个模型模块）与 `bisheng.sensitive_word.domain.models` 加入白名单
  · 三个 gateway 模块已在 T007 登记；本次补 `bisheng.sensitive_word.domain.models.sensitive_word_policy`
  · 按**具体模块**而非包名登记：`sensitive_word/domain/models/__init__.py` 是空文件，导入包名不会带上模型（已核实）
  · 该表有 `tenant_id` 列但此前不在 `_discover_tenant_aware_tables()` 里——列是装饰品，这正是 v2.5 knowledgefile/flowversion/roleaccess 的 bug 形态
  · 回归：`test/sensitive_word` + `test/workstation` 的 FAILED 集合在登记前后逐字节相同（12 条既有环境失败），零新增
  · 2026-11-17 实现 | 无；T061

## Wave 2：License 准入门（首个可灰度能力）

| 状态 | ID/类型 | 文件与自包含任务 | 依赖/配对 |
| --- | --- | --- | --- |
| [x] | T011 测试/Domain | `BT/test_license_gate.py`：纯函数表驱动——缺失/已过期/未生效/有效四种状态 × {SSO 登录入口, 商业管理面, 授权状态查询, 开源面, DSH 面} → 放行或 26603；**DSH 面与授权状态查询在四种授权状态下全部放行**（授权状态查询若被自己的门关掉，前端横幅将无法展示真实状态，AC-35）；SSO 登录入口与商业管理面在缺失/过期/未生效时拒绝；过期后不返回缓存放行；存储不可用或同步未产出 → fail-closed 拒绝 SSO 与商业管理面，核心/开放/DSH/授权状态查询不受影响；**仅 `etl` 行有效即放行**（`license_codes` 默认 `[etl, gateway]`）；**不触发 ETL 刷新、不写 `license_info`**（只读，决策 6）；`BISHENG_PRO` 不参与裁决。覆盖 AC: AC-02, AC-33, AC-34, AC-47 | T010；T012 |
| [x] | T012 Domain | `BE/gateway/domain/services/license_gate.py`：读 `LicenseInfoRepository`（不新建查询路径）+ 进程内 60s 缓存；有效期用 Owner 的 `compute_display_state`/`compute_days_remaining` 重算（不信任缓存行的 `display_state`，`aggregator.py:44-45` 同样每次读取重算）；**只读，不写表、不发起 ETL HTTP**；输出拒绝原因分类。覆盖 AC: AC-33 | T011 |
| [x] | T013 测试/基础设施 | `BT/test_gateway_middleware_license.py`：TestClient 打 SSO 登录入口与商业管理面（过期→v1 200+26603 信封、v2 真 403）、授权状态查询与开源面（放行）；**`/api/v1/dsh/**` 在过期授权下放行**；`gateway.enabled=false` 时中间件不存在；验证注册顺序（租户 ContextVar 对中间件可见）。覆盖 AC: AC-01, AC-02, AC-03, AC-33, AC-47 | T010, T012；T014 |
| [x] | T014 基础设施 | `BE/gateway/infrastructure/middleware.py`（纯 ASGI 骨架：路径分流 v1/v2/v3/dsh、`GATEWAY_METRIC` 结构化日志、X-Trace-ID 透传、license 门接线）+ `BE/main.py` 在 `CustomMiddleware` 之前一行注册（design §5.2、坑 #5/#7）。覆盖 AC: AC-01, AC-03, AC-05, AC-42 | T013 |
| [x] | T015 测试/基础设施 | `BT/test_license_source_gateway.py`（2026-11-10 改版）：`/api/v1/gateway/license/status` 委托 Owner 的 `list_license_status` 返回、与 `license_info` 行一致；**仅一条未过期 `etl` 行即可放行商业面**（`license_codes` 默认含 etl）；`/api/v1/commercial-licenses/gateway` 外部注入路径仍可用且**F120 未改动 `commercial_license/` 下任何文件**（用 `git diff` 断言该目录零改动——release-contract 第 49 行只允许读取/调用 Owner Service）；不因 `BISHENG_PRO=true` 放行（环境变量不是授权依据）。覆盖 AC: AC-35, AC-36 | T014；T016 |
| [x] | T016 Domain/API | `BE/gateway/api/endpoints/license.py`（授权状态聚合，**只读**：调 Owner 的 `list_license_status` + `LicenseInfoRepository.get_all`，不触发 ETL 刷新、不写表）+ `BE/gateway/api/router.py` 聚合 + `main.py` 挂载。**硬约束：不得修改 `commercial_license/` 下任何文件**（`etl_sync.py:42` 无 source 参数、license_code 硬编码 `"etl"`，所谓 `source='gateway'` 行在代码里不存在；release-contract 第 49 行禁止非 Owner 改其他对象写入行为）。**`main.py` 必须在模块顶层 import gateway router**（不能放进 `create_app` 内部）：`_discover_tenant_aware_tables`（`core/database/tenant_filter.py:187`）依赖 SQLModel.metadata，而 `register_tenant_filter_events` 在 DatabaseManager 初始化时才跑（`core/database/manager.py:62`），顶层导入才能保证新表进入租户过滤发现集。覆盖 AC: AC-35 | T015 |

## Wave 3：内容安全（DSH 接入 + 管理面 + 拦截记录）

| 状态 | ID/类型 | 文件与自包含任务 | 依赖/配对 |
| --- | --- | --- | --- |
| [x] | T017 测试/Domain | `BT/test_dsh_chat_input_safety.py`：命中→返回 auto_reply 形态的正常回答 + SSE 安全事件、非 4xx；未命中→原文透传；策略关闭→透传；无有效词→透传。覆盖 AC: AC-15, AC-22 | T005；T018 |
| [x] | T018 Domain | `BE/dsh/api/endpoints/models.py`（chat/completions 入口）+ `BE/dsh/domain/services/` 输入判定：调 `SensitiveWordPolicyService`（business_type=`dsh_chat`），不新建匹配逻辑；保持 DSH 凭证/额度/审计语义。覆盖 AC: AC-15 | T017 |
| [x] | T019 测试/Domain | `BT/test_dsh_chat_output_safety.py`：流式命中跨分片/UTF-8/转义序列在完整词交付前阻断；保留已交付前缀；事件顺序不变；非流式同样覆盖。覆盖 AC: AC-16 | T018；T020 |
| [x] | T020 Domain | `BE/dsh/domain/services/` 流式输出：包 `StreamContentSafetyScanner`（DSH_CHAT），增量 feed + finish；不改协议事件结构。覆盖 AC: AC-16 | T019 |
| [ ] | T021 测试/Domain | `BT/test_dsh_safety_policy_api.py`：DSH 策略 ↔ `SensitiveWordPolicyPayload` 映射 round-trip（input/output_enabled→extra_config、word_source↔words_types、extra_words↔custom_words、block_message↔auto_reply）；假启用拒绝（26608）；并发保存冲突拒绝；权限三分（无权限/租户管理员/超管指定租户）；生效时延 ≤5s（clear_cache）。覆盖 AC: AC-17, AC-18, AC-19, AC-20, AC-21 | T005；T022 |
| [ ] | T022 Domain/API | `BE/gateway/api/endpoints/dsh_content_safety.py`：settings/capabilities/preview 三端点 + 策略映射适配层；只走 `SensitiveWordPolicyService` 读写（坑 #10）；权限判定经 `PermissionService`（C4）复用平台租户管理员判定（内部走 OpenFGA），不采信 DSH profile 身份、不建第二套权限模型（design §5.4）。覆盖 AC: AC-17, AC-18, AC-19, AC-21 | T021 |
| [ ] | T023 测试/Domain | `BT/test_block_record.py`：命中写入字段完整（face/stage/词/预览截断/话术/request_id）；分页与租户隔离；跨租户不可见；90 天边界清理判定。覆盖 AC: AC-23 | T007；T024 |
| [ ] | T024 Domain/API | `BE/gateway/api/endpoints/block_record.py` + `BE/gateway/domain/services/block_record_service.py`：write-behind 写入（复用 `BatchedRecordWriter` 模式）+ 分页查询。覆盖 AC: AC-23 | T023 |
| [ ] | T025 Domain | `BE/workstation/domain/services/chat_service.py`、`BE/channel/domain/services/channel_service.py`、`BE/knowledge/` 文件解析链、`BE/linsight/api/endpoints/linsight.py` 各命中点：追加一行拦截记录写入；不改判定结果/话术/事件/错误（零变化回归护栏见 T064）。覆盖 AC: AC-24 | T024 |
| [ ] | T064 测试/Domain | `BT/test_gateway_zero_change_on_existing_faces.py`（2026-11-10 增补）：对四个已接入面同一命中输入，在**接入前**（基线提交 `git show HEAD:<path>` 取接入前源码，或预先保存的 fixture 快照）与**接入后**逐一比对——工作台/DSH/渠道/知识四面的话术文案、`DshSensitivePolicy` 事件写入、错误信封与状态码、`auto_reply` 内容完全一致，仅多一条 `gateway_block_record` 行。**这是 AC-24 唯一的护栏测试**：没有它，T025 的「不改」只是承诺。覆盖 AC: AC-24 | T025 |
| [ ] | T026 Domain/API | `BE/gateway/api/endpoints/dsh_content_safety.py` 增加 events 分页与 `events/{id}` 详情（读 `dsh_content_safety_event` 既有审计 + 新记录关联）。覆盖 AC: AC-23 | T022, T024 |
| [ ] | T027 Worker | `BE/worker/gateway/tasks.py` + `core/config/settings.py` beat：拦截记录 90 天保留清理（仿 `knowledge_chat_history_retention.py`），参数带 tenant_id、执行前恢复 ContextVar。覆盖 AC: AC-23 | T024 |
| [ ] | T028 前端 | `FE/controllers/API/dshContentSafety.ts`（或等效处）：`/api/sensitive/dsh/*` 切到后端同路径兼容路由；契约字段已对齐故页面零改动；确认 `dshContentSafetyStyles` scope 仍覆盖。覆盖 AC: AC-17, AC-39<br>**手动验证**：`BISHENG_PRO=true` 起后端，打开 DSH 内容安全设置页→改策略保存→5s 内新会话生效；造一个命中词发起会话→见阻断话术与拦截记录 | T022 |

| [ ] | T057 测试/Domain | `BT/test_open_api_chat_safety.py`（2026-11-10 增补）：模型协议面输入命中→调用模型前即被拒（断言未发起上游调用）、错误语义与既有开放面一致、不透传命中内容；流式输出命中→中断且保留已交付前缀、事件顺序不变；未命中→原文透传。覆盖 AC: AC-43, AC-44, AC-24 | T005, T017；T058 |
| [ ] | T058 Domain | `BE/open_api/api/endpoints/model_gateway.py`（`/chat/completions`）输入判定 + 流式输出包 `StreamContentSafetyScanner`（business_type 传开放面枚举），命中抛该面既有错误类型；**不改为伪装回答**（API 客户端按错误码处理）；命中点补 `gateway_block_record`。覆盖 AC: AC-43, AC-44, AC-24 | T057, T024 |

## Wave 4：流量控制（QPS + WS 在线计数）

| 状态 | ID/类型 | 文件与自包含任务 | 依赖/配对 |
| --- | --- | --- | --- |
| [ ] | T029 测试/基础设施 | `BT/test_token_bucket_lua.py`（真实 Redis）：放行扣减、拒绝返回等待 ms、按 `now_ms` 补充（禁依赖本机时钟）、burst 上限、单 key 可 Cluster 分片、cost>burst 直接拒绝。覆盖 AC: AC-25 | 无；T030 |
| [ ] | T030 基础设施 | `BE/gateway/infrastructure/lua/token_bucket.lua` + `domain/services/rate_limiter.py`（now_ms 由应用传入、拒绝时返回等待时间、Redis 异常上抛由中间件 fail-open）。覆盖 AC: AC-25, AC-29 | T029 |
| [ ] | T031 测试/基础设施 | `BT/test_online_counter_lua.py`：建连 incr、超限拒绝、断连按 session 映射精确 decr、异常断连不泄漏、部门/组/资源三级 key。覆盖 AC: AC-30, AC-31, AC-46 | 无；T032 |
| [ ] | T032 基础设施 | `BE/gateway/infrastructure/lua/online_counter.lua` + `domain/services/online_counter.py`（`gw_online:{tenant}:{scope}` + `gw_online_session:{session_id}` 两级映射；scope 覆盖 dept / group+resource）。覆盖 AC: AC-30, AC-46 | T031 |
| [ ] | T033 测试/基础设施 | `BT/test_gateway_middleware_ratelimit.py`：v1 面 200+26601 信封、v2 面真 429；双 worker 语义（共享存储下总量=配置，禁进程内计数）；规则解析优先级 user>group>dept>global 与默认 QPS；Redis 故障 fail-open 且有 `GATEWAY_METRIC` 痕迹；`filter_url` 未命中路径不限流。覆盖 AC: AC-25, AC-26, AC-27, AC-29, AC-42 | T014, T030；T034 |
| [ ] | T034 Domain/基础设施 | `BE/gateway/infrastructure/middleware.py` 接线限流 + 规则解析服务（`domain/services/rate_rule_resolver.py`，进程内 30s 缓存 + 订阅 `delete_group` 频道失效，复用 `user_group_service.py:44` 既有频道）。覆盖 AC: AC-26, AC-27, AC-28 | T033 |
| [ ] | T035 测试/基础设施 | `BT/test_gateway_middleware_online.py`：WS 建连前拒绝（关闭码 4029 + reason）、正常建连计数、断连回收、部门 `concurrent_session_limit` 阈值生效、用户组/资源阈值独立生效、`online.enabled=false` 时跳过。覆盖 AC: AC-30, AC-31, AC-46 | T032；T036 |
| [ ] | T036 基础设施 | `BE/gateway/infrastructure/middleware.py` WS 分支接线（纯 ASGI `scope["type"]=="websocket"`，accept/close 事件拦截，禁 BaseHTTPMiddleware）；阈值解析支持部门/用户组/资源三级。覆盖 AC: AC-05, AC-30, AC-46 | T035 |
| [ ] | T037 测试/API | `BT/test_rate_rule_api.py`：规则 CRUD + 唯一键冲突 + 权限（租户管理员限本租户/超管指定租户）+ 保存后 30s 生效 + 用户组变更后缓存失效。覆盖 AC: AC-28 | T034；T038 |
| [ ] | T038 Domain/API | `BE/gateway/api/endpoints/rate_rule.py`（CRUD）+ `BE/gateway/api/endpoints/group.py`（`/api/group/list\|save`、`/api/resource/groupFlows` 兼容面：读 `user_group` 模块，组级阈值写 `gateway_rate_rule`，不新建组模型）；权限判定经 `PermissionService`（C4，平台管理员）。覆盖 AC: AC-27, AC-28 | T037 |
| [ ] | T039 前端 | `FE/controllers/API/rateControl.ts` + `FE/pages/SystemPage/` 限流规则与拦截记录 tab（Zustand + react-query，走 controllers 层）+ 三语文案（走 T065）。覆盖 AC: AC-32, AC-39<br>**手动验证**：设某助手 1 QPS → 连续请求见 26601 提示；设部门并发 1 → 第二个 WS 见 4029 关闭与提示（Client 侧提示见 T063）；拦截记录页按面/用户/时间筛选 | T038, T024, T065 |

## Wave 5：SSO / OAuth Provider 侧

| 状态 | ID/类型 | 文件与自包含任务 | 依赖/配对 |
| --- | --- | --- | --- |
| [ ] | T040 测试/Domain | `BT/test_oauth_provider.py`：配置校验（缺端点/空 clientId/未知 type→26608）；secret 密文存取 round-trip；CRUD 权限；停用后 authorize/callback 双端 26605。覆盖 AC: AC-06, AC-12, AC-41 | T007；T041 |
| [ ] | T041 Domain/API | `BE/gateway/api/endpoints/provider_admin.py` + `domain/services/oauth_provider_service.py`：CRUD + Fernet 密文（参照 `sandbox_endpoint` token ciphertext 模式）；禁默认空值放行。覆盖 AC: AC-41 | T040 |
| [ ] | T042 测试/Domain | `BT/test_oauth_state.py`：state 一次性消费（并发双消费只一次成功）、TTL 到期、return_to 非同源拒绝、缺失 26604。覆盖 AC: AC-07 | 无；T043 |
| [ ] | T043 Domain | `BE/gateway/domain/services/oauth_service.py`：state 生成/消费（Redis 单 key Lua GET+DEL，仿 `tickets.py:38`）+ return_to 同源校验 + authorize 302 构造。覆盖 AC: AC-07 | T042 |
| [ ] | T044 测试/Domain | `BT/test_rsa_key_service.py`：密钥对首次生成并以 Fernet 密文落 `config` 表（新增 `ConfigKeyEnum.GATEWAY_RSA_KEY`）、重启后稳定、密文不明文可读（DB dump 不含 PEM 私钥）、`getkey` 返回 PKCS#1 PEM 公钥、轮换时旧钥仍可解 grace 期。覆盖 AC: AC-11, AC-41 | 无；T045 |
| [ ] | T045 Domain | `BE/gateway/domain/services/rsa_key_service.py`（Fernet 密文存取复用 `app_publish/domain/services/secret_security.py` 模式；**不仿 `user/domain/services/jwt_secret.py` 的明文落库**——`jwt_secret.py:81` 直接 `Config(value=candidate)` 且 `common/models/config.py` 无加密层，明文私钥违反 C6，design §5.3）+ `BE/gateway/api/endpoints/oauth2.py` 的 `getkey`。覆盖 AC: AC-11 | T044 |
| [ ] | T046 测试/API | `BT/test_oauth_callback.py`（mock IdP）：完整授权码流 authorize→token→userinfo→进程内 `LoginSyncService.execute`→Set-Cookie+302；换 token 失败/超时/结构异常→26606 且不建用户不签发；禁用用户拒绝； claims 与平台登录同构（同 cookie key、同 user_id/tenant_id/token_version）；bump token_version 后旧会话失效。覆盖 AC: AC-08, AC-09, AC-10, AC-13 | T043, T045；T047 |
| [ ] | T047 Domain/API | `BE/gateway/api/endpoints/oauth2.py`：providers 列表 / authorize / callback；in-process 调 `LoginSyncService`（禁自调用 HTTP，design 决策 2）；httpx 超时 5s；Set-Cookie 用平台同一 cookie key。覆盖 AC: AC-06, AC-08, AC-09, AC-10 | T046 |
| [ ] | T048 测试/API | `BT/test_ldap_login.py`（mock ldap3）：bind 成功→同一登录路径；失败→26607；空配置/不可达→明确错误；断言密码不出现在日志、响应、异常消息中。覆盖 AC: AC-11 | T047；T049 |
| [ ] | T049 Domain/API | `BE/gateway/domain/services/ldap_service.py` + `ldap` 端点；`src/backend/pyproject.toml` 增加 `ldap3` 并记录依赖许可评审结论。覆盖 AC: AC-11 | T048 |
| [ ] | T050 Domain | `BE/gateway/domain/services/oauth_service.py` 增加 `wecom_qr`/`wecom_web`：企微 OAuth 端点 + userinfo（复用 `org_sync/domain/providers/wecom.py` 的 token 缓存，不新造 HTTP 客户端）。覆盖 AC: AC-06, AC-09 | T047 |
| [ ] | T051 前端 | `FE/controllers/API/pro.ts`（`getSSOurlApi`/`getKeyApi`/`ldapLoginApi`/`/api/group/*`/`/api/sensitive/*` 系列切到 `/api/v1/gateway/*` 同源）+ `FE/pages/LoginPage/loginBridge.tsx`、`login.tsx` 自动跳转与 return_to 契约不变（`utils/loginReturnTo.ts`）。覆盖 AC: AC-14, AC-39<br>**手动验证**：登录页出现各登录方式→点击完成登录→落地页正确→登出→禁用账号立即失效 | T047, T049, T050, T065 |
| [ ] | T063 前端 Client | `client/src/api/request.ts` WS 关闭码分支（2026-11-10 增补，design §5.5 决策 5 承诺）：4029 复用既有业务码 toast 通道（参照 11001 的处理，`client/src/api/request.ts:207-224`），提示文案三语（走 T065）；**不新增 store、不改 recoil atom**（C7）。覆盖 AC: AC-31, AC-39<br>**手动验证**：设部门并发 1 → 第二个 WS 连接被关且用户看到「并发已满」提示，已有会话不受影响 | T032, T065 |
| [ ] | T066 前端 | `FE/controllers/API/license.ts`：自研模式下不再调 `reportGatewayLicense`（后端只读 `license_info`，双跑期保留开关）；`FE/vite.config.mts:174-181` 的 `VITE_GATEWAY_PROXY_TARGET` `/api/license` 独立代理退场（后端无此路径，404-silent 已兼容；注意 :197-217「更具体前缀先注册」注释）。覆盖 AC: AC-35, AC-36 | T051, T016 |

| [ ] | T059 测试/Domain | `BT/test_wecom_org_sync.py`（2026-11-10 增补）：周期同步成功落部门树+成员；IdP 失败留下可观测痕迹且不影响其他登录方式与 DSH 本地面；web 回调带应用标识时按配置内嵌地址落地、缺配置回落默认首页。覆盖 AC: AC-45, AC-48 | T044, T050；T060 |
| [ ] | T060 Domain/Worker | `BE/gateway/domain/services/wecom_org_sync.py` + `BE/worker/gateway/tasks.py` + `core/config/settings.py` beat（周期可配，复用 `org_sync/domain/providers/wecom.py` 的 `fetch_departments`/`fetch_members` 拉取 + `org_sync/domain/services/reconciler.py` 的 `reconcile_departments`/`reconcile_members` 纯函数落地，不新造 HTTP 客户端；**不要调 `OrgReconcileService.reconcile_config`——它没有任何调用方、属未接线状态**，design §5.3 / 坑 #15）；`oauth_service` 的 `wecom_web` 回调解析应用标识并按 `web_home_url` 落地。**租户传递**：Celery 任务头取 tenant_id、执行前恢复 ContextVar（同 T027），跨租户遍历 `gateway_oauth_provider` 时逐 provider 建 session，禁止沿用上一个任务的 ContextVar（C3）。覆盖 AC: AC-45, AC-48 | T050 |

## Wave 6：迁移、e2e 与收口

| 状态 | ID/类型 | 文件与自包含任务 | 依赖/配对 |
| --- | --- | --- | --- |
| [ ] | T052 测试/基础设施 | `BT/test_route_ownership.py`：`route_ownership` 白名单外前缀中间件不生效；与 Java gateway 并存模拟（stub 外部 8180）下两类能力互不干扰。覆盖 AC: AC-04, AC-37 | T014；T053 |
| [ ] | T053 基础设施 | `BE/core/config/gateway.py` 增加 `route_ownership` + `CFG/config/config.yaml` 注释说明双跑切换；`DOC/11-gateway.md` 增加"自研网关（F120）"章节并标注 Java gateway 为遗留可选组件。覆盖 AC: AC-04, AC-37 | T052 |
| [ ] | T054 e2e | `/e2e-test features/v3.0.0/120-native-gateway`：双层——API pytest 覆盖 SSO 登录→聊天命中→拦截记录可查→限流拒绝→license 过期商业管理面拒绝（**并断言同期 DSH 与开放 API 面不受授权影响**）；页面手动清单（登录页、DSH 设置页、限流/拦截记录 tab、License 横幅）。全链路 `BISHENG_PRO=true`。**fixture 前置**：先经 Owner 的 `upsert_mapped` 写一条未过期 `license_info` 行，否则 SSO 登录入口被 26603 挡住、e2e 无法登录（`BISHENG_PRO` 不是授权依据）。覆盖 AC: AC-01, AC-02, AC-03, AC-04, AC-05, AC-06, AC-07, AC-08, AC-09, AC-10, AC-11, AC-12, AC-13, AC-14, AC-15, AC-16, AC-17, AC-18, AC-19, AC-20, AC-21, AC-22, AC-23, AC-24, AC-25, AC-26, AC-27, AC-28, AC-29, AC-30, AC-31, AC-32, AC-33, AC-34, AC-35, AC-36, AC-37, AC-38, AC-39, AC-40, AC-41, AC-42, AC-43, AC-44, AC-45, AC-46, AC-47, AC-48 | T028, T039, T051, T063 |
| [ ] | T055 基础设施 | `features/v3.0.0/120-native-gateway/deployment.md`：nginx/vite target 切换步骤、五阶段灰度与回滚（design §5.11）、运维检查清单（license 同步、Redis、三语检查）。覆盖 AC: AC-37, AC-38 | T053 |
| [ ] | T056 收口 | 逐任务 `/task-review` 勾选本清单；`/sdd-code-review --base origin/main`；更新 design.md 修订历史与「实际偏差记录」。覆盖 AC: 全部 | T054 |

---

## 实际偏差记录

> 只留一行指针，论证在 design.md（决策 / 坑）。推翻已 ★ 确认的决策时先停下与用户重新确认。

- （暂无）
