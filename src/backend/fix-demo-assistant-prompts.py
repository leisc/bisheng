"""给 QA 知识库 demo 收尾：把助手真正接通知识库。

背景（这次排查挖出的问题）：
  setup-demo-data.py 的 phase_assistant 用 PUT /assistant 补配置，但
    1) 没检查返回值 —— 助手已上线时 PUT 必返 10403，脚本照样打印 "+"；
    2) 没把助手下线再上线。
  所以三个助手的 prompt / model_name 从来没写进去，SystemMessage 是空的，
  spark-x2.6 又不在助手的可选模型里，模型压根不调 knowledge 工具。

本脚本做三件事，全部幂等：
  1. 把 qwen3.8-flash(id=7) 加进 tenant_system_model_config.assistant_llm.llm_list；
  2. 三个助手：下线 -> PUT(prompt + model_name="7") -> 上线（失败回滚下线）；
  3. --check 只读，不写任何东西。

用法：
  .venv/bin/python fix-demo-assistant-prompts.py
  .venv/bin/python fix-demo-assistant-prompts.py --check
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC_FILE = HERE / "setup-demo-data.py"

# 极速模型：qwen3.8-flash。助手要能选它，必须先出现在 assistant_llm.llm_list。
FAST_MODEL_ID = 7

# 助手 id -> 系统提示词。setup-demo-data.py 的 ASSISTANTS 里已有同样的文案，
# 这里重复一份是为了让本脚本能独立跑（不依赖那个文件的执行顺序）。
PROMPTS = {
    # 助手 id -> (名称, guide_word, guide_question, 系统提示词)
    #
    # 踩坑记录：第一版提示词只写「请依据知识库回答」，qwen3.8-flash 直接就
    # 用兜底话术作答，tool_calls 为空，检索工具压根没被调用。必须写成
    # 「回答前必须执行」的硬性前置动作 + 未命中时的唯一出口，模型才会先检索。
    "568b6ccc0a114bf08d3fd239344d2e83": (
        "制度问答助手",
        "你好，我是制度问答助手，可以帮你查考勤休假、报销、入职流程等公司制度。",
        ["试用期工资打折吗？", "病假需要什么证明？", "差旅住宿标准是多少？"],
        (
            "你是 INSOFT 公司制度问答助手。\n"
            "【回答前必须执行】先调用知识空间检索工具检索相关制度文件，\n"
            "把检索到的内容作为唯一事实来源。禁止凭记忆或常识回答。\n"
            "检索命中：依据原文作答，逐条注明依据的文件名称和条款编号。\n"
            "检索未命中：只回答「制度库中暂无此规定，请联系人力资源部确认」，\n"
            "不要推测、不要补充通用建议、不要列举别家制度。"
        ),
    ),
    "dc368c59520d49359ad942e29185fe01": (
        "产品支持助手",
        "你好，我是产品支持助手，可以解答平台功能、API 接入和部署相关的问题。",
        ["怎么调用对话接口？", "知识库支持哪些格式？", "平台怎么部署？"],
        (
            "你是 INSOFT 智能体平台的产品支持助手。\n"
            "【回答前必须执行】先调用知识空间检索工具检索产品文档，\n"
            "把检索到的内容作为唯一事实来源。禁止凭记忆或常识回答。\n"
            "检索命中：依据原文作答，涉及接口时给出文档中的完整请求示例，\n"
            "并注明出自哪份文档。\n"
            "检索未命中：只回答「产品知识库中暂无此内容，请联系技术支持」，\n"
            "不要寒暄、不要反问、不要给通用建议。"
        ),
    ),
    "05e229b4721b4258986de097b5e59648": (
        "IT 运维助手",
        "你好，我是 IT 运维助手，可以帮你查编制、版本记录和常见故障排查路径。",
        ["VPN 连不上怎么办？", "忘记密码怎么重置？", "Milvus 连不上怎么查？"],
        (
            "你是 INSOFT IT 运维助手，服务对象是信息技术部。\n"
            "【回答前必须执行】先调用 FAQ 检索工具和知识空间检索工具，\n"
            "把检索到的内容作为唯一事实来源。禁止凭记忆或常识回答。\n"
            "检索命中：引用 FAQ 原文口径，给出可执行的排查步骤，标注每步的预期结果。\n"
            "检索未命中：先明确说「运维 FAQ 里没有这条」，再给通用建议并注明非标准流程。\n"
            "涉及生产环境操作时必须提醒先走变更审批。"
        ),
    ),
}


def load_setup():
    spec = importlib.util.spec_from_file_location("setup_demo_data", SPEC_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # setup-demo-data.py 的 Client 只处理登录 + F114 管理态请求头，
    # 复用它可以少写一遍 RSA 登录
    mod.cli = mod.Client()
    return mod


def cfg_of(r: dict):
    """解 /llm/* 的响应：{data:{data:{...}, inherited_from_root, fallback_blocked}}。

    setup-demo-data.data_of 只解一层且要求内层是 list，这里内层是 dict，
    会把 envelope 一起返回，所以自己解两层。
    """
    d = r.get("data")
    if isinstance(d, dict) and isinstance(d.get("data"), dict):
        return d["data"]
    return d if isinstance(d, dict) else {}


def phase_models(m, check: bool) -> bool:
    """把极速模型加进助手的可选模型清单。"""
    print("━━━ ① 助手可选模型清单 ━━━")
    cfg = cfg_of(m.cli.get("/llm/assistant"))
    lst = cfg.get("llm_list") or []
    have = {str(x.get("model_id")) for x in lst}
    print("  现有: " + (", ".join(
        f"id={x.get('model_id')}{' (default)' if x.get('default') else ''}" for x in lst) or "(空)"))
    if str(FAST_MODEL_ID) in have:
        print(f"  ✓ qwen3.8-flash(id={FAST_MODEL_ID}) 已在清单里")
        return True

    # 追加，不动其他条目
    lst.append({
        "model_id": FAST_MODEL_ID,
        "agent_executor_type": "function call",
        "knowledge_max_content": 15000,
        "knowledge_sort_index": False,
        "streaming": True,
        "default": False,
    })
    print(f"  + 追加 qwen3.8-flash(id={FAST_MODEL_ID})")
    if check:
        return True
    r = m.cli.post("/llm/assistant", json={"llm_list": lst, "auto_llm": cfg.get("auto_llm")})
    if not m.ok(r):
        print(f"  ✗ 写模型清单失败: {r.get('status_code')} {m.message_of(r)[:120]}")
        return False
    print("  ✓ 已写入")
    return True


def phase_prompts(m, check: bool) -> bool:
    """下线 -> 写 prompt/model_name -> 上线。"""
    print("\n━━━ ② 助手系统提示词 + 模型 ━━━")
    allok = True
    for aid, (name, guide_word, guide_question, prompt) in PROMPTS.items():
        try:
            d = m.data_of(m.cli.get(f"/assistant/info/{aid}")) or {}
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {name}: 读详情失败 {e}")
            allok = False
            continue
        want = str(FAST_MODEL_ID)
        if ((d.get("prompt") or "").strip() == prompt
                and str(d.get("model_name") or "") == want
                and (d.get("guide_word") or "") == guide_word):
            print(f"  ✓ {name:<8} prompt / guide_word / model_name={want} 都已就位")
            continue
        if check:
            print(f"  ! {name:<8} 待更新: prompt={'空' if not (d.get('prompt') or '').strip() else '非空'}"
                  f" model_name={d.get('model_name')!r}")
            continue

        # PUT 在上线态必返 10403，必须先下线；失败时把原状态还回去
        was_online = d.get("status") == 2
        if was_online:
            r = m.cli.post("/assistant/status", json={"id": aid, "status": 1})
            if not m.ok(r):
                print(f"  ✗ {name}: 下线失败 {m.message_of(r)[:100]}")
                allok = False
                continue
        r = m.cli.put("/assistant", json={
            "id": aid,
            "prompt": prompt,
            "model_name": want,
            "temperature": 0.3,
            "max_token": 32000,
            # NOT NULL 列，缺了后端 500；其余字段按现状原样带回
            "desc": d.get("desc") or "",
            "logo": d.get("logo") or "",
            # 引导语和推荐问题也要写进去：它们同样是 NOT NULL/前端首屏要用的字段，
            # setup-demo-data.py 那次 PUT 失败后它们一直是空的
            "guide_word": guide_word,
            "guide_question": guide_question,
        })
        if not m.ok(r):
            print(f"  ✗ {name}: PUT 失败 {r.get('status_code')} {m.message_of(r)[:120]}")
            if was_online:
                m.cli.post("/assistant/status", json={"id": aid, "status": 2})
            allok = False
            continue
        if was_online:
            r = m.cli.post("/assistant/status", json={"id": aid, "status": 2})
            if not m.ok(r):
                print(f"  ✗ {name}: 重新上线失败 {m.message_of(r)[:100]}")
                allok = False
                continue
        print(f"  + {name:<8} prompt 已写入, model_name={want}")
    return allok


def main():
    ap = argparse.ArgumentParser(description="接通助手的知识库（prompt + 模型）")
    ap.add_argument("--check", action="store_true", help="只读，不写")
    a = ap.parse_args()

    m = load_setup()
    m.cli.login()
    ok = phase_models(m, a.check) and phase_prompts(m, a.check)
    print("\n" + ("=" * 56))
    print("完成" if ok else "有失败项，见上")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
