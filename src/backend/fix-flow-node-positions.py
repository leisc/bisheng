#!/usr/bin/env python3
"""补齐演示工作流缺失的 position / viewport / type —— 修复画布打不开的问题。

背景 / 根因
----------
`setup-demo-data.py` 造演示流程时，节点只写了 ``{"id": nid, "data": {...}}``，
图也只有 ``{"nodes": [...], "edges": [...]}``。当时注释写的是「结构与库里
flow.data 的真实形态对齐」，但参照对象选错了——看的是工作流 WS 通道
``init_data`` 的归一化形态（后端只回 id+data），而不是 ``flow.data`` 列本身。

真实形态（对照 f-decision 827b0e3b8e824c82bb0b74162ee18ea2）：

    viewport: {"x": ..., "y": ..., "zoom": ...}
    nodes: [{"id": ..., "type": "flowNode", "dragging": false,
             "measured": {"width": 333, "height": 845},
             "position": {"x": ..., "y": ...}, "selected": false,
             "data": {...}}]

缺这三样的后果：

1. ``position`` 缺失 → xyflow 的 ``adoptUserNodes`` → ``getNodePositionWithOrigin``
   读 ``node.position.x`` 直接抛
   ``TypeError: Cannot read properties of undefined (reading 'x')``，
   React 在 commitPassiveEffects 阶段崩掉，整个画布白屏。
   报栈：@xyflow_react.js adoptUserNodes → setNodes → commitHookEffectListMount。
2. ``type`` 缺失 → ``Panne.tsx:23`` 的 ``nodeTypes = {flowNode: FlowNode}``
   匹配不上，节点渲染不出自定义组件（只剩一个默认框）。
3. ``viewport`` 缺失 → 初始视野无定义（Panne.tsx 会 fitView 兜一下，但
   ``reactFlow.setViewport`` 之类路径仍需要它存在）。

注意：**这只影响画布 UI，不影响工作流执行**。后端跑流程不读 position，
所以此前 WS 回归测试 10/10 全过——问题只在平台端打开流程图时才暴露。

修法
----
按拓扑深度从左到右自动布点（BFS 求深度 → 每列取该列最大宽度 + GAP 累加成
x；同列节点以 y=0 为中心、VGAP 间距纵向堆叠），并补上 type/dragging/measured/
selected/viewport。已布过点的流程图原样跳过（幂等）。

用法::

    .venv/bin/python fix-flow-node-positions.py --check   # 只看不改
    .venv/bin/python fix-flow-node-positions.py            # 写入
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from collections import deque

import pymysql

MYSQL = dict(host='localhost', port=3306, user='root', password='1234',
             database='bisheng', charset='utf8mb4')

# 列间距 / 同列节点纵向间距
GAP_X = 300
GAP_Y = 140

# 节点 measured 尺寸，取自 f-decision 的真实值
NODE_SIZE = {
    'start': (333, 845),
    'input': (333, 629),
    'llm': (333, 838),
    'condition': (561, 462),
    'output': (333, 452),
    'end': (333, 133),
}
DEFAULT_SIZE = (333, 400)


def _node_kind(node: dict) -> str:
    """取逻辑类型。优先 data.type，退回 data.node.type，再退回 id 前缀。"""
    data = node.get('data') or {}
    t = data.get('type')
    if not t:
        t = (data.get('node') or {}).get('type')
    if not t:
        t = str(node.get('id', '')).split('_')[0]
    return t


def _size(kind: str) -> tuple[int, int]:
    return NODE_SIZE.get(kind, DEFAULT_SIZE)


def _depths(nodes: list[dict], edges: list[dict]) -> dict[str, int]:
    """BFS 求每个节点的拓扑深度（从入度为 0 的节点出发）。"""
    ids = [n['id'] for n in nodes]
    indeg = {i: 0 for i in ids}
    adj: dict[str, list[str]] = {i: [] for i in ids}
    for e in edges:
        s, t = e.get('source'), e.get('target')
        if s in adj and t in indeg:
            adj[s].append(t)
            indeg[t] += 1

    depth = {i: 0 for i in ids}
    q = deque([i for i in ids if indeg[i] == 0] or ids[:1])
    seen = set(q)
    while q:
        cur = q.popleft()
        for nxt in adj[cur]:
            depth[nxt] = max(depth[nxt], depth[cur] + 1)
            if nxt not in seen:
                seen.add(nxt)
                q.append(nxt)
    # 环 / 不可达节点兜底
    for i in ids:
        if i not in seen:
            depth[i] = 0
    return depth


def _layout(nodes: list[dict], edges: list[dict]) -> dict[str, dict]:
    """按深度分列、同列纵向居中堆叠，返回 {node_id: {x, y}}。"""
    depth = _depths(nodes, edges)
    cols: dict[int, list[dict]] = {}
    for n in nodes:
        cols.setdefault(depth[n['id']], []).append(n)

    pos: dict[str, dict] = {}
    x = 0.0
    for d in sorted(cols):
        members = cols[d]
        w = max(_size(_node_kind(n))[0] for n in members)
        # 同列总高（含间距）
        heights = [_size(_node_kind(n))[1] for n in members]
        total_h = sum(heights) + GAP_Y * (len(members) - 1)
        y = -total_h / 2
        for n, h in zip(members, heights):
            pos[n['id']] = {'x': round(x, 2), 'y': round(y, 2)}
            y += h + GAP_Y
        x += w + GAP_X
    return pos


def _needs_fix(data: dict) -> bool:
    nodes = data.get('nodes') or []
    if not nodes:
        return False
    if not data.get('viewport'):
        return True
    return any(not n.get('position') or n.get('type') != 'flowNode' for n in nodes)


def fix_one(cur, fid: str, name: str, check: bool) -> str:
    cur.execute('SELECT data FROM flow WHERE id=%s', (fid,))
    row = cur.fetchone()
    if not row or not row[0]:
        return '跳过（无 data）'
    data = json.loads(row[0])
    if not _needs_fix(data):
        return 'OK 已布点，无需修改'

    nodes = data['nodes']
    pos = _layout(nodes, data.get('edges') or [])
    for n in nodes:
        kind = _node_kind(n)
        w, h = _size(kind)
        n['type'] = 'flowNode'
        n['position'] = pos[n['id']]
        n['dragging'] = False
        n['selected'] = False
        n['measured'] = {'width': w, 'height': h}
    data['viewport'] = {'x': 60.0, 'y': 120.0, 'zoom': 0.5}

    if check:
        return '待修复：%d 个节点缺 position/type，缺 viewport' % len(nodes)
    cur.execute('UPDATE flow SET data=%s WHERE id=%s',
                (json.dumps(data, ensure_ascii=False), fid))
    return '已布点 %d 个节点 + viewport' % len(nodes)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', help='只看不改')
    args = ap.parse_args()
    check = args.check

    conn = pymysql.connect(**MYSQL)
    cur = conn.cursor()
    try:
        cur.execute('SELECT id, name, data FROM flow WHERE flow_type=10 ORDER BY create_time')
        rows = cur.fetchall()
        print('演示工作流 %d 张' % len(rows))
        bad = 0
        for fid, name, data in rows:
            try:
                d = json.loads(data) if data else {}
            except Exception as e:
                print('  %-22s JSON 解析失败: %s' % (name, e))
                bad += 1
                continue
            if not _needs_fix(d):
                print('  %-22s OK 已布点，无需修改' % name)
                continue
            bad += 1
            msg = fix_one(cur, fid, name, check)
            print('  %-22s %s' % (name, msg))
        if not check:
            conn.commit()
            print('\n已提交。')
        return 1 if bad and check else 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
