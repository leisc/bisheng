"""给日常对话模式补上系统提示词（身份说明）。

用法:
    .venv/bin/python set-daily-system-prompt.py --check
    .venv/bin/python set-daily-system-prompt.py

背景 / 症状
-----------
日常对话模式问「请介绍一下你自己」，模型回答「我是通义千问（Qwen），
由阿里巴巴集团通义实验室研发……」，完全暴露底层模型身份，客户演示时很尴尬。

根因
----
bisheng/workstation/domain/services/chat_service.py:2100 只有在
`ws_config.systemPrompt` 非空时才插入 SystemMessage:

    sys_prompt = None
    if ws_config.systemPrompt:
        sys_prompt = ws_config.systemPrompt.replace(...)
    ...
    llm_messages.insert(0, SystemMessage(content=sys_prompt))

而 `WorkstationConfig.systemPrompt` 默认是 None（bisheng/api/v1/schemas.py:488），
本环境 tenant_workstation_config 表为空、GET /workstation/config/daily 返回
`is_fallback=True`，说明从没存过配置，所以没有任何系统提示词。

修复
----
POST /workstation/config/daily 是全量覆盖，必须先 GET 当前配置，
只补一个 systemPrompt 字段，其余原样回写。
"""

import json
import sys

import httpx

BASE = 'http://localhost:7860/api/v1'

SYSTEM_PROMPT = """你是 INSOFT 智能体平台的演示助手。INSOFT 是一个企业级大模型应用开发与运维平台，
提供智能体编排、工作流、知识库检索、模型统一管理等能力。

回答要求：
- 始终以 INSOFT 平台助手的身份回答，不要提及、也不要暗示你是由哪家公司或哪个基础模型研发的。
- 用户问到「你是谁」时，回答你是 INSOFT 智能体平台的演示助手，并简要说明平台能做什么。
- 回答简洁、专业，面向企业 IT 团队。
- 涉及公司制度、产品功能、开放 API 的问题，优先基于知识库检索结果回答；知识库没有覆盖时，
  明确说明「知识库中暂无此内容」，再给出一般性说明。
- 不要编造具体的数据、接口参数或版本号；不确定时建议查阅产品文档。"""


def main():
    check = '--check' in sys.argv
    c = httpx.Client(base_url=BASE, timeout=60, follow_redirects=True)
    import base64

    import rsa
    pk = c.get('/user/public_key').json()['data']['public_key']
    pub = rsa.PublicKey.load_pkcs1(pk.encode())
    pw = base64.b64encode(rsa.encrypt(b'Admin@2026', pub)).decode()
    c.post('/user/login', json={'user_name': 'admin-001', 'password': pw})

    d = c.get('/workstation/config/daily').json()['data']
    data = d.get('data') or {}
    current = data.get('systemPrompt')

    if current:
        print('当前 systemPrompt 已存在（%d 字符）：' % len(current))
        print(current[:300])
        print('\n如需覆盖，删除 --check 后重跑。')
        return

    print('当前 systemPrompt: 未配置（is_fallback=%s）' % d.get('is_fallback'))
    print('将写入 %d 字符的系统提示词。' % len(SYSTEM_PROMPT))
    if check:
        return

    payload = dict(data)
    payload['systemPrompt'] = SYSTEM_PROMPT
    r = c.post('/workstation/config/daily', json=payload)
    body = r.json()
    print('POST /workstation/config/daily ->', r.status_code, body.get('message'))
    if body.get('status_code') != 200:
        print('失败:', json.dumps(body.get('data'), ensure_ascii=False)[:500])
        return

    # 回读确认
    again = c.get('/workstation/config/daily').json()['data']
    after = (again.get('data') or {}).get('systemPrompt')
    print('回读 systemPrompt: %d 字符, is_fallback=%s' % (len(after or ''), again.get('is_fallback')))
    print('模型列表仍在:', [m.get('displayName') for m in (again.get('data') or {}).get('models', [])])
    print('\n已写入。重启后端进程后生效（或直接发一轮对话验证）。')


if __name__ == '__main__':
    main()
