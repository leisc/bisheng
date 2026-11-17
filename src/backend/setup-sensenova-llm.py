"""接入 SenseNova(阶跃星辰) sensenova-6.8-flash-lite 到毕昇 LLM 模型。

用法:
    .venv/bin/python setup-sensenova-llm.py            # 创建/补模型 + 加入工作台可选列表
    .venv/bin/python setup-sensenova-llm.py --force    # 已存在同名服务商则先删再建
    .venv/bin/python setup-sensenova-llm.py --default  # 同时设为日常对话/灵思默认模型

背景:
    端点   POST https://token.sensenova.cn/v1/chat/completions
    模型   sensenova-6.8-flash-lite (llm, 支持 tool_calls)
    实测   短回答 ~1.3s, 300 字 ~8.3s, 工具调用 ~2.3s

    服务商类型用 openai_compatible:
        该网关不暴露 GET /models(返回 401 Forbidden), 也没有独立的 spark 协议,
        OpenAI 兼容端点 + 单 key 是唯一可行路径。

    模型自带思维链(响应里 reasoning 字段), 不支持 enable_thinking/thinking 等
    关闭参数(传了报 invalid arguments)。作为日常对话模型会略慢但可接受;
    工作流里做分类/判断节点时建议另配 qwen-flash。

    ⚠️ POST /llm/workbench 是整体覆盖(models 之外的字段传 None 即清除),
       所以本脚本先 GET 当前配置, 只往 models 里追加, 其余原样写回。
"""

import argparse
import json
import sys

import httpx

API = "http://localhost:7860/api/v1"
ACCOUNT = "admin-001"
PASSWORD = b"Admin@2026"

SERVER_NAME = "SenseNova"
BASE_URL = "https://token.sensenova.cn/v1"
API_KEY = "sk-tjeKEEaK9EZwPUZCoqWuUYKen9Fr4yzZ"
MODEL_NAME = "sensenova-6.8-flash-lite"
MODEL_LABEL = "SenseNova-6.8-Flash-Lite"
MODEL_DESC = "阶跃星辰 sensenova-6.8-flash-lite, 推理模型, 支持工具调用"

DB = dict(host="localhost", port=3306, user="root", password="1234", database="bisheng", charset="utf8mb4")


def repair_dangling_model_refs(stale_ids: list[int], new_id: int) -> int:
    """删除旧模型后, tenant_system_model_config 里指向它的引用会变成悬空值。"""
    if not stale_ids:
        return 0
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
    import base64

    import rsa

    pk = client.get("/user/public_key").json()["data"]["public_key"]
    pub = rsa.PublicKey.load_pkcs1(pk.encode())
    enc = base64.b64encode(rsa.encrypt(PASSWORD, pub)).decode()
    r = client.post("/user/login", json={"user_name": ACCOUNT, "password": enc}).json()
    if r.get("status_code") != 200:
        sys.exit(f"登录失败: {r.get('status_message')}")
    return r["data"]["access_token"]


def ws_entry(model_id: int, server_id: int) -> dict:
    """工作台可选模型列表条目(WSModel)。"""
    return {
        "id": str(model_id),
        "name": MODEL_NAME,
        "displayName": MODEL_LABEL,
        "provider_id": str(server_id),
        "provider_name": SERVER_NAME,
        "description": MODEL_DESC,
        "realtime": False,
        "visual": False,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="已存在同名服务商则先删除重建")
    ap.add_argument("--default", action="store_true", help="同时设为日常对话/灵思默认模型")
    args = ap.parse_args()

    client = httpx.Client(base_url=API, timeout=180)
    token = login(client)
    h = {"Authorization": f"bearer {token}"}
    print(f"✅ 已登录 {ACCOUNT}")

    # ── 1. 查重 ────────────────────────────────────────────────────────────
    existing = client.get("/llm", headers=h).json().get("data") or []
    if isinstance(existing, dict):
        existing = existing.get("data") or []
    dup = [s for s in existing if s.get("name") == SERVER_NAME]
    stale_ids: list[int] = []
    server_id = None

    if dup:
        if not args.force:
            server_id = dup[0]["id"]
            print(f"服务商 [{SERVER_NAME}] 已存在(id={server_id}), 复用。加 --force 重建。")
        else:
            for s in dup:
                stale_ids.extend(m["id"] for m in (s.get("models") or []))
                client.request("DELETE", "/llm", headers=h, json={"server_id": s["id"]})
            print(f"已删除 {len(dup)} 个同名服务商(涉及模型 {stale_ids})")

    # ── 2. 创建服务商 + 模型(或给已存在服务商补模型) ───────────────────────
    if server_id is None:
        payload = {
            "name": SERVER_NAME,
            "description": "阶跃星辰 SenseNova OpenAI 兼容端点",
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
                    "description": MODEL_DESC,
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
    else:
        have = [m for m in (dup[0].get("models") or []) if m.get("model_name") == MODEL_NAME]
        if not have:
            old = [
                {
                    "id": m["id"],
                    "name": m.get("name"),
                    "description": m.get("description"),
                    "model_name": m["model_name"],
                    "model_type": m.get("model_type", "llm"),
                    "online": m.get("online", True),
                    "config": m.get("config") or {},
                }
                for m in (dup[0].get("models") or [])
            ]
            old.append(
                {
                    "name": MODEL_LABEL,
                    "description": MODEL_DESC,
                    "model_name": MODEL_NAME,
                    "model_type": "llm",
                    "online": True,
                    "config": {},
                }
            )
            r = client.put("/llm", headers=h, json={"id": server_id, "models": old}).json()
            if r.get("status_code") != 200:
                sys.exit(f"追加模型失败: {json.dumps(r, ensure_ascii=False)[:400]}")
            print(f"✅ 已给服务商 {server_id} 追加模型 {MODEL_NAME}")

    # ── 3. 找到模型 id ─────────────────────────────────────────────────────
    info = client.get("/llm/info", headers=h, params={"server_id": server_id}).json()
    models = (info.get("data") or {}).get("models") or []
    target = [m for m in models if m.get("model_name") == MODEL_NAME]
    if not target:
        sys.exit(f"服务商下找不到模型 {MODEL_NAME}: {json.dumps(info, ensure_ascii=False)[:400]}")
    model_id = target[0]["id"]
    print(f"✅ 模型已就绪      id={model_id}  {MODEL_NAME}  online={target[0].get('online')}")

    if stale_ids:
        fixed = repair_dangling_model_refs(stale_ids, model_id)
        print(f"{'✅' if fixed else '⚠️ '} 悬空引用修复    {fixed} 处")

    # ── 4. 探活 ────────────────────────────────────────────────────────────
    v = client.post("/llm/verify", headers=h, json={"model_id": model_id}).json()
    data = v.get("data") or {}
    ok = data.get("status") == 0
    print(f"{'✅' if ok else '❌'} 探活结果        status={data.get('status')} {data.get('remark') or ''}")
    if not ok:
        return 1

    # ── 5. 加入工作台可选模型列表(不覆盖其他配置) ───────────────────────────
    cfg = (client.get("/llm/workbench", headers=h).json() or {}).get("data", {}).get("data", {}) or {}
    cur_models = list(cfg.get("models") or [])
    cur_models = [m for m in cur_models if str(m.get("id")) != str(model_id)]
    cur_models.append(ws_entry(model_id, server_id))

    body: dict = {"models": cur_models}
    # 其余字段原样写回, 传 None 会被清除
    for key in (
        "linsight_default_model_id",
        "chat_default_model_id",
        "embedding_model",
        "asr_model",
        "tts_model",
        "chat_title_llm",
        "image_read_model",
    ):
        val = cfg.get(key)
        if val is not None:
            body[key] = val

    if args.default:
        body["linsight_default_model_id"] = str(model_id)
        body["chat_default_model_id"] = str(model_id)
        body["chat_title_llm"] = ws_entry(model_id, server_id)

    w = client.post("/llm/workbench", headers=h, json=body).json()
    print(f"{'✅' if w.get('status_code') == 200 else '❌'} 工作台可选模型  {w.get('status_message') or ''}")
    if w.get("status_code") != 200:
        print(json.dumps(w, ensure_ascii=False)[:500])

    # ── 6. 回读校验 ────────────────────────────────────────────────────────
    cfg = (client.get("/llm/workbench", headers=h).json() or {}).get("data", {}).get("data", {}) or {}
    final_models = cfg.get("models") or []
    print("\n回读校验:")
    checks = [
        ("可选模型列表", [(m.get("displayName"), m.get("id")) for m in final_models]),
        ("日常对话默认模型", cfg.get("chat_default_model_id")),
        ("灵思默认模型", cfg.get("linsight_default_model_id")),
        ("标题生成模型", (cfg.get("chat_title_llm") or {}).get("id")),
        ("Embedding 模型", (cfg.get("embedding_model") or {}).get("id")),
    ]
    all_ok = True
    for label, val in checks:
        good = bool(val) and val != [] and val != [None]
        all_ok &= good
        print(f"  {'✅' if good else '❌'} {label:16} {val}")
    in_list = any(str(m.get("id")) == str(model_id) for m in final_models)
    all_ok &= in_list
    print(f"  {'✅' if in_list else '❌'} {'新模型在可选列表':16} id={model_id}")

    print("\n配置完成。可在管理端「模型管理」查看, 或直接建助手/工作流选用。")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
