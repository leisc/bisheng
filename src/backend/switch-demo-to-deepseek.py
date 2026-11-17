"""把演示工作流 + 助手从 qwen3.8-flash（llm_model.id=7）切到
deepseek-v4.1-flash-1017（llm_model.id=9，服务商「阿里云MaaS」id=6）。

用法:
    .venv/bin/python switch-demo-to-deepseek.py --check
    .venv/bin/python switch-demo-to-deepseek.py

背景
----
qwen3.8-flash 额度即将用尽。新模型 deepseek-v4.1-flash-1017 由
setup-deepseek-llm.py 接入，端点:
    https://llm-zdu7vclrg1178n99.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
（阿里云百炼 MaaS 专属实例，OpenAI 兼容，服务商类型 openai_compatible）

三处都要改，漏一处就出现「后台显示换了但实际还在跑旧模型」：
  ① flow.data 里 llm 节点的 model_id        —— 工作流真正执行的配置
  ② tenant_system_model_config.assistant_llm.llm_list —— 助手模型下拉的可见性
  ③ assistant.model_name                     —— 助手实际用的模型

不动的东西：
  * llm_model.id=4（Qwen3.7-Embedding-Flash）—— 向量化模型，与对话额度无关
  * flowversion 的 v0 历史快照（还在 model 3）—— 那是版本记录，不是执行体
  * 任何 temperature / 提示词 —— 只换模型，不调行为

注意
----
* PUT /assistant 在上线态必返 10403，必须先下线，写完再上线，失败回滚下线。
* /llm/assistant 的 POST 同样是整体覆盖，llm_list 必须带全量条目。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pymysql

HERE = Path(__file__).resolve().parent
OLD_MODEL_ID = 7   # qwen3.8-flash（即将耗尽）
NEW_MODEL_ID = 9   # deepseek-v4.1-flash-1017

DB = dict(host="localhost", port=3306, user="root", password="1234",
          database="bisheng", charset="utf8mb4")


# ── ① 工作流 ──────────────────────────────────────────────────────────────
def phase_flows(check: bool) -> bool:
    print("━━━ ① 工作流 LLM 节点 model_id ━━━")
    conn = pymysql.connect(**DB)
    cur = conn.cursor()
    ok = True
    try:
        cur.execute("SELECT id, name, data FROM flow ORDER BY create_time")
        for fid, name, data in cur.fetchall():
            d = json.loads(data)
            changes = []
            for n in d.get("nodes", []):
                if n["data"].get("type") != "llm":
                    continue
                g = {p["key"]: p for grp in n["data"]["group_params"]
                     for p in grp.get("params", [])}
                if "model_id" not in g:
                    continue
                mid = g["model_id"]["value"]
                if mid == OLD_MODEL_ID:
                    g["model_id"]["value"] = NEW_MODEL_ID
                    changes.append(f"{n['id']}({n['data'].get('name')}) {OLD_MODEL_ID}->{NEW_MODEL_ID}")
                elif mid != NEW_MODEL_ID:
                    changes.append(f"⚠ {n['id']}({n['data'].get('name')}) 意外 model_id={mid}")
                    ok = False
            print(f"  {fid[:8]} {name:<18} {' | '.join(changes) if changes else '已是新模型，跳过'}")
            if changes and not check:
                cur.execute("UPDATE flow SET data=%s WHERE id=%s",
                            (json.dumps(d, ensure_ascii=False), fid))
        if not check:
            conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        print("  失败:", e)
        ok = False
    finally:
        conn.close()
    return ok


# ── ② 助手可选模型清单 ────────────────────────────────────────────────────
def cfg_of(r: dict):
    """解 /llm/* 双层 envelope: {data:{data:{...}}}。"""
    d = r.get("data")
    if isinstance(d, dict) and isinstance(d.get("data"), dict):
        return d["data"]
    return d if isinstance(d, dict) else {}


def phase_llm_list(m, check: bool) -> bool:
    print("\n━━━ ② 助手可选模型清单 assistant_llm.llm_list ━━━")
    cfg = cfg_of(m.cli.get("/llm/assistant"))
    lst = list(cfg.get("llm_list") or [])
    have = {str(x.get("model_id")) for x in lst}
    print("  现有: " + (", ".join(f"id={x.get('model_id')}"
                                  f"{' (default)' if x.get('default') else ''}" for x in lst) or "(空)"))
    if str(NEW_MODEL_ID) in have:
        print(f"  ✓ 新模型(id={NEW_MODEL_ID}) 已在清单里")
        return True
    lst.append({
        "model_id": NEW_MODEL_ID,
        "agent_executor_type": "function call",
        "knowledge_max_content": 15000,
        "knowledge_sort_index": False,
        "streaming": True,
        "default": False,
    })
    print(f"  + 追加 id={NEW_MODEL_ID}")
    if check:
        return True
    r = m.cli.post("/llm/assistant", json={"llm_list": lst, "auto_llm": cfg.get("auto_llm")})
    if not m.ok(r):
        print(f"  ✗ 写模型清单失败: {r.get('status_code')} {m.message_of(r)[:120]}")
        return False
    print("  ✓ 已写入")
    return True


# ── ③ 助手 model_name ─────────────────────────────────────────────────────
def phase_assistants(m, check: bool) -> bool:
    print("\n━━━ ③ 助手 model_name ━━━")
    conn = pymysql.connect(**DB)
    cur = conn.cursor()
    cur.execute("SELECT id, name, model_name, status FROM assistant ORDER BY create_time")
    rows = cur.fetchall()
    conn.close()

    want = str(NEW_MODEL_ID)
    ok = True
    for aid, name, cur_model, status in rows:
        d = m.data_of(m.cli.get(f"/assistant/info/{aid}")) or {}
        if str(d.get("model_name") or "") == want:
            print(f"  ✓ {name:<10} model_name={want} 已就位")
            continue
        if str(d.get("model_name") or "") != str(cur_model):
            print(f"  ! {name:<10} DB(model_name={cur_model}) 与接口(model_name={d.get('model_name')}) 不一致")
        if check:
            print(f"  ! {name:<10} 待更新 model_name={d.get('model_name')!r} -> {want}")
            ok = False
            continue

        was_online = d.get("status") == 2
        if was_online:
            r = m.cli.post("/assistant/status", json={"id": aid, "status": 1})
            if not m.ok(r):
                print(f"  ✗ {name}: 下线失败 {m.message_of(r)[:100]}")
                ok = False
                continue
        # AssistantUpdateReq 里空串 = 不更新，但 NOT NULL 列必须给值；
        # 这里把读到的现值原样带回，避免误清空 prompt/引导语。
        r = m.cli.put("/assistant", json={
            "id": aid,
            "model_name": want,
            "desc": d.get("desc") or "",
            "logo": d.get("logo") or "",
            "prompt": d.get("prompt") or "",
            "guide_word": d.get("guide_word") or "",
            "guide_question": d.get("guide_question") or [],
            "temperature": d.get("temperature"),
            "max_token": d.get("max_token") or 32000,
        })
        if not m.ok(r):
            print(f"  ✗ {name}: PUT 失败 {r.get('status_code')} {m.message_of(r)[:120]}")
            if was_online:
                m.cli.post("/assistant/status", json={"id": aid, "status": 2})
            ok = False
            continue
        if was_online:
            r = m.cli.post("/assistant/status", json={"id": aid, "status": 2})
            if not m.ok(r):
                print(f"  ✗ {name}: 重新上线失败 {m.message_of(r)[:100]}")
                ok = False
                continue
        print(f"  + {name:<10} model_name {d.get('model_name')!r} -> {want}")
    return ok


def load_setup():
    """复用 setup-demo-data.Client（RSA 登录 + F114 管理态头），少写一遍登录。"""
    spec = importlib.util.spec_from_file_location("setup_demo_data", HERE / "setup-demo-data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.cli = mod.Client()
    mod.cli.login()
    return mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只读，不写")
    a = ap.parse_args()

    m = load_setup()
    print(f"目标: model {OLD_MODEL_ID} (qwen3.8-flash) -> {NEW_MODEL_ID} (deepseek-v4.1-flash-1017)")
    ok = phase_flows(a.check)
    ok = phase_llm_list(m, a.check) and ok
    ok = phase_assistants(m, a.check) and ok
    print("\n" + "=" * 56)
    print("完成" if ok else "有失败项，见上")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
