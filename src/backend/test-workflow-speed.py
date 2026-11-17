#!/usr/bin/env python3
"""实测工作流 f-decision 的端到端耗时。

协议(从前端 ChatPane.getMessage / ChatInput.handleSendClick 反推):
  1. {"action":"init_data","flow_id":..,"chat_id":null,"data":{nodes,edges,viewport}}
  2. 等到输入节点就绪(收到 input_schema), 再发
     {"action":"input","flow_id":..,"chat_id":null,
      "data":{"<input节点id>":{"data":{"<变量名>":msg,"dialog_files_content":[]},
                                "message":msg,"message_id":..,"category":"question",
                                "extra":"","source":0}}}

⚠️ WS 鉴权只认 Cookie(auth.py:182 会用 cookie 覆盖 t 参数), 不是 ?t= 也不是 Authorization 头。
⚠️ init_data 后必须等输入节点就绪再发 input, 否则工作流直接 close。
⚠️ 工作流跑在**后端 uvicorn 进程**里, 用它自己的 60s 进程内模型缓存。
   改模型配置后若立刻测可能读到旧值; 等 60s 或重启 uvicorn 再测。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx
import websockets

API = "http://localhost:7860/api/v1"
WS = "ws://localhost:7860/api/v1/workflow/chat"
FLOW_ID = "827b0e3b8e824c82bb0b74162ee18ea2"
QUESTION = "今天天气怎么样"
MAX_WAIT = 300.0


async def main() -> int:
    import importlib.util

    path = Path(__file__).with_name("setup-xfyun-llm.py")
    spec = importlib.util.spec_from_file_location("setup_xfyun_llm", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    with httpx.Client(base_url=API, timeout=60) as client:
        token = mod.login(client)
        h = {"Authorization": f"bearer {token}"}
        r = client.get(f"/workflow/get_one_flow/{FLOW_ID}", headers=h).json()
        if not (r.get("data") or {}).get("data"):
            sys.exit(f"❌ 取不到工作流: {json.dumps(r, ensure_ascii=False)[:300]}")
        fd = r["data"]["data"]
        nodes = fd["nodes"]
        input_node = next(
            (n["id"] for n in nodes if "input" in n["id"] or n["data"].get("name") == "输入"),
            nodes[0]["id"],
        )
        variable = "user_input"
        for g in next(n["data"].get("group_params", []) for n in nodes if n["id"] == input_node):
            for p in g.get("params", []):
                if p.get("tab") == "input":
                    variable = p["key"]
        print(f"工作流: {r['data']['name']}  输入节点={input_node} 变量={variable}")

    msg_id = uuid.uuid4().hex
    url = f"{WS}/{FLOW_ID}"
    headers = {"Cookie": f"access_token_cookie={token}"}
    t0 = time.time()
    timeline = []
    chunks = []
    sent = False

    async with websockets.connect(url, additional_headers=headers, max_size=20 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "action": "init_data", "flow_id": FLOW_ID, "chat_id": None,
            "data": {"nodes": fd["nodes"], "edges": fd["edges"], "viewport": fd.get("viewport")},
        }))

        while time.time() - t0 < MAX_WAIT:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=MAX_WAIT)
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
            try:
                d = json.loads(raw)
            except Exception:
                continue

            el = time.time() - t0
            msg = d.get("message")
            cat, typ = d.get("category"), d.get("type")

            # 输入节点就绪 -> 发用户输入
            if not sent and isinstance(msg, dict) and "input_schema" in msg:
                await ws.send(json.dumps({
                    "action": "input", "flow_id": FLOW_ID, "chat_id": None,
                    "data": {input_node: {
                        "data": {variable: QUESTION, "dialog_files_content": []},
                        "message": QUESTION, "message_id": msg_id,
                        "category": "question", "extra": "", "source": 0,
                    }},
                }))
                sent = True
                timeline.append((el, "→ 发送用户输入", QUESTION))
                continue

            # 记录节点执行
            if cat == "node_run" and isinstance(msg, dict) and msg.get("node_id"):
                timeline.append((el, f"节点 {msg.get('name')}", typ))
            # 流式/最终输出累积
            if isinstance(msg, str) and msg.strip():
                chunks.append(msg)
            if typ == "over" and isinstance(msg, str) and msg.strip():
                timeline.append((el, "★ 输出", msg[:70]))
            if typ == "over" and isinstance(msg, dict) and (msg.get("message") or msg.get("output")):
                timeline.append((el, "★ 输出", str(msg.get("message") or msg.get("output"))[:70]))
            if cat == "processing" and typ == "close":
                timeline.append((el, "结束", ""))
                break

    total = time.time() - t0
    print(f"\n{'='*70}\n问题: {QUESTION}\n总耗时: {total:.1f}s\n{'='*70}")
    for el, what, detail in timeline:
        print(f"  {el:6.1f}s  {what:16} {detail}")
    if chunks:
        print(f"\n最终输出({len(''.join(chunks))}字): {''.join(chunks)[:500]}")
    else:
        print("\n⚠️ 未捕获到最终输出")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        QUESTION = sys.argv[1]
    sys.exit(asyncio.run(main()))
