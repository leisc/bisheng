"""接入 deepseek-v4.1-flash-1017（阿里云百炼 MaaS 专属实例）到毕昇模型。

用法:
    .venv/bin/python setup-deepseek-llm.py            # 创建服务商+模型+探活+加入工作台清单
    .venv/bin/python setup-deepseek-llm.py --check    # 只读，不写
    .venv/bin/python setup-deepseek-llm.py --force    # 同名服务商已存在则删除重建

背景
----
原演示主力模型 qwen3.8-flash（llm_model.id=7，服务商「通义千问MaaS」id=4）
额度即将用尽，换成同一阿里云账号下另一个 MaaS 专属实例上的
deepseek-v4.1-flash-1017。

端点（用户提供的真实形态，OpenAI SDK compatible-mode）::

    client = OpenAI(api_key=os.getenv("DASHSCOPE_API_KEY"),
                    base_url="https://llm-zdu7vclrg1178n99.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")
    model = "deepseek-v4.1-flash-1017"

和旧端点的区别只是 host：旧的是 cn-beijing.maas.qianwenaiapi.com，
    新的是 llm-zdu7vclrg1178n99.cn-beijing.maas.aliyuncs.com，
    key 可以复用（同一个阿里云账号的 MaaS）。

为什么用 openai_compatible 而不是 deepseek 服务商类型：
    这个网关托管多家模型（deepseek / kimi / glm / minimax / qwen / step ...），
    不是 DeepSeek 官方端点。deepseek 类型会走 CustomChatDeepSeek
    （给 tools 请求补 reasoning_content），在这里没有意义；
    openai_compatible 和已有的「讯飞MaaS」「SenseNova」走同一条通用路径。

注意
----
* ⚠️ PUT /llm 的 models 列表是整体替换（update_server_with_models），
  必须把已有模型连 id 一起带上，否则会被清掉。
* 该模型是推理模型（/models 显示 effort 可调），分类节点可能比
  qwen3.8-flash 慢；本脚本只负责接通，不做速度调优。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

API = "http://localhost:7860/api/v1"
ACCOUNT = "admin-001"
PASSWORD = b"Admin@2026"

SERVER_NAME = "阿里云MaaS"
BASE_URL = "https://llm-zdu7vclrg1178n99.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
# 用户片段里是 os.getenv("DASHSCOPE_API_KEY")；这里允许环境变量覆盖，
# 缺省用已验证可用的 key（与旧「通义千问MaaS」同一个阿里云账号）。
API_KEY = os.getenv("DASHSCOPE_API_KEY") or "sk-5784cfa33595487ba6b2c490f3df16af"

MODEL_NAME = "deepseek-v4.1-flash-1017"
MODEL_LABEL = "DeepSeek-V4.1-Flash"
MODEL_DESC = "deepseek-v4.1-flash-1017，阿里云百炼 MaaS 专属实例，推理模型，支持工具调用与图像输入"
# WSModel.description 上限 50 字符（/llm 的模型描述没有这个限制），工作台清单用短版
MODEL_DESC_SHORT = "deepseek-v4.1-flash-1017 推理模型"


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


def servers(client: httpx.Client, h: dict) -> list:
    d = client.get("/llm", headers=h).json().get("data") or []
    return d.get("data") if isinstance(d, dict) else d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只读，不写")
    ap.add_argument("--force", action="store_true", help="同名服务商已存在则删除重建")
    args = ap.parse_args()

    client = httpx.Client(base_url=API, timeout=300)
    token = login(client)
    h = {"Authorization": f"bearer {token}"}
    print(f"✅ 已登录 {ACCOUNT}")

    existing = servers(client, h)
    dup = [s for s in existing if s.get("name") == SERVER_NAME]
    server_id = None
    created = False

    if dup:
        if not args.force:
            server_id = dup[0]["id"]
            have = [m for m in (dup[0].get("models") or []) if m.get("model_name") == MODEL_NAME]
            print(f"服务商 [{SERVER_NAME}] 已存在(id={server_id})，"
                  f"{'模型已在' if have else '需补模型'}。加 --force 重建。")
        else:
            if args.check:
                print(f"服务商 [{SERVER_NAME}] 已存在，--force 会删除重建")
                return 0
            for s in dup:
                client.request("DELETE", "/llm", headers=h, json={"server_id": s["id"]})
            print(f"已删除 {len(dup)} 个同名服务商")
    elif args.check:
        print(f"服务商 [{SERVER_NAME}] 不存在，需创建")
        return 0

    if server_id is None and not args.check:
        payload = {
            "name": SERVER_NAME,
            "description": "阿里云百炼 MaaS 专属实例（OpenAI 兼容端点），托管 deepseek 等多厂模型",
            "type": "openai_compatible",
            "limit_flag": False,
            "limit": 0,
            "config": {"openai_api_key": API_KEY, "openai_api_base": BASE_URL},
            "models": [{
                "name": MODEL_LABEL,
                "description": MODEL_DESC,
                "model_name": MODEL_NAME,
                "model_type": "llm",
                "online": True,
                "config": {},
            }],
        }
        r = client.post("/llm", headers=h, json=payload).json()
        if r.get("status_code") != 200:
            sys.exit(f"创建失败: {json.dumps(r, ensure_ascii=False)[:400]}")
        server_id = r["data"]["id"]
        created = True
        print(f"✅ 服务商创建成功  id={server_id}  type=openai_compatible")

    if args.check:
        return 0

    # 服务商已存在但缺模型时补上（PUT 的 models 是整体替换，必须带旧模型 id）
    info = client.get("/llm/info", headers=h, params={"server_id": server_id}).json()
    models = (info.get("data") or {}).get("models") or []
    target = [m for m in models if m.get("model_name") == MODEL_NAME]
    if not target:
        old = [{
            "id": m["id"],
            "name": m.get("name"),
            "description": m.get("description"),
            "model_name": m["model_name"],
            "model_type": m.get("model_type", "llm"),
            "online": m.get("online", True),
            "config": m.get("config") or {},
        } for m in models]
        old.append({
            "name": MODEL_LABEL,
            "description": MODEL_DESC,
            "model_name": MODEL_NAME,
            "model_type": "llm",
            "online": True,
            "config": {},
        })
        r = client.put("/llm", headers=h, json={"id": server_id, "models": old}).json()
        if r.get("status_code") != 200:
            sys.exit(f"追加模型失败: {json.dumps(r, ensure_ascii=False)[:400]}")
        info = client.get("/llm/info", headers=h, params={"server_id": server_id}).json()
        models = (info.get("data") or {}).get("models") or []
        target = [m for m in models if m.get("model_name") == MODEL_NAME]
        if not target:
            sys.exit(f"追加后仍找不到模型 {MODEL_NAME}")
        print(f"✅ 已给服务商 {server_id} 追加模型 {MODEL_NAME}")

    model_id = target[0]["id"]
    print(f"✅ 模型已就绪      id={model_id}  {MODEL_NAME}  online={target[0].get('online')}")

    # 探活：会真的打一次接口。只有新建/补模型时才打，重复跑不浪费额度。
    if created or "--verify" in sys.argv:
        v = client.post("/llm/verify", headers=h, json={"model_id": model_id}).json()
        data = v.get("data") or {}
        ok = data.get("status") == 0
        print(f"{'✅' if ok else '❌'} 探活结果        status={data.get('status')} {data.get('remark') or ''}")
        if not ok:
            return 1
    else:
        print("·  模型已存在，跳过探活（省额度；要打加 --verify）")

    # 加入工作台可选模型列表（不覆盖其他字段）
    cfg = (client.get("/llm/workbench", headers=h).json() or {}).get("data", {}).get("data", {}) or {}
    cur_models = [m for m in (cfg.get("models") or []) if str(m.get("id")) != str(model_id)]
    cur_models.append({
        "id": str(model_id),
        "name": MODEL_NAME,
        "displayName": MODEL_LABEL,
        "provider_id": str(server_id),
        "provider_name": SERVER_NAME,
        "description": MODEL_DESC_SHORT,
        "realtime": False,
        "visual": False,
    })
    body: dict = {"models": cur_models}
    for key in ("linsight_default_model_id", "chat_default_model_id", "embedding_model",
                "asr_model", "tts_model", "chat_title_llm", "image_read_model"):
        val = cfg.get(key)
        if val is not None:
            body[key] = val
    w = client.post("/llm/workbench", headers=h, json=body).json()
    print(f"{'✅' if w.get('status_code') == 200 else '❌'} 工作台可选模型  {w.get('status_message') or ''}")
    if w.get("status_code") != 200:
        print(json.dumps(w, ensure_ascii=False)[:500])
        return 1

    print(f"\n完成。新模型 id={model_id}，服务商 id={server_id}")
    print("下一步: .venv/bin/python switch-demo-to-deepseek.py --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
