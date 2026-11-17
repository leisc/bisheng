#!/usr/bin/env python3
"""给「通义千问MaaS」服务商追加 qwen-flash 快速对话模型。

动机: 演示工作流 f-decision 的意图分类节点原用 spark-x2.6(讯飞推理模型),
实测单次分类耗时 19s, 而 qwen-flash 只要 0.3s 且分类更准
("今天天气怎么样" -> 闲聊; 推理模型反而答"其他")。

⚠️ PUT /llm 的 models 列表是整体替换(update_server_with_models),
   所以必须把已有的 embedding 模型连 id 一起带上, 否则会被清掉。
"""
from __future__ import annotations

import sys

import httpx

API = "http://localhost:7860/api/v1"
SERVER_ID = 4  # 通义千问MaaS
FAST_MODEL = "qwen-flash"


def main() -> int:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).with_name("setup-xfyun-llm.py")
    spec = importlib.util.spec_from_file_location("setup_xfyun_llm", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    with httpx.Client(base_url=API, timeout=120) as client:
        token = mod.login(client)
        h = {"Authorization": f"bearer {token}"}

        srv = next(
            s for s in (client.get("/llm", headers=h).json() or {})["data"]
            if s["id"] == SERVER_ID
        )
        old_models = srv.get("models") or []
        print(f"服务商 id={srv['id']} type={srv['type']} 现有 {len(old_models)} 个模型:")
        for m in old_models:
            print(f"  - id={m['id']} {m['model_name']} ({m['model_type']})")

        if any(m["model_name"] == FAST_MODEL for m in old_models):
            print(f"\n{FAST_MODEL} 已存在，无需添加。")
            return 0

        # 保留旧模型(带 id)，追加新模型(不带 id)
        models = [
            {
                "id": m["id"],
                "name": m.get("name"),
                "description": m.get("description"),
                "model_name": m["model_name"],
                "model_type": m["model_type"],
                "online": m.get("online", True),
                "config": m.get("config") or {},
            }
            for m in old_models
        ]
        models.append({
            "name": "Qwen-Flash",
            "description": "极速对话模型，适合意图分类等轻量任务",
            "model_name": FAST_MODEL,
            "model_type": "llm",
            "online": True,
            "config": {},
        })

        r = client.put(
            "/llm",
            headers=h,
            json={
                "id": srv["id"],
                "name": srv["name"],
                "description": srv.get("description") or "",
                "type": srv["type"],
                "limit_flag": srv.get("limit_flag", False),
                "limit": srv.get("limit"),
                "config": srv.get("config") or {},
                "share_to_children": True,
                "models": models,
            },
        ).json()
        if r.get("status_code") != 200:
            sys.exit(f"❌ 更新失败: {r.get('status_message')}")

        new = next(
            s for s in (client.get("/llm", headers=h).json() or {})["data"]
            if s["id"] == SERVER_ID
        )
        added = [m for m in (new.get("models") or []) if m["model_name"] == FAST_MODEL]
        if not added:
            sys.exit("❌ 回读未见新模型")
        mid = added[0]["id"]
        print(f"\n✅ 已添加 {FAST_MODEL}  model_id={mid}")

        # 探活
        v = client.post("/llm/verify", headers=h, json={"model_id": mid}).json()
        data = v.get("data") or {}
        ok = data.get("status") == 0
        print(f"{'✅' if ok else '❌'} 探活 status={data.get('status')} {data.get('remark') or ''}")

        # 确认 embedding 模型没丢
        kept = [m["model_name"] for m in (new.get("models") or [])]
        print(f"\n当前服务商模型: {kept}")
        if not any("embedding" in k for k in kept):
            sys.exit("❌ embedding 模型被冲掉了，需检查")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
