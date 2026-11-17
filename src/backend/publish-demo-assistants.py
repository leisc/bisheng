"""把演示助手上线（status: 1 离线 -> 2 在线）。

背景：`setup-demo-data.py` 的 `phase_workflow` 会给工作流 PATCH status=2 上线，
但 `phase_assistant` 建完助手就结束了，从没调过 `POST /assistant/status`。
`AssistantStatus.OFFLINE=1 / ONLINE=2`（bisheng/database/models/assistant.py），
而应用广场的数据源 `/api/v1/chat/online` 只返回 status=2 的应用，
所以 3 个演示助手在广场里根本不显示。

注意：接口 docstring 写的「0 offline, 1 online」是错的，真实取值是 1/2，
跟着它传 status=1 会静默无操作（`assistant.status == status` 直接 return）。

用法：
    .venv/bin/python publish-demo-assistants.py            # 执行
    .venv/bin/python publish-demo-assistants.py --check    # 只查不改
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sd = _load("setup_demo_data", os.path.join(BASE, "setup-demo-data.py"))

ONLINE = 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只查询，不修改")
    args = ap.parse_args()

    cli = sd.Client()
    cli.login()

    apps = sd.list_of(cli.get("/assistant", params={"page": 1, "limit": 100}))
    print(f"助手 {len(apps)} 个")
    if not apps:
        print("  ! 一个助手都没有，先跑 setup-demo-data.py")
        return 1

    changed = 0
    for a in apps:
        name, aid, st = a.get("name"), a.get("id"), a.get("status")
        if st == ONLINE:
            print(f"  OK {name:<10} (id={aid}) 已在线")
            continue
        if args.check:
            print(f"  -- {name:<10} (id={aid}) status={st} 离线，需要上线")
            continue
        r = cli.post("/assistant/status", json={"id": aid, "status": ONLINE})
        if sd.ok(r):
            changed += 1
            print(f"  + {name:<10} (id={aid}) status={st} -> {ONLINE}")
        else:
            print(f"  ✗ {name:<10} (id={aid}) 上线失败: {r.get('status_message')} {str(r)[:200]}")

    if args.check:
        print("\n--check: 未做任何修改")
        return 0

    # 复核：广场数据源 /chat/online 应包含全部应用
    online = cli.get("/chat/online", params={"limit": 50, "action": "visible"})
    names = [x.get("name") for x in sd.list_of(online)]
    print(f"\n应用广场可见 {len(names)} 个: {names}")
    flows = [n for n in names if n in {f["name"] for f in sd.WORKFLOWS}]
    missing_flows = [f["name"] for f in sd.WORKFLOWS if f["name"] not in flows]
    if missing_flows:
        print(f"  ! 仍有流程未上线: {missing_flows}")
    if len(names) == len(apps) + len(sd.WORKFLOWS):
        print("  OK 全部应用均可见")
    return 0


if __name__ == "__main__":
    sys.exit(main())
