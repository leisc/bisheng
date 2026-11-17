# 宿主机源码运行与演示指南

在 macOS arm64 上**不构建任何 Docker 镜像**，用宿主机 Python 进程直接跑毕昇后端，中间件由 Docker Compose 提供。

> 适用场景：本地开发调试、给别人演示、快速验证改动。
> 生产部署请走标准镜像方案（`docker compose up -d` 全量）。

---

## 0.5 快速恢复（环境挂了先读这段）

下次发现 `localhost:7860` 不通、或 `docker ps` 里中间件全 `Exited`，按这个顺序拉：

```bash
# ① 中间件（Docker 容器整体退出时，通常是机器重启/Docker 重启导致）
cd /Users/louissum/apps/bisheng/docker
docker compose -p bisheng up -d mysql redis elasticsearch etcd minio milvus openfga

# ② 宿主机后端进程（⚠️ 不要用 start-host.sh api，见 §1.1，手动 nohup 更稳）
cd /Users/louissum/apps/bisheng/src/backend
mkdir -p /tmp/bisheng-host
(set -a; . ./.env.host; set +a; PYTHONPATH=. nohup .venv/bin/python -m uvicorn bisheng.main:app \
   --host 0.0.0.0 --port 7860 --workers 1 --no-access-log > /tmp/bisheng-host/api.log 2>&1 &)
(set -a; . ./.env.host; set +a; PYTHONPATH=. nohup .venv/bin/celery -A bisheng.worker.main worker \
   -l info -c 100 -P threads -Q knowledge_celery,ocr_celery,workflow_celery,celery -n min_worker@%h \
   > /tmp/bisheng-host/worker.log 2>&1 &)
(set -a; . ./.env.host; set +a; PYTHONPATH=. nohup .venv/bin/python bisheng/linsight/worker.py \
   --worker_num 1 --max_concurrency 5 > /tmp/bisheng-host/linsight.log 2>&1 &)
(set -a; . ./.env.host; set +a; PYTHONPATH=. nohup .venv/bin/celery -A bisheng.worker.main beat -l info \
   > /tmp/bisheng-host/beat.log 2>&1 &)

# ③ 两个前端
mkdir -p /tmp/bisheng-frontend
cd /Users/louissum/apps/bisheng/src/frontend/platform
(VITE_MINIO_PROXY_TARGET=http://localhost:9100 nohup pnpm start -- --host 0.0.0.0 \
   > /tmp/bisheng-frontend/platform.log 2>&1 &)
cd /Users/louissum/apps/bisheng/src/frontend/client
(VITE_MINIO_PROXY_TARGET=http://localhost:9100 nohup pnpm start \
   > /tmp/bisheng-frontend/client.log 2>&1 &)
```

约 60 秒后验证：

```bash
for p in 7860 3001 4001; do nc -z localhost $p && echo "$p OK" || echo "$p DOWN"; done
curl -s -o /dev/null -w "api: %{http_code}\n" http://localhost:7860/api/v1/user/public_key
```

> `mkdir -p /tmp/bisheng-frontend` 不能省——日志目录不存在时 nohup 直接失败，前端起不来且报错只有一行
> `No such file or directory`，很容易看漏。

---

## 0. 前置条件

| 项 | 要求 | 验证命令 |
|---|---|---|
| 系统 | macOS arm64（aarch64） | `uname -m` → `arm64` |
| Docker Desktop | ≥ 4.40 | `docker version` |
| 内存 | ≥ 8 GiB（中间件实测占 2.81 GiB） | `docker stats --no-stream` |
| Python | 3.11+ | `.venv/bin/python -V` |

**一次性准备**（已完成的可跳过）：

```bash
cd src/backend
uv sync --frozen          # 装依赖，产出 .venv（约 1.0 GiB）
```

---

## 1. 一键启动

```bash
src/backend/start-host.sh all
```

起 API → Celery worker → linsight worker → beat。约 40 秒后全部就绪。

### 1.1 ⚠️ `start_api` 会挂住，建议手动起

`start-host.sh` 里 `start_api()` 用 `(cmd &)` 把 uvicorn 丢后台，但**shell 会一直等到管道关闭才返回**——表现为命令卡住不退出，而 uvicorn 其实已经起来了。

> 实测（2026-11-12）：`timeout 30 bash -x ./start-host.sh api` 60 秒都没返回，但 `ps` 里 uvicorn 已在跑，`nc -z 7860` 通。
>
> 规避：直接用 §0.5 的手动 `nohup` 写法，或者接受它卡住、另开一个终端继续操作。

### 其他子命令

```bash
src/backend/start-host.sh status       # 看进程/容器/端口总览
src/backend/start-host.sh middleware   # 只起 7 个中间件容器(显式列服务名)
src/backend/start-host.sh api         # 只起 API(会挂住, 见 §1.1)
src/backend/start-host.sh worker      # 只起 Celery worker
src/backend/start-host.sh stop        # 停所有宿主机进程(不动容器)
```

> **`all` 不再起中间件。** 原来 `all` 里是不带服务名的 `docker compose up -d`，会把
> `backend` / `backend_worker` / `frontend` / `code-runner-*` / `runtime-manager` 一起拉下来
> （实测拉了 1.1 GiB，其中 `dsh-minimal` sandbox 镜像单个就 445 MB）。现在中间件独立成
> `middleware` 子命令。

---

## 2. 手动启动（理解每一步）

### 2.1 中间件

```bash
cd docker
docker compose -p bisheng up -d mysql redis elasticsearch etcd minio milvus openfga
```

> **必须显式列服务名。** 直接 `docker compose up -d` 会连带启动 `backend` / `backend_worker` / `frontend` / `code-runner-*`，这些镜像本地没有，会去 `cr.dataelem.com` 慢速拉取，而且 `backend` 会抢占 7860 端口。
>
> `openfga-migrate` 是一次性任务（`Exited (0)` 即成功），不用显式起，`openfga` 依赖它。

> **网络是固定名 `bisheng-network`**（compose 里写死了，不随 `-p` 项目名变）。所以
> `docker compose -p bisheng up -d ...` 和 `docker compose -f docker/docker-compose.yml up -d ...`
> （默认项目名 `docker`）会复用同一批容器——容器靠 `container_name` 匹配，不会起重复的。
> 判断有没有起重复：`docker ps -a --filter name=bisheng-` 应该只有 8 个。

中间件清单与端口：

| 容器 | 宿主机端口 | 用途 |
|---|---|---|
| `bisheng-mysql` | 3306 | 业务库 + OpenFGA 数据源 |
| `bisheng-redis` | 6379 | 缓存 / Celery broker(db1) / result(db2) |
| `bisheng-es` | 9200 | 稀疏召回（BM25） |
| `bisheng-milvus-standalone` | 19530 | 密集向量召回 |
| `bisheng-milvus-etcd` | — | Milvus 元数据 + 服务发现 |
| `bisheng-milvus-minio` | 9100 | Milvus 对象存储 + 毕昇文件存储 |
| `bisheng-openfga` | 8080 | 权限（ReBAC） |

> `bisheng-milvus-minio` 是**双职责**：既给 Milvus 存 segment，也给毕昇存上传文件。所以去掉 Milvus 也不能去掉 MinIO。

### 2.2 数据库迁移

```bash
cd src/backend
set -a && . ./.env.host && set +a
PYTHONPATH=. .venv/bin/alembic upgrade head
```

建 181 张表 + 种子数据（默认租户、两个内置角色、`initdb_config`）。

> `set -a && . ./.env.host && set +a` 后面每一节都要。环境变量缺失时配置加载会直接失败，最常见的报错是 `Environment variable BISHENG_GATEWAY_HMAC_SECRET not found`。

### 2.3 API

```bash
cd src/backend
set -a && . ./.env.host && set +a
PYTHONPATH=. .venv/bin/uvicorn bisheng.main:app --host 0.0.0.0 --port 7860 --workers 1 --no-access-log
```

启动日志里应看到 9 个 context 全部初始化：

```
Registered context: database / redis / minio / elasticsearch / openfga
                    permission_runtime / prompts / http_client / statistics_elasticsearch
```

### 2.4 Celery Worker

```bash
set -a && . ./.env.host && set +a
PYTHONPATH=. .venv/bin/celery -A bisheng.worker.main worker \
  -l info -c 100 -P threads \
  -Q knowledge_celery,ocr_celery,workflow_celery,celery -n min_worker@%h
```

### 2.5 Linsight Worker（灵思 Agent）

```bash
set -a && . ./.env.host && set +a
PYTHONPATH=. .venv/bin/python bisheng/linsight/worker.py --worker_num 1 --max_concurrency 5
```

### 2.6 Celery Beat（定时任务）

```bash
set -a && . ./.env.host && set +a
PYTHONPATH=. .venv/bin/celery -A bisheng.worker.main beat -l info
```

> beat 的日志会转到 `src/backend/data/bisheng.log`（loguru sink），`/tmp/*.log` 里只有前半段。判断 beat 是否工作要看 `data/bisheng.log` 里周期任务是否在跑，例如 `trigger_dispatch_task` 每 30 秒一次。

---

## 3. 管理员账号

### 3.1 已有账号

迁移后数据库是**空白的**（`user` 表 0 行），需要自己注册第一个用户。**第一个注册的用户自动成为超级管理员**（user_id=1 + `AdminRole`）。

当前环境已创建：

| 项 | 值 |
|---|---|
| 登录账号 | `admin-001` |
| 密码 | `Admin@2026` |

### 3.1.1 两个前端的登录入口

| 前端 | 地址 | 说明 |
|---|---|---|
| 管理端/搭建端 | http://localhost:3001 | 模型管理、知识库、工作流、助手 |
| 终端用户端 | http://localhost:4001/workspace | 日常对话（注意带 `/workspace` 路径） |

两边用同一套账号密码。API 前缀不同（platform `/api/v1`，client `/workspace/api/v1`），
详见 §4。

**已配好的模型**（三个，都实测可用）：

| id | 用途 | 服务商 | 模型 | 类型 | 配置脚本 |
|---|---|---|---|---|---|
| 3 | 对话（默认） | 讯飞MaaS | `spark-x2.6` | llm | `setup-xfyun-llm.py` |
| 4 | 向量 | 通义千问MaaS | `qwen3.7-text-embedding-flash` | embedding（1024 维） | `setup-qwen-embedding.py` |
| 5 | 对话 | SenseNova | `sensenova-6.8-flash-lite` | llm | `setup-sensenova-llm.py` |

工作台默认（`/llm/workbench`）：日常对话 = 3、灵思 = 3、标题生成 = 3、embedding = 4。

> `spark-x2.6` 和 `sensenova-6.8-flash-lite` 端到端耗时接近（简单问答 3-5s，见 §5.5），
> 不必为演示专门换默认模型。
>
> ⚠️ `sensenova-6.8-flash-lite` 自我认知是「商汤科技日日新·商量 / SensenNova-v6.8-Flash-Lite」，
> 不是部署品牌名。客户问「这是什么模型」时答不上来会尴尬。

### 3.2 ⚠️ 登录账号只认 `external_id`

这是最容易踩的坑。`User.aget_login_candidates_by_account`（`bisheng/user/domain/models/user.py:238`）的 WHERE 条件只有：

```python
User.external_id == acc
```

**没有 `user_name`。** 用 `admin` 登录会得到 `10600 Account or password error`，即使密码完全正确、库里 MD5 也匹配。

### 3.3 重新创建管理员

```bash
cd src/backend
.venv/bin/python - << 'EOF'
import httpx, rsa, base64
c = httpx.Client(base_url='http://localhost:7860/api/v1', timeout=30)
pk = c.get('/user/public_key').json()['data']['public_key']      # RSA 公钥(存 redis, TTL 3600s)
pub = rsa.PublicKey.load_pkcs1(pk.encode())
enc = base64.b64encode(rsa.encrypt(b'Admin@2026', pub)).decode() # 密码需 RSA 加密
r = c.post('/user/regist', json={'user_name': 'admin', 'password': enc, 'external_id': 'admin-001'})
print(r.json())
EOF
```

密码策略：**≥8 位且含大小写 + 数字 + 符号**。

### 3.4 关闭验证码（本地开发推荐）

`use_captcha` 默认 `True`，且不在 `config.yaml` 里，而是 `alembic upgrade head` 时种进 DB 的 `config` 表（`key='initdb_config'` 的 YAML 字符串）。不关掉注册/登录都要验证码。

```bash
cd src/backend
.venv/bin/python - << 'EOF'
import pymysql, redis
c = pymysql.connect(host='localhost', port=3306, user='root', password='1234',
                    database='bisheng', charset='utf8mb4')
cur = c.cursor()
cur.execute("SELECT value FROM config WHERE `key`='initdb_config'")
val = cur.fetchone()[0].replace('use_captcha: True', 'use_captcha: False')
cur.execute("UPDATE config SET value=%s WHERE `key`='initdb_config'", (val,))
c.commit()
redis.Redis(host='localhost', port=6379, db=1).delete('config:initdb_config')  # 清缓存
print('done')
EOF
```

---

## 4. 前端启动

前端是 pnpm workspace，在 workspace 根一次安装：

```bash
cd src/frontend
pnpm install
```

### 管理端 / 搭建端（平台）

```bash
cd src/frontend/platform
pnpm start -- --host 0.0.0.0
```

→ http://localhost:3001 ，自动把 `/api/` 和 `/health` 代理到 `localhost:7860`，文件路由代理到 MinIO。

### 终端用户端（客户端）

```bash
cd src/frontend/client
pnpm start
```

→ http://localhost:4001 （端口硬编码，被占用会直接报错而不是漂移到 4002）

⚠️ **必须在 `client/` 目录里直接跑 `pnpm start`，不能从 workspace 根用 `pnpm --filter bishengchat start`**。
后者会报 `ERR_PNPM_UNSUPPORTED_ENGINE ... Expected >=9.15.9 <10 Got: 10.6.5`（实测，根目录 `pnpm -v` 明明是 9.15.9，疑似 `--filter` 链路上某个嵌套 pnpm 解析到了别的版本）。platform 用 `--filter` 反而正常。

⚠️ **两个前端的 API 前缀不同**：

| 前端 | API 前缀 | 示例 |
|---|---|---|
| platform :3001 | `/api/v1/...` | `http://localhost:3001/api/v1/user/public_key` |
| client :4001 | `/workspace/api/v1/...` | `http://localhost:4001/workspace/api/v1/user/public_key` |

直接打 `:4001/api/v1/...` 会 404。

### ⚠️ MinIO 代理目标必须与后端 sharepoint 一致

两个前端的 Vite 都会把 `/bisheng/...` 对象路径代理到 MinIO。默认目标是 `http://127.0.0.1:9000`，
但本项目 compose 把 MinIO 容器 9000 映射到**宿主机 9100**，且后端 `BS_MINIO_SHAREPOINT=localhost:9100`。

SigV4 预签名 URL 签的是 Host 头，目标与 sharepoint 不一致会导致上传/下载文件时
`403 SignatureDoesNotMatch`。所以两个前端都要显式指定：

```bash
cd src/frontend/platform
VITE_MINIO_PROXY_TARGET=http://localhost:9100 pnpm start -- --host 0.0.0.0

cd src/frontend/client
VITE_MINIO_PROXY_TARGET=http://localhost:9100 pnpm start
```

后台常驻（日志落盘，重启机器后照抄即可）：

```bash
mkdir -p /tmp/bisheng-frontend    # ⚠️ 目录不存在则 nohup 直接失败
cd src/frontend/platform
(VITE_MINIO_PROXY_TARGET=http://localhost:9100 nohup pnpm start -- --host 0.0.0.0 \
   > /tmp/bisheng-frontend/platform.log 2>&1 &)
cd src/frontend/client
(VITE_MINIO_PROXY_TARGET=http://localhost:9100 nohup pnpm start \
   > /tmp/bisheng-frontend/client.log 2>&1 &)
```

约 40-50 秒后 `nc -z localhost 3001` / `nc -z localhost 4001` 应通。日志末尾出现
`Local: http://localhost:3001/` 和 `[vite-plugin-static-copy] Collected N items.` 即就绪。

> `loadFrontendEnv`（`src/frontend/config/vite.ts:5`）的注释说「process.env 优先于文件」，
> 但实现只调了 `loadEnv`（纯读文件）。实测传环境变量**是生效的**（Vite 的 `loadEnv`
> 会把匹配前缀的 process.env 一起返回），所以不必建 `.env.development` 污染仓库。
>
> 想验证代理是否通：`curl "http://localhost:3001/bisheng/?list-type=2"`，
> 返回 MinIO 的 `AccessDenied` XML 说明代理通了（bucket 私有，匿名列举本就该被拒）；
> 返回 `MinIO proxy error: ...` 才是代理本身失败。

---

## 5. 演示流程

### 5.1 能演示什么（诚实说明）

刚迁移完的数据库是空白的：`knowledge` / `flow` / `assistant` 三张表都是 0 行，且**没有配置任何模型**。

本机当前已越过这个阶段——3 个模型已配好（见 §3.1.1），可以演示：

- ✅ 登录 / 登出 / 租户切换 / 权限体系（OpenFGA）
- ✅ 模型管理（多服务商、多模型、探活、工作台默认模型切换）
- ✅ 日常对话（`/workstation/chat/completions`，SSE）
- ✅ 知识库 RAG（embedding 已配，上传文档后可验证双路召回）
- ✅ 中间件全链路健康（MySQL / Redis / ES / Milvus / MinIO / OpenFGA）
- ✅ Celery 任务派发与执行
- ❌ **HostedApp 应用发布** —— `GET /apps/runtime-status` 返回 500 `service_identity_not_configured`，k8s runtime 没配，本机跑不了
- ❌ **多租户数据隔离演示** —— `multi_tenant.enabled` 是部署级开关（改 `config.yaml` + 重启 + 迁移），当前关闭

### 5.2 演示脚本

1. **起服务**：按 §0.5 顺序拉，等端口全通
2. **登管理端**：http://localhost:3001 ，账号 `admin-001` / `Admin@2026`
3. **配模型**：管理端 → 模型管理 → 查看已配的 3 个模型，现场「探活」
4. **日常对话**：http://localhost:4001/workspace 直接聊，切模型对比
5. **建知识库**：上传文档 → 观察 Celery worker 日志里解析 / embedding / 写入 Milvus + ES
6. **验证双路召回**：`curl localhost:9200/_cat/indices` 查 ES 索引；Milvus collection 需进容器查
7. **建助手/工作流**：对话或跑工作流，观察 `data/bisheng.log`
8. **验开放 API**：按 §5.5 用脚本调一次 `/workstation/chat/completions`

### 5.3 命令行验证

```bash
cd src/backend
.venv/bin/python - << 'EOF'
import httpx, rsa, base64
c = httpx.Client(base_url='http://localhost:7860/api/v1', timeout=30)
pk = c.get('/user/public_key').json()['data']['public_key']
pub = rsa.PublicKey.load_pkcs1(pk.encode())
enc = base64.b64encode(rsa.encrypt(b'Admin@2026', pub)).decode()
tok = c.post('/user/login', json={'user_name':'admin-001','password':enc}).json()['data']['access_token']
h = {'Authorization': f'bearer {tok}'}
print('登录       ', c.post('/user/login', json={'user_name':'admin-001','password':enc}).json()['status_message'])
print('管理员身份 ', c.get('/user/admin', headers=h).json()['data'])
print('模型配置   ', c.get('/llm/workbench', headers=h).json()['status_code'])
EOF
curl -s localhost:7860/health            # 注意: /health 在根路径, 不在 /api/v1 下
curl -s localhost:9200/_cluster/health   # ES
curl -s localhost:8080/healthz          # OpenFGA
docker exec bisheng-redis redis-cli -n 1 ping
```

---

## 5.4 配置模型（以讯飞 MaaS 为例）

刚迁移完的库**没有任何模型**，知识库/助手/工作流都跑不起来。第一步是配模型。

### 一键配置 LLM

```bash
cd src/backend
.venv/bin/python setup-xfyun-llm.py            # 创建服务商+模型并挂到工作台默认
.venv/bin/python setup-xfyun-llm.py --force    # 已存在则重建(自动修复悬空引用)
```

脚本做四件事：建服务商 → 建模型 → 探活 → 设为工作台默认模型，最后回读校验。

### 已实测结论

| 模型 | 模型 ID | 状态 |
|---|---|---|
| Spark-X2.6（对话） | `spark-x2.6` | ✅ 可用，已配为默认 |
| Qwen3-Embedding-8B（向量） | `xop3qwen8bembedding` | ❌ 403 `11200` 未授权 |
| Qwen3-Reranker-8B（重排） | `xop3qwen8breranker` | ❌ 403 `11200` 未授权 |

端点：`POST https://maas-api.cn-huabei-1.xf-yun.com/v2/chat/completions`

**Embedding 被挡的原因**：官方错误码表 `403 | 11200 | APIKEY 没有对应授权 | 为应用开通该模型服务，或更换 APIKEY`。当前 key 只授权了聊天模型。需去控制台「API调用 / APIKEY管理」为该应用开通向量模型后才能配。

### ⚠️ 服务商类型选 `openai_compatible`，不是 `spark`

代码里明明有 `LLMServerType.SPARK`，但别用：

```python
# bisheng/llm/domain/llm/llm.py:240  _get_spark_params
"api_key": f"{server_config.get('api_key')}:{server_config.get('api_secret')}"
```

它把 key 拼成 `api_key:api_secret`——那是讯飞**旧版** Spark 接口（APPID/APISecret）的格式。MaaS 的 `ak-` 单 key 会被拼成 `ak-xxx:None`，必失败。

用 `openai_compatible`，`_get_openai_params` 正好读 `openai_api_key` / `openai_api_base`：

```json
{
  "name": "讯飞MaaS",
  "type": "openai_compatible",
  "config": {
    "openai_api_key": "ak-ce1c5949889cfe31a5e7a1040a8fd819",
    "openai_api_base": "https://maas-api.cn-huabei-1.xf-yun.com/v2"
  }
}
```

> 注意 embedding 侧的 `_node_type` 里**没有** `spark`，只有 `openai_compatible` 能用。

### 接入 SenseNova（阶跃星辰）

```bash
cd src/backend
.venv/bin/python setup-sensenova-llm.py              # 创建/补模型 + 加入工作台可选列表
.venv/bin/python setup-sensenova-llm.py --force      # 已存在同名服务商则删再建
.venv/bin/python setup-sensenova-llm.py --default    # 同时设为日常对话/灵思默认
```

| 项 | 值 |
|---|---|
| 服务商 | `SenseNova`，`openai_compatible` |
| base | `https://token.sensenova.cn/v1` |
| 模型 | `sensenova-6.8-flash-lite`（llm，支持 tool_calls） |

脚本默认**只往工作台可选列表里追加**，不动默认模型——因为 `POST /llm/workbench` 是整体覆盖，
必须先把当前配置 GET 出来、其余字段原样写回，否则会把 `chat_title_llm` / `asr_model` 等清空。

实测：

- `GET /v1/models` → 401 `Forbidden`（该网关不开放模型列表，别指望自动发现）
- `chat/completions` 短问答 ~1.3s，300 字 ~8.3s，tool_calls ~2.3s
- **思维链关不掉**：`enable_thinking=false` / `thinking={type:disabled}` / `reasoning_effort=none` 全部报 `invalid arguments`

---

### `spark-x2.6` 是推理模型

思维链走 `reasoning_content`，**不混进 `content`**。`max_tokens` 给太小（如 80）会全耗在思维链上、`content` 返回空——实测 `max_tokens=500` 才有 39 字正文。

毕昇的 `ChatOpenAICompatible` 继承自 `ChatOpenAIReasoning`，会把 `reasoning_content` 分流到 `additional_kwargs`，`content` 保持干净，所以聊天界面正常。

### ⚠️ workbench 接口响应是双层嵌套

```
GET /api/v1/llm/workbench
  → data.data           ← 真正的配置在这
  → data.inherited_from_root
```

只读 `data` 会看到全 `null`，误判成“配置被清空了”。（我踩过。）

另外这个 setter 是**整体覆盖**（`update_workbench_llm` 无字段级合并）：`models` 传 `null` 会保留原列表，但 `chat_title_llm` / `asr_model` / `tts_model` 传 `null` 就是**真清除**。

---

## 5.5 命令行发起一轮对话（验收用）

`POST /workstation/chat/completions` 的坑比较多，照这个写：

```python
import base64, json, time, httpx, rsa
c = httpx.Client(base_url='http://localhost:7860/api/v1', timeout=300)
pk = c.get('/user/public_key').json()['data']['public_key']
pub = rsa.PublicKey.load_pkcs1(pk.encode())
enc = base64.b64encode(rsa.encrypt(b'Admin@2026', pub)).decode()
tok = c.post('/user/login', json={'user_name':'admin-001','password':enc}).json()['data']['access_token']
c.headers['Authorization'] = f'bearer {tok}'

t0 = time.time(); ans = ''
with c.stream('POST', '/workstation/chat/completions', json={
        'clientTimestamp': '2026-11-12T00:00:00Z',   # ⚠️ 必填, 少了报 422
        'model': '5',                                # 模型 id, 不是 model_name
        'text': '用一句话介绍你自己',                  # ⚠️ 字段名是 text, 不是 messages
}) as resp:
    for line in resp.iter_lines():
        if not line.startswith('data:'): continue
        p = line[5:].strip()
        if not p or p == '[DONE]': continue
        try: j = json.loads(p)
        except Exception: continue
        m = j.get('message') or {}
        if j.get('category') == 'agent_answer' and j.get('type') == 'stream':
            ans += (m.get('msg') if isinstance(m, dict) else m) or ''
print('%.1fs %s' % (time.time()-t0, ans))
```

三个必踩的坑：

| 坑 | 说明 |
|---|---|
| `clientTimestamp` 必填 | 漏了报 `422 missing`，且不带 `conversationId` 会自动建会话 |
| 请求字段是 `text` 不是 `messages` | 这是 `APIChatCompletion`（`bisheng/workstation/domain/schemas/chat.py:57`），不是 OpenAI 格式 |
| 响应是 SSE，正文在 `message.msg` | 不是 `message.content`、不是 `choices[].delta.content` |

阶段（`category`）顺序：`processing` → `question` → `agent_thinking`（仅推理模型）→ `agent_answer`。
`agent_answer` 的 `type` 有 `stream`（增量）和 `end`（全文 + `events`）。

---

## 6. 停止与重启

```bash
# 停宿主机进程(保留容器和数据)
src/backend/start-host.sh stop

# 停容器(数据在 named volume 里, 不丢)
cd docker && docker compose -p bisheng down

# 停容器并删数据(重来一遍)
cd docker && docker compose -p bisheng down -v
```

**日常改代码**：只重启 API 即可，中间件不用动。

```bash
# ⚠️ start-host.sh api 会挂住(§1.1)，用这条更稳：
pkill -f "uvicorn bisheng.main:app"
cd src/backend
(set -a; . ./.env.host; set +a; PYTHONPATH=. nohup .venv/bin/python -m uvicorn bisheng.main:app \
   --host 0.0.0.0 --port 7860 --workers 1 --no-access-log > /tmp/bisheng-host/api.log 2>&1 &)
```

> `pkill` 只杀 API，不要用 `start-host.sh stop`（那个会把 worker/linsight/beat 一起杀，
> 而 linsight 重启要 25 秒以上）。

---

## 7. 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `Environment variable BISHENG_GATEWAY_HMAC_SECRET not found` | 没 source `.env.host` | `set -a && . ./.env.host && set +a` |
| 登录报 `10600 Account or password error` | 用了 `user_name` 而不是 `external_id` | 用 `admin-001` 登录 |
| 注册报 `Kode verifikasi salah` | `use_captcha` 还是 True | 按 §3.4 关掉 |
| `SELECT ... FROM user` 返回空但用户明明注册成功了 | **`user` 是 MySQL 保留字** | 写 `` FROM `user` `` |
| 7860 端口被占 | `bisheng-backend` 容器在跑 | `docker stop bisheng-backend bisheng-backend-worker bisheng-frontend` |
| Celery beat 像卡死了 | 日志转去了 `data/bisheng.log` | 看 `data/bisheng.log` 里周期任务是否在跑 |
| Milvus 连不上 | etcd 没起来 | `docker compose -p bisheng up -d etcd` |
| ES 容器 Status 空白 | compose 没配 healthcheck，不代表没起 | `curl localhost:9200` |
| `uv run` 卡住/超时 | 触发重新同步 + 联网 | 用 `.venv/bin/python` 直接跑 |
| MinIO 连不上 | 容器 9000 → 宿主机 **9100** | `BS_MINIO_ENDPOINT=localhost:9100` |
| `localhost:7860` 不通、`docker ps` 里中间件全 `Exited` | 机器重启 / Docker 重启把容器带走了 | 按 §0.5 顺序拉：中间件 → 后端 → 前端 |
| `./start-host.sh api` 卡住不返回 | 已知缺陷，uvicorn 其实已起来 | 另开终端继续，或改用 §0.5 手动 nohup |
| 前端 nohup 后端口不通，日志只有一行 `No such file or directory` | `/tmp/bisheng-frontend` 目录不存在 | `mkdir -p /tmp/bisheng-frontend` |
| 上传/下载文件报 `403 SignatureDoesNotMatch` | 前端 MinIO 代理目标 ≠ 后端 sharepoint | 两个前端都加 `VITE_MINIO_PROXY_TARGET=http://localhost:9100` |
| `docker compose up -d` 开始疯狂 Downloading | 漏了服务名，在拉 backend/code-runner 镜像 | Ctrl-C，改成显式列 7 个中间件名 |

---

## 8. 与标准 Docker 部署的差异

| 项 | 宿主机源码运行 | 标准 Docker 部署 |
|---|---|---|
| 后端进程 | 宿主机 Python | 容器内，`--workers 8` |
| 配置 | `bisheng/config.yaml`（容器名改 `localhost`） | `docker/bisheng/config/config.yaml` |
| 日志 | `./data/bisheng.log`、`./data/statistic.log` | 容器内 `/app/data/` |
| MySQL 镜像 | `mysql:8.0`（arm64 原生） | `cr.dataelem.com/mysql:8.0`（amd64） |
| 中间件 | 只起 7 个，显式列服务名 | 全量含 backend/frontend/code-runner |

> `docker/.env` 第 19 行 `MYSQL_IMAGE` 已从 `cr.dataelem.com/mysql:8.0` 改为 `mysql:8.0`，因为前者是 amd64，在 Apple Silicon 上要走 Rosetta。如果改回了，记得改回来。

---

## 9. 关键文件

| 路径 | 作用 |
|---|---|
| `src/backend/start-host.sh` | 一键启停脚本（`middleware` / `all` / `api` / `stop` / `status`） |
| `src/backend/setup-xfyun-llm.py` | 讯飞 MaaS LLM 一键配置脚本 |
| `src/backend/setup-qwen-embedding.py` | 通义 embedding 一键配置脚本 |
| `src/backend/setup-sensenova-llm.py` | SenseNova LLM 一键配置脚本 |
| `src/backend/setup-insoft-brand.py` | INSOFT 品牌素材（logo + brandName）上传脚本 |
| `src/backend/.env.host` | 宿主机环境变量 |
| `src/backend/bisheng/config.yaml` | 宿主机配置（端口/主机名已改 localhost） |
| `src/backend/data/` | 日志落地目录 |
| `docker/docker-compose.yml` | 中间件定义 |
| `docker/.env` | 镜像 tag / 密码 |
