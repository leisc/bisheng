"""修复 f-decision（决策引擎）工作流的意图路由。

用法:
    .venv/bin/python fix-fdecision-intent-routing.py            # 修复
    .venv/bin/python fix-fdecision-intent-routing.py --check    # 只检查

背景 / 症状
-----------
工作流首包 14.2s，且「公司制度问答」「新闻搜索」两个条件分支从未命中，
所有输入都落到 else 兜底分支（大模型3）。

根因（三层）
-----------
1. **分类节点提示词不约束输出形态**
   llm_08bf8 的 user_prompt 只说「输出：闲聊，公司制度问答，...」，
   模型会把分析过程和结论一起写进 content。

2. **条件分支是严格相等比较**
   bisheng/workflow/nodes/condition/conidition_case.py 对
   ComparisonType.EQUAL 用 `==` 比对 `llm_08bf8.output` 与右侧常量，
   长文本永远不相等 => 两个 case 全落空，走 else。

3. **深度思考拖慢分类且放大 1、2**
   qwen3.8-flash 是思考模型，分类这种简单任务会产生 60~350 个
   reasoning token，单次 2~8s 且抖动剧烈。
   注意：关闭思考要用 `enable_thinking`，langchain-openai 的
   `extra_body` 会被 openai SDK 拍平进请求体顶层
   （openai/_base_client.py:525 _merge_mappings），所以
   llm_model.config.user_kwargs.extra_body.enable_thinking=false
   是生效的；不要改成 model_kwargs（会被 BishengLLM 挪回 extra_body）。

附带修复
--------
「新闻搜索」分支原本依赖预置 web_search 工具（t_gpts_tools.id=38），
该工具 extra 为 null，load_tools 的 _EXTRA_PARAM_TOOLS['web_search']
要求必填参数 ['type','config']，任何走该分支的请求都会以
10541 "Workflow task execution failed" 中断。系统未配置任何搜索服务
key（bing/bocha/serp/tavily/jina/cloudsway/searXNG/deepseek 均无），
故把该分支改为不依赖外部服务的 LLM 节点。
若后续提供搜索 key，把 type/config 写进 t_gpts_tools.id=38 的 extra
即可恢复真联网搜索。

效果
----
分类节点 2~8s -> 0.9~1.3s；总耗时 14.2s -> 1.5~1.9s；
「你好」-> 闲聊、「最近AI领域有什么重大新闻」-> 新闻搜索、
「试用期工资怎么算？」-> 公司制度问答，路由全部正确。
"""

import json
import sys

import pymysql

FLOW_ID = '827b0e3b8e824c82bb0b74162ee18ea2'
CLASSIFY_NODE = 'llm_08bf8'
NEWS_LLM_NODE = 'llm_eafcd'
NEWS_TOOL_NODE = 'tool_f0eff'
CASE_NEWS = 'c70824db'          # condition_35459 上「新闻搜索」case 的 sourceHandle
CASE_POLICY = '05e02ee9'        # 「公司制度问答」case
COND_NODE = 'condition_35459'

CLASSIFY_PROMPT = (
    '判断用户意图。只输出下列五个类别名称中的某一个，'
    '不要输出解释、标点、引号或任何多余文字。\n'
    '类别：闲聊、公司制度问答、专业知识问答、新闻搜索、其他\n'
    '用户问题：{{#input_d2931.user_input#}}'
)
NEWS_PROMPT = (
    '用户的问题属于新闻/时事类，请基于你的知识简洁扼要地回答，'
    '不要编造具体日期或数字；如果涉及最新进展，请明确说明你的知识'
    '有时间截止、建议查阅权威新闻源。\n\n用户问题：{{#input_d2931.user_input#}}'
)


def _params(node):
    return {p['key']: p for grp in node['data']['group_params']
            for p in grp.get('params', [])}


def load_flow(cur):
    cur.execute('SELECT data, status FROM flow WHERE id=%s', (FLOW_ID,))
    row = cur.fetchone()
    if not row:
        raise SystemExit('工作流 %s 不存在' % FLOW_ID)
    data = row[0] if isinstance(row, tuple) else row['data']
    status = row[1] if isinstance(row, tuple) else row['status']
    return json.loads(data), status


def apply(d):
    """把 d 修到目标形态，返回改动说明列表。"""
    changes = []
    nodes = {n['id']: n for n in d['nodes']}

    # --- 1. 分类节点：约束输出 + 降温 + 不透传 ---
    if CLASSIFY_NODE not in nodes:
        raise SystemExit('节点 %s 不存在' % CLASSIFY_NODE)
    g = _params(nodes[CLASSIFY_NODE])
    if g['user_prompt']['value'] != CLASSIFY_PROMPT:
        g['user_prompt']['value'] = CLASSIFY_PROMPT
        changes.append('%s.user_prompt -> 强制只输出类别名称' % CLASSIFY_NODE)
    if g['temperature']['value'] != 0.1:
        g['temperature']['value'] = 0.1
        changes.append('%s.temperature %s -> 0.1' % (
            CLASSIFY_NODE, g['temperature']['value']))
    if g['output_user']['value'] is not False:
        g['output_user']['value'] = False
        changes.append('%s.output_user -> False（不再把裸标签透传给用户）'
                       % CLASSIFY_NODE)

    # --- 2. 新闻搜索分支：去掉 web_search 工具依赖 ---
    has_tool = NEWS_TOOL_NODE in nodes
    if has_tool:
        d['nodes'] = [n for n in d['nodes'] if n['id'] != NEWS_TOOL_NODE]
        changes.append('移除节点 %s（预置联网搜索，未配置 key）' % NEWS_TOOL_NODE)
    d['edges'] = [e for e in d['edges']
                  if not (e['source'] == NEWS_TOOL_NODE)]
    for e in d['edges']:
        if (e['source'] == COND_NODE and e.get('sourceHandle') == CASE_NEWS
                and e['target'] == NEWS_TOOL_NODE):
            e['target'] = NEWS_LLM_NODE
            e['id'] = 'xy-edge__%s%s-%sleft_handle' % (
                COND_NODE, CASE_NEWS, NEWS_LLM_NODE)
            changes.append('边 %s -> %s 改直连 %s' % (
                COND_NODE, NEWS_TOOL_NODE, NEWS_LLM_NODE))
    if NEWS_LLM_NODE in nodes:
        g2 = _params(nodes[NEWS_LLM_NODE])
        if g2['user_prompt']['value'] != NEWS_PROMPT:
            g2['user_prompt']['value'] = NEWS_PROMPT
            changes.append('%s.user_prompt -> 去掉 {{#tool_f0eff.output#}} 引用'
                           % NEWS_LLM_NODE)
    return changes


def report(d):
    nodes = {n['id']: n for n in d['nodes']}
    print('节点:', [n['id'] for n in d['nodes']])
    print('边:')
    for e in d['edges']:
        print('   %s --%s--> %s' % (e['source'], e.get('sourceHandle'), e['target']))
    for nid in (CLASSIFY_NODE, NEWS_LLM_NODE):
        if nid in nodes:
            g = _params(nodes[nid])
            print('%s: temperature=%s output_user=%s' % (
                nid, g['temperature']['value'], g['output_user']['value']))
            print('   prompt: %s' % g['user_prompt']['value'][:70].replace('\n', ' | '))


def main():
    check_only = '--check' in sys.argv
    conn = pymysql.connect(host='localhost', port=3306, user='root',
                           password='1234', database='bisheng',
                           charset='utf8mb4', autocommit=False)
    cur = conn.cursor()
    try:
        d, status = load_flow(cur)
        print('工作流 %s status=%s（2=已发布，改动即时生效）' % (FLOW_ID, status))
        print('=== 当前形态 ===')
        report(d)
        if check_only:
            return
        changes = apply(d)
        if not changes:
            print('\n已是目标形态，无需改动。')
            return
        cur.execute('UPDATE flow SET data=%s WHERE id=%s',
                    (json.dumps(d, ensure_ascii=False), FLOW_ID))
        conn.commit()
        print('\n=== 已应用 ===')
        for c in changes:
            print('  *', c)
        print('\n=== 修复后 ===')
        report(d)
    except Exception as e:
        conn.rollback()
        print('失败:', e)
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
