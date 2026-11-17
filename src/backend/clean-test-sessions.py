"""清理测试产生的会话与消息，只留演示数据。

用法:
    .venv/bin/python clean-test-sessions.py --check
    .venv/bin/python clean-test-sessions.py

删除范围
--------
1. chat_id 命中 TEST_PREFIXES 的会话。历次回归/dry-run/浏览器手工验证都用过
   不同的前缀（probe / dbg / rg- / dry- / cls / chk / raw- / qa- / a- / b- / c-），
   逐个枚举在这里，新增测试脚本时同步补一行。
2. flow_id 为空、create_time 在 CUTOFF 之后的日常会话（计时测试产生，chat_id 是
   uuid，无法用前缀识别），但 KEEP_CHAT_IDS 里的白名单会话不删。
3. 上述会话的 chatmessage 与 message_citation_relation。

保留范围
--------
- flow_id 非空的历史会话（f-decision 的 2 条早期演示会话）。
- KEEP_CHAT_IDS 白名单（日常问答 RAG 的演示会话）。
"""

import sys
from datetime import datetime

import pymysql

# 测试脚本产生的会话前缀，命中即删
TEST_PREFIXES = (
    'probe', 'dbg', 'rg-', 'dry-', 'cls', 'cls2-', 'chk', 'chk2', 'chk3',
    'raw-', 'qa-', 'a-', 'b-', 'c-', 'ds-',
)

# 日常问答 RAG 演示会话（不带 flow_id，靠前缀认不出来，显式白名单保留）
KEEP_CHAT_IDS = ('be06cfc6c53c4f17943a6684da32015f',)

# 早于该时间的日常会话视为演示数据/历史数据，不动
CUTOFF = datetime(2026, 11, 12, 11, 25, 0)


def main():
    check = '--check' in sys.argv
    conn = pymysql.connect(host='localhost', port=3306, user='root',
                           password='1234', database='bisheng',
                           charset='utf8mb4', autocommit=False)
    cur = conn.cursor()
    try:
        like_sql = ' OR '.join(['chat_id LIKE %s'] * len(TEST_PREFIXES))
        cur.execute(
            "SELECT chat_id FROM message_session "
            "WHERE (" + like_sql + ") OR (flow_id = '' AND create_time > %s)",
            tuple(p + '%' for p in TEST_PREFIXES) + (CUTOFF,))
        victims = [r[0] for r in cur.fetchall() if r[0] not in KEEP_CHAT_IDS]
        print('待删会话 %d 个:' % len(victims))
        for v in victims[:10]:
            print('   ', v)
        if len(victims) > 10:
            print('    ...（其余 %d 个）' % (len(victims) - 10))

        for t in ('chatmessage', 'message_citation_relation'):
            cur.execute('SHOW TABLES LIKE %s', (t,))
            if not cur.fetchone():
                continue
            n = 0
            for v in victims:
                if t == 'message_citation_relation':
                    # 该表没有 chat_id，通过 chatmessage 的 message id 反查
                    cur.execute('SELECT id FROM chatmessage WHERE chat_id = %s', (v,))
                    mids = [r[0] for r in cur.fetchall()]
                    for mid in mids:
                        cur.execute('DELETE FROM message_citation_relation WHERE message_id = %s', (mid,))
                        n += cur.rowcount
                else:
                    cur.execute('DELETE FROM chatmessage WHERE chat_id = %s', (v,))
                    n += cur.rowcount
            print('   %s: 删除 %d 行' % (t, n))

        cur.execute("SHOW TABLES LIKE 'message_citation'")
        if cur.fetchone():
            n = 0
            for v in victims:
                cur.execute('DELETE FROM message_citation WHERE chat_id = %s', (v,))
                n += cur.rowcount
            print('   message_citation: 删除 %d 行' % n)

        for v in victims:
            cur.execute('DELETE FROM message_session WHERE chat_id = %s', (v,))
        print('   message_session: 删除 %d 行' % len(victims))

        # 4. 会话删完后 message_citation_relation 会留下孤儿行（message_id 指向
        #    已删除的 chatmessage）。快照里带 350 行孤儿只会让导入后的库更脏，
        #    这里按 LEFT JOIN 反查删掉。
        cur.execute("SHOW TABLES LIKE 'message_citation_relation'")
        if cur.fetchone():
            cur.execute(
                'DELETE r FROM message_citation_relation r '
                'LEFT JOIN chatmessage m ON r.message_id = m.id WHERE m.id IS NULL')
            print('   message_citation_relation 孤儿: 删除 %d 行' % cur.rowcount)

        # 5. auditlog 是运行时动作流水，测试期会堆出近千行 create_chat。
        #    dashboard 读的是 ES 中间表（dashboard_dataset），不依赖它，
        #    演示快照里留空比留一堆测试记录干净。
        cur.execute('DELETE FROM auditlog')
        print('   auditlog: 删除 %d 行' % cur.rowcount)

        if not check:
            conn.commit()
            print('\n已提交。')
        else:
            conn.rollback()
            print('\n--check 模式，已回滚。')

        for t in ('message_session', 'chatmessage', 'message_citation_relation', 'auditlog'):
            cur.execute('SELECT COUNT(*) FROM %s' % t)
            print('%s 剩余 %d 行' % (t, cur.fetchone()[0]))
    except Exception as e:
        conn.rollback()
        print('失败:', e)
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
