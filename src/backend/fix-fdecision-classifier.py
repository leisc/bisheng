"""修正 f-decision 的意图分类提示词。

问题：分类器只有 user_prompt（system_prompt 是 "\n"），类别之间没有
边界说明，导致「知识库支持哪些文件格式」「产品支持助手能做什么」这类
产品/技术问题被误判为「公司制度问答」，走不到 else 分支。

修法：把 user_prompt 改成带类别定义 + 正反例的版本，temperature 降到 0，
让 5 个类别边界清晰、结果稳定。

用法: .venv/bin/python3 fix-fdecision-classifier.py
"""
import json
import sys

import pymysql

DB = dict(host='localhost', port=3306, user='root', password='1234',
          database='bisheng', charset='utf8mb4')
FLOW_ID = '827b0e3b8e824c82bb0b74162ee18ea2'
NODE_ID = 'llm_08bf8'

NEW_SYSTEM_PROMPT = """你是 BiSheng 平台的意图路由器。你的唯一任务是把用户问题分到 5 个类别中的一个。

类别定义与判据：
1. 公司制度问答 —— 问的是公司内部的人事/行政/财务/考勤/休假/报销/用章/采购/安全规范等规章制度本身。典型词：年假、病假、试用期、工资、报销、考勤、离职、入职、保密协议。
2. 专业知识问答 —— 问的是 BiSheng 产品本身的功能、用法、技术细节。典型词：知识库、文件格式、上传、工作流、编排、API、鉴权、限流、模型、部署、权限。
3. 新闻搜索 —— 想了解近期时事、新闻、行情、发布会、政策动态。
4. 闲聊 —— 打招呼、寒暄、与业务无关的日常对话。
5. 其他 —— 不属于以上任何一类。

判定规则（按优先级）：
- 只要问题里出现产品功能或技术名词（知识库、文件格式、工作流、API、模型、上传、部署等），无论多么简短，一律归「专业知识问答」。
- 只有当问题问的是"公司的某项制度/流程怎么规定"时，才归「公司制度问答」。
- 想了解最新消息、时事、动态 → 「新闻搜索」。
- 不确定时归「其他」，不要猜。

只输出类别名称本身，不要输出解释、标点、引号、序号或任何多余文字。"""

NEW_USER_PROMPT = """请判断下面这个问题属于哪一类。

用户问题：{{#input_d2931.user_input#}}

示例（仅供你理解边界，不要复述）：
- "试用期工资打折吗？" → 公司制度问答
- "年假怎么申请？" → 公司制度问答
- "知识库支持哪些文件格式？" → 专业知识问答
- "API 有速率限制吗？" → 专业知识问答
- "今天有什么新闻？" → 新闻搜索
- "你好呀" → 闲聊

只输出类别名称本身："""


def main():
    conn = pymysql.connect(**DB)
    cur = conn.cursor()
    cur.execute('SELECT name, data FROM flow WHERE id=%s', (FLOW_ID,))
    row = cur.fetchone()
    if not row:
        sys.exit('flow %s 不存在' % FLOW_ID)
    name, data = row
    graph = json.loads(data)
    node = next((n for n in graph['nodes'] if n['id'] == NODE_ID), None)
    if not node:
        sys.exit('节点 %s 不存在' % NODE_ID)

    changed = []
    for group in node['data'].get('group_params', []):
        for param in group.get('params', []):
            if param.get('key') == 'system_prompt':
                changed.append(('system_prompt', param.get('value')))
                param['value'] = NEW_SYSTEM_PROMPT
            elif param.get('key') == 'user_prompt':
                changed.append(('user_prompt', param.get('value')))
                param['value'] = NEW_USER_PROMPT
            elif param.get('key') == 'temperature':
                param['value'] = 0
    if len(changed) != 2:
        sys.exit('未找到 system_prompt/user_prompt，实际命中: %r' % [c[0] for c in changed])

    cur.execute('UPDATE flow SET data=%s WHERE id=%s', (json.dumps(graph, ensure_ascii=False), FLOW_ID))
    conn.commit()
    conn.close()
    print('已更新 %s / %s' % (name, NODE_ID))
    for k, old in changed:
        print('  %s: %r -> 新值 %d 字符' % (k, (old or '')[:40], len(NEW_SYSTEM_PROMPT if k == 'system_prompt' else NEW_USER_PROMPT)))
    print('temperature -> 0')


if __name__ == '__main__':
    main()
