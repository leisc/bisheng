"""BiSheng 客户演示数据一键 setup。

按顺序建：
  ① 组织：4 个部门 + 2 个子部门
  ② 用户：6 个演示账号，分配部门和角色
  ③ 用户组：2 个
  ④ 知识空间：2 个 + 上传 9 份演示文档
  ⑤ 助手：3 个
  ⑥ 工作流：3 个

幂等：已存在的对象会跳过并复用 id，可重复执行。

用法：
    .venv/bin/python setup-demo-data.py            # 全量
    .venv/bin/python setup-demo-data.py --org      # 只跑组织/用户/组
    .venv/bin/python setup-demo-data.py --knowledge
    .venv/bin/python setup-demo-data.py --assistant
    .venv/bin/python setup-demo-data.py --workflow
    .venv/bin/python setup-demo-data.py --status   # 只看现状
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
import rsa

BACKEND = Path(__file__).parent
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("PYTHONPATH", str(BACKEND))

API = "http://localhost:7860/api/v1"
ADMIN = "admin-001"
PASSWORD = "Admin@2026"
DOCS = BACKEND / "demo-docs"

DEFAULT_ROLE_ID = 2          # 普通用户
ADMIN_ROLE_ID = 1            # System Admin

# ─────────────────────────── 数据定义 ───────────────────────────

# (名称, 父部门名 or None, 说明)
DEPARTMENTS = [
    ("信息技术部", None),
    ("人力资源部", None),
    ("市场部", None),
    ("数据智能部", None),
    ("系统运维组", "信息技术部"),
    ("应用开发组", "信息技术部"),
]

# 用户组（group 表，legacy 用户组，用户创建时通过 group_roles 引用）
GROUPS = [
    "IT 技术评审组",
    "全员通知组",
]

# (登录账号, 姓名, 部门名 or None, 角色 id, 用户组名 or None)
USERS = [
    ("it-manager", "李伟", "信息技术部", DEFAULT_ROLE_ID, "IT 技术评审组"),
    ("it-ops", "张强", "系统运维组", DEFAULT_ROLE_ID, "IT 技术评审组"),
    ("it-dev", "王磊", "应用开发组", DEFAULT_ROLE_ID, "IT 技术评审组"),
    ("hr-manager", "王芳", "人力资源部", DEFAULT_ROLE_ID, "全员通知组"),
    ("mkt-specialist", "赵敏", "市场部", DEFAULT_ROLE_ID, "全员通知组"),
    ("data-engineer", "陈静", "数据智能部", DEFAULT_ROLE_ID, "IT 技术评审组"),
]

# 知识空间 → 文档目录里的文件名
KNOWLEDGE_SPACES = [
    {
        "name": "INSOFT 公司制度库",
        "description": "员工手册、考勤休假、差旅报销、信息安全等公司制度文件，面向全体员工开放。",
        "files": [
            "01-新员工入职指南.md",
            "02-考勤与休假制度.docx",
            "03-信息安全手册.txt",
            "04-各部门编制与缺口.csv",
        ],
    },
    {
        "name": "INSOFT 产品知识库",
        "description": "智能体平台的产品 FAQ、开放 API 文档、部署架构说明与版本发布记录，面向 IT 与集成方。",
        "files": [
            "05-平台常见问题FAQ.md",
            "06-开放API快速上手.md",
            "07-平台部署架构说明.md",
            "08-版本发布记录.docx",
            "09-发布记录明细.csv",
        ],
    },
]

# create-demo-qa-library.py 建的 QA 知识库名，phase_assistant 绑定 IT 运维助手时用
QA_KB_NAME = "INSOFT IT 运维 FAQ 库"

ASSISTANTS = [
    # 踩坑记录（排查过一次，别再踩）：
    #   第一版提示词只写「请依据知识库回答」，qwen3.8-flash 直接用兜底话术
    #   作答，tool_calls 为空，检索工具压根没被调用。必须写成「回答前必须
    #   执行」的硬性前置动作 + 未命中时的唯一出口，模型才会先检索。
    #   model_name 也必须是 assistant_llm.llm_list 里已有的 id，否则
    #   init_llm 抛 AssistantModelNotConfigError。
    {
        "name": "制度问答助手",
        "desc": "基于公司制度库回答员工关于考勤、休假、报销、入职流程、信息安全规范的问题。只依据知识库内容回答，不确定时明确说明。",
        "model_name": "7",
        "prompt": (
            "你是 INSOFT 公司制度问答助手。\n"
            "【回答前必须执行】先调用知识空间检索工具检索相关制度文件，\n"
            "把检索到的内容作为唯一事实来源。禁止凭记忆或常识回答。\n"
            "检索命中：依据原文作答，逐条注明依据的文件名称和条款编号。\n"
            "检索未命中：只回答「制度库中暂无此规定，请联系人力资源部确认」，\n"
            "不要推测、不要补充通用建议、不要列举别家制度。"
        ),
        "guide_word": "你好，我是制度问答助手，可以帮你查考勤休假、报销、入职流程等公司制度。",
        "guide_question": ["试用期工资打折吗？", "病假需要什么证明？", "差旅住宿标准是多少？"],
    },
    {
        "name": "产品支持助手",
        "desc": "面向客户与集成方的产品答疑助手，覆盖平台 FAQ、开放 API、部署架构、版本记录。回答时给出接口示例和文档出处。",
        "model_name": "7",
        "prompt": (
            "你是 INSOFT 智能体平台的产品支持助手。\n"
            "【回答前必须执行】先调用知识空间检索工具检索产品文档，\n"
            "把检索到的内容作为唯一事实来源。禁止凭记忆或常识回答。\n"
            "检索命中：依据原文作答，涉及接口时给出文档中的完整请求示例，\n"
            "并注明出自哪份文档。\n"
            "检索未命中：只回答「产品知识库中暂无此内容，请联系技术支持」，\n"
            "不要寒暄、不要反问、不要给通用建议。"
        ),
        "guide_word": "你好，我是产品支持助手，可以解答平台功能、API 接入和部署相关的问题。",
        "guide_question": ["怎么调用对话接口？", "知识库支持哪些格式？", "平台怎么部署？"],
    },
    {
        "name": "IT 运维助手",
        "desc": "面向 IT 部门的运维与故障排查助手，可查询编制缺口、发布记录，并给出常见故障的排查路径。",
        "model_name": "7",
        "prompt": (
            "你是 INSOFT IT 运维助手，服务对象是信息技术部。\n"
            "【回答前必须执行】先调用 FAQ 检索工具和知识空间检索工具，\n"
            "把检索到的内容作为唯一事实来源。禁止凭记忆或常识回答。\n"
            "检索命中：引用 FAQ 原文口径，给出可执行的排查步骤，标注每步的预期结果。\n"
            "检索未命中：先明确说「运维 FAQ 里没有这条」，再给通用建议并注明非标准流程。\n"
            "涉及生产环境操作时必须提醒先走变更审批。"
        ),
        "guide_word": "你好，我是 IT 运维助手，可以帮你查编制、版本记录和常见故障排查路径。",
        "guide_question": ["VPN 连不上怎么办？", "忘记密码怎么重置？", "Milvus 连不上怎么查？"],
    },
]

# 工作流定义。data 为 flow JSON（nodes + edges + viewport）。
WORKFLOWS: list[dict] = []


# ── 工作流节点构造器 ────────────────────────────────────────────
# 注意：节点只有 {id, data} 是**不完整**的。flow.data 里每个节点还必须带
# position / type / measured / dragging / selected，图还要带 viewport，
# 否则平台端打开流程图时 xyflow 的 adoptUserNodes →
# getNodePositionWithOrigin 读 node.position.x 会直接 TypeError 白屏
# （nodeTypes={flowNode: FlowNode} 匹配不上也渲染不出自定义节点）。
# 这里只构造 {id, data}，布点交给 phase_workflow 末尾统一调
# fix-flow-node-positions.py 完成（幂等，逻辑单一来源）。
# 边 id 格式为 xy-edge__{source}{sourceHandle}-{target}{targetHandle}。


def _start_node(nid: str, guide_word: str, questions: list[str]) -> dict:
    return {"id": nid, "data": {
        "v": "3", "id": nid, "name": "开始", "type": "start",
        "description": "工作流运行的起始节点。",
        "group_params": [
            {"name": "开场引导", "params": [
                {"key": "guide_word", "type": "textarea", "label": "true",
                 "value": guide_word, "placeholder": "true"},
                {"key": "guide_question", "help": "true", "type": "input_list",
                 "label": "true", "value": list(questions) + [""], "placeholder": "true"}]},
            {"name": "全局变量", "params": [
                {"key": "user_info", "type": "var", "label": "true", "value": "", "global": "key"},
                {"key": "current_time", "type": "var", "label": "true", "value": "", "global": "key"},
                {"key": "chat_history", "type": "chat_history_num", "value": 10, "global": "key"},
                {"key": "preset_question", "help": "true", "type": "input_list", "label": "true",
                 "value": [{"key": "fbcf8b", "value": ""}], "global": "item:input_list",
                 "placeholder": "true"},
                {"key": "custom_variables", "help": "true", "type": "global_var", "label": "true",
                 "value": [], "global": "item:input_list"}]}]}}


def _input_node(nid: str) -> dict:
    return {"id": nid, "data": {
        "v": "3", "id": nid,
        "tab": {"value": "dialog_input",
                "options": [{"key": "dialog_input", "help": "true", "label": "true"},
                            {"key": "form_input", "help": "true", "label": "true"}]},
        "name": "输入", "type": "input",
        "description": "接收用户在会话页面的输入，支持 2 种形式：对话框输入，表单输入。",
        "group_params": [
            {"name": "接收文本", "params": [
                {"key": "user_input", "tab": "dialog_input", "type": "var", "label": "true",
                 "global": "key"}]},
            {"name": "", "groupKey": "inputfile", "params": [
                {"key": "user_input_file", "tab": "dialog_input", "value": True, "groupTitle": True},
                {"key": "file_parse_mode", "tab": "dialog_input", "type": "select_parsemode",
                 "value": "extract_text"},
                {"key": "dialog_files_content", "tab": "dialog_input", "type": "var", "label": "true",
                 "global": "key"},
                {"key": "dialog_files_content_size", "min": 0, "tab": "dialog_input",
                 "type": "char_number", "label": "true", "value": 15000},
                {"key": "dialog_file_accept", "tab": "dialog_input", "type": "select_fileaccept",
                 "label": "true", "value": "all"},
                {"key": "dialog_image_files", "tab": "dialog_input", "help": "true", "type": "var",
                 "label": "true", "global": "key"},
                {"key": "dialog_file_paths", "tab": "dialog_input", "help": "true", "type": "var",
                 "label": "true", "global": "key"}]},
            {"name": "", "params": [
                {"key": "recommended_questions_flag", "tab": "dialog_input", "help": "true",
                 "label": "true", "value": False, "hidden": "true", "groupTitle": True},
                {"key": "recommended_llm", "tab": "dialog_input", "type": "bisheng_model",
                 "label": "true", "value": None, "required": True, "placeholder": "true"},
                {"key": "recommended_system_prompt", "tab": "dialog_input", "type": "var_textarea",
                 "label": "true", "value": ""}]}]}}


def _llm_node(nid: str, name: str, system: str, user: str,
              in_var: str = "input_d2931.user_input", in_label: str = "输入/user_input",
              model_id: int = 3) -> dict:
    return {"id": nid, "data": {
        "v": "2", "id": nid, "name": name, "type": "llm",
        "description": "调用大模型回答用户问题或者处理任务。",
        "tab": {"value": "single", "options": [{"key": "single", "label": "true"},
                                               {"key": "batch", "label": "true"}]},
        "group_params": [
            {"params": [{"key": "batch_variable", "tab": "batch", "help": "true", "test": "var",
                         "type": "user_question", "label": "true", "value": [], "global": "self",
                         "linkage": "output", "required": True, "placeholder": "true"}]},
            {"name": "模型设置", "params": [
                {"key": "model_id", "type": "bisheng_model", "label": "true", "value": model_id,
                 "required": True, "placeholder": "true"},
                {"key": "temperature", "step": 0.1, "type": "slide", "label": "true",
                 "scope": [0, 2], "value": 0.3}]},
            {"name": "提示词", "params": [
                {"key": "system_prompt", "test": "var", "type": "var_textarea", "label": "true",
                 "value": system},
                {"key": "user_prompt", "test": "var", "type": "var_textarea", "label": "true",
                 "value": user, "varZh": {in_var: in_label}, "required": True},
                {"key": "image_prompt", "help": "true", "type": "image_prompt", "label": "true",
                 "value": []}]},
            {"name": "输出", "params": [
                {"key": "output_user", "help": "true", "type": "switch", "label": "true", "value": True},
                {"key": "output", "help": "true", "type": "var", "label": "true", "value": [],
                 "global": "code:value.map(el => ({ label: el.label, value: el.key }))"}]}]}}


def _condition_node(nid: str, src_llm: str, src_label: str,
                    branches: list[tuple[str, str]]) -> dict:
    """branches: [(source_handle_id, 该分支匹配的输出值)]"""
    return {"id": nid, "data": {
        "v": "1", "id": nid, "name": "条件分支", "type": "condition",
        "description": "根据条件表达式执行不同的分支。",
        "group_params": [{"params": [{
            "key": "condition", "type": "condition", "label": "",
            "value": [{"id": hid, "operator": "and", "conditions": [{
                "id": f"cond_{i}", "left_var": f"{src_llm}.output",
                "left_label": f"{src_label}/output", "right_label": "",
                "right_value": val, "right_value_type": "input",
                "comparison_operation": "equals"}]}
                for i, (hid, val) in enumerate(branches)]}]}]}}


def _output_node(nid: str, msg: str, extra_zh: dict | None = None) -> dict:
    zh = {"input_d2931.user_input": "输入/user_input"}
    if extra_zh:
        zh.update(extra_zh)
    return {"id": nid, "data": {
        "v": "2", "id": nid, "name": "输出", "type": "output",
        "description": "可向用户发送文本和文件，并且支持进行更丰富的交互。",
        "group_params": [{"params": [
            {"key": "message", "type": "var_textarea_file", "label": "true",
             "value": {"msg": msg, "files": []}, "varZh": zh, "global": "key",
             "required": True, "placeholder": "true"},
            {"key": "output_result", "type": "output_form", "label": "用户输入内容",
             "value": {"type": "", "value": ""}, "global": "value.type=input", "options": [],
             "required": False}]}]}}


def _end_node(nid: str) -> dict:
    return {"id": nid, "data": {"v": "1", "id": nid, "name": "结束", "type": "end",
                                "description": "工作流运行到此结束。", "group_params": []}}


def _edge(src: str, tgt: str, sh: str = "right_handle", th: str = "left_handle") -> dict:
    return {"id": f"xy-edge__{src}{sh}-{tgt}{th}", "type": "customEdge",
            "source": src, "target": tgt, "animated": True,
            "sourceHandle": sh, "targetHandle": th}


def _flow(nodes: list[dict], edges: list[dict]) -> dict:
    return {"nodes": nodes, "edges": edges}


def build_workflows() -> None:
    # ── 工作流 1：员工问题分类与路由 ──
    n = [
        _start_node("start_b5a94", "请输入你的问题，我会判断它属于哪一类并给出对应入口。",
                    ["试用期工资打折吗？", "平台怎么调用对话接口？", "你好"]),
        _input_node("input_d2931"),
        _llm_node("llm_08bf8", "意图分类",
                  "你是问题分类器。把用户输入分类为以下四类之一，只输出类别名称本身，"
                  "不要输出任何解释、标点或多余文字：\n公司制度问答\n产品技术问答\n闲聊\n其他",
                  "请判断下面这个问题属于哪一类：\n\n{{#input_d2931.user_input#}}"),
        _condition_node("condition_35459", "llm_08bf8", "意图分类", [
            ("05e02ee9", "公司制度问答"),
            ("c70824db", "产品技术问答"),
        ]),
        _output_node("output_f681e",
                     "你的问题属于「公司制度」类，请使用「制度问答助手」咨询，"
                     "或联系人力资源部（内线 8001）。\n\n原问题：{{#input_d2931.user_input#}}",
                     {"llm_08bf8.output": "意图分类/输出"}),
        _output_node("output_9a1b2",
                     "你的问题属于「产品技术」类，请使用「产品支持助手」咨询，"
                     "或查阅产品知识库中的《开放API快速上手》。\n\n原问题：{{#input_d2931.user_input#}}",
                     {"llm_08bf8.output": "意图分类/输出"}),
        _output_node("output_3c4d5",
                     "你好！我是 INSOFT 智能体平台的演示助手。你可以问我公司制度、"
                     "产品功能或开放 API 相关的问题。"),
        _end_node("end_be242"),
    ]
    e = [
        _edge("start_b5a94", "input_d2931"),
        _edge("input_d2931", "llm_08bf8"),
        _edge("llm_08bf8", "condition_35459"),
        _edge("condition_35459", "output_f681e", "05e02ee9"),
        _edge("condition_35459", "output_9a1b2", "c70824db"),
        _edge("condition_35459", "output_3c4d5", "left_handle"),
        _edge("output_f681e", "end_be242"),
        _edge("output_9a1b2", "end_be242"),
        _edge("output_3c4d5", "end_be242"),
    ]
    WORKFLOWS.append({
        "name": "员工问题分类与路由",
        "description": "先用大模型判断用户问题属于制度问答、产品技术问答还是闲聊，"
                       "再分流到对应输出节点。演示工作流的分支编排能力。",
        "guide_word": "请输入你的问题，我会判断它属于哪一类并给出对应入口。",
        "data": _flow(n, e),
    })

    # ── 工作流 2：IT 故障排查引导 ──
    n = [
        _start_node("start_c3d4e", "请描述你遇到的故障现象，我给出对应的排查路径。",
                    ["MySQL 连接失败", "模型响应很慢", "知识库搜不到内容"]),
        _input_node("input_e5f6a"),
        _llm_node("llm_g7h8i", "故障类型识别",
                  "你是 IT 故障分类器。根据用户描述的故障现象，判断属于以下哪一类，"
                  "只输出类别名称本身，不要输出任何解释：\n数据库连接失败\n模型响应超时\n"
                  "文件上传失败\n其他",
                  "请判断下面这个故障属于哪一类：\n\n{{#input_e5f6a.user_input#}}",
                  in_var="input_e5f6a.user_input", in_label="输入/user_input"),
        _condition_node("condition_i9j0k", "llm_g7h8i", "故障类型识别", [
            ("a1b2c3d4", "数据库连接失败"),
            ("e5f6a7b8", "模型响应超时"),
        ]),
        _output_node("output_k1l2m",
                     "数据库连接失败排查路径：\n"
                     "1. 确认容器状态：docker ps -a --filter name=bisheng-mysql\n"
                     "2. 确认端口连通：nc -z localhost 3306\n"
                     "3. 看连接数：docker exec bisheng-mysql mysqladmin -uroot -p1234 status\n"
                     "4. 查后端日志 /tmp/bisheng-host/api.log 中的 connect timeout\n"
                     "5. 若连接池耗尽，重启 API 进程\n\n原问题：{{#input_e5f6a.user_input#}}",
                     {"llm_g7h8i.output": "故障类型识别/输出"}),
        _output_node("output_m3n4o",
                     "模型响应超时排查路径：\n"
                     "1. 管理端 → 模型管理 → 对该模型点「探活」，记录耗时\n"
                     "2. 若为推理模型，检查思维链是否过长（reasoning_content 占比）\n"
                     "3. 检查大模型节点 max_tokens 是否过小导致正文为空\n"
                     "4. 确认网络到服务商 endpoint 的连通性与限流\n"
                     "5. 把轻任务（标题生成、意图分类）换成更快的模型\n\n原问题：{{#input_e5f6a.user_input#}}",
                     {"llm_g7h8i.output": "故障类型识别/输出"}),
        _output_node("output_o5p6q",
                     "未识别的故障类型。请补充以下信息后重试：故障发生时间、"
                     "影响的模块、相关日志片段、是否最近有变更。\n\n原问题：{{#input_e5f6a.user_input#}}"),
        _end_node("end_q7r8s"),
    ]
    e = [
        _edge("start_c3d4e", "input_e5f6a"),
        _edge("input_e5f6a", "llm_g7h8i"),
        _edge("llm_g7h8i", "condition_i9j0k"),
        _edge("condition_i9j0k", "output_k1l2m", "a1b2c3d4"),
        _edge("condition_i9j0k", "output_m3n4o", "e5f6a7b8"),
        _edge("condition_i9j0k", "output_o5p6q", "left_handle"),
        _edge("output_k1l2m", "end_q7r8s"),
        _edge("output_m3n4o", "end_q7r8s"),
        _edge("output_o5p6q", "end_q7r8s"),
    ]
    WORKFLOWS.append({
        "name": "IT 故障排查引导",
        "description": "识别用户描述的 IT 故障类型，给出对应的排查路径与可执行命令。"
                       "面向 IT 部门的运维辅助工作流。",
        "guide_word": "请描述你遇到的故障现象，我给出对应的排查路径。",
        "data": _flow(n, e),
    })

    # ── 工作流 3：周报要点提炼（单链 LLM） ──
    n = [
        _start_node("start_s9t0u", "把本周的工作记录贴进来，我帮你整理成周报。",
                    ["本周完成了接口联调，写了 3 个用例，周三分库方案评审被阻塞"]),
        _input_node("input_v1w2x"),
        _llm_node("llm_y3z4a", "要点提炼",
                  "你是周报提炼助手。把用户提供的本周工作记录提炼为结构化周报，"
                  "输出 Markdown，包含：\n## 本周完成\n## 进行中\n## 风险与阻塞\n## 下周计划\n"
                  "每个条目一行，控制在 200 字以内。不要编造未提供的信息。",
                  "请把下面的本周工作记录提炼成周报：\n\n{{#input_v1w2x.user_input#}}",
                  in_var="input_v1w2x.user_input", in_label="输入/user_input"),
        _output_node("output_b5c6d",
                     "以下是提炼后的周报：\n\n{{#llm_y3z4a.output#}}\n\n"
                     "（以上内容由工作流自动生成，请核对后提交。）",
                     {"llm_y3z4a.output": "要点提炼/输出"}),
        _end_node("end_e7f8g"),
    ]
    e = [
        _edge("start_s9t0u", "input_v1w2x"),
        _edge("input_v1w2x", "llm_y3z4a"),
        _edge("llm_y3z4a", "output_b5c6d"),
        _edge("output_b5c6d", "end_e7f8g"),
    ]
    WORKFLOWS.append({
        "name": "周报要点提炼",
        "description": "把零散的工作记录提炼为结构化周报（本周完成 / 进行中 / 风险阻塞 / "
                       "下周计划）。演示单链 LLM 工作流。",
        "guide_word": "把本周的工作记录贴进来，我帮你整理成周报。",
        "data": _flow(n, e),
    })


# ─────────────────────────── HTTP 客户端 ───────────────────────────

# F114 管理范围中间件：这些 (方法, 路径) 组合的写请求必须带 X-Management-Tenant，
# 否则 19703。规则与前端 src/frontend/platform/src/controllers/managementScope.ts
# 的 requiresManagementScope 保持一致。值从 GET /knowledge/space/{id}/info 的
# 响应头 x-management-tenant 学到（即目标空间的 tenant_id，admin 为 1）。
MANAGEMENT_SCOPE_RULES: list[tuple[tuple[str, ...], "re.Pattern"]] = [
    (("POST", "DELETE"), re.compile(r"/skill/template/(?:create|[0-9]+)$")),
    (("PATCH", "DELETE"), re.compile(r"/apps/[^/]+$")),
    (("POST",), re.compile(r"/apps/[^/]+/actions/[^/]+$")),
    (("POST", "PUT", "DELETE"), re.compile(r"/apps/[^/]+/secrets/[^/]+$")),
    (("PATCH",), re.compile(r"/apps/[^/]+/data/tables/[^/]+/rows(?:/[^/]+)?$")),
    (("POST", "PATCH"), re.compile(r"/apps/[^/]+/runtime/(?:materials(?:/[^/]+)?|restore|operations/[^/]+/cancel)$")),
    (("POST",), re.compile(r"/apps/[^/]+/(?:withdraw|publish/manual-publish)$")),
    (("POST", "DELETE"), re.compile(r"/apps/[^/]+/versions/[^/]+/preview(?:/[^/]+)?$")),
    (("POST", "PUT", "PATCH", "DELETE"), re.compile(r"/(?:roles|user-groups)(?:/[^/]+)?$")),
    (("POST",), re.compile(r"/workflow/(?:create|versions|change_version)$")),
    (("PATCH",), re.compile(r"/workflow/(?:update/[^/]+|status)$")),
    (("PUT", "DELETE"), re.compile(r"/workflow/versions/[^/]+$")),
    (("DELETE",), re.compile(r"/flows/[^/]+$")),
    (("POST", "PUT"), re.compile(r"/assistant$")),
    (("POST",), re.compile(r"/assistant/(?:delete|status|flow|tool)$")),
    (("POST",), re.compile(r"/knowledge/(?:create|copy|qa/copy|upload/[^/]+|tags|file/tags(?:/batch)?)$")),
]


class Client:
    def __init__(self) -> None:
        # follow_redirects: 部分路由定义带尾斜杠(如 /departments/)，POST 会返 307
        self.c = httpx.Client(base_url=API, timeout=180, trust_env=False,
                              follow_redirects=True)
        self.management_tenant: str | None = None

    def login(self) -> None:
        pk = self.c.get("/user/public_key").json()["data"]["public_key"]
        pub = rsa.PublicKey.load_pkcs1(pk.encode())
        enc = base64.b64encode(rsa.encrypt(PASSWORD.encode(), pub)).decode()
        r = self.c.post("/user/login", json={"user_name": ADMIN, "password": enc})
        tok = r.json()["data"]["access_token"]
        self.c.headers["Authorization"] = f"bearer {tok}"

    def _scope(self, method: str, path: str) -> dict:
        """命中 F114 规则时补上管理范围头。"""
        verb = method.upper()
        p = path.split("?")[0].rstrip("/")
        if any(verb in verbs and rx.search(p) for verbs, rx in MANAGEMENT_SCOPE_RULES):
            return {"X-Management-Tenant": self.management_tenant or "1"}
        return {}

    def _j(self, resp: httpx.Response) -> dict:
        try:
            return resp.json()
        except Exception:
            return {"status_code": resp.status_code,
                    "status_message": f"非 JSON 响应: {resp.text[:200]}",
                    "data": None}

    def get(self, path: str, **kw):
        kw.setdefault("headers", {}).update(self._scope("GET", path))
        return self._j(self.c.get(path, **kw))

    def post(self, path: str, **kw):
        kw.setdefault("headers", {}).update(self._scope("POST", path))
        return self._j(self.c.post(path, **kw))

    def put(self, path: str, **kw):
        kw.setdefault("headers", {}).update(self._scope("PUT", path))
        return self._j(self.c.put(path, **kw))

    def patch(self, path: str, **kw):
        kw.setdefault("headers", {}).update(self._scope("PATCH", path))
        return self._j(self.c.patch(path, **kw))

    def delete(self, path: str, **kw):
        kw.setdefault("headers", {}).update(self._scope("DELETE", path))
        return self._j(self.c.delete(path, **kw))

    def ok(self, r: dict) -> bool:
        return ok(r)


def data_of(r: dict):
    """兼容三种响应包装：data / data.data(list) / data(list)。"""
    d = r.get("data")
    if isinstance(d, dict):
        inner = d.get("data")
        if isinstance(inner, list):
            return inner
        return d
    return d


def ok(r: dict) -> bool:
    return r.get("status_code") in (200, 201)


def message_of(r: dict) -> str:
    """统一取错误文案。后端三种键都用过：status_message / message / status_msg。"""
    for k in ("status_message", "message", "status_msg", "error"):
        v = r.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def list_of(r: dict) -> list:
    d = data_of(r)
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("list", "data", "items", "records"):
            if isinstance(d.get(k), list):
                return d[k]
    return []


# ─────────────────────────── 各阶段 ───────────────────────────


def phase_org(cli: Client) -> dict:
    print("\n━━━ ① 组织与用户 ━━━")

    def walk(parent_id: int, acc: dict) -> None:
        for d in (data_of(cli.get("/departments/children", params={"parent_id": parent_id})) or []):
            acc[d["name"]] = d
            walk(d["id"], acc)

    acc: dict = {d["name"]: d for d in (data_of(cli.get("/departments/children")) or [])}
    root_id = acc["默认组织"]["id"] if "默认组织" in acc else 1
    walk(root_id, acc)
    print(f"  已有部门 {len(acc)} 个: {sorted(acc)}")

    dept_ids: dict[str, int] = {}      # name -> 自增 id
    dept_ext: dict[str, str] = {}      # name -> BS@xxx
    for name, parent in DEPARTMENTS:
        if name in acc:
            dept_ids[name] = acc[name]["id"]
            dept_ext[name] = acc[name]["dept_id"]
            print(f"  = {name} 已存在 (id={acc[name]['id']}, {acc[name]['dept_id']})")
            continue
        pid = dept_ids.get(parent) if parent else root_id
        if pid is None:
            print(f"  ✗ {name} 上级部门 {parent} 未建成，挂到根")
            pid = root_id
        r = cli.post("/departments/", json={"name": name, "parent_id": pid, "sort_order": 0})
        if ok(r) and data_of(r):
            d = data_of(r)
            dept_ids[name] = d["id"]
            dept_ext[name] = d["dept_id"]
            print(f"  + {name:<12} (id={d['id']}, {d['dept_id']}, 上级={parent or '默认组织'})")
        else:
            print(f"  ✗ {name} 创建失败: {r.get('status_message')} {str(r)[:160]}")

    # 用户组（group 表）
    grp_raw = data_of(cli.get("/group/list")) or {}
    glist = grp_raw.get("records") if isinstance(grp_raw, dict) else grp_raw
    glist = glist or []
    gmap = {g["group_name"]: g for g in glist}
    print(f"  已有用户组 {len(gmap)} 个: {sorted(gmap)}")
    for gname in GROUPS:
        if gname in gmap:
            print(f"  = {gname} 已存在 (id={gmap[gname]['id']})")
            continue
        r = cli.post("/group/create", json={"group_name": gname})
        if ok(r) and data_of(r):
            gmap[gname] = data_of(r)
            print(f"  + {gname} (id={data_of(r).get('id')})")
        else:
            print(f"  ✗ {gname} 创建失败: {r.get('status_message')} {str(r)[:160]}")

    # /user/create 的 password 字段要求 RSA 加密后的 base64（见
    # UserService.decrypt_md5_password_strict -> decrypt_password_plain），不是明文
    pk = cli.get("/user/public_key")["data"]["public_key"]
    pub = rsa.PublicKey.load_pkcs1(pk.encode())
    enc_pwd = base64.b64encode(rsa.encrypt(PASSWORD.encode(), pub)).decode()

    users = {u["user_name"]: u for u in (list_of(cli.get("/user/list", params={"page": 1, "limit": 100})) or [])}
    by_person = {u.get("external_id"): u for u in users.values()}
    print(f"  已有用户 {len(users)} 个")

    # 清掉早期用 /user/create 建的「显示名=登录ID」账号（external_id == user_name），
    # 改用 /departments/{dept_id}/local-members 建（可分别指定显示名和登录ID）。
    # 这些账号是本次演示刚建的，无业务数据，直接走 SQL 删除。
    want_names = {name for _, name, *_ in USERS}
    stale = [u for u in users.values() if u["user_name"] in want_names
             and (u.get("external_id") == u["user_name"])]
    if stale:
        ids = [u["user_id"] for u in stale]
        print(f"  清理 {len(ids)} 个旧账号（登录ID=显示名）: {[u['user_name'] for u in stale]}")
        try:
            import pymysql
            conn = pymysql.connect(host="localhost", port=3306, user="root",
                                   password="1234", database="bisheng", charset="utf8mb4")
            with conn.cursor() as cur:
                ph = ",".join(["%s"] * len(ids))
                for t in ("usergroup", "userrole", "user"):
                    try:
                        cur.execute(f"DELETE FROM `{t}` WHERE user_id IN ({ph})", ids)
                    except Exception:
                        pass
            conn.commit(); conn.close()
            print("    ✅ 已删除")
        except Exception as e:
            print(f"    ✗ SQL 清理失败: {e}")

    user_ids: dict[str, int] = {}
    for account, name, dept, role_id, gname in USERS:
        if account in by_person:
            user_ids[account] = by_person[account]["user_id"]
            print(f"  = {account:<16} 已存在 (id={by_person[account]['user_id']})")
            continue
        ext = dept_ext.get(dept)
        if not ext:
            print(f"  ✗ {account} 部门 {dept} 不存在，跳过")
            continue
        r = cli.post(f"/departments/{ext}/local-members", json={
            "user_name": name,
            "person_id": account,
            "password": enc_pwd,
            "role_ids": [role_id],
        })
        if ok(r) and data_of(r):
            uid = data_of(r).get("user_id")
            user_ids[account] = uid
            print(f"  + {account:<16} {name:<6} (id={uid}, 部门={dept})")
        else:
            print(f"  ✗ {account} 创建失败: {r.get('status_message')} {str(r)[:200]}")

    # 挂用户组
    for account, name, dept, role_id, gname in USERS:
        uid = user_ids.get(account)
        gid = (gmap.get(gname) or {}).get("id")
        if not uid or not gid:
            continue
        r = cli.post("/group/set_user_group", json={"user_id": uid, "group_id": [gid]})
        print(f"  {'+' if ok(r) else '✗'} {name} → 用户组 {gname}")

    return {"dept_ids": dept_ids, "dept_ext": dept_ext, "user_ids": user_ids}


def phase_knowledge(cli: Client) -> dict:
    print("\n━━━ ② 知识空间与文档 ━━━")
    slist = list_of(cli.get("/knowledge/space/mine", params={"page": 1, "limit": 100}))
    smap = {s["name"]: s for s in slist}
    print(f"  已有知识空间 {len(smap)} 个: {sorted(smap)}")

    out: dict[str, dict] = {}
    for spec in KNOWLEDGE_SPACES:
        name = spec["name"]
        if name in smap:
            sid = smap[name]["id"]
            print(f"  = {name} 已存在 (id={sid})")
        else:
            r = cli.post("/knowledge/space", json={
                "name": name, "description": spec["description"], "auth_type": "public"})
            if not (cli.ok(r) and data_of(r)):
                print(f"  ✗ {name} 创建失败: {r.get('status_message')} {str(r)[:200]}")
                continue
            sid = data_of(r)["id"]
            print(f"  + {name} (id={sid})")
        out[name] = {"id": sid, "files": []}

        # 已有文件
        info = data_of(cli.get(f"/knowledge/space/{sid}/info")) or {}
        existing_files = {f.get("name") for f in (info.get("files") or [])}
        children = data_of(cli.get(f"/knowledge/space/{sid}/children", params={"page": 1, "limit": 200})) or {}
        clist = children.get("data") if isinstance(children, dict) else children
        clist = clist or []
        for f in clist:
            if isinstance(f, dict):
                existing_files.add(f.get("file_name") or f.get("name"))
        print(f"    已有文件 {len(existing_files)} 个")

        # 上传前必须先 GET space info 读响应头 x-management-tenant，
        # 再作为 X-Management-Tenant 请求头发回去（F114 管理范围中间件）
        _info = cli.c.get(f"/knowledge/space/{sid}/info")
        _mt = _info.headers.get("x-management-tenant")
        up_headers = {"X-Management-Tenant": str(_mt)} if _mt else {}

        for fname in spec["files"]:
            if fname in existing_files:
                print(f"    = {fname} 已存在")
                out[name]["files"].append(fname)
                continue
            path = DOCS / fname
            if not path.exists():
                print(f"    ✗ {fname} 本地不存在")
                continue
            up = cli.c.post(f"/knowledge/upload/{sid}",
                            files={"file": (fname, path.read_bytes(),
                                            "application/octet-stream")},
                            headers=up_headers).json()
            fp = data_of(up)
            if not (ok(up) and fp and fp.get("file_path")):
                print(f"    ✗ {fname} 上传失败: {up.get('status_message')} {str(up)[:160]}")
                continue
            reg = cli.post(f"/knowledge/space/{sid}/files",
                           json={"file_path": [fp["file_path"]], "parent_id": None})
            if ok(reg):
                print(f"    + {fname}")
                out[name]["files"].append(fname)
            else:
                print(f"    ✗ {fname} 注册失败: {reg.get('status_message')} {str(reg)[:160]}")
    return out


def phase_assistant(cli: Client, kb: dict) -> None:
    print("\n━━━ ③ 助手 ━━━")

    # 0. 先把极速模型加进助手的可选模型清单。
    #    assistant_agent.init_llm 用 `str(model_id) == assistant.model_name` 在
    #    LLMService.get_assistant_llm().llm_list 里匹配，匹配不到就抛
    #    AssistantModelNotConfigError。默认清单里只有 id=3，所以 model_name="7"
    #    的助手不写这一步必挂。
    #    GET /llm/assistant 是 data.data.llm_list 双层信封。
    fast_id = 7
    try:
        raw = cli.get("/llm/assistant")
        inner = data_of(raw) or {}
        lst = inner.get("llm_list")
        if isinstance(inner.get("data"), dict) and isinstance(inner["data"].get("llm_list"), list):
            lst = inner["data"]["llm_list"]
        lst = lst or []
        if not any(str(e.get("model_id")) == str(fast_id) for e in lst):
            lst.append({"model_id": fast_id, "agent_executor_type": "function call",
                        "knowledge_max_content": 15000, "knowledge_sort_index": False,
                        "streaming": True, "default": False})
            w = cli.post("/llm/assistant", json={"llm_list": lst, "auto_llm": inner.get("auto_llm") or {}})
            print(f"  {'+' if ok(w) else '✗'} 助手可选模型加入 id={fast_id}"
                  f"{'' if ok(w) else ': ' + message_of(w)[:120]}")
        else:
            print(f"  = 助手可选模型已含 id={fast_id}")
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ 读助手可选模型失败: {e}")

    existing = {a["name"]: a for a in (data_of(cli.get("/assistant", params={"page": 1, "limit": 100})) or [])}
    print(f"  已有助手 {len(existing)} 个: {sorted(existing)}")

    # 助手 -> 要绑的知识库（列表）。IT 运维助手同时绑产品知识库(SPACE) 和
    # IT 运维 FAQ 库(QA)，所以这里必须是列表而不是单个名字。
    # assistant_agent.init_tools 按 KnowledgeTypeEnum 拆分：type=3 走
    # build_space_knowledge_tool（工具名 knowledge_space_retriever），其余走
    # ToolExecutor.init_knowledge_tool（工具名 knowledge_{id}）。
    # IT 运维 FAQ 库由 create-demo-qa-library.py 单独创建，不在本脚本的 kb 字典里，
    # 用 QA_KB_NAME 兜底查一次库。
    kb_by_kw = {
        "制度问答助手": ["INSOFT 公司制度库"],
        "产品支持助手": ["INSOFT 产品知识库"],
        "IT 运维助手": ["INSOFT 产品知识库", QA_KB_NAME],
    }
    for spec in ASSISTANTS:
        name = spec["name"]
        if name in existing:
            aid = existing[name]["id"]
            print(f"  = {name} 已存在 (id={aid})，补全配置")
        else:
            r = cli.post("/assistant", json={"name": name, "prompt": spec["prompt"],
                                             "logo": ""})
            if not (ok(r) and data_of(r)):
                print(f"  ✗ {name} 创建失败: {r.get('status_message')} {str(r)[:180]}")
                continue
            aid = data_of(r)["id"]

        # 补描述与开场白。注意 PUT /assistant 的 AssistantUpdateReq 里 temperature/
        # model_name 默认分别是 None/""，而 update_assistant 无条件写库
        # （assistant.temperature = req.temperature），temperature 列又是 NOT NULL ——
        # 不显式传 temperature 会 500。所以这里必须给全量字段。
        #
        # 踩坑记录（别再踩）：
        #   PUT /assistant 在助手已上线时必返 10403「Assistant is live and not editable」，
        #   而这个函数原来不看返回值，照样打印 "+"，于是 prompt/model_name 从来没
        #   写进库，三个助手都是空 SystemMessage + spark-x2.6（不在 assistant_llm
        #   .llm_list 里），模型压根不调检索工具。
        #   正确顺序是：先下线 -> PUT -> 再上线，并且每一步都校验返回值。
        was_online = True
        info = data_of(cli.get(f"/assistant/info/{aid}")) or {}
        if info:
            was_online = info.get("status") == 2
        if was_online:
            off = cli.post("/assistant/status", json={"id": aid, "status": 1})
            if not ok(off):
                print(f"  ✗ {name}: 下线失败 {message_of(off)[:100]}")
                continue

        # knowledge_list 是**全量覆盖**语义，且 /assistant/info 返回的
        # knowledge_list 是 dict 列表（不是 int 列表），取 id 再并集。
        # 取并集而不是只发目标：QA 库要是没建，只发 [产品库] 会把之前绑好的
        # FAQ 库静默冲掉。
        current = []
        for x in (info.get("knowledge_list") or []):
            xid = x.get("id") if isinstance(x, dict) else x
            try:
                current.append(int(xid))
            except (TypeError, ValueError):
                pass
        wanted = set(current)
        kbnames = kb_by_kw.get(name) or []
        for kbname in kbnames:
            kid = (kb.get(kbname) or {}).get("id")
            if kid is None:
                # QA 库不在 kb 字典里，/knowledge 默认 type=0 也查不到它，要显式 type=1
                found = [k for k in (data_of(cli.get("/knowledge",
                                                     params={"page": 1, "limit": 100, "type": 1})) or [])
                         if k.get("name") == kbname]
                kid = found[0]["id"] if found else None
                if kid is None:
                    print(f"    ! {kbname} 尚未创建（跑 create-demo-qa-library.py），沿用现有绑定")
                    continue
            wanted.add(int(kid))
        ids = sorted(wanted)

        # 注意：AssistantUpdateReq 的字段注释写「Leave empty to not update」，那是**假的** ——
        # update_assistant 里 `assistant.prompt = req.prompt` 是无条件赋值，缺字段就用
        # schema 默认值（prompt=""/model_name=""/desc=""/guide_question=[]）把库刷掉。
        # 所以所有字段必须在**同一次 PUT** 里带全，绝不能拆成两次
        # （拆开的话第二次会把第一次写的 prompt/model_name 清空）。
        r1 = cli.put("/assistant", json={
            "id": aid,
            "name": name,
            "desc": spec["desc"],
            "prompt": spec["prompt"],
            "guide_word": spec["guide_word"],
            "guide_question": spec["guide_question"],
            "logo": "",
            "model_name": spec.get("model_name", ""),
            "temperature": 0.3,
            "max_token": 32000,
            "knowledge_list": ids,
        })
        if not ok(r1):
            print(f"  ✗ {name}: PUT 失败 {r1.get('status_code')} {message_of(r1)[:140]}")
            if was_online:
                cli.post("/assistant/status", json={"id": aid, "status": 2})
            continue
        print(f"    + 配置已写入（prompt/model_name={spec.get('model_name')}/知识库 {ids}）")
        print(f"  + {name:<10} (id={aid})")

        # 上线。AssistantStatus.OFFLINE=1 / ONLINE=2，应用广场的数据源
        # /api/v1/chat/online 只返回 status=2 的应用 —— 不上线的助手在广场里
        # 根本不显示。
        # 注意 POST /assistant/status 的 docstring 写的是「0 offline, 1 online」，
        # 那是错的，照它传 status=1 会命中 `assistant.status == status` 直接
        # return，静默无操作。必须传 2。
        st = cli.post("/assistant/status", json={"id": aid, "status": 2})
        print(f"    {'+' if ok(st) else '✗'} 上线: {'' if ok(st) else st.get('status_message')}")


def phase_workflow(cli: Client) -> None:
    print("\n━━━ ④ 工作流 ━━━")
    existing = {f["name"]: f for f in (data_of(cli.get("/workflow/list", params={"page": 1, "limit": 100})) or [])}
    print(f"  已有工作流 {len(existing)} 个: {sorted(existing)}")
    for spec in WORKFLOWS:
        name = spec["name"]
        if name in existing:
            fid = existing[name]["id"]
            print(f"  = {name} 已存在 (id={fid})")
        else:
            r = cli.post("/workflow/create", json={
                "name": name, "description": spec["description"],
                "guide_word": spec["guide_word"], "data": spec["data"], "logo": "",
            })
            if not (ok(r) and data_of(r)):
                print(f"  ✗ {name} 创建失败: {r.get('status_message')} {str(r)[:220]}")
                continue
            fid = data_of(r).get("id")

        # 上线（status=2）。未上线的工作流无法对话，会报 13010
        # "The current workflow is not online and cannot be chatted directly"。
        st = cli.patch(f"/workflow/update/{fid}", json={"name": name, "description": spec["description"],
                                                 "status": 2})
        print(f"  + {name:<14} (id={fid}) {'上线成功' if ok(st) else '上线失败: ' + str(st.get('status_message'))}")

    # 画布布点。新建流程的节点只有 {id, data}，缺 position/type/viewport ——
    # 后端跑流程不读 position，所以功能测试全过，但平台端一打开画布就
    # TypeError 白屏。布局逻辑复用 fix-flow-node-positions.py（幂等）。
    print("  · 画布布点（position / type / viewport）")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix-flow-node-positions.py")
    r = subprocess.run([sys.executable, script], capture_output=True, text=True)
    for line in (r.stdout or "").strip().splitlines():
        print("    " + line)
    if r.returncode:
        print("    ! 布点脚本返回 %d: %s" % (r.returncode, (r.stderr or "").strip()[:200]))


def phase_status(cli: Client) -> None:
    print("\n━━━ 现状 ━━━")
    def show(label, r, key="name"):
        items = list_of(r)
        print(f"  {label:<14} {len(items):>3}  {[i.get(key) for i in items][:12]}")

    show("知识空间", cli.get("/knowledge/space/mine", params={"page": 1, "limit": 100}))
    show("助手", cli.get("/assistant", params={"page": 1, "limit": 100}))
    show("工作流", cli.get("/workflow/list", params={"page": 1, "limit": 100}))
    show("用户", cli.get("/user/list", params={"page": 1, "limit": 100}), "user_name")
    d = list_of(cli.get("/departments/children"))
    print(f"  {'部门(一层)':<14} {len(d):>3}  {[i.get('name') for i in d]}")
    m = list_of(cli.get("/llm"))
    print(f"  {'模型':<14} {sum(len(s.get('models') or []) for s in m):>3}  "
          f"{[(s['name'], [x['name'] for x in (s.get('models') or [])]) for s in m]}")


# ─────────────────────────── main ───────────────────────────


def phase_doc_libraries(cli: Client) -> None:
    """平台端 /filelib 的文档知识库（type=0）。

    知识空间（type=3）有自己的端点，客户端能看，但平台端「知识」菜单只有
    /filelib 一个入口，而它的数据源 GET /api/v1/knowledge 显式拒绝 SPACE：
    `if knowledge_type is KnowledgeTypeEnum.SPACE: raise ...`。平台端也没有
    知识空间管理页（routes/index.tsx 只有 /filelib*）。所以 IT 部门登管理控制台
    看不到任何演示知识 —— 这里补两个同名文档库，灌同样的文档。
    """
    print("\n━━━ ② ½ 平台端文档知识库 ━━━")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "create-demo-doc-libraries.py")
    r = subprocess.run([sys.executable, script, "--wait", "180"],
                       capture_output=True, text=True)
    for line in (r.stdout or "").strip().splitlines():
        print("  " + line)
    if r.returncode:
        print("  ! 文档库脚本返回 %d: %s" % (r.returncode, (r.stderr or "").strip()[:300]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", action="store_true")
    ap.add_argument("--knowledge", action="store_true")
    ap.add_argument("--doc-libraries", action="store_true",
                    help="为平台端 /filelib 建同名文档知识库(type=0)")
    ap.add_argument("--assistant", action="store_true")
    ap.add_argument("--workflow", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    only = any([args.org, args.knowledge, args.doc_libraries,
                args.assistant, args.workflow])

    build_workflows()
    cli = Client()
    cli.login()
    print("✅ 已登录", ADMIN)

    if args.status and not only:
        phase_status(cli)
        return

    ran: set[str] = set()
    if args.org or not only:
        phase_org(cli); ran.add("org")
    kb: dict = {}
    if args.knowledge or not only:
        kb = phase_knowledge(cli); ran.add("knowledge")
    if args.doc_libraries or not only:
        phase_doc_libraries(cli); ran.add("doc_libraries")
    if args.assistant or not only:
        if not ran & {"knowledge"}:
            kb = phase_knowledge(cli)
        phase_assistant(cli, kb); ran.add("assistant")
    if args.workflow or not only:
        phase_workflow(cli); ran.add("workflow")

    print("\n━━━ 完成 ━━━")
    phase_status(cli)
    print("\n登录入口：")
    print("  管理/搭建端  http://localhost:3001")
    print("  终端用户端   http://localhost:4001/workspace")
    print("演示账号密码均为 Admin@2026，登录账号见上方「用户」列表（用 external_id 登录）。")


if __name__ == "__main__":
    main()
