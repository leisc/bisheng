#!/usr/bin/env python3
"""关闭 spark-x2.6 的思维链, 大幅提速。

动机: 演示工作流 f-decision 的意图分类节点每次调用 spark-x2.6 都要先烧
思维链才输出一个类别。实测该网关支持多种关思考写法:

    reasoning_effort="none"        -> 1.3s, 0 思维链, 分类正确   ← 采用
    thinking={"type":"disabled"}   -> 1.5s, 0 思维链
    enable_thinking=False          -> 2.3s, 0 思维链, 分类较差
    reasoning_effort="low/minimal" -> 无效(被静默忽略, 仍有思维链)
    extra_body.enable_thinking     -> 无效(被静默忽略)

实现位置: 模型 config 的 user_kwargs。因为
    _get_openai_params() -> _get_user_kwargs(model_config) -> user_kwargs.update(params)
    -> class_object(**params) = ChatOpenAICompatible(reasoning_effort="none", ...)
而 langchain_openai 的 ChatOpenAI.reasoning_effort 是 `str | None`(非受限 Literal),
且 _default_params 无条件把它放进 payload(chat_models/base.py:1291),
所以能原样透传到 /chat/completions。

⚠️ 工作流 LLM 节点本身不支持传额外参数(nodes/llm/llm.py 无 user_kwargs/extra_body
   透传), 只能改模型配置, 因此这是**全局生效**的。

⚠️ LLMService 有 60s 进程内缓存且写操作不跨进程失效, 所以测试必须用独立进程:
      python disable-spark-thinking.py --test      # 基线
      python disable-spark-thinking.py --apply     # 关闭思考
      python disable-spark-thinking.py --test      # 关闭后
      python disable-spark-thinking.py --revert    # 恢复
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

API = "http://localhost:7860/api/v1"
MODEL_ID = 3  # spark-x2.6
SERVER_ID = 3  # 讯飞MaaS
BACKUP = Path("/tmp/spark_model_config.json")
NEW_CONFIG = {"user_kwargs": {"reasoning_effort": "none"}}

Q = "根据用户问题「今天天气怎么样」判断用户意图。只输出一个类别。可选：闲聊，公司制度问答，专业知识问答，新闻搜索，其他"


def _load_mod():
    import importlib.util

    path = Path(__file__).with_name("setup-xfyun-llm.py")
    spec = importlib.util.spec_from_file_location("setup_xfyun_llm", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _set_config(client, h, new_config):
    srv = next(
        s for s in (client.get("/llm", headers=h).json() or {})["data"]
        if s["id"] == SERVER_ID
    )
    models = []
    for m in srv.get("models") or []:
        models.append({
            "id": m["id"],
            "name": m.get("name"),
            "description": m.get("description"),
            "model_name": m["model_name"],
            "model_type": m["model_type"],
            "online": m.get("online", True),
            "config": new_config if m["id"] == MODEL_ID else (m.get("config") or {}),
        })
    r = client.put("/llm", headers=h, json={
        "id": srv["id"], "name": srv["name"], "description": srv.get("description") or "",
        "type": srv["type"], "limit_flag": srv.get("limit_flag", False),
        "limit": srv.get("limit"), "config": srv.get("config") or {},
        "share_to_children": True, "models": models,
    }).json()
    if r.get("status_code") != 200:
        sys.exit(f"❌ 更新失败: {r.get('status_message')}")


def _test():
    """经毕昇自己的 LLM 链路调用一次, 打印耗时与思维链长度"""
    import asyncio

    sys.path.insert(0, str(Path(__file__).parent))
    from bisheng.llm.domain.services.llm import LLMService

    async def _run():
        llm = await LLMService.get_bisheng_llm(
            model_id=MODEL_ID, app_type="model_test", app_id="", app_name="speed-test",
            user_id=1, temperature=0, streaming=False,
        )
        t = time.time()
        out = await llm.ainvoke(Q)
        el = time.time() - t
        reasoning = (out.additional_kwargs or {}).get("reasoning_content") or ""
        return el, (out.content or "").strip(), len(reasoning)

    el, content, rlen = asyncio.run(_run())
    print(f"  {el:5.1f}s  reasoning={rlen:4}字  content={content[:40]!r}")
    return el, rlen


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    mod = _load_mod()

    if mode == "--test":
        print("=== 经毕昇调用 spark-x2.6 ===")
        _test()
        return 0

    with httpx.Client(base_url=API, timeout=180) as client:
        h = {"Authorization": f"bearer {mod.login(client)}"}

        if mode == "--apply":
            if not BACKUP.exists():
                BACKUP.write_text(json.dumps({}, ensure_ascii=False))
            _set_config(client, h, NEW_CONFIG)
            print(f"✅ 已关闭 spark-x2.6 思考: {json.dumps(NEW_CONFIG, ensure_ascii=False)}")
            print(f"   原配置备份在 {BACKUP}")
            return 0

        if mode == "--revert":
            old = json.loads(BACKUP.read_text()) if BACKUP.exists() else {}
            _set_config(client, h, old)
            print(f"✅ 已恢复原配置: {json.dumps(old, ensure_ascii=False)}")
            return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
