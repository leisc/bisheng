"""给平台端 /filelib 补文档知识库（type=0），让管理控制台能看到演示知识。

背景：`setup-demo-data.py` 的 `phase_knowledge` 走 `POST /knowledge/space`
建的是**知识空间**（`KnowledgeTypeEnum.SPACE=3`）。知识空间有独立端点，
客户端 `/workspace/knowledge` 和广场都能正常显示；但平台端「知识」菜单只有
`/filelib` 一个入口，它的数据源 `GET /api/v1/knowledge` **显式拒绝 type=3**：

    if knowledge_type is KnowledgeTypeEnum.SPACE:
        raise KnowledgeSpaceListNotSupportedError.http_exception()

注释写得很清楚：知识空间有自己的列表端点，这个 handler 只管文档库/QA库。
前端文档知识库 tab 默认发 `type=0`（`readFileLibDatabase` 的 `type = 0`），
所以 `/filelib` 对演示知识空间永远是一片空白。平台端也**没有**知识空间管理页
（`src/routes/index.tsx` 只有 `/filelib*`）。

本脚本为两个演示知识空间各建一个同名的**文档知识库**（type=0）并灌同样的文档，
让 IT 部门在管理控制台里也能看到知识库、解析状态、分段、权限。

上传链路（与前端 filesUpload 一致）：
  1. POST /knowledge/create            {name, description, model, type:0}
  2. POST /knowledge/upload/{kb_id}    multipart -> file_path
  3. POST /knowledge/process           {knowledge_id, file_list:[{file_path}], ...}

用法：
    .venv/bin/python create-demo-doc-libraries.py            # 执行
    .venv/bin/python create-demo-doc-libraries.py --check    # 只查不改
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sd = _load("setup_demo_data", os.path.join(BASE, "setup-demo-data.py"))

# 文档知识库用的 embedding 模型（qwen3.7-text-embedding-flash, llm_model.id=4）
EMBEDDING_MODEL_ID = "4"

# 与知识空间同名的文档库。files 与 setup-demo-data.py 的 KNOWLEDGE_SPACES 对齐。
DOC_LIBRARIES = [
    {"space_name": "INSOFT 公司制度库",
     "description": "INSOFT 公司制度文档库（平台端文档知识库视图）"},
    {"space_name": "INSOFT 产品知识库",
     "description": "INSOFT 产品知识文档库（平台端文档知识库视图）"},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只查询，不修改")
    ap.add_argument("--wait", type=int, default=90, help="等待解析完成的秒数")
    args = ap.parse_args()

    cli = sd.Client()
    cli.login()

    spaces = {s["name"]: s for s in sd.list_of(
        cli.get("/knowledge/space/mine", params={"page": 1, "limit": 100}))}
    docs = {s["name"]: s for s in sd.KNOWLEDGE_SPACES}
    if not spaces:
        print("! 没有知识空间，先跑 setup-demo-data.py")
        return 1

    # 平台端文档知识库列表（type=0）
    existing = {k["name"]: k for k in sd.list_of(
        cli.get("/knowledge", params={"page": 1, "page_size": 100, "type": 0, "action": "visible"}))}
    print(f"已有文档知识库(type=0) {len(existing)} 个: {sorted(existing)}")

    for spec in DOC_LIBRARIES:
        name = spec["space_name"]
        space = spaces.get(name)
        if not space:
            print(f"  ✗ {name} 知识空间不存在，跳过")
            continue
        files = (docs.get(name) or {}).get("files") or []
        print(f"\n── {name} (space id={space['id']}, {len(files)} 个文件)")

        if name in existing:
            kid = existing[name]["id"]
            print(f"  = {name} 已存在 (id={kid})")
        else:
            if args.check:
                print(f"  -- {name} 不存在，需要创建")
                continue
            r = cli.post("/knowledge/create", json={
                "name": name,
                "description": spec["description"],
                "model": EMBEDDING_MODEL_ID,
                "type": 0,
            })
            if not (sd.ok(r) and sd.data_of(r)):
                print(f"  ✗ 创建失败: {r.get('status_message')} {str(r)[:200]}")
                continue
            kid = sd.data_of(r).get("id")
            print(f"  + {name} (id={kid})")

        # 已有文件
        info = sd.data_of(cli.get(f"/knowledge/file_list/{kid}", params={"page": 1, "page_size": 200})) or {}
        rows = info.get("data") if isinstance(info, dict) else info
        rows = rows or []
        have = {f.get("file_name") for f in rows if isinstance(f, dict)}
        print(f"    已有文件 {len(have)} 个: {sorted(have)}")

        todo = [f for f in files if f not in have]
        if not todo:
            print("    = 文件已齐")
            continue
        if args.check:
            print(f"    -- 待上传 {len(todo)} 个: {todo}")
            continue

        # 上传前必须先 GET info 读响应头 x-management-tenant，再作为
        # X-Management-Tenant 请求头发回去（F114 管理范围中间件，与
        # setup-demo-data.py 里知识空间上传同一套解法）。
        _info = cli.c.get("/knowledge/info", params={"knowledge_id": kid})
        _mt = _info.headers.get("x-management-tenant")
        up_headers = {"X-Management-Tenant": str(_mt)} if _mt else {}

        # 上传 -> 拿 file_path
        paths = []
        for fname in todo:
            p = sd.DOCS / fname
            if not p.exists():
                print(f"    ✗ {fname} 本地不存在")
                continue
            up = cli.c.post(f"/knowledge/upload/{kid}",
                            files={"file": (fname, p.read_bytes(), "application/octet-stream")},
                            headers=up_headers).json()
            fp = sd.data_of(up) or {}
            if not (sd.ok(up) and fp.get("file_path")):
                print(f"    ✗ {fname} 上传失败: {up.get('status_message')} {str(up)[:160]}")
                continue
            paths.append(fp["file_path"])
            print(f"    + {fname} 上传")

        if not paths:
            continue

        # 触发解析/分段/向量化
        pr = cli.post("/knowledge/process", json={
            "knowledge_id": kid,
            "file_list": [{"file_path": p} for p in paths],
            "split_mode": "auto",
            "chunk_size": 1000,
            "chunk_overlap": 100,
        })
        print(f"    {'+' if sd.ok(pr) else '✗'} 触发解析: {'' if sd.ok(pr) else pr.get('status_message')}")

        # 等解析完成
        deadline = time.time() + args.wait
        while time.time() < deadline:
            time.sleep(5)
            info = sd.data_of(cli.get(f"/knowledge/file_list/{kid}",
                                      params={"page": 1, "page_size": 200})) or {}
            rows = info.get("data") if isinstance(info, dict) else info
            rows = rows or []
            st = [f.get("status") for f in rows if isinstance(f, dict)]
            done = sum(1 for s in st if s in (2, 3))
            if done >= len(paths):
                print(f"    解析完成 {done}/{len(paths)} (status={st})")
                break
        else:
            st2 = [f.get("status") for f in rows if isinstance(f, dict)]
            print(f"    ! {args.wait}s 内未全部完成 (status={st2})，可稍后到 /filelib 查看")

    # 复核：平台端 /filelib 的数据源
    online = sd.list_of(cli.get("/knowledge", params={
        "page": 1, "page_size": 100, "type": 0, "action": "visible"}))
    print(f"\n平台端文档知识库(type=0) 可见 {len(online)} 个: {[k.get('name') for k in online]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
