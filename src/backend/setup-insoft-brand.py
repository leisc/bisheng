"""把系统品牌切换为 INSOFT: 上传 logo 资产 + 设置 brandName。

用法:
    .venv/bin/python setup-insoft-brand.py            # 上传 + 保存
    .venv/bin/python setup-insoft-brand.py --dry-run  # 只打印计划

背景:
    前端有两层 logo 来源 ——
      1) 静态兜底: platform/client public/assets/bisheng/*.png(首帧, 未请求接口前)
      2) 运行时配置: GET /api/v1/brand/runtime-config -> window.BRAND_CONFIG
    本脚本只负责第 2 层; 第 1 层由 make-insoft-logo.py 生成的文件替换完成。

    MinIO 返回的是 clear_host 后的相对路径(/bisheng/...), 前端的
    withBrandBaseUrl() 会补 BASE_URL, 再走 vite 的 /bisheng 代理打到 MinIO,
    所以这里不需要也不能写绝对地址。
"""

import argparse
import json
import os
import sys

import httpx

API = "http://localhost:7860/api/v1"
ACCOUNT = "admin-001"
PASSWORD = b"Admin@2026"
ASSET_DIR = "/tmp/insoft-assets"

# category -> (文件名, 说明)
ASSETS = {
    "favicon": ("favicon.ico", "浏览器标签页图标"),
    "loginHeroLight": ("login-logo-big.png", "平台登录页浅色主视觉"),
    "loginHeroDark": ("login-logo-dark.png", "平台登录页深色主视觉"),
    "headerLogoLight": ("login-logo-small.png", "浅色模式 header logo"),
    "headerLogoDark": ("logo-small-dark.png", "深色模式 header logo"),
}


def login(client: httpx.Client) -> str:
    import base64

    import rsa

    pk = client.get("/user/public_key").json()["data"]["public_key"]
    pub = rsa.PublicKey.load_pkcs1(pk.encode())
    enc = base64.b64encode(rsa.encrypt(PASSWORD, pub)).decode()
    r = client.post("/user/login", json={"user_name": ACCOUNT, "password": enc}).json()
    if r.get("status_code") != 200:
        sys.exit(f"登录失败: {r.get('status_message')}")
    return r["data"]["access_token"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for _, (fname, _) in ASSETS.items():
        p = os.path.join(ASSET_DIR, fname)
        if not os.path.exists(p):
            sys.exit(f"缺少资产 {p}; 先跑 make-insoft-logo.py")

    client = httpx.Client(base_url=API, timeout=180)
    token = login(client)
    h = {"Authorization": f"bearer {token}"}
    print(f"✅ 已登录 {ACCOUNT}")

    if args.dry_run:
        for cat, (fname, desc) in ASSETS.items():
            print(f"  [dry-run] {cat:16} <- {fname:22} ({desc})")
        print("  [dry-run] brandName = {zh: INSOFT, en: INSOFT}")
        return 0

    # 1. 上传资产
    uploaded = {}
    for cat, (fname, desc) in ASSETS.items():
        with open(os.path.join(ASSET_DIR, fname), "rb") as fh:
            r = client.post(
                "/brand/assets",
                headers=h,
                files={"file": (fname, fh, "application/octet-stream")},
                data={"category": cat},
            ).json()
        if r.get("status_code") != 200:
            sys.exit(f"上传 {cat} 失败: {json.dumps(r, ensure_ascii=False)[:400]}")
        d = r["data"]
        uploaded[cat] = {"url": d["url"], "relative_path": d["relative_path"], "file_name": d["file_name"]}
        print(f"  ⬆️  {cat:16} {d['file_name']:22} -> {d['relative_path']}")

    # 2. 读当前配置, 只改要改的(save_config 是整体覆盖, 不能整份丢)
    cur = client.get("/brand/config", headers=h).json()["data"]
    payload = {
        "brandName": {"zh": "INSOFT", "en": "INSOFT"},
        "assets": uploaded,
        "loading": cur.get("loading") or {},
        "URLLoadingIcon": cur.get("URLLoadingIcon") or "",
        "workbenchTheme": cur.get("workbenchTheme") or "blue",
    }
    r = client.put("/brand/config", headers=h, json=payload).json()
    if r.get("status_code") != 200:
        sys.exit(f"保存品牌配置失败: {json.dumps(r, ensure_ascii=False)[:500]}")
    saved = r["data"]
    print(f"✅ brandName = {saved['brandName']}")
    for k, v in saved["assets"].items():
        print(f"     {k:16} {v['url']}")

    # 3. 校验运行时配置(前端真正读的接口)
    rt = client.get("/brand/runtime-config").json()["data"]
    assert rt["brandName"]["zh"] == "INSOFT", rt["brandName"]
    print("✅ /brand/runtime-config 已生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
