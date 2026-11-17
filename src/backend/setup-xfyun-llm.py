"""配置讯飞 MaaS Spark-X2.6 为毕昇 LLM 模型(宿主机源码运行环境)。

用法:
    .venv/bin/python setup-xfyun-llm.py            # 创建并验证
    .venv/bin/python setup-xfyun-llm.py --force    # 已存在则先删再建

背景:
    端点  POST https://maas-api.cn-huabei-1.xf-yun.com/v2/chat/completions
    key    ak-ce1c5949889cfe31a5e7a1040a8fd819  (仅授权了 spark-x2.6 聊天模型)
    模型   spark-x2.6 —— 推理模型, 思维链走 additional_kwargs.reasoning_content

    服务商类型用 openai_compatible 而非 spark:
        _get_spark_params 会把 key 拼成 "api_key:api_secret"(旧版 Spark 接口),
        不适用 MaaS 的 ak- 单 key。
"""

import argparse
import json
import sys

import httpx

API = "http://localhost:7860/api/v1"
ACCOUNT = "admin-001"
PASSWORD = b"Admin@2026"

SERVER_NAME = "讯飞MaaS"
BASE_URL = "https://maas-api.cn-huabei-1.xf-yun.com/v2"
API_KEY = "ak-ce1c5949889cfe31a5e7a1040a8fd819"
MODEL_NAME = "spark-x2.6"
MODEL_LABEL = "Spark-X2.6"

DB = dict(host="localhost", port=3306, user="root", password="1234", database="bisheng", charset="utf8mb4")


def repair_dangling_model_refs(stale_ids: list[int], new_id: int) -> int:
    """删除旧模型后, tenant_system_model_config 里指向它的引用会变成悬空值。

    这些配置行存的是 JSON 字符串, 键名以 model_id 结尾(source_model_id /
    model_id / ...), 递归把这些值从 stale_ids 改写到 new_id。
    """
    if not stale_ids:
        return 0
    import json

    import pymysql

    stale = set(stale_ids)
    conn = pymysql.connect(**DB)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, `key`, value FROM tenant_system_model_config")
        rows = cur.fetchall()
        total = 0
        for rid, key, val in rows:
            obj = json.loads(val)
            n = 0

            def walk(node):
                nonlocal n
                if isinstance(node, dict):
                    for k, v in node.items():
                        if k.endswith("model_id") and v in stale:
                            node[k] = new_id
                            n += 1
                        else:
                            walk(v)
                elif isinstance(node, list):
                    for item in node:
                        walk(item)

            walk(obj)
            if n:
                cur.execute(
                    "UPDATE tenant_system_model_config SET value=%s WHERE id=%s",
                    (json.dumps(obj, ensure_ascii=False), rid),
                )
                print(f"  🔧 {key:18} 重定向 {n} 处引用 -> 模型 {new_id}")
                total += n
        conn.commit()
        return total
    finally:
        conn.close()


def login(client: httpx.Client) -> str:
    pk = client.get("/user/public_key").json()["data"]["public_key"]
    import base64

    import rsa

    pub = rsa.PublicKey.load_pkcs1(pk.encode())
    # 后端用 rsa.decrypt(b64decode(password)) —— 必须是 base64, 不是 hex
    enc = base64.b64encode(rsa.encrypt(PASSWORD, pub)).decode()
    r = client.post(
        "/user/login", json={"user_name": ACCOUNT, "password": enc}
    ).json()
    if r.get("status_code") != 200:
        sys.exit(f"登录失败: {r.get('status_message')}")
    return r["data"]["access_token"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="已存在同名服务商则先删除")
    args = ap.parse_args()

    client = httpx.Client(base_url=API, timeout=120)
    token = login(client)
    h = {"Authorization": f"bearer {token}"}

    # 1. 查重
    existing = client.get("/llm", headers=h).json().get("data") or []
    dup = [s for s in existing if s.get("name") == SERVER_NAME]
    stale_ids: list[int] = []
    if dup:
        if not args.force:
            print(f"服务商 [{SERVER_NAME}] 已存在(id={dup[0]['id']}), 跳过创建。加 --force 重建。")
            server_id = dup[0]["id"]
        else:
            for s in dup:
                stale_ids.extend(m["id"] for m in (s.get("models") or []))
                client.request("DELETE", "/llm", headers=h, json={"server_id": s["id"]})
            print(f"已删除 {len(dup)} 个同名服务商(涉及模型 {stale_ids})")
            server_id = None
    else:
        server_id = None

    # 2. 创建服务商 + 模型
    if server_id is None:
        payload = {
            "name": SERVER_NAME,
            "description": "讯飞开放平台 MaaS(Spark-X2.6), OpenAI 兼容端点",
            "type": "openai_compatible",
            "limit_flag": False,
            "limit": 0,
            "config": {
                "openai_api_key": API_KEY,
                "openai_api_base": BASE_URL,
            },
            "models": [
                {
                    "name": MODEL_LABEL,
                    "description": "讯飞星火 Spark-X2.6, 推理模型, 128k 上下文",
                    "model_name": MODEL_NAME,
                    "model_type": "llm",
                    "online": True,
                    "config": {},
                }
            ],
        }
        r = client.post("/llm", headers=h, json=payload).json()
        if r.get("status_code") != 200:
            sys.exit(f"创建失败: {json.dumps(r, ensure_ascii=False)[:400]}")
        server_id = r["data"]["id"]
        print(f"✅ 服务商创建成功  id={server_id}  type=openai_compatible")

    # 3. 找到模型 id
    info = client.get("/llm/info", headers=h, params={"server_id": server_id}).json()
    models = info.get("data", {}).get("models") or []
    target = [m for m in models if m.get("model_name") == MODEL_NAME]
    if not target:
        sys.exit(f"服务商下找不到模型 {MODEL_NAME}: {json.dumps(info, ensure_ascii=False)[:300]}")
    model_id = target[0]["id"]
    print(f"✅ 模型已就绪      id={model_id}  {MODEL_NAME}  online={target[0].get('online')}")

    if stale_ids:
        fixed = repair_dangling_model_refs(stale_ids, model_id)
        print(f"{'✅' if fixed else '⚠️ '} 悬空引用修复    {fixed} 处")

    # 4. 探活
    v = client.post("/llm/verify", headers=h, json={"model_id": model_id}).json()
    data = v.get("data") or {}
    ok = data.get("status") == 0
    print(f"{'✅' if ok else '❌'} 探活结果        status={data.get('status')} {data.get('remark') or ''}")
    if not ok:
        return 1

    # 5. 挂到工作台默认模型(可选模型列表 + 日常对话/灵思默认 + 标题生成)
    #    注意: 该 setter 是整体覆盖, models 之外的字段传 None 即清除
    ws = {
        "id": str(model_id),
        "name": MODEL_NAME,
        "displayName": MODEL_LABEL,
        "provider_id": str(server_id),
        "provider_name": SERVER_NAME,
        "description": "讯飞星火推理模型",
        "realtime": False,
        "visual": False,
    }
    w = client.post(
        "/llm/workbench",
        headers=h,
        json={
            "models": [ws],
            "linsight_default_model_id": str(model_id),
            "chat_default_model_id": str(model_id),
            "chat_title_llm": dict(ws),
        },
    ).json()
    print(f"{'✅' if w.get('status_code') == 200 else '❌'} 工作台默认模型  {w.get('status_message') or ''}")

    # 6. 回读校验(注意响应嵌套在 data.data)
    cfg = (client.get("/llm/workbench", headers=h).json() or {}).get("data", {}).get("data", {})
    models = cfg.get("models") or []
    checks = [
        ("可选模型列表", [m.get("displayName") for m in models]),
        ("日常对话默认模型", cfg.get("chat_default_model_id")),
        ("灵思默认模型", cfg.get("linsight_default_model_id")),
        ("标题生成模型", (cfg.get("chat_title_llm") or {}).get("id")),
    ]
    print("\n回读校验:")
    all_ok = True
    for label, val in checks:
        good = bool(val) and val != [] and val != [None]
        all_ok &= good
        print(f"  {'✅' if good else '❌'} {label:16} {val}")
    if not all_ok:
        return 1

    print("\n配置完成。可在管理端「模型管理」查看, 或直接建助手/工作流选用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
