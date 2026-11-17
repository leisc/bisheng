"""修复条件节点的 else 兜底边。

用法:
    .venv/bin/python fix-condition-else-edge.py --check
    .venv/bin/python fix-condition-else-edge.py

背景 / 症状
-----------
「IT 故障排查引导」输入「知识库搜不到内容」，「员工问题分类与路由」
输入不在两个 case 内的问题时，工作流走到条件节点就停了，
没有任何输出，前端一直转圈。

根因
----
bisheng/workflow/nodes/condition/condition.py:33 的兜底逻辑是硬编码的:

    if next_node_ids is None:
        self._next_node_id = self.get_next_node_id('right_handle')

也就是说 else 分支必须连在条件节点的 **right_handle** 上。
这两个演示流程的 else 边连的是 left_handle，get_next_node_id
取不到，next_node_ids 为 None，_next_node_id 也为 None，
LangGraph 找不到后继节点，流程静默终止（不报错、不输出）。

修复
----把非 case 的兜底边的 sourceHandle 从 left_handle 改成
right_handle，同步更新边 id（xy-edge__{source}{handle}-{target}left_handle）。
"""

import json
import sys

import pymysql


def main():
    check = '--check' in sys.argv
    conn = pymysql.connect(host='localhost', port=3306, user='root',
                           password='1234', database='bisheng',
                           charset='utf8mb4', autocommit=False)
    cur = conn.cursor()
    try:
        cur.execute('SELECT id, name, data FROM flow ORDER BY create_time')
        rows = cur.fetchall()
        total = 0
        for fid, name, data in rows:
            d = json.loads(data)
            changes = []
            for n in d['nodes']:
                if n['data'].get('type') != 'condition':
                    continue
                nid = n['id']
                params = {p['key']: p for grp in n['data']['group_params']
                          for p in grp.get('params', [])}
                cases = {c['id'] for c in params['condition']['value']}
                for e in d['edges']:
                    if e['source'] != nid:
                        continue
                    sh = e.get('sourceHandle')
                    if sh in cases or sh == 'right_handle':
                        continue
                    old_id = e['id']
                    e['sourceHandle'] = 'right_handle'
                    e['id'] = 'xy-edge__%sright_handle-%sleft_handle' % (nid, e['target'])
                    changes.append('%s 的兜底边 %s -> %s：sourceHandle %s -> right_handle'
                                   % (nid, e['target'], e['target'], sh))
                    changes[-1] = ('%s 的兜底边 --> %s：sourceHandle %s -> right_handle '
                                   '(边 id %s -> %s)' % (nid, e['target'], sh, old_id, e['id']))
            if changes:
                print('## %s' % name)
                for c in changes:
                    print('   *', c)
                total += len(changes)
                if not check:
                    cur.execute('UPDATE flow SET data=%s WHERE id=%s',
                                (json.dumps(d, ensure_ascii=False), fid))
        if not total:
            print('所有条件节点的兜底边都已连在 right_handle，无需改动。')
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
