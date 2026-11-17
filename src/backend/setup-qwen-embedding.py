#!/usr/bin/env python3
"""配置通义千问 embedding 模型(经 maas.qianwenaiapi.com 兼容网关)。

与 setup-xfyun-llm.py 的区别:
  - 服务商类型是 qwen(不是 openai_compatible): 毕昇 QWEN 走 OpenAIEmbeddings +
    _get_qwen_params, 会强制 base_url 到兼容模式路径, 并对 qwen3.7-text-embedding-flash
    强制 chunk_size<=20。
  - 模型类型是 embedding, 探活方式不同(aembed_query 而非 ainvoke)。
  - 额外把模型设为工作台 embedding_model —— 这是知识库 RAG 的必需项。

⚠️ 该网关的 OpenAI 兼容路径是 /compatible-mode/v1/embeddings,
   /v1/embeddings 和 /embeddings 都是 404。别把 URL 里的 compatible-mode 去掉。
   (用户给的 dashscope 原生路径 /services/embeddings/... 是另一套响应格式,
   毕昇 QWEN 类型不走那条。)

⚠️ workbench setter 是整体覆盖(update_workbench_llm 无字段级合并), 所以本脚本
   先读回现有配置、只改 embedding_model 一个字段, 再把整份发回去, 避免把已配好的
   对话模型/默认模型清掉。
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import httpx

API = "http://localhost:7860/api/v1"
ACCOUNT = "admin-001"
PASSWORD = b"Admin@2026"

SERVER_NAME = "通义千问MaaS"
BASE_URL = "https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1"
API_KEY = "sk-5784cfa33595487ba6b2c490f3df16af"
MODEL_NAME = "qwen3.7-text-embedding-flash"
MODEL_LABEL = "Qwen3.7-Embedding-Flash"


def _load_sibling(name: str):
    """导入同目录下的 setup-xfyun-llm.py, 复用其中的悬空引用修复逻辑。"""
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    if spec is None or spec.loader is None:
        sys.exit(f"找不到 {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser(description="配置通义千问 embedding 模型")
    ap.add_argument("--force", action="store_true", help="已存在则删除重建")
    args = ap.parse_args()

    sibling = _load_sibling("setup-xfyun-llm.py")
    login = sibling.login
    repair = sibling.repair_dangling_model_refs

    with httpx.Client(base_url=API, timeout=120) as client:
        token = login(client)
        h = {"Authorization": f"bearer {token}"}

        # 1. 服务商
        existing = (client.get("/llm", headers=h).json() or {}).get("data", []) or []
        dup = [s for s in existing if s.get("name") == SERVER_NAME]
        stale_ids: list[int] = []
        server_id = None
        if dup:
            if not args.force:
                print(f"服务商 [{SERVER_NAME}] 已存在(id={dup[0]['id']}), 跳过创建。加 --force 重建。")
                server_id = dup[0]["id"]
            else:
                for s in dup:
                    stale_ids.extend(m["id"] for m in (s.get("models") or []))
                    client.request("DELETE", "/llm", headers=h, json={"server_id": s["id"]})
                print(f"已删除 {len(dup)} 个同名服务商(涉及模型 {stale_ids})")

        if server_id is None:
            r = client.post(
                "/llm",
                headers=h,
                json={
                    "name": SERVER_NAME,
                    "description": "通义千问 Embedding（兼容网关）",
                    "type": "qwen",
                    "limit_flag": False,
                    "config": {
                        "openai_api_key": API_KEY,
                        "openai_api_base": BASE_URL,
                    },
                    "share_to_children": True,
                    "models": [
                        {
                            "name": MODEL_LABEL,
                            "description": "1024 维，中文语义检索",
                            "model_name": MODEL_NAME,
                            "model_type": "embedding",
                            "online": True,
                            "config": {},
                        }
                    ],
                },
            ).json()
            if r.get("status_code") != 200:
                sys.exit(f"❌ 创建失败: {r.get('status_message')}")
            server_id = r["data"]["id"]
            print(f"✅ 服务商创建成功  id={server_id}  type=qwen")

        # 2. 确认模型就绪
        srv = next(s for s in (client.get("/llm", headers=h).json() or {})["data"] if s["id"] == server_id)
        target = [m for m in (srv.get("models") or []) if m.get("model_name") == MODEL_NAME]
        if not target:
            sys.exit(f"❌ 模型 {MODEL_NAME} 未出现在服务商 {server_id} 下")
        model_id = target[0]["id"]
        if not target[0].get("online"):
            client.put(f"/llm/model/{model_id}", headers=h, json={"online": True})
        print(f"✅ 模型已就绪      id={model_id}  {MODEL_NAME}  online=True")

        if stale_ids:
            fixed = repair(stale_ids, model_id)
            print(f"{'✅' if fixed else '⚠️ '} 悬空引用修复    {fixed} 处")

        # 3. 探活(embedding 走 aembed_query)
        v = client.post("/llm/verify", headers=h, json={"model_id": model_id}).json()
        data = v.get("data") or {}
        ok = data.get("status") == 0
        print(f"{'✅' if ok else '❌'} 探活结果        status={data.get('status')} {data.get('remark') or ''}")
        if not ok:
            return 1

        # 4. 设为工作台 embedding_model —— 先读回现有配置, 只改这一个字段
        cur = ((client.get("/llm/workbench", headers=h).json() or {}).get("data") or {}).get("data") or {}
        ws = {
            "id": str(model_id),
            "name": MODEL_NAME,
            "displayName": MODEL_LABEL,
            "provider_id": str(server_id),
            "provider_name": SERVER_NAME,
            "description": "1024 维中文语义向量",
            "realtime": False,
            "visual": False,
        }
        payload = {
            "models": cur.get("models"),
            "linsight_default_model_id": cur.get("linsight_default_model_id"),
            "chat_default_model_id": cur.get("chat_default_model_id"),
            "embedding_model": ws,
            "asr_model": cur.get("asr_model"),
            "tts_model": cur.get("tts_model"),
            "chat_title_llm": cur.get("chat_title_llm"),
            "image_read_model": cur.get("image_read_model"),
        }
        w = client.post("/llm/workbench", headers=h, json=payload).json()
        print(f"{'✅' if w.get('status_code') == 200 else '❌'} 工作台向量模型  {w.get('status_message') or ''}")

        # 5. 回读校验(响应嵌套在 data.data)
        after = ((client.get("/llm/workbench", headers=h).json() or {}).get("data") or {}).get("data") or {}
        checks = [
            ("embedding_model", (after.get("embedding_model") or {}).get("id")),
            ("可选模型列表", [m.get("displayName") for m in (after.get("models") or [])]),
            ("日常对话默认模型", after.get("chat_default_model_id")),
            ("灵思默认模型", after.get("linsight_default_model_id")),
        ]
        print("\n回读校验:")
        all_ok = True
        for label, val in checks:
            good = bool(val) and val != [] and val != [None]
            all_ok &= good
            print(f"  {'✅' if good else '❌'} {label:16} {val}")
        if not all_ok:
            return 1

        print("\n配置完成。现在可以建知识库并上传文档验证 RAG 检索。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
