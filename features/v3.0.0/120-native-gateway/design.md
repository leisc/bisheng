# Design: 自研网关（Native Gateway）

> **本文档定位 — 现状快照（Why this How）**
>
> - `spec.md` 回答 **做什么**（目标、AC、边界）—— spec 已于 2026-11-10 经用户确认（✅ 已评审，48 条 AC），本文档与其对齐
> - `design.md`（本文）回答 **为什么这么实现**：关键决策、运行时不直观的事实、对外契约
> - `tasks.md` 是 **流水账**：拆了哪些任务、做了什么改动
>
> 调整原则（详见 `docs/SDD-Guide.md` §3-§4）：
> - 实现变化 → **覆盖更新本文档**，只留"今天的状态"、不留旧快照；但每个决策保留"为什么 + 被否方案"和坑（护栏，见 §3/§8）
> - **偏差分级**：推翻已 ★ 确认的决策 → 停下与用户重新确认；纯实现细节 → 直接改 design，不必停

**关联**: [spec.md](./spec.md)（✅ 已评审 2026-11-10，48 条 AC） · [tasks.md](./tasks.md)（6 Wave / 60 任务，未开工）
**版本**: v3.0.0
**最后更新**: 2026-11-10（初版基于对当前工作树的只读调研；同日经 `/sdd-review design` 后按用户裁决修订——RSA 私钥改 Fernet 密文、新表补租户过滤登记、SSO 登录入口纳入 License 授权门；所有行号锚点以当日代码为准）

---

## 1. 目标与非目标

- **目标**：把商业版 Java 网关（`bisheng-gateway`，架构说明见 `docs/architecture/11-gateway.md`）的四类能力——SSO/OAuth 统一认证、内容安全、流量控制（限流 + 在线计数）、License 准入——收敛进后端进程，形成进程内原生网关层 `bisheng/gateway/`。
  - 价值：前端在网关模式下直连后端，消除"第二套进程 + 第二套数据库 + 第二套密钥 + 两套 HMAC"的长期维护面（为何不新建代理进程见决策 1）。
- **非目标**（明确不做，防范围蔓延）：
  - **不做通用反向代理**。HTTP/WS 反代由 nginx（`src/frontend/nginx.conf`）与部署侧承接，不自研代理进程（理由见决策 1）。
  - **不重写敏感词引擎**。AC 自动机、策略、流式扫描全部复用 `bisheng/sensitive_word/`（决策 3）。
  - **不替代托管应用入口代理**。`app_runtime` 的 app-proxy / OBO / HMAC 体系由 F054 拥有，本 feature 只读复用，不扩展。
  - **不动 DSH 桌面端本地认证**（F110 已解除 Java Gateway 依赖，`docs/architecture/11-gateway.md:5`）；本 feature 只给 DSH 聊天面补内容安全（§5.4）。
  - **不做多租户级 OAuth Provider 配置的完整产品化**（v1 只做全局默认 + 租户覆盖的存储预留，见 §9）。
  - **不改平台登录 JWT 的 claims 结构**（签发函数 `LoginUser.create_access_token` 原样复用）。

---

## 2. 关键约束

本功能**特有**的硬性约束（决定下面方案对比的取舍空间）。全局架构铁律（DDD 分层 / 双 DB / 多租户 / 权限 / 错误码 / 密钥）遵循 `docs/constitution.md` C1–C7，不在此重抄。

| # | 约束 | 量化 / 依据 |
|---|---|---|
| K1 | **单进程热路径预算**：网关中间件对每个请求的额外开销（限流查询 + license 缓存读）p99 ≤ 2ms | 限流 = 1 次 Redis 单 key 访问（ Lua 一次搞定）；license = 进程内缓存（60s TTL）+ Redis 备份，禁每请求查库 |
| K2 | **流式不可破坏**：SSE / WebSocket 路径不得被缓冲或改写 | 现有 chat SSE 在 `workstation/domain/services/chat_service.py:1710-1755` 等处直接产 `StreamingResponse`；`BaseHTTPMiddleware` 会打断流，网关中间件必须是**纯 ASGI** |
| K3 | **与 Java gateway 双跑期契约不变**：HMAC 签名公式 `METHOD\nPATH\nBODY` + `X-Signature` 头保持字节一致 | `sso_sync/domain/services/hmac_auth.py:44`、`app_runtime/domain/services/hmac_auth.py:52`、`org_sync` 侧共 3 份独立拷贝；规范实现是 `common/utils/hmac_signature.py:10` |
| K4 | **License fail-closed**：商业面在授权失效时必须拒绝，不得静默降级 | 现状 `BISHENG_PRO` 是进程级环境变量（`common/services/config_service.py:480,488` 覆盖 DB），与 `license_info` 表两套机制已证实会漂移（决策 6） |
| K5 | **拒绝语义按面分裂**：v1 面 HTTP 200 + body 信封；v2/v3/app-proxy 面真 HTTP 状态码 | `main.py:35` 全局 handler vs `open_api/api/exception_handlers.py:161`；新过滤器必须先按前缀分流，不能假设状态码语义一致 |
| K6 | **DM8 兼容 + 多租户自动注入**：新表一律 `dialect_helpers`（`JsonType`/`LargeText`），禁手写 `WHERE tenant_id = X` | constitution C2/C3 |
| K7 | **密钥治理**：所有 secret 走 `config.yaml !env` / Fernet 密文，禁硬编码、禁默认空值放行 | constitution C6；参照 `sso_sync.gateway_hmac_secret` 空 = fail-closed 模式（`core/config/sso_sync.py:31`） |
| K8 | **前端三语文禁**：新错误码 / 共享文案必须三语同步并过 `pnpm --filter @bisheng/locales check` | `src/frontend/packages/locales/README.md` |

---

## 3. 方案对比与选定

> 核心设计决策。agent 接手时先读这里——下面每条都记录了"想当然会走但被否决"的路。

### 决策 1：网关落点 = 后端进程内模块，不新建独立代理进程

- **备选**：
  - A. **独立 Python 代理进程**（如 `src/gateway/`，反代到 `backend:7860`，复刻 Java gateway 拓扑）——优点：与后端故障隔离、可独立扩缩容；缺点：要么把整套 JWT 解析 / 租户上下文 / 权限判定**复刻一遍**，要么把身份判断回源给后端（多一跳网络、多一套密钥、多一个故障点）；SSE/WS 代理要处理连接升级、背压、`X-Forwarded-*` 头语义（`docs/architecture/11-gateway.md` §10.1 记载的坑会原样继承）。
  - B. **后端进程内 `bisheng/gateway/` 模块**（纯 ASGI 中间件 + 路由），前端直连后端——优点：认证 / 租户 / 权限 / 内容安全执行层全在同一进程，零身份复刻；中间件天然跑在路由之前，无需"代理"；运维面不增；缺点：网关逻辑与后端共享进程资源（限流 / AC 匹配的 CPU 占用计入后端）；后端整体故障时没有"网关级维护页"。
- **选定**：B
- **原因**：Java gateway 的存在理由是"后端没有这些能力"。现在执行层（敏感词、配额、SSO 接收侧、license 表）全在后端，代理进程只剩"转发 + 重复鉴权"的空壳。前端切换成本为零（`VITE_PROXY_TARGET` 改回 7860 或删掉网关层）。"维护页"诉求用 nginx `error_page` 等价实现。
- **何时该重新考虑**：① 出现外部 IdP 硬性要求前置代理改 Host/Path；② 网关 QPS 需要与后端独立扩缩容（按当前单机双 worker 规模不成立）；③ 合规要求网关与业务进程网络隔离。

### 决策 2：SSO/OAuth Provider 侧在后端自实现，配置入库，登录态签发复用 LoginSyncService（in-process）

- **备选**：
  - A. 继续让外部进程做 Provider 侧，后端只留 HMAC 接收端点（现状）——优点：零新增；缺点：Java gateway 不在了就没有 Provider 侧，SSO 直接不可用。
  - B. 后端新增完整 OAuth2 授权码客户端（authorize / callback / LDAP / RSA getkey），Provider 配置落表 `gateway_oauth_provider`，state/nonce 存 Redis，回调后**进程内直接调** `LoginSyncService.execute`。
  - C. 后端实现 Provider 侧但走 HTTP 调自己的 `/api/v1/internal/sso/login-sync`（保留 HMAC 往返）——自调用自签，纯粹浪费。
- **选定**：B
- **原因**：接收侧（用户 upsert / 跨源收养 / 部门绑定 / 叶子租户派生 / JWT 签发 / 禁用短路）已在 `sso_sync/domain/services/login_sync_service.py:100-230` 完整实现且经过生产验证，in-process 调用免去 HMAC 密钥与网络往返；Provider 侧（多 IdP 配置化 + 授权码流 + LDAP bind + RSA 密码传输）是本仓库**完全空白**的部分，正是要补的。配置入库而非 `config.yaml`：运营改 IdP 不能重启进程，且为后续租户级配置留位。
- **被否细节**： Provider 配置塞 `config.yaml`（无法热更新、违反运营常识）；复用 DSH 的 PKCE 设备流（语义不符，DSH 是桌面端循环回调，不是浏览器三方登录）。
- **何时该重新考虑**： 需要 OIDC 标准发现 / JWKS 轮换（当前只做授权码 + userinfo 两点式）。

### 决策 3：内容安全不重写边缘过滤，复用 `sensitive_word` 执行层并按 face 补齐

- **备选**：
  - A. 在网关中间件复刻 Java gateway 的 4 个 GlobalFilter（请求过滤 + SSE 响应过滤 + 拦截记录）——优点：与 Java gateway 行为一比一；缺点：`workstation/domain/services/chat_service.py:1739/2667`（输入判定）与 `:1849-2531`（流式输出扫描）**已经在做同样的事**，linsight 入口 `linsight/api/endpoints/linsight.py:189` 已过检；重复扫描 = 双倍 AC 匹配成本 + 两套 `auto_reply` 话术漂移风险。
  - B. 不新增边缘过滤；把现有执行层显式封装为"内容安全门"应用服务，**只给未接入的 face 补接入**（DSH 聊天面），网关模块负责管理面端点 + 拦截记录 + 未接入面的显式拒绝策略。
- **选定**：B
- **原因**：执行层在内核（`sensitive_word/domain/services/ac_automaton.py` + `sensitive_word_policy_service.py` + `stream_scanner.py`），边缘复制没有价值。Java gateway 的边缘过滤是"后端无此能力"时代的补丁；它的真正遗产是**拦截记录与管理面**，这部分入库实现。
- **关键行为契约（不可改）**：命中不返回 4xx，而是**伪装成正常 LLM 回答**推 `auto_reply` + SSE `content_safety_blocked` 事件（`workstation/domain/services/content_safety.py:16-45`）。改成错误码会破坏前端 chat UI。
- **何时该重新考虑**： 出现后端不可控的第三方聊天入口（客户自建前端直连内部端点）。

### 决策 4：QPS 限流 = Redis Lua 令牌桶（单 key，Cluster 安全），不用进程内计数器

- **备选**：
  - A. 进程内令牌桶（每 worker 一份）——部署是 `uvicorn --workers 2`（`src/backend/entrypoint.sh:57`），两份桶 = 限额翻倍，语义错误。
  - B. Redis Lua 令牌桶，key = `gw_rl:{tenant}:{subject}:{route_group}`，一次 `EVAL` 完成取令牌 + 计数——Cluster 安全（单 key），跨 worker 一致，失败方向可设计。
  - C. 复用 DSH 的 `DshAuthorizationService.rate()`（`dsh/domain/services/authorization.py:23`）——其 key 结构 / 错误码（`DshTooManyRequestsError`）绑死 DSH 授权域，且没有"租户 + 资源维度"概念。
- **选定**：B
- **原因**：A 语义错误；C 是另一个域的专用件。Lua 现成先例：`linsight/domain/services/task_message_broadcast.py:9`（`LINDEX + PUBLISH + 条件 LPOP`，单 key 兼容 Cluster）。`quota_redis` 的 admit/settle 是**月度 token 口径**，与 QPS 不是一回事，禁止混用同一 ledger。
- **失败方向**：Redis 不可用时**限流面 fail-open 放行**（与 `CustomMiddleware` 的 Redis 黑名单检查一致，`utils/http_middleware.py:519` `except: pass`），但打点告警；license 门相反，必须 fail-closed（K4）。
- **何时该重新考虑**：出现 Redis 多活 / 多集群部署（key 前缀需带集群隔离），或限流口径要从"按 subject"改为"按 IP / API key / 第三方应用"。

### 决策 5：WebSocket 在线计数 = 纯 ASGI 中间件 + Redis Lua incr/decr + session 映射

- **现状**：仓库**没有**通用 WS 会话计数。`department/api/endpoints/department_limit.py:1-4` 的 docstring 明说：部门级阈值存在 `department.concurrent_session_limit`，**per-resource 限制"直到部门-资源限制表引入前返回空页"**。
- **备选**：
  - A. 进程内 dict 计数（每 worker 一份）——与决策 4 备选 A 同样的多 worker 虚低问题，且进程重启即丢计数、断连回收不可靠；
  - C. 复用 DSH 授权域的会话能力（`dsh/domain/services/authorization.py`）——语义绑死 DSH 授权域，没有"租户 + 组/资源"两级结构。
- **选定**：新增 `gateway/online/`：中间件拦截 `websocket.connect`（建连前查限额）与 accept/close 事件（incr/decr）；Redis 结构仿 Java gateway 的 `SESSION_ONLINE` 两级映射——`gw_online:{tenant}:{scope}` hash 存计数，`gw_online_session:{session_id}` hash 存"该连接挂在哪个计数 key 下"，断连时据此精确回收，避免泄漏。
- **阈值来源**：部门 `concurrent_session_limit`（存量字段，只读）+ `gateway_rate_rule.concurrent_limit`（新增列，本 feature 的产品面）。
- **拒绝方式**：handshake 阶段直接 `websocket.close` code **4029**（应用私有段）+ reason；前端 WS 客户端按业务码弹既有 toast（参照 client `11001` 的处理，`client/src/api/request.ts:207-224`）。
- **何时该重新考虑**：WS 连接规模大到单 key hash 成为热点（需分片计数 + 近似合并），或前端可消费真正的 HTTP 语义而不必用 4029 私有码。

### 决策 6：License 准入以 `license_info` 表为准；商业面 fail-closed，平台核心不停摆

- **备选**：
  - A. 以 `BISHENG_PRO` 环境变量为准——进程级、`config_service.py:480,488` 会用 env **覆盖 DB 配置**，与 `license_info` 表漂移；改一次 license 要重启全部 unit。
  - B. 以 `license_info` 表 + `commercial_license` ETL 同步为准（`commercial_license/domain/services/etl_sync.py`，Celery beat 已定时刷新，`core/config/settings.py:285-287`），中间件按面 fail-closed。
- **选定**：B
- **与 Java gateway 的刻意偏离**：Java gateway 是 license 过期 `System.exit(0)`（**整个商业面停摆**）。自研网关改为：**商业面请求级拒绝**（错误码 26603），平台核心（开源能力）继续可用。理由：开源部署没有 gateway license，若整体停摆则 OSS 单机版直接被 license 状态绑架；前端 `LicenseBanner` 已承担告知职责（`platform/src/layout/LicenseBanner.tsx`）。
- **`/api/license/status` 不复刻**：该路径属于外部 ETL 服务，毕昇只做缓存投影（`commercial_license/api/router.py:19-31`）。前端 `getLicenseStatus` 的 "404 即预期" 逻辑保留（`platform/src/controllers/API/license.ts:9-12`）。
- **何时该重新考虑**：法务 / 商务要求"过期即整体停摆"（届时把 26603 的拒绝范围从商业面扩到全站，一个配置项的事）。

### 决策 7：与 Java gateway 双跑灰度——路径归属配置化 + target 切换，不做数据同步

- **选定**：
  1. `config.yaml` 新增 `gateway.route_ownership`（路径前缀清单），中间件只对自己 owned 的前缀生效，双跑期两类能力可逐面切换；
  2. 切换 = 改 nginx `PROXY_PASS_URL` / vite target + 停 Java gateway 进程，**零数据迁移**；
  3. **前置重构任务**：把 3 份 HMAC 拷贝收敛到 `common/utils/hmac_signature.py` 唯一实现（`app_runtime/hmac_auth.py:52`、`app_runtime/orchestrator_client.py:113` 各自重写了一份），否则双跑期改签名公式要改 4 处。
- **被否**：一次性替换（商业客户在网，风险不可控）；`gt_*` 表与平台表双向同步（模型不同构，同步层是长期负债，且 `gt_user_group.id` 与主库组 id 一致这种隐式约定会持续咬人）。
- **何时该重新考虑**：双跑期结束、Java gateway 停运后，`route_ownership` 应清空并最终删除该配置项——它只是灰度期开关，不留成永久性路径开关。

### 决策 8：新表 3 张，全部租户感知；错误码新开 26600 段

- **选定**：`gateway_oauth_provider` / `gateway_rate_rule` / `gateway_block_record` 三张新表（§5.7），alembic 迁移 `down_revision` 必须取 `uv run alembic heads` 当日输出（现 head = `f114_app_authorization`，`core/database/alembic/AGENTS.md` 明确警告文件名前缀不表示拓扑顺序）。
- **错误码**：新文件 `bisheng/common/errcode/gateway.py`，占用 **26600–26699** 段。核实：262xx 已被 `dsh_market.py` 占、263xx 被 `mcp_face.py` 占（且注释声明 `26340+` reserved）、265xx 被 `app_publish.py` 占、270xx = `commercial_license`、28xxx 已用；**266 / 267 / 268 / 269 空闲**，取 266。
- **何时该重新考虑**：`gateway_block_record` 单表超千万行（需按 face 分区或转冷存），或 26600–26699 段耗尽需再开一段。

---

## 4. 系统现状（接手必读）

### 4.1 请求链路现状

**网关模式（Java gateway 在位）**：
```
浏览器 → nginx:8860 → Vite:3001（VITE_PROXY_TARGET=:8180）
      → Java Gateway:8180（SSO/敏感词/限流/license，自有 MySQL bisheng_gateway + Redis DB3）
      → Backend:7860
```
**直连模式（当前 OSS / DSH 无网关发布，`feat/bisheng-work-110`）**：
```
浏览器 → nginx → Vite:3001（VITE_API_PROXY_TARGET=:7860）→ Backend:7860
```
后端入口：`bisheng/main.py:192 create_app()`；中间件注册 `:235-252`（**后注册 = 更外层**）；路由树 `bisheng/api/router.py:79`（`/api/v1`）、`:158`（`/api/v2`，router 级 `verify_open_api_access` 依赖）；DSH 兼容面 `dsh/api/router.py:30` 直接挂 app 根；部门限流 `main.py:269-273`（`/api/department-limit`）。

### 4.2 能力盘点矩阵（Java gateway 能力 → 仓库现状 → 缺口）

| Java gateway 能力 | 仓库现状（锚点） | 缺口 = 本 feature 要补的 |
|---|---|---|
| HTTP/WS 反向代理 | nginx 已承接（`src/frontend/nginx.conf:14-31`） | 无 |
| **SSO/OAuth**（自定义 SSO / 企微扫码 / 企微网页 / LDAP）+ 签发 JWT + 写 Cookie | 接收侧齐全：`/api/v1/internal/sso/login-sync`（`sso_sync/api/endpoints/login_sync.py:26`）、`/api/v1/departments/sync`、`/api/v1/internal/sso/gateway-wecom-org-sync`（`:64`）；签发 = `LoginUser.create_access_token`（`user/domain/services/auth.py:494`）；企微通讯录 pull 侧有 `org_sync/domain/providers/wecom.py` | **Provider 侧全空**：authorize/callback、LDAP bind、RSA getkey、Provider 配置存储、state 防 CSRF；**企微组织同步无任何周期任务**（pull→reconcile 管线已实现但 `OrgReconcileService.reconcile_config` 无调用方，§8 坑 15） |
| **内容安全**（请求 + 响应 + SSE） | 执行层齐全：`ac_automaton.py`、`sensitive_word_policy_service.py`、`stream_scanner.py`；workstation 聊天已接入（`chat_service.py:1739/2667/1849-2531`）、linsight（`linsight.py:189`）、频道文章（`channel_service.py:355-380`）、知识库文件（`knowledge_file_pipeline.py:110-115`）、开放 API 任务模式（`task_mode_service.py:272`，**抛 `OpenApiContentBlockedError`**）；Pro 门控 `sensitive_word_policy_service.py:350-358` | DSH 聊天面（`/api/v1/dsh/chat/completions`）**未接入**；开放 API 模型协议面（`open_api/api/endpoints/model_gateway.py:48` `/chat/completions`）**未接入**；管理面端点 `/api/sensitive/dsh/*` 不存在于后端；旧资源级 `/api/sensitive/saveWords|wordsDetail` 不存在；拦截记录无网关级表 |
| **限流 QPS** | DSH 授权有 Redis `rate()`（`dsh/domain/services/authorization.py:23`）；`quota_redis` 是月度 token 口径（`dsh/infrastructure/quota_redis.py:124` admit）；部门并发阈值只存不用（`department_limit.py`） | 通用 QPS 令牌桶、规则表、按面拒绝语义 |
| **WS 在线计数** | 无（`department_limit.py:1-4` 明说 per-resource 返回空页） | 计数器 + session 映射 + 阈值裁决 |
| **License** | `commercial_license`：`license_info` 表、ETL 同步、beat 任务、`/api/v1/commercial-licenses/status|gateway`（`commercial_license/api/router.py:19,32`）、前端横幅 | 请求级 fail-closed 准入中间件 |
| 拦截记录（`gt_block_record`） | DSH 审计事件 `dsh_content_safety_event`（HMAC 推送摄入，`dsh/content_safety_runtime.py:21`）；聊天被拒写消息行（`chat_service.py:1693`） | 网关级拦截记录表 + 查询面 |
| 用户组 / 资源组管理（`/api/group/*`） | `user_group` 模块存在（`/api/v1/user-group`，F003，`api/router.py:117` 挂载） | `/api/group/*`、`/api/resource/groupFlows` 兼容面 + 组级限流阈值存储 |
| 前端仍在调的网关自有路径 | `platform/src/controllers/API/pro.ts`：`/api/sensitive/saveWords|wordsDetail`、`/api/resource/groupFlows`、`/api/group/save|list`、`/api/oauth2/list`、`/api/getkey`、`/api/oauth2/ldap`；`dshContentSafety.ts`：`/api/sensitive/dsh/*`；`license.ts`：`/api/license/status` | 全部需要后端承接（兼容路由）或前端改靶 |

### 4.3 关键模块职责（现状 + 新增）

| 模块 / 文件 | 职责 | 不做什么 |
|---|---|---|
| `sensitive_word/domain/services/*` | 敏感词策略、AC 匹配、流式扫描（**执行层，被复用不改语义**） | 不做网关路由 / 限流 |
| `sso_sync/domain/services/login_sync_service.py` | 外部身份 → 用户 upsert / 部门 / 租户 / JWT（**被进程内调用**） | 不做 OAuth 协议 |
| `org_sync/domain/providers/wecom.py` | 企微 API pull（token 缓存、部门 / 成员） | 不做 OAuth 登录 |
| `commercial_license/*` | license 表 + ETL 同步 + 聚合 | 不做准入裁决 |
| `dsh/*` | 桌面端认证 / 配额 / 审计 | 不做平台 SSO |
| **新增** `bisheng/gateway/` | Provider 侧 OAuth、限流、在线计数、license 门、管理面端点、拦截记录 | 不重写敏感词引擎、不反代、不碰 app_runtime |

**新增模块结构**（遵循 C1：Router → Endpoint → Service → Repository → DB）：
```
bisheng/gateway/
├── api/
│   ├── router.py                  # 聚合 + 旧网关路径兼容挂载（仿 dsh/api/router.py:30 模式）
│   └── endpoints/
│       ├── oauth2.py              # providers 列表 / authorize / callback / ldap / getkey
│       ├── provider_admin.py      # Provider CRUD（超管）
│       ├── dsh_content_safety.py  # /sensitive/dsh/settings|capabilities|preview|events
│       ├── rate_rule.py           # 限流规则 CRUD + /groups 兼容面
│       ├── block_record.py        # 拦截记录分页查询
│       └── license.py             # 授权状态聚合（后端自持）
├── domain/
│   ├── models/  oauth_provider.py · rate_rule.py · block_record.py
│   ├── services/ oauth_service.py · ldap_service.py · rsa_key_service.py
│   │              rate_limiter.py · online_counter.py · license_gate.py
│   ├── schemas/
│   └── repositories/
├── infrastructure/
│   ├── lua/  token_bucket.lua · online_counter.lua
│   └── middleware.py              # GatewayMiddleware（纯 ASGI，§5.2）
core/config/gateway.py             # GatewaySettings（frozen + strict，仿 core/config/dsh.py）
common/errcode/gateway.py          # 26600–26699
```

---

## 5. 目标形态设计

### 5.1 总体拓扑

```
浏览器 → nginx(TLS/反代, X-Forwarded-*) → Backend:7860
                                        └─ ASGI 管线:
   WSLogging → Custom(租户/JWT/trace) → 【Gateway 中间件】 → OpenApiAudit → … → 路由
                                              │
        ┌─────────────────────────────────────┼──────────────────────────────┐
        │ license 门(缓存)  限流(Lua)  WS 在线计数(Lua)   请求侧内容安全(仅未接入 face) │
        └─────────────────────────────────────┼──────────────────────────────┘
                                              ▼
                      路由 → Endpoint → Service（sensitive_word / sso_sync / dsh …）
```

### 5.2 中间件管线（顺序是设计的一部分）

- **注册位置**：`main.py` 中紧邻 `CustomMiddleware`（`:251`）**之前**一行注册 `GatewayMiddleware`——后注册 = 更外层，故它比 Custom 更**内层**，入站顺序为 `WSLogging → Custom → Gateway → OpenApiAudit → 路由`。**必须在 Custom 之后**：限流 / 内容安全需要租户与用户身份（K1 的 subject 解析不能做两遍）。
- **subject 获取（小改造）**：`CustomMiddleware.dispatch` 已经为 F012 解码过一次 JWT（`utils/http_middleware.py:451` `decoded_subject`），但**没有向下游暴露**。新增 `core/context/user.py` 的 `current_subject` ContextVar，Custom 解码后 `set()` 一次，Gateway 中间件直接读——避免热路径二次 HS256 验签。这是对共享中间件的**最小侵入**（只加一个 set/reset，不改判定逻辑）。
- **路径分流（K5）**：中间件启动即按前缀分派——`/api/v1`（信封语义）、`/api/v2`（真状态码 + scope）、`/api/v3`（匿名）、`/api/v1/dsh/*` 与 `/api/dsh/*`（DSH 自有凭证，参照 `owns_dsh_credential`，`utils/http_middleware.py:104`）。拒绝时按面选信封：v1 用 `resp_200` 形状 + 266xx 业务码；v2 用真 429/403。
- **WS 分支**：`scope["type"] == "websocket"` 时走在线计数逻辑（§5.5），不走 HTTP 限流。
- **纯 ASGI**：不用 `BaseHTTPMiddleware`（K2）。请求体只在配置了 `filter_url` 匹配且该 face 未接入执行层时才读（决策 3，正常情况下不读 body）。
- **审计落库**：中间件 / 拦截记录一律 write-behind，复用 `BatchedRecordWriter` 模式（`open_api/domain/services/call_audit_service.py:18`）+ lifespan flush（`main.py:182`），禁同步写库。

### 5.3 SSO/OAuth 子系统

**Provider 配置**（`gateway_oauth_provider` 表）：

| provider_type | 协议 | 关键配置项（JSON，敏感字段密文） |
|---|---|---|
| `custom_sso` | OAuth2 授权码（4 端点可配） | authorizeUrl / accessTokenUrl / userInfoUrl / refreshUrl / clientId / clientSecret / userNameJsonPath |
| `wecom_qr` | 企微扫码 OAuth | corpId / corpSecret / agentId / redirectUri |
| `wecom_web` | 企微网页 OAuth | 同上 + redirectWebUri |
| `ldap` | LDAP bind（无回调） | ldapUrl / baseDn / bindDn / bindPassword / userFilter / userNameAttr |

**授权码流**（`custom_sso` / `wecom_*`）：
```
1. GET /api/v1/gateway/oauth2/providers        → 前端渲染登录方式（替代 /api/oauth2/list）
2. GET /api/v1/gateway/oauth2/authorize/{type}
   → 生成 state（Redis 单 key，TTL 600s，Lua GET+DEL 一次性消费，仿 tickets.py:38）
   → 保存 return_to（10 分钟，同源校验，复用 utils/loginReturnTo.ts 的语义约定）
   → 302 IdP authorize URL（client_id + redirect_uri + state）
3. IdP 认证 → 302 GET /api/v1/gateway/oauth2/callback/{type}?code&state
   → state 消费（失败 = 26604，防 CSRF / 重放）
   → httpx 换 token（超时 5s，禁无限等待）→ 取 userinfo
   → 映射 external_user_id / 部门 / 用户属性（对齐 LoginSyncRequest 字段，sso_sync/domain/schemas/payloads.py:64-116）
   → **进程内** LoginSyncService.execute()（同一把 user:sso_lock 锁、同一套跨源收养 / 访客兜底 / 租户闸门）
   → Set-Cookie access_token_cookie（cookie key 与平台登录一致，auth.py:156）+ 302 home_url
```
**LDAP 流**：前端 `GET /api/v1/gateway/getkey` 取 RSA 公钥（PKCS#1 PEM）→ 加密密码 POST `/api/v1/gateway/oauth2/ldap` → 服务端私钥解密 → `ldap3` bind 校验 → 走同一 LoginSyncService。密钥对启动时解析，私钥以 **Fernet 密文**持久化（新增 `ConfigKeyEnum.GATEWAY_RSA_KEY`，密文存取复用 `app_publish/domain/services/secret_security.py` 的模式）。**不仿 `JWT_SECRET` 的明文落库**：`user/domain/services/jwt_secret.py:81` 直接 `Config(value=candidate)`，`common/models/config.py` 无加密层（`SANDBOX_ENDPOINT` 注释里的 "token ciphertext" 是调用方自理），明文私钥违反 C6。轮换 = 管理端点替换 + 旧密钥保留一个 grace 期（解密侧双钥尝试，旧密文到期删除）。

**企微组织同步调度（AC-45）**：`org_sync` 的 pull→reconcile 管线**已实现但无调用方**——`OrgReconcileService.reconcile_config` 既无端点也无 beat 任务（§8 坑 15）。本 feature 的周期同步不指望它自动运行：注册 beat 任务 `gateway_wecom_org_sync`（沿用 `core/config/settings.py:197` 的 `beat_schedule` 装配模式），对每个启用的 `wecom_qr` / `wecom_web` provider 调 `WeComProvider.fetch_departments` / `fetch_members`（`org_sync/domain/providers/wecom.py:326,364`）拉取，再走 `reconciler.py` 的两个纯函数 `reconcile_departments` / `reconcile_members`（`:93` / `:255`，入参为 remote DTO + 本地列表，返回操作清单）落地部门与成员。间隔取 provider 配置；失败重试 3 次后退避，写结构化日志与 telemetry journal，不影响其他登录方式与 DSH 本地面功能。

**企微网页内嵌落地（AC-48）**：`wecom_web` 的 `redirectWebUri` 可携带应用标识；回调成功后 302 到该内嵌地址，标识缺失或配置缺失时回落 `gateway.home_url`。同源校验与 `return_to` 同一套逻辑。

**依赖新增**：`ldap3`（pyproject 新增，需过依赖许可评审，参照 `src/backend/AGENTS.md` 依赖许可约定）。企微 OAuth 的 userinfo（`/cgi-bin/user/getuserinfo`）复用 `org_sync/domain/providers/wecom.py` 的 token 缓存，不新造 HTTP 客户端。

**与平台登录的关系**：签发的是**同一个** `LoginUser.create_access_token`，claims `{user_id, user_name, tenant_id, token_version}` 与平台登录完全同构——SSO 登录后前端 `userContext` / 租户切换 / 权限判定零差异。账号禁用走 `invalidate_jwt_after_account_disabled`（`user/domain/services/user.py:156`，bump token_version + 清 TenantScope）。

### 5.4 内容安全子系统

- **执行层**：零改动复用。新增 `business_type = dsh_chat`（`sensitive_word/domain/schemas.py:9` 枚举加一项），DSH 聊天链路接入：
  - 输入侧：`/api/v1/dsh/chat/completions`（`dsh/api/endpoints/models.py:95`）入口调 `evaluate` 等价判定；
  - 输出侧：流式响应包 `StreamContentSafetyScanner`（business_type 传 `DSH_CHAT`），增量 feed + finish；
  - 命中语义与 workstation 完全一致：`auto_reply` 伪装回答 + SSE 事件，**不返回 4xx**（决策 3 契约）。
- **管理面**：`/api/v1/gateway/sensitive/dsh/settings|capabilities|preview|events|events/{id}`（兼容挂 `/api/sensitive/dsh/*`）。存储 = `sensitive_word_policy` 表的 `dsh_chat` 行（`tenant + business_type + scope` 唯一键已存在，`:64`），**不建第二张策略表**。前端契约（`platform/src/types/dshContentSafety.ts` 的 `DshSensitivePolicy`：enabled / input_enabled / output_enabled / word_source / extra_words / block_message）与 `SensitiveWordPolicyPayload`（enabled / words_types / custom_words / auto_reply）做适配层映射：`input_enabled`/`output_enabled` 落 `extra_config` JSON；`word_source` ↔ `words_types`；`extra_words` ↔ `custom_words`；`block_message` ↔ `auto_reply`。管理面裁决经 `PermissionService`（C4）复用平台租户管理员判定（内部走 OpenFGA），不采信 DSH profile 身份、不建第二套权限模型。
- **旧资源级端点** `/api/sensitive/saveWords|wordsDetail`（前端 `pro.ts:9,24` 仍在调）：v1 以兼容路由承接，底层改写为按 scope 粒度写 `sensitive_word_policy`（resource scope 需要扩 `scope_type` 枚举——列为 §9 后续，本期先做 tenant scope 承接并返回明确"已迁移"提示）。
- **拦截记录**：所有 face 的命中（含已接入的 workstation / channel / knowledge / 开放 API 任务模式，在各自命中点补一行）统一写 `gateway_block_record`（§5.7），查询面 `/api/v1/gateway/block-records`（租户管理员）。保留期 90 天，Celery beat 清理任务仿 `worker/knowledge/knowledge_chat_history_retention.py:17`（**批量删除前必须恢复租户 ContextVar**，tasks T027）。
- **开放 API 模型协议面**（`open_api/api/endpoints/model_gateway.py:48` `/chat/completions`，Java gateway `filter-url` 的 `api/v2/assistant/chat` 对位面）：**本期补齐**，语义为**抛错误**（沿用该面既有错误语义，与开放 API 任务模式一致），不用伪装回答——API 客户端按错误码处理，伪装回答会让客户端把阻断当成模型输出（AC-43 / AC-44）。
- **语义分界（不互换）**：人类对话面（workstation / channel / DSH）命中 = 伪装回答；API 面（开放 API 任务模式、开放 API 模型协议、知识库文件解析 `/api/v1/knowledge/process`）命中 = 错误拒绝。已接入面零改动，判定结果 / 话术 / 事件 / 错误语义一律不变（AC-24）。

### 5.5 流量控制子系统

**QPS 令牌桶**（`infrastructure/lua/token_bucket.lua`）：
```lua
-- KEYS[1] = gw_rl:{tenant}:{subject}:{route_group}
-- ARGV   = rate, burst, now_ms, cost
-- 返回: 1=放行(剩余令牌) / 0=拒绝(重试等待 ms)
```
要点：`now_ms` 由应用传入（禁 `redis TIME`，多节点时钟漂移会让桶速率不一致）；单 key，Cluster 安全；拒绝时中间件按面返回（v1：200 + 26601；v2：真 429）。

**规则解析**（`gateway_rate_rule`）：`scope_type ∈ {global, department, user_group, user}` × `resource_type ∈ {assistant, skill, workflow, path}` × `resource_id`（空 = 该类全量）。中间件解析顺序：user → user_group → department → global，取最具体命中；无命中用 `GatewaySettings.rate_limit.default_qps`（默认 10，对齐 Java gateway 兜底值）。规则缓存进程内 30s + Redis Pub/Sub `delete_group` 频道联动失效（该频道已存在，`user_group_service.py:44`——规则与用户组同生命周期，天然复用）。规则管理面（CRUD）的权限判定同样经 `PermissionService`（C4，平台管理员），不新增第二套权限模型。

**WS 在线计数**（`infrastructure/lua/online_counter.lua`）：建连前 `check_and_incr`（超限返回 close 4029）；断连 `decr` 按 `gw_online_session:{session_id}` 存的 key 精确回收。计数 key = `gw_online:{tenant}:{dept}|{tenant}:{group+resource}` 两级（仿 Java gateway 的 GROUP_ONLINE / FLOW_ONLINE）。

### 5.6 License 准入子系统

`license_gate.py` 纯函数裁决，输入 = `license_info` 快照（进程内缓存 60s，来源 `LicenseInfoRepository`）：
```
face ∈ 商业面集合（SSO 登录入口 `/api/v1/gateway/oauth2/**`、DSH 敏感词策略管理 `/api/v1/gateway/sensitive/**`、限流规则管理 `/api/v1/gateway/rate-rules`、拦截记录查询 `/api/v1/gateway/block-records`）
  ∧ (gateway license 缺失 ∨ 过期)  → 26603（v1 信封）/ 403（v2）
其余面 → 放行（OSS 核心可用，决策 6）
```
**`/api/v1/dsh/**` 明确排除在商业面集合之外**（2026-11-10 用户裁决）：F111 已确认 spec AC-03 规定 DSH 的模型鉴权 / 权限 / 额度 / 计量不得恢复 License 或席位准入，且 INV-37 要求开源完整功能不以许可证为前提。DSH 本地面能力在任何授权状态下行为一致。**授权状态查询 `/api/v1/gateway/license/*` 同样排除**：授权过期时前端 License 横幅仍须展示真实状态（AC-35），查询端点不能被自己的门关掉。SSO 登录入口纳入授权门为 2026-11-10 用户裁决，已同步修订 spec AC-02/33/34/47（取代 spec 初版「仅两个管理面」的口径）。
商业面集合来自 `GatewaySettings.license_faces` 配置（可灰度收紧）。license 数据写入：自研网关自己同步 ETL（复用 `etl_sync.sync_etl_license`，`source='gateway'` 行），**不经前端上报**；`POST /api/v1/commercial-licenses/gateway` 保留给双跑期的 Java gateway 注入（`commercial_license/api/router.py:32`，前端 `reportGatewayLicense` 无感）。

### 5.7 数据模型（新表，全部 dialect_helpers + 租户列；`gateway_block_record` 为追加型表，不设 logic_delete / update_time）

```sql
-- 1. OAuth Provider 配置（决策 2）
gateway_oauth_provider(
  id            PK
  tenant_id     int        -- 1=root 全局默认；>1=租户覆盖（解析顺序 tenant→root）
  provider_type varchar(32) -- custom_sso | wecom_qr | wecom_web | ldap
  name          varchar(128)
  config        JsonType     -- 端点/凭据；clientSecret/corpSecret/bindPassword 为密文（C6）
  enabled       bool
  create_time / update_time / logic_delete
)

-- 2. 限流规则（决策 4/5）
gateway_rate_rule(
  id                PK
  tenant_id         int
  scope_type        varchar(32)  -- global | department | user_group | user
  scope_id          varchar(64)  -- dept 业务键 BS@xxx / group_id / user_id；global 为空
  resource_type     varchar(32)  -- assistant | skill | workflow | path
  resource_id       varchar(64)  -- 空=该类全量
  qps_limit         int          -- 0=不限
  burst             int
  concurrent_limit  int          -- WS 在线阈值，0=不限
  enabled           bool
  唯一键 (tenant_id, scope_type, scope_id, resource_type, resource_id, logic_delete)
)

-- 3. 网关级拦截记录（决策 3/§5.4）
gateway_block_record(
  id             PK (bigint)
  tenant_id      int
  user_id        int
  face           varchar(32)   -- workstation_chat | dsh_chat | channel_article | knowledge_file | open_api_chat | open_api_model
  stage          varchar(16)   -- input | output
  resource_type  varchar(32)
  resource_id    varchar(64)
  matched_words  JsonType      -- 截断（最多 50 个词 + hit_count）
  user_input     LargeText     -- 预览截断（500 字符）
  auto_reply     varchar(500)
  request_id     varchar(64)   -- 关联 trace_id
  create_time    datetime
  索引 (tenant_id, face, create_time) / (tenant_id, user_id, create_time)
)
```
**归属登记**：三表 Owner = 本 feature，需在 `features/v3.0.0/release-contract.md` 表 1 追加行（SDD 流程要求，tasks T007）。

**租户过滤登记（C3，漏一次 = 三张新表跨租户可见）**：`bisheng/gateway/domain/models/` 下三个模型模块必须显式加入 `core/database/tenant_filter.py:42` 的 `_TENANT_AWARE_MODEL_MODULES` 元组。该清单是**显式 import 白名单**：不在清单里的模型即使带了 `tenant_id`，也会静默跳过 `before_flush` 自动填充与 `do_orm_execute` 过滤——源码注释原话即这正是 v2.5 knowledgefile / flowversion / roleaccess / userrole 把子租户资源写到 root 的成因。

本 feature 同时补登记存量缺口 `bisheng.sensitive_word.domain.models`（`docs/architecture/12-multi-tenant.md:226` 已记载该表不在清单内）：DSH 策略行的租户归属目前依赖偶然 import，若网关侧先建行、之后某次重构切断 import 链，DSH 策略会静默写到 root 租户。

### 5.8 错误码与信封契约（`common/errcode/gateway.py`，26600–26699）

| Code | 类名 | HTTP 语义 | 触发点 |
|---|---|---|---|
| 26601 | `GatewayRateLimitedError` | v1: 200+信封 / v2: 429 | 令牌桶拒绝 |
| 26602 | `GatewayOnlineLimitExceededError` | WS close 4029 | 在线计数超限 |
| 26603 | `GatewayLicenseExpiredError` | v1: 200+信封 / v2: 403 | license 门 |
| 26604 | `GatewayOAuthStateInvalidError` | 400 | state 缺失 / 过期 / 已消费 |
| 26605 | `GatewayOAuthProviderNotFoundError` | 404 | provider 不存在或停用 |
| 26606 | `GatewayOAuthTokenExchangeError` | 502 | IdP 换 token / userinfo 失败 |
| 26607 | `GatewayLdapAuthError` | 401 | LDAP bind 失败 |
| 26608 | `GatewayProviderConfigInvalidError` | 400 | Provider 配置校验失败 |

文案三语落 `src/frontend/packages/locales/src/api_errors/{zh-Hans,en,ja}.json`（K8）。

### 5.9 配置（`core/config/gateway.py` → `GatewaySettings`，frozen + strict 仿 `DshSettings`）

```yaml
gateway:
  enabled: false               # 总开关；false 时中间件与路由均不注册
  license_gate: true           # 商业面 fail-closed（K4）
  license_faces:               # 受 license 门约束的前缀（不含 /api/v1/dsh/** 与授权状态查询，见决策 6）
    - /api/v1/gateway/oauth2/       # SSO/OAuth 登录入口（2026-11-10 用户裁决纳入）
    - /api/v1/gateway/sensitive/     # DSH 敏感词策略管理
    - /api/v1/gateway/rate-rules     # 限流规则管理
    - /api/v1/gateway/block-records  # 拦截记录查询
  filter_url: []               # 限流生效路径模式（沿用 Java gateway bisheng.filter-url 语义）
  home_url: http://host:port   # SSO 回调落地页
  rate_limit:
    enabled: true
    default_qps: 10
  online:
    enabled: true
  oauth:
    state_ttl_seconds: 600
    http_timeout_seconds: 5
  route_ownership:             # 双跑期路径归属（决策 7）
    - /api/v1/gateway/
    - /api/sensitive/
    - /api/group/
    - /api/oauth2/
```

### 5.10 前端改造（`src/frontend/`）

| 位置 | 改造 | 约束 |
|---|---|---|
| `platform/src/controllers/API/pro.ts:113-127` | `getSSOurlApi/getKeyApi/ldapLoginApi` 与 `/api/group/*`、`/api/sensitive/*` 改指后端 `/api/v1/gateway/*`（同源，去掉跨进程 target） | 走 controllers 层，禁 store 直连 HTTP（C7） |
| `platform/src/pages/LoginPage/loginBridge.tsx:43-113` | SSO / 企微 / LDAP 按钮渲染逻辑不变，仅换端点；`isPro` 门控保留 | 自动跳转（`login.tsx:52-107`）与 return_to 契约（`utils/loginReturnTo.ts`）不变 |
| `platform/src/controllers/API/license.ts` | `getLicenseStatus`（ETL 404-silent）保留；`reportGatewayLicense` 仅在双跑期需要，自研模式下后端自持 license，前端可不再上报 | banner 逻辑（`licenseBannerGateway.ts` 24h staleness）不改 |
| `platform/src/pages/SystemPage/dsh/DshContentSafetySettings.tsx` | 端点由 `/api/sensitive/dsh/*`（Java gateway）切到后端同路径兼容路由；契约字段已对齐（§5.4 映射层），**页面零改动** | 样式 scope 插件 `dshContentSafetyStyles` 要求新子组件必须放 `src/pages/SystemPage/dsh/`（`vite.config.mts:52`） |
| 新页面：限流规则 + 拦截记录 | 系统管理新 tab（或并入 `DepartmentTrafficControl` / Pro 安全页） | 三语文案；分页沿用既有模式 |
| `platform/vite.config.mts:174-181` | `VITE_GATEWAY_PROXY_TARGET` 的 `/api/license` 独立代理在自研模式可退场（后端无此路径，404-silent 已兼容） | 注意"更具体前缀先注册"注释（`:197-217`） |

### 5.11 迁移与灰度（每阶段独立可回滚，开关 = 配置）

| 阶段 | 内容 | 回滚方式 |
|---|---|---|
| 0 前置重构 | HMAC 公式收敛到 `common/utils/hmac_signature.py`（决策 7）；`CustomMiddleware` stash subject；加 `dsh_chat` 枚举 | 纯重构，行为等价 |
| 1 只读面 | license 聚合后端自持；拦截记录表 + 查询面 | 关 `gateway.enabled` |
| 2 内容安全 | DSH 聊天接入；`/api/sensitive/dsh/*` 管理面后端承接 | 停 DSH face 的策略行 |
| 3 SSO | Provider CRUD + authorize/callback/ldap/getkey；登录页切后端；企微组织同步 beat（AC-45） | 前端端点回退 / 停 provider / 摘除 beat 条目 |
| 4 流量控制 | QPS + 在线计数中间件；规则管理面 | `rate_limit.enabled=false` / `filter_url` 置空 |
| 5 关停 | 停 Java gateway 进程，nginx/vite target 直连后端；`route_ownership` 全量接管 | 重启 Java gateway + target 回切（数据无依赖，随时可逆） |

---

## 6. 对外契约与依赖

### 6.1 我提供给别人的（Outgoing）

| 契约 | 形式 | 谁在用 |
|---|---|---|
| `/api/v1/gateway/oauth2/authorize\|callback\|ldap\|getkey\|providers` | HTTP（含 302 / Set-Cookie） | platform 登录页、loginBridge |
| `/api/v1/gateway/sensitive/dsh/*` | HTTP | DSH 内容安全设置页 |
| `/api/v1/gateway/rate-rules`、`/block-records` | HTTP | 平台管理端（新） |
| `/api/oauth2/*`、`/api/group/*`、`/api/sensitive/*`、`/api/getkey`、`/api/resource/groupFlows` | 兼容路由（仅 `gateway.enabled`） | 存量前端调用（`pro.ts`、`dshContentSafety.ts`） |
| `GatewayMiddleware` | ASGI 中间件 | main.py 装配 |
| `SensitiveWordPolicyService`（新增 dsh_chat 类型） | 内部 Python API | DSH 聊天链路 |
| `gateway_block_record.face/stage/matched_words` 语义，与 `DshSensitivePolicy ↔ SensitiveWordPolicyPayload` 字段映射（`input_enabled`→`extra_config`、`word_source`→`words_types`、`extra_words`→`custom_words`、`block_message`→`auto_reply`） | 数据格式（JSON） | 拦截记录查询页、DSH 内容安全设置页 |

### 6.2 我依赖别人的（Incoming）

| 依赖 | 形式 | 风险点 |
|---|---|---|
| `LoginSyncService.execute` 的 11 步契约（用户 upsert / 跨源收养 / 租户闸门） | 内部 Python API | sso_sync 改字段会静默改变 SSO 登录结果；**必须加契约测试** |
| `LoginUser.create_access_token` claims 结构 | 内部 Python API | 改 claims = 全平台令牌失效 |
| `license_info` 表 + ETL 同步及时性 | 数据契约 | ETL 挂 → license 门按缓存降级（60s）→ 过期临界窗口 |
| `sensitive_word_policy` 唯一键与缓存失效（`clear_cache`） | 数据契约 | 绕过 service 直改表会导致 AC 自动机缓存陈旧 |
| `delete_group` Redis 频道 | 隐式数据契约 | user_group 模块改频道名 → 限流规则缓存失效失灵 |
| `department.concurrent_session_limit` 字段 | 数据契约 | department 模块改字段语义 → 在线阈值失真 |
| `sensitive_word_policy` 表的租户过滤（该模块不在 `_TENANT_AWARE_MODEL_MODULES` 清单内） | 数据契约 | 目前靠偶然 import 获得租户自动填充；后端重构切断 import 链 → DSH 策略静默写 root 租户（§5.7 补登记） |
| 第三方 IdP 的 token / userinfo 响应结构（由 `userNameJsonPath` 等配置适配） | 第三方服务契约 | IdP 改版改字段名或改错误信封 → 映射静默取到空用户名或报 26606；适配层必须对缺失字段显式报错 |
| `ldap3` 新依赖 | 系统依赖 | 许可评审 + 镜像体积 |

---

## 7. 测试与可观测

- **单元**：Lua 脚本（令牌桶 / 在线计数，CI 起真实 Redis，禁 mock 掉时间语义）；OAuth state 一次性消费；Provider 配置校验（26608）；DSH 策略 ↔ `SensitiveWordPolicyPayload` 映射 round-trip；`license_gate` 裁决纯函数表驱动。
- **集成**：中间件顺序（租户 ContextVar 对 Gateway 可见——回归 CustomMiddleware 的 subject stash）；双面对决拒绝语义（v1 信封 vs v2 真 429）；LDAP bind（mock ldap3 server）；SSO 回调端到端（mock IdP：authorize→token→userinfo→login-sync→Cookie）；license fail-closed（过期行 → 商业面 26603、开源面放行）。
- **e2e**（`/e2e-test` 双层）：API 层 pytest 走完整登录 → 聊天命中敏感词 → 拦截记录可查；页面手动清单覆盖登录页 SSO 按钮、DSH 设置页、限流规则 CRUD、License 横幅。
- **手动验证**：`BISHENG_PRO=true` + `gateway.enabled=true` 本地起后端；登录页出现 SSO 入口；聊天触发 `auto_reply`；连续打满 QPS 见 26601；WS 超限见 close 4029。
- **可观测**：中间件命中 / 拒绝计数走 loguru 结构化日志（沿用 `HTTP_ACCESS_METRIC` 风格做 `GATEWAY_METRIC` 标记，`utils/http_middleware.py:535`）；license 状态、限流拒绝率、在线计数峰值接入既有 telemetry journal（`common/constants/enums/telemetry.py`）；拦截记录表本身即审计面。

---

## 8. 已知坑 / 反直觉事实

| # | 反直觉事实 | 如果不知道会怎样 | 在哪处理 |
|---|---|---|---|
| 1 | `sso_sync` 与 `app_runtime` 的 HMAC **没有时间窗 / 没有 nonce**，只签 `METHOD\nPATH\nBODY`；重放防护只剩 login-sync 的 per-user Redis 锁 | 以为"HMAC 签名 = 防重放"，漏掉跨会话重放窗口 | 自研端点不复用该公式；双跑期不改 Java gateway 侧签名（决策 7 前置重构只收敛代码副本，不改协议） |
| 2 | 同一 HMAC 公式仓内有 **3 份独立拷贝**（`sso_sync/hmac_auth.py:44` 委托规范实现；`app_runtime/hmac_auth.py:52`、`orchestrator_client.py:113` 各自重写） | 改一处漏三处，签名静默不匹配 | 阶段 0 收敛到 `common/utils/hmac_signature.py:10` |
| 3 | `/api/license/status` **不属于后端**，是外部 ETL 的路径；后端只缓存投影 | 在后端找该路由找到怀疑人生；或给后端加一个永远同步不及时的镜像端点 | 决策 6；`commercial_license/api/router.py:19` |
| 4 | `BISHENG_PRO` 环境变量会**覆盖 DB 配置**（`config_service.py:480,488`），与 `license_info` 表是两套会漂移的机制 | 以为改了表就生效，实际跑的是 env | 决策 6 以表为准；运维文档写明 env 是遗留兼容 |
| 5 | `BaseHTTPMiddleware` 会缓冲 / 打断 SSE 与 WS | 网关中间件一上线聊天流式卡死或首字节延迟爆炸 | K2：纯 ASGI |
| 6 | 部署是 `uvicorn --workers 2`，**进程内限流 = 限额翻倍** | 限流配置 10 QPS 实际放到 20 | 决策 4：Redis Lua |
| 7 | 错误码语义按面分裂：v1 全局 HTTP 200 + body 信封，v2/v3/app-proxy 真状态码 | 限流拒绝在 v1 返回真 429，前端 axios 拦截器不认、UI 无提示 | K5；§5.8 双语义表 |
| 8 | 敏感词命中**不是错误**，是伪装成 LLM 回答的 `auto_reply` + SSE 事件（`content_safety.py:16-45`） | 手贱改成 4xx，chat UI 直接把被拒会话渲染成异常 | 决策 3 契约；DSH 接入照抄 |
| 9 | `sensitive_word` 的 workbench 能力有 **Pro 门控**（`sensitive_word_policy_service.py:350-358`） | 开源部署下接了线也不生效，误判为 bug | 接入前先判 `is_workbench_content_safety_active`；e2e 必须 `BISHENG_PRO=true` |
| 10 | `sensitive_word_policy` 的 AC 自动机按 (tenant, business_type, scope) **缓存**，只有 `clear_cache()` 能失效 | 绕过 service 直写表 → 策略改了不生效 | 管理面只走 service |
| 11 | `CustomMiddleware` 已解码 JWT 但**不向下游暴露** subject；重复解码 = 热路径双倍验签 | 网关中间件自己再解一遍，p99 多出几十微秒 × 全站 | §5.2 subject ContextVar 小改造 |
| 12 | SSO 端点全在 `TENANT_CHECK_EXEMPT_PATHS`（`http_middleware.py:34-41`）豁免租户检查，service 内部自行 `bypass_tenant_filter` + root 租户 | 新回调端点忘记登记 → 登录时报 NoTenantContextError | 新路径按同一模式登记 |
| 13 | 平台登录没有独立 refresh token（`user/api/user.py:135` 是 legacy：access 兼 refresh） | 以为能像 Java gateway 一样写 refresh_token_cookie | 登录态续期沿用平台现状；如需刷新 = 后续项 |
| 14 | alembic 文件名前缀**不表示拓扑顺序**；`down_revision` 必须取 `alembic heads` 当日输出（现 head `f114_app_authorization`） | 迁移链分叉，升级炸 | `core/database/alembic/AGENTS.md` |
| 15 | `org_sync` 的 pull→reconcile 主流程 `OrgReconcileService.reconcile_config` **没有任何调用方**（无端点、无 beat 任务） | 以为"配了 org_sync 就会自动同步"，上线后组织树从不更新 | §5.3：周期同步自己注册 beat + 调 `reconciler.py` 纯函数 |

---

## 9. 后续改进 / 不打算做的事

- **多租户 OAuth Provider 产品化**：表结构已留 `tenant_id`，但本期只做 root 全局配置 + 租户覆盖的解析顺序，不做管理 UI 的租户切换视图（对齐 `docs/architecture/11-gateway.md` §13 的 v2.5 规划，留待产品决议）。
- **资源级（scope_type=resource）敏感词策略**：`SensitiveWordScopeType` 目前只有 `TENANT`；旧 `/api/sensitive/saveWords` 的资源级语义需要扩枚举 + 唯一键，本期只承接 + 提示迁移。
- **租户级全局敏感词 / 租户级配额**：同上，§13 规划项，需先有产品口径。
- **JWT `aud` claim**：平台会话 JWT 外层只有 `iss` 没有 `aud`（`user/domain/services/auth.py:143`）；自建网关若要签发自有令牌应对齐，但本期复用平台签发，不动。
- **与 OpenFGA 组模型对齐**：Java gateway 的 `gt_user_group` / `gt_group_resource` 与平台 `user_group`（F003）/ ReBAC 是两套组织语义；本期只做限流阈值消费，不做模型统一。
- **egress / 出站代理**：归属另有开源方案（`docs/reviews/vibe-app-open-source-technical-design`），不在本 feature。
- **整体停摆式 license**：决策 6 的"何时重新考虑"。

---

## 修订历史

| 日期 | 改动 | 触发原因 |
|---|---|---|
| 2026-11-10 | 初版：8 项核心决策、能力盘点矩阵、目标形态、数据模型、灰度路径 | 用户提出"基于当前代码库自研扩充网关能力"，先沉淀设计再进 SDD spec 评审 |
| 2026-11-10 | design 评审后修订：RSA 私钥改 Fernet 密文（C6）；新表与 `sensitive_word` 模型补租户过滤登记（C3）；SSO 登录入口纳入 License 授权门并与 spec AC-02/33/34/47 对齐；补企微组织同步调度（AC-45）与网页内嵌落地（AC-48）设计；决策 5 补备选方案、决策 4/5/7/8 补"何时该重新考虑"；§6 补依赖风险点与新端点数据格式；§5.7 修正 logic_delete 声明；§8 补坑 15 | `/sdd-review design` 报 3 high（C6 / C3 / 与 spec 矛盾）+ 12 medium/low；用户裁决：Fernet 密文、补齐登记、SSO 纳入授权门 |
