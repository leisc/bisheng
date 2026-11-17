# Feature: 自研网关（Native Gateway）

> 本文只定义需求、验收标准和范围边界。实现决策、接口、字段、表结构、索引、缓存分层和性能验证方案见 [design.md](./design.md)（spec ✅ 已评审；design 于 2026-11-10 评审后按用户裁决修订中）。

- **特性编号**：F120
- **关联**：[design.md](./design.md)（8 项核心决策 + 能力盘点矩阵 + 14 条已知坑）；被替代对象为商业版 Java 网关 `bisheng-gateway`（架构说明 `docs/architecture/11-gateway.md`）
- **优先级**：P0（商业部署硬依赖：SSO 登录、内容安全、限流、License 四能力当前不在本仓库）
- **所属版本**：v3.0.0
- **依赖**：F111 DSH 敏感词控制（`../111-dsh-sensitive-word-control/spec.md`；其"Gateway 侧执行"前提经 2026-11-10 用户裁决改为"backend 进程内执行"，修订记录见该 spec）；F110 DSH 本地接入；`sensitive_word` 执行层；`sso_sync` 接收侧；`commercial_license`；`user_group`（F003）
- **状态**：✅ 已评审（2026-11-10 用户确认 spec；同日 design 评审后按用户裁决修订 AC-02/33/34/47 与范围边界——SSO/OAuth 登录入口纳入 License 授权门）
- **创建日期**：2026-11-10

## 范围边界

- **本次纳入**：
  - 后端进程内网关层：`bisheng/gateway/` 模块 + `GatewayMiddleware`（纯 ASGI）+ `GatewaySettings` 配置。
  - SSO/OAuth Provider 侧：自定义 SSO、企微扫码、企微网页、LDAP 四种登录方式的 authorize/callback/LDAP 校验/RSA 公钥下发，登录态签发复用平台 JWT。
  - 内容安全：DSH 模型转发面的输入与流式输出检查接入（复用现有执行层）；DSH 策略管理面；开放 API 模型对话面的输入输出检查补齐；网关级拦截记录与查询。
  - 流量控制：通用 QPS 令牌桶（Redis Lua）、WebSocket 在线计数、规则管理面、用户组/资源组兼容面。
  - License 准入：以 `license_info` 表为准的商业面 fail-closed 裁决。
  - 与 Java gateway 的双跑灰度：路径归属配置化、前端端点切换、关停步骤与回滚。
- **本次明确排除**：
  - 不做通用 HTTP/WS 反向代理进程；反代继续由 nginx 与部署侧承接（design 决策 1）。
  - 不重写敏感词匹配引擎、不改动已接入面的现有判定与话术语义（design 决策 3）：人类对话面（工作台、频道、DSH）命中时为伪装回答，API 面（开放 API 任务模式、开放 API 模型协议、知识库文件解析）命中时为错误拒绝，两类语义不互换。
  - **DSH 本地接入面不设 License 授权门**：与 F111 AC-03「DSH 不得恢复 License 或席位准入」一致，`/api/v1/dsh/**` 的行为不受授权状态影响。
  - **License 门只覆盖商业能力请求**：SSO/OAuth 登录入口与本期新增商业能力管理面（DSH 敏感词策略管理、限流规则管理、拦截记录查询）受授权状态约束；平台核心能力、开放 API、DSH 本地接入面与授权状态查询端点永不受授权状态影响（与 F111 AC-03、INV-37 一致；2026-11-10 design 评审按用户裁决把 SSO 登录入口纳入授权门，取代 spec 初版「仅两个管理面」的口径）。
  - 商业版按 `resource_id` 粒度的敏感词配置（`gt_sensitive_words.resource_id`）本期不提供，以租户级策略替代；Java gateway 停运后该粒度配置即失效，不做数据迁移（design §9）。
  - 不扩展 `app_runtime` 托管应用入口代理 / OBO / HMAC 体系（F054 所有）。
  - 不改变平台登录 JWT 的 claims 结构，不新增独立 refresh token 体系。
  - 不做多租户 OAuth Provider 管理 UI、资源级（scope_type=resource）敏感词策略、租户级全局敏感词/配额（design §9）。
  - 不改 DSH 桌面端本地认证与设备会话（F110 已解除 Java Gateway 依赖）。
  - 不做 `gt_*` 表数据迁移；Java gateway 关停为"停进程 + 切 target"，无数据依赖（design 决策 7）。
  - 本轮不含提交、发布与真实部署；交付到本地实现 + 定向验证 + e2e 清单为止。

## 1. 用户故事

### 1.1 商业客户运维人员

作为商业客户运维人员，我希望在只有本仓库后端一个进程的前提下获得 SSO 统一登录、内容安全、限流与 License 准入，以便不再维护第二套 Java 进程、第二套数据库和两套签名密钥。

### 1.2 企业员工

作为企业员工，我希望用公司 SSO / 企微 / LDAP 账号登录毕昇并使用聊天，命中敏感词时看到与普通回答一致的阻断提示，以便不察觉底层差异、也不被错误页面打断。

### 1.3 租户管理员

作为租户管理员，我希望按租户配置内容安全策略与限流规则，并查询谁在什么入口被什么词拦截，以便合规自查与容量治理。

### 1.4 平台超管

作为平台超管，我希望授权失效时商业能力明确拒绝而平台核心继续可用，并能在 License 横幅看到状态，以便区分"授权问题"与"系统故障"。

### 1.5 开源单机部署者

作为开源单机部署者，我希望网关模块默认关闭、无 license 也不影响任何现有行为，以便零风险升级。

## 2. 验收标准

> AC-43 及以后为 2026-11-10 spec 评审按用户裁决增补，按主题插入所在小节而非按序号排列。

### 2.1 部署形态与独立运行

- **AC-01** — WHERE 部署未开启网关能力，THE SYSTEM SHALL 不注册网关中间件与路由，且全部现有行为（含响应头、日志格式、错误码）与开启前逐字节一致。
- **AC-02** — WHERE 部署不存在有效 gateway license，THE SYSTEM SHALL 继续提供平台核心能力、开放 API、DSH 本地接入面与授权状态查询，仅拒绝 SSO/OAuth 登录入口与本期新增商业能力管理面（DSH 敏感词策略管理、限流规则管理、拦截记录查询）的请求（AC-33）。
- **AC-03** — WHEN 网关中间件执行限流或内容安全裁决，THE SYSTEM SHALL 以平台既有中间件已建立的租户与用户身份为准，不自行重复解析凭据，也不改变 `/api/v2`、`/api/v3` 与 DSH 自有凭证面的身份解析与错误语义。
- **AC-04** — WHILE Java gateway 与自研网关并存，THE SYSTEM SHALL 按部署配置的路径归属只对归属自身的前缀生效，使任一能力可按面单向切换且可回退。
- **AC-05** — WHERE 网关中间件作用于流式响应或 WebSocket，THE SYSTEM SHALL 保持分片顺序与既有响应头语义不被改变，不缓冲、不改写响应体，流式首字节不因网关介入被额外延迟。

### 2.2 SSO / OAuth 统一认证

- **AC-06** — WHEN 前端请求登录方式列表，THE SYSTEM SHALL 仅返回已启用的 provider 及其展示名与类型。
- **AC-07** — WHEN 发起授权，THE SYSTEM SHALL 生成一次性 state（TTL 600 秒），并在回调时消费；WHEN state 缺失、过期或已消费，THEN THE SYSTEM SHALL 以 26604 拒绝且不签发任何令牌。
- **AC-08** — IF 换取 token 或拉取 userinfo 失败、超时或返回非预期结构，THEN THE SYSTEM SHALL 以 26606 终止本次登录，不创建用户、不签发令牌、不泄露 IdP 响应细节。
- **AC-09** — WHEN 外部身份登录成功，THE SYSTEM SHALL 签发与平台登录同构的 JWT（相同 claims 与 cookie key）、按同源校验后的 return_to 落地，并保持前端登录态、租户切换与权限判定与平台登录无差异。
- **AC-10** — WHEN 外部身份首次登录或跨源命中既有账号，THE SYSTEM SHALL 沿用既有接收侧语义完成用户创建、部门绑定与租户派生；WHEN 账号被禁用，THEN THE SYSTEM SHALL 拒绝登录并使既有会话失效。
- **AC-11** — WHEN 前端请求登录公钥，THE SYSTEM SHALL 下发 RSA 公钥；WHEN LDAP 绑定失败，THEN THE SYSTEM SHALL 以 26607 拒绝，且密码不明文出现在日志、响应或任何持久化介质中。
- **AC-12** — WHEN provider 被停用或不存在，THE SYSTEM SHALL 在发起授权与回调两端均以 26605 拒绝。
- **AC-13** — WHEN 管理员禁用已通过 SSO 登录的用户，THE SYSTEM SHALL 使该用户后续请求立即失败，与平台登录用户一致。
- **AC-14** — WHERE 部署开启 SSO，THE SYSTEM SHALL 使平台登录页呈现对应登录入口并可完成登录与登出。
- **AC-45** — WHEN 部署启用企业微信组织同步，THE SYSTEM SHALL 按配置周期自动拉取部门树与成员并完成同步；IF 同步失败，THEN THE SYSTEM SHALL 留下可观测痕迹，且不影响其他登录方式与 DSH 本地面功能。
- **AC-48** — WHERE 企业微信网页登录携带应用标识，THE SYSTEM SHALL 在登录成功后按配置的内嵌落地地址跳转；缺失该配置时回落默认首页。

### 2.3 内容安全（DSH 模型转发面）

- **AC-15** — WHEN DSH 模型请求输入命中本租户策略，THE SYSTEM SHALL 返回与普通回答一致的阻断话术与安全事件，不返回 4xx，且工具调用与后续模型执行不继续。
- **AC-16** — WHEN DSH 流式输出命中策略，THE SYSTEM SHALL 在完整命中词交付客户端前识别并阻断，保留已交付前缀，并保持原协议事件顺序。
- **AC-17** — WHEN 管理员配置本租户 DSH 策略，THE SYSTEM SHALL 支持统一配置启停、输入检查、输出检查、内置词库、自定义词与阻断提示，并在同一租户全部 DSH 模型上生效。
- **AC-18** — WHEN 无目标租户管理权限的人员读写策略，THE SYSTEM SHALL 拒绝；WHEN 租户管理员操作，THEN THE SYSTEM SHALL 限于本租户；WHEN 超级管理员指定目标租户，THEN THE SYSTEM SHALL 校验其可管理范围。
- **AC-19** — IF 某方向启用检查但无有效词或词库/词条/提示不满足校验要求，THEN THE SYSTEM SHALL 拒绝保存并指出原因，不得出现界面已启用而实际无规则的状态。
- **AC-20** — WHEN 多个管理员基于同一旧版本先后保存，THE SYSTEM SHALL 明确拒绝冲突保存，不静默覆盖。
- **AC-21** — WHEN 策略保存成功，THE SYSTEM SHALL 使新请求在最多 5 秒内使用新策略；进行中的请求继续使用进入检查时的策略。
- **AC-22** — WHERE 部署未启用 Pro 能力，THE SYSTEM SHALL 保持 DSH 透传行为并明确提示该能力不可用，不出现"看似开启实际不检查"。
- **AC-23** — WHEN 任一受管面发生命中，THE SYSTEM SHALL 记录面、阶段、命中词、输入预览、返回话术与请求标识，支持按面/用户/时间分页查询，并仅对有权租户可见。
- **AC-24** — WHERE 已接入面发生命中，THE SYSTEM SHALL 追加拦截记录，且不改变这些面既有的判定结果、话术、事件与错误语义；其中人类对话面（工作台、频道、DSH）为伪装回答，API 面（开放 API 任务模式、知识库文件解析 `/api/v1/knowledge/process`）为错误拒绝。
- **AC-43** — WHEN 开放 API 模型对话面（模型协议 `/chat/completions`）的输入命中本租户策略，THE SYSTEM SHALL 在调用模型前以该面既有错误语义拒绝，不透传命中内容。
- **AC-44** — WHEN 开放 API 模型对话面的流式输出命中本租户策略，THE SYSTEM SHALL 中断本次响应并以该面既有错误语义告知，不透传命中内容；IF 已交付部分内容，THEN THE SYSTEM SHALL 保留已交付前缀并保持协议事件顺序。

### 2.4 流量控制

- **AC-25** — WHERE 限流启用，THE SYSTEM SHALL 使命名主体在单位时间内的请求总数不超过配置阈值，且该总计不随进程实例数放大。
- **AC-26** — WHEN 请求被限流拒绝，THE SYSTEM SHALL 对 v1 面返回 200 + 信封业务码 26601，对 v2/v3 与开放面返回真 429，且响应可被前端既有拦截器识别。
- **AC-27** — WHEN 解析限流规则，THE SYSTEM SHALL 按 user → user_group → department → global 取最具体命中；无命中时使用默认 QPS。
- **AC-28** — WHEN 管理员保存限流规则，THE SYSTEM SHALL 在 30 秒内对新请求生效，并在用户组变更时即时失效对应缓存。
- **AC-29** — IF 限流存储不可用，THEN THE SYSTEM SHALL 放行请求并留下可观测痕迹，不因限流组件故障阻断业务。
- **AC-30** — WHEN WebSocket 建连，THE SYSTEM SHALL 在建连前校验在线限额，超限以应用私有关闭码拒绝，并在断连时精确回收计数，不出现计数泄漏。
- **AC-31** — WHERE 部门配置了并发会话阈值，THE SYSTEM SHALL 对该部门成员的 WebSocket 建连按该阈值裁决。
- **AC-46** — WHERE 用户组或资源配置了并发阈值，THE SYSTEM SHALL 对该用户组成员或该资源的 WebSocket 建连按阈值裁决，并与部门阈值独立生效。
- **AC-32** — WHERE 管理员打开限流与拦截记录管理面，THE SYSTEM SHALL 提供规则增删改查与记录筛选（手动验证）。

### 2.5 License 准入

- **AC-33** — WHEN 授权缺失或已过期，THE SYSTEM SHALL 拒绝 SSO/OAuth 登录入口与本期新增商业能力管理面（DSH 敏感词策略管理、限流规则管理、拦截记录查询）的请求，按面返回 26603 或 403，且不缓存过期后的放行决定；授权状态查询端点保持可访问，以供前端展示真实授权状态。
- **AC-34** — WHERE 授权缺失或已过期，THE SYSTEM SHALL 不影响平台核心面、开放 API、DSH 本地接入面与授权状态查询的可用性与响应语义（AC-02）。
- **AC-35** — WHEN 授权状态变化，THE SYSTEM SHALL 从授权表与同步任务取得状态，并使前端 License 横幅反映真实状态。
- **AC-36** — WHILE Java gateway 仍在位，THE SYSTEM SHALL 保留外部授权注入入口，不因自研网关接入而失效。
- **AC-47** — IF 授权状态无法取得（存储不可用或同步任务未产出），THEN THE SYSTEM SHALL 按 fail-closed 拒绝 SSO/OAuth 登录入口与本期新增商业能力管理面并留下可观测痕迹，平台核心面、开放 API、DSH 本地接入面与授权状态查询不受影响。

### 2.6 数据、安全与迁移

- **AC-37** — WHERE 双跑期需要逐面切换，THE SYSTEM SHALL 通过配置变更完成归属切换，切换不需要数据迁移，也不需要重启本仓库之外的接入系统。
- **AC-38** — WHEN Java gateway 停运，THE SYSTEM SHALL 使登录、内容安全、限流与授权全链路可用，且由端到端验证证明。
- **AC-39** — WHERE 新增面向用户的文案或错误码，THE SYSTEM SHALL 同步三种语言并使 locales 检查通过。
- **AC-40** — WHERE 新增表与错误码，THE SYSTEM SHALL 提供可升级可降级的数据库迁移，并在错误码登记处注册。
- **AC-41** — WHERE 系统保存第三方凭据或密钥，THE SYSTEM SHALL 加密存储、不明文落日志、不提供默认空值放行。
- **AC-42** — WHERE 网关中间件发生放行、拒绝或降级，THE SYSTEM SHALL 留下可检索的结构化痕迹（含拒绝原因分类），供容量与排障使用。

## 3. 非功能要求

- **NFR-01 性能**：网关能力对单请求的额外开销必须有界，且不得改变流式响应的分片时序；预算值与验证方案见 [design.md](./design.md)。
- **NFR-02 可用性**：限流存储故障 fail-open、授权门 fail-closed；任一网关子能力故障不使平台核心不可用。
- **NFR-03 安全**：state 一次性消费；LDAP/凭据不明文；provider secret 密文；HMAC 公式全仓唯一实现。
- **NFR-04 兼容**：DM8/MySQL 双库迁移；多租户自动注入；前端存量网关路径（`/api/sensitive/*`、`/api/group/save`、`/api/group/list`、`/api/group/get_group_resources`、`/api/resource/groupFlows`、`/api/oauth2/list`、`/api/oauth2/ldap`、`/api/getkey`）经兼容路由承接，404 静默的路径保持静默。
