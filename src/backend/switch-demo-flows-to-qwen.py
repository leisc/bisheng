"""把演示工作流的 LLM 节点统一到 qwen3.8-flash（llm_model.id=7）。

用法:
    .venv/bin/python switch-demo-flows-to-qwen.py --check
    .venv/bin/python switch-demo-flows-to-qwen.py

背景
----
f-decision 已切到 id=7 并把分类节点从 2~8s 降到 0.9~1.3s；
其余三个演示工作流还挂在 spark-x2.6（id=3，分类约 1.6s）上。
为演示观感一致，统一换成 qwen3.8-flash，并给分类节点关掉
output_user（否则裸类别名会作为一条独立消息推给用户）。

注意
----
* llm_model.id=7 的 config.user_kwargs.extra_body.enable_thinking=false
  是生效的：openai SDK 的 _merge_mappings 会把 extra_body 拍平进请求体
  顶层（openai/_base_client.py:525），DashScope 只认顶层的
  enable_thinking。不要挪到 model_kwargs。
* 若某节点原本就是 id=7，跳过；不改 temperature / 提示词。
"""

import json
import sys

import pymysql

FAST_MODEL_ID = 7
# 只给「意图/分类」类节点关透传，按节点名匹配
CLASSIFY_NODE_NAMES = {'意图分类', '大模型'}


def _params(node):
    return {p['key']: p for grp in node['data']['group_params']
            for p in grp.get('params', [])}


def main():
    check = '--check' in sys.argv
    conn = pymysql.connect(host='localhost', port=3306, user='root',
                           password='1234', database='bisheng',
                           charset='utf8mb4', autocommit=False)
    cur = conn.cursor()
    try:
        cur.execute('SELECT id, name, data FROM flow ORDER BY create_time')
        rows = cur.fetchall()
        for fid, name, data in rows:
            d = json.loads(data)
            changes = []
            for n in d['nodes']:
                if n['data'].get('type') != 'llm':
                    continue
                g = _params(n)
                mid = g['model_id']['value']
                if mid != FAST_MODEL_ID:
                    changes.append('%s(%s).model_id %s -> %s'
                                   % (n['id'], n['data']['name'], mid, FAST_MODEL_ID))
                    g['model_id']['value'] = FAST_MODEL_ID
                if n['data']['name'] in CLASSIFY_NODE_NAMES and g['output_user']['value'] is not False:
                    changes.append('%s(%s).output_user -> False'
                                   % (n['id'], n['data']['name']))
                    g['output_user']['value'] = False
            print('%-12s %s' % (name, ' | '.join(changes) if changes else '无需改动'))
            if changes and not check:
                cur.execute('UPDATE flow SET data=%s WHERE id=%s',
                            (json.dumps(d, ensure_ascii=False), fid))
        if not check:
            conn.commit()
            print('\n已提交。')
    except Exception as e:
        conn.rollback()
        print('失败:', e)
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
