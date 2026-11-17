#!/usr/bin/env python3
"""修复「知识广场看不到知识库」+「统计看板用户数/应用数/知识库数不对」。

用法:
    .venv/bin/python fix-demo-dashboard.py --check
    .venv/bin/python fix-demo-dashboard.py

三处根因与对应修法
------------------

根因 1  知识广场看不到知识库
    `knowledge.is_released = 0`。知识广场走
    `KnowledgeService.async_get_public_spaces` / `get_knowledge_square`，
    查询条件是 `Knowledge.is_released == True`。setup-demo-data.py 建库时
    没有走发布流程，所以 `/knowledge/space/square` 返回 0 条，
    前端「知识广场」页面是空的。
    修法：`UPDATE knowledge SET is_released = 1 WHERE id IN (2, 3)`。

根因 2  看板的用户数 / 应用数 / 知识库数全是残值
    看板这 4 个总量指标（总用户数、总应用数、总QA知识库数、总文档知识库数）
    在 `dashboard_component.data_config` 里都是 `isVirtual: true`，
    由 `telemetry_search/domain/services/component.py::query_one_metric`
    转成 ES 查询落到**遥测中间表**：

        mid_user_increment.total_user_count      = CUMULATIVE_SUM(CARDINALITY(user_id))
        mid_app_increment.total_app_count        = CUMULATIVE_SUM(CARDINALITY(app_id))
        mid_knowledge_increment.total_*_count    = CUMULATIVE_SUM(CARDINALITY(knowledge_id))

    中间表由 Celery beat 每日同步（`worker/telemetry/mid_table.py` 的
    `sync_mid_user_increment` / `sync_mid_app_increment` /
    `sync_mid_knowledge_increment`，crontab `30 0 * * *`），不是实时库存。

    关键坑：`get_yesterday_date_range()` 里
        lastest_time = mid_table.get_latest_record_time_sync()
        if lastest_time: start_date = lastest_time + 1s
    即**只要索引里已有记录，显式传入的 start_date 就会被顶到
    「最后一条记录 + 1 秒」**。而演示数据创建于 2026-11-12 09:53~10:01，
    晚于当天 09:29 那次同步（worker.log 里三次 sync 的范围都截止到
    2026-11-12 00:00:00），所以这些用户/应用/知识库永远不会进中间表，
    看板显示的是昨天的残值（用户 2、应用 1、知识库 0）。

    修法：先删掉 3 个中间表索引（`get_latest_record_time_sync` 随之返回 None，
    显式日期范围才生效），再用覆盖演示数据创建时间的显式范围调用同步任务。
    删索引是安全的：`BaseMidTable.__init__` 会调 `ensure_index_exists_sync()`
    按 `_mappings | common_properties` 重建索引（含 single_char_analyzer），
    所以 `knowledge_type` 仍是 keyword，`term/terms` 过滤不会失效。
    `insert_records_sync` 用 `rec.es_id` 作 `_id`，`helpers.bulk` 是 upsert，
    所以脚本可重复执行。

    注意：不能直接 `fn(start_date=..., end_date=...)`。`@bisheng_celery.task()`
    在 app 未 finalized 时返回 `celery.local.PromiseProxy`，调用它只构造
    signature、不同步执行（返回 None，且 celery 会在 DEBUG 里打印一行
    `def <task>(*args, **kwargs): return 1` 的类型存根，那不是真 stub）。
    走 `Task.run` 才是 Celery worker 内部 `__call__` 的同一条路径，且 `run`
    就是 `_cross_tenant` 包装后的函数，租户过滤绕过仍然生效。

根因 3  即使同步成功，知识空间也计入不到「文档知识库」
    `dashboard_dataset.schema_config` 里 `mid_knowledge_increment` 的
    `total_document_knowledge_base_count` /
    `new_document_knowledge_base_count` 带
    `term knowledge_type = 0`（KnowledgeTypeEnum.NORMAL 文档知识库），
    而演示库是 `type = 3`（KnowledgeTypeEnum.SPACE 知识空间）——
    这个过滤条件早于 SPACE 类型存在，会把知识空间整体漏掉。
    修法：把该过滤从 `term 0` 改成 `terms [0, 3]`
    （`TermsOp` 已存在于 query_builder.py，前端过滤器也支持）。
    QA 口径（`term knowledge_type = 1`）不动，演示环境没有 QA 库。

根因 4  看板时间过滤器把「今天」创建的记录全部筛掉（1 天滞后）
    看板的「选择日期」查询组件默认值是 `最近180天`，前端据此发送
    `time_filters=[{mode: dynamic, recentDays: 180}]`。后端
    `TimeFilter.get_start_end_date()`（telemetry_search/domain/schemas/
    component.py:89）返回的是**毫秒**：

        end_date = int(datetime(昨天 23:59:59).timestamp() * 1000)

    而中间表索引的 timestamp 映射是
        date  strict_date_optional_time||epoch_second
    存的是**秒**；ES 对 date 字段的范围查询一律按 epoch_millis 解析数字
    （与字段声明的 format 无关），于是 lte 被解析成 2026-11-12 00:00 UTC。
    再叠加 `DataQueryService.query_sum_metric` 会把 timestamp 的 `gte`
    抹掉（总量指标不设下界，只留 lte），最终效果是：

        **总量只统计到「昨天 23:59:59」，今天创建的实体一概不计入。**

    这是看板固有的 1 天滞后（正常部署里 beat 每天 00:30 同步，当天数据
    本来就要等第二天才上看板），但演示数据是当天灌的，直接导致
    总用户数=2 / 总应用数=1 / 总文档知识库数=0。
    修法见 step_shift_demo_create_time：不动 get_yesterday_date_range /
    query_sum_metric 这些核心代码（改它们影响所有租户、所有看板），
    也不把看板过滤器改成固定区间（×1000 后的毫秒值会落到公元 58000 年，
    `in_time_range` 会把面积图整张滤空），而是把演示实体的 create_time
    回拨一天，落进看板既有的统计窗口。

根因 5  看板里残留测试占位副标题
    `preset_oss`(dashboard_id=10) 的 总应用数/总用户数/点赞次数
    style_config.subtitle 还是 `1111`；`preset_commercial`(11) 的
    Token消耗量 是 `26年1月`。另有两个组件 subtitle='' 但
    showSubtitle=True，会在标题下渲染一条空白副标题行。
    修法：占位符统一改成「全公司截至目前」，空白副标题直接关掉 showSubtitle。
"""

import json
import sys
from datetime import datetime

import pymysql
import requests

BASE = 'http://localhost:7860/api/v1'

# 演示数据的创建时间落在 2026-11-11 ~ 2026-11-12，取一个覆盖全段的窗口
SYNC_START = '2026-11-11T00:00:00'
SYNC_END = '2026-11-13T00:00:00'

# 演示知识库 id（setup-demo-data.py 建立的两条 SPACE 知识空间）
DEMO_KNOWLEDGE_IDS = (2, 3)

ES = 'http://localhost:9200'
ES_AUTH = ('elastic', 'bisheng')

# 遥测中间表索引 —— 清空后同步任务的显式日期范围才会生效
MID_INDICES = ('mid_user_increment', 'mid_app_increment', 'mid_knowledge_increment')

MYSQL = dict(host='localhost', port=3306, user='root', password='1234',
             database='bisheng', charset='utf8mb4')


def step_release_knowledge(cur, check):
    """根因 1：is_released=0 让知识广场过滤掉两条演示知识库。"""
    cur.execute('SELECT id, name, is_released FROM knowledge WHERE id IN %s',
                (DEMO_KNOWLEDGE_IDS,))
    rows = cur.fetchall()
    if not rows:
        print('    ! 未找到演示知识库 %s' % (DEMO_KNOWLEDGE_IDS,))
        return False
    for kid, name, released in rows:
        print('    id=%s %-16s is_released=%s' % (kid, name, released))
    todo = [r[0] for r in rows if not r[2]]
    if not todo:
        print('    OK 已全部发布，无需修改')
        return True
    if check:
        print('    --check: 跳过 UPDATE')
        return True
    cur.execute('UPDATE knowledge SET is_released = 1 WHERE id IN %s', (tuple(todo),))
    print('    -> 已置 is_released=1: %s (%d 行)' % (todo, cur.rowcount))
    return True


def step_fix_knowledge_filter(cur, check):
    """根因 3：dashboard_dataset 里 knowledge_type term 0 漏掉 SPACE(3)。"""
    cur.execute(
        "SELECT id, dataset_code, schema_config FROM dashboard_dataset "
        "WHERE dataset_code = 'mid_knowledge_increment'")
    rows = cur.fetchall()
    if not rows:
        print('    ! 未找到 mid_knowledge_increment 数据集定义')
        return False
    changed = 0
    for row in rows:
        ds_id, code, raw = row[0], row[1], row[2]
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()
        cfg = json.loads(raw)
        for metric in cfg.get('metrics', []):
            flt = metric.get('filter')
            if not flt:
                continue
            ops = flt.get('filters') or []
            for op in ops:
                if op.get('field') != 'knowledge_type' or op.get('operator') != 'term':
                    continue
                if op.get('value') != 0:
                    continue
                field_name = metric.get('field')
                if check:
                    print('    --check: %s.%s 会把 term 0 -> terms [0,3]'
                          % (code, field_name))
                    changed += 1
                    continue
                op['operator'] = 'terms'
                op['value'] = [0, 3]
                cur.execute('UPDATE dashboard_dataset SET schema_config = %s WHERE id = %s',
                            (json.dumps(cfg, ensure_ascii=False), ds_id))
                print('    -> %s.%s: term 0 -> terms [0,3]'
                      % (code, field_name))
                changed += 1
    if not changed:
        print('    OK 已是 terms [0,3]，无需修改')
    return True


def step_clean_placeholder_subtitles(cur, check):
    """清理看板组件里残留的测试占位副标题。

    `preset_oss`(id=10) 的 总应用数/总用户数/点赞次数 三个指标卡
    style_config.subtitle 还是 `1111`（早期手工调试的残留，showSubtitle=False
    所以没渲染出来）；总QA知识库数/总文档知识库数是 subtitle='' +
    showSubtitle=True，会在标题下顶出一条空白副标题行。
    `preset_commercial`(id=11) 的 Token消耗量 subtitle 是 `26年1月`。
    商业版预设当前在开源版里被过滤掉，但客户若是商业部署会直接看到，一并清掉。
    """
    cur.execute('SELECT id, dashboard_id, title, style_config FROM dashboard_component')
    rows = cur.fetchall()
    changed = 0
    for row in rows:
        cid, did, title, raw = row[0], row[1], row[2], row[3]
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()
        try:
            style = json.loads(raw)
        except Exception:
            continue
        subtitle = style.get('subtitle')
        if not subtitle:
            # 空副标题 + showSubtitle=True 会渲染出一条空白行，直接关掉
            if style.get('showSubtitle'):
                if check:
                    print('    --check: dash=%s %s 关闭空白副标题' % (did, title))
                    changed += 1
                    continue
                style['showSubtitle'] = False
                cur.execute('UPDATE dashboard_component SET style_config = %s WHERE id = %s',
                            (json.dumps(style, ensure_ascii=False), cid))
                print('    -> dash=%s %s 关闭空白副标题' % (did, title))
                changed += 1
            continue
        if subtitle in ('1111', '26年1月'):
            if check:
                print("    --check: dash=%s %s 副标题 %r -> '全公司截至目前'"
                      % (did, title, subtitle))
                changed += 1
                continue
            style['subtitle'] = '全公司截至目前'
            style['showSubtitle'] = True
            cur.execute('UPDATE dashboard_component SET style_config = %s WHERE id = %s',
                        (json.dumps(style, ensure_ascii=False), cid))
            print("    -> dash=%s %s 副标题 %r -> '全公司截至目前'" % (did, title, subtitle))
            changed += 1
    if not changed:
        print('    OK 无占位副标题')
    return True


DEMO_CREATE_DATE = datetime(2026, 11, 12)

# 演示实体表：这些表里 create_time >= 2026-11-12 的行都是本次演示灌进去的
# （user 表 11-11 的 admin/admin2 是基础账号，不在范围内）
DEMO_ENTITY_TABLES = [
    ('user', None),
    ('flow', 'flow_type = 10'),
    ('assistant', None),
    ('knowledge', None),
]


def step_shift_demo_create_time(cur, check):
    """把今天创建的演示数据 create_time 回拨一天。

    根因 5  看板时间过滤器把「今天」创建的记录全部筛掉
        看板的「选择日期」查询组件默认值是 `最近180天`，前端据此发送
        `time_filters=[{mode: dynamic, recentDays: 180}]`。后端
        `TimeFilter.get_start_end_date()` 返回的是**毫秒**：
            end_date = (昨天 23:59:59).timestamp() * 1000
        而中间表索引的 timestamp 字段映射是
            date  strict_date_optional_time||epoch_second
        存的是**秒**；ES 对 date 字段的范围查询一律按 epoch_millis 解析
        数字（与字段 format 无关），于是 lte 被解析成 2026-11-12 00:00 UTC。
        叠加 `query_sum_metric` 又会把 timestamp 的 `gte` 抹掉
        （总量指标不设下界，只留 lte），最终效果就是：
            **只统计到昨天 23:59:59，今天创建的实体一概不计入总量。**

        这是看板固有的 1 天滞后（正常部署里 beat 每天 00:30 同步，
        当天数据本来就要等第二天才上看板），但演示数据是当天灌的，
        直接导致 总用户数=2 / 总应用数=1 / 总文档知识库数=0。

    修法
        不动 `get_yesterday_date_range` / `query_sum_metric` 这些核心代码
        （改它们影响所有租户、所有看板），也不改看板的过滤器配置
        （改成固定区间会让 ×1000 后的毫秒值落到公元 58000 年，
        `in_time_range` 会把面积图整张滤空），而是把演示实体的
        create_time 回拨一天，让它们落进看板既有的统计窗口。
        灌数时间改成「昨天晚上」对演示环境也完全自然。
        索引重建后 ES 里的 timestamp 会跟着变成 11-11，
        新增用户数 / 新增应用数 / 总知识库数 三张图也一起有数据。

    可重复
        WHERE 条件带 `create_time >= 2026-11-12`，回拨后这些行落在 11-11，
        第二次执行匹配不到任何行。
    """
    cutoff = DEMO_CREATE_DATE.strftime('%Y-%m-%d %H:%M:%S')
    changed = 0
    for table, extra in DEMO_ENTITY_TABLES:
        where = "create_time >= '%s'" % cutoff
        if extra:
            where += ' AND ' + extra
        cur.execute('SELECT COUNT(*) FROM `%s` WHERE %s' % (table, where))
        n = cur.fetchone()[0]
        if not n:
            print('    %-10s OK 无今天创建的行' % table)
            continue
        if check:
            print('    %-10s --check: %d 行 create_time -1 天' % (table, n))
            changed += n
            continue
        cur.execute('UPDATE `%s` SET create_time = DATE_SUB(create_time, INTERVAL 1 DAY) WHERE %s'
                    % (table, where))
        print('    %-10s %d 行 create_time -1 天' % (table, cur.rowcount))
        changed += cur.rowcount
    if not changed:
        print('    OK 无需回拨')
    return True


def step_resync_mid_tables(check):
    """根因 2：清空遥测中间表后按显式日期范围重新同步。"""
    if check:
        print('    --check: 跳过索引清空与同步')
        return True

    for index in MID_INDICES:
        r = requests.delete('%s/%s' % (ES, index), auth=ES_AUTH, timeout=30)
        # 404 = 索引本来就不存在（mid_knowledge_increment 就是空的）
        if r.status_code in (200, 404):
            print('    -> 已清空索引 %s' % index)
        else:
            print('    ! 清空 %s 失败: %s %s' % (index, r.status_code, r.text[:200]))
            return False

    # 索引已空，get_yesterday_date_range 的 latest_time 为 None，
    # 显式 start_date/end_date 才会被采用。
    #
    # bisheng.worker 导入链会读 config.yaml，需要宿主机运行的环境变量
    # （见 .env.host），否则 config_service 抛
    # "Environment variable BISHENG_GATEWAY_HMAC_SECRET not found"。
    _load_host_env()

    from bisheng.worker.telemetry.mid_table import (
        sync_mid_app_increment,
        sync_mid_knowledge_increment,
        sync_mid_user_increment,
    )

    # 注意：不能直接 fn(start_date=..., end_date=...)。@bisheng_celery.task()
    # 在 app 未 finalized 时返回 celery.local.PromiseProxy，调用它只会构造
    # signature、不会同步执行（返回 None）。走 Task.run 才是 Celery worker
    # 内部 __call__ 的同一条路径，且 run 就是 _cross_tenant 包装后的函数，
    # 租户过滤绕过仍然生效。
    for name, fn in (('mid_user_increment', sync_mid_user_increment),
                     ('mid_app_increment', sync_mid_app_increment),
                     ('mid_knowledge_increment', sync_mid_knowledge_increment)):
        fn.run(start_date=SYNC_START, end_date=SYNC_END)
        # bulk 写入后 ES 默认 1s 才刷新，直接 _count 会读到 0，
        # 先显式 refresh 再统计
        requests.post('%s/%s/_refresh' % (ES, name), auth=ES_AUTH, timeout=30)
        r = requests.get('%s/%s/_count' % (ES, name), auth=ES_AUTH, timeout=30)
        print('    -> %s 同步完成，当前文档数 %s'
              % (name, r.json().get('count') if r.status_code == 200 else '?'))
    return True


def _load_host_env():
    """把 .env.host 里的变量注入当前进程环境。"""
    import os
    import subprocess
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env.host')
    if not os.path.exists(env_file):
        return
    out = subprocess.run(
        ['bash', '-c',
         'set -a; . "%s"; set +a; '
         'python3 -c "import os,json;print(json.dumps(dict(os.environ)))"' % env_file],
        capture_output=True, text=True)
    if out.returncode == 0 and out.stdout.strip():
        os.environ.update(json.loads(out.stdout.strip().splitlines()[-1]))


def main():
    check = '--check' in sys.argv
    conn = pymysql.connect(autocommit=False, **MYSQL)
    cur = conn.cursor()
    ok = True
    try:
        print('\n[1] 知识库 is_released')
        if not step_release_knowledge(cur, check):
            ok = False
        print('\n[2] dashboard_dataset 知识库类型过滤')
        if not step_fix_knowledge_filter(cur, check):
            ok = False
        print('\n[3] 演示数据 create_time 回拨一天')
        if not step_shift_demo_create_time(cur, check):
            ok = False
        print('\n[4] 看板占位副标题清理')
        if not step_clean_placeholder_subtitles(cur, check):
            ok = False
        if not ok:
            conn.rollback()
            print('\n前置步骤失败，已回滚，未执行同步。')
            return 1
        conn.commit()
        print('\n    MySQL 变更已提交')
    finally:
        conn.close()

    print('\n[5] 遥测中间表重新同步')
    if not step_resync_mid_tables(check):
        return 1

    print('\n完成。请刷新平台端 /dashboard 页面核对数字。')
    print('预期：总用户数 8（2 管理员 + 6 演示用户）、'
          '总应用数 7（4 工作流 + 3 助手）、'
          '总文档知识库数 4（2 条 SPACE 知识空间 + 2 个平台端文档库）、'
          '总QA知识库数 0。')
    print('注意：get_knowledge_by_time_range() 不按 knowledge_type 过滤，'
          '所以平台端 /filelib 的文档库（type=0）也会被同步进'
          'mid_knowledge_increment 并被看板统计到。若只想数知识空间，'
          '把 dashboard 的 knowledge_type 过滤条件从 terms [0,3] '
          '改回 term 3。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
