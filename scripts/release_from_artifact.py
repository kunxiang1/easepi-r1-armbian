#!/usr/bin/env python3
"""把某次 Actions 运行里已经构建好的镜像产物，直接发布成 GitHub Release。

用途：workflow 漏了 Release 步骤（或改了 workflow 但不想为同一个固件再跑两小时）时，
把 run 里的 artifact 直接补发到 Releases 页面。

用法:
  python scripts/release_from_artifact.py --token-file D:/Easepi-R1/.gh_token
  python scripts/release_from_artifact.py --token-file ... --run-id 39729268091 --tag easepi-r1-trixie-server-current-20261022
  python scripts/release_from_artifact.py --token-file ... --dry-run

依赖：仅标准库。大文件走流式下载/上传，GB 级也不会吃内存。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
TIMEOUT = 600          # 单次 socket 超时（秒）
CHUNK = 4 * 1024 * 1024


def log(msg: str) -> None:
    print(msg, flush=True)


def _req(url: str, token: str, method: str = "GET", data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "easepi-release-helper")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return req


def api_json(path: str, token: str, method: str = "GET", payload=None, ok=(200, 201)):
    url = path if path.startswith("http") else API + path
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(_req(url, token, method, data, headers), timeout=TIMEOUT) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code in ok:
                return json.loads(detail or "{}")
            if e.code >= 500 and attempt < 3:
                log(f"  ! {e.code} {url} 重试 {attempt}/3…")
                time.sleep(3 * attempt)
                continue
            raise SystemExit(f"API {e.code} {url}\n{detail}")


def pick_run(repo: str, token: str, run_id: str | None) -> dict:
    if run_id:
        return api_json(f"/repos/{repo}/actions/runs/{run_id}", token)
    runs = api_json(f"/repos/{repo}/actions/runs?per_page=20", token)["workflow_runs"]
    for run in runs:
        if run["status"] == "completed" and run["conclusion"] == "success":
            arts = api_json(f"/repos/{repo}/actions/runs/{run['id']}/artifacts", token)["artifacts"]
            if any(a["name"].startswith("easepi-r1-") and not a["expired"] for a in arts):
                return run
    raise SystemExit("没有找到带镜像产物的成功运行，请用 --run-id 指定")


def download_artifact(repo: str, token: str, artifact: dict, dest: Path) -> Path:
    size_mb = artifact["size_in_bytes"] / 1048576
    zip_path = dest / (artifact["name"] + ".zip")
    url = f"{API}/repos/{repo}/actions/artifacts/{artifact['id']}/zip"
    log(f"  下载 artifact {artifact['name']} ({size_mb:.1f} MB) …")
    t0 = time.time()
    req = _req(url, token)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(zip_path, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        last = 0.0
        while True:
            buf = r.read(CHUNK)
            if not buf:
                break
            f.write(buf)
            got += len(buf)
            now = time.time()
            if now - last > 5:
                last = now
                pct = f"{got * 100 / total:.0f}%" if total else "?"
                log(f"    {got / 1048576:.0f} MB ({pct}) {got / 1048576 / max(now - t0, 1e-9):.1f} MB/s")
    log(f"  下载完成 {zip_path.name} {zip_path.stat().st_size / 1048576:.1f} MB，{time.time() - t0:.0f}s")
    return zip_path


def extract(zip_path: Path, dest: Path) -> list[Path]:
    out = dest / "files"
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        log(f"  解压 {len(names)} 个文件: {', '.join(Path(n).name for n in names)}")
        for n in names:
            z.extract(n, out)
    files = [p for p in out.rglob("*") if p.is_file()]
    return files


def ensure_release(repo: str, token: str, tag: str, name: str, body: str) -> dict:
    try:
        rel = api_json(f"/repos/{repo}/releases/tags/{tag}", token)
        log(f"  Release {tag} 已存在 (id={rel['id']})，复用并覆盖同名资产")
        return rel
    except SystemExit as e:
        if "API 404" not in str(e):
            raise
    rel = api_json(
        f"/repos/{repo}/releases", token, "POST",
        {"tag_name": tag, "name": name, "body": body, "draft": False, "prerelease": False},
    )
    log(f"  已创建 Release {tag} (id={rel['id']})")
    return rel


def upload_asset(repo: str, token: str, release: dict, path: Path) -> dict:
    name = path.name
    for a in release.get("assets", []):
        if a["name"] == name:
            log(f"  同名资产已存在，删除旧的 {name}")
            api_json(f"/repos/{repo}/releases/assets/{a['id']}", token, "DELETE", ok=(204,))
    size = path.stat().st_size
    log(f"  上传 {name} ({size / 1048576:.1f} MB) …")
    url = f"{UPLOADS}/repos/{repo}/releases/{release['id']}/assets?name={urllib.parse.quote(name)}"
    t0 = time.time()
    # 用 curl 流式上传：几百 MB 的镜像不会读进内存，断线还能 -T 重试
    curl = shutil.which("curl")
    if curl:
        cmd = [
            curl, "-sS", "--fail-with-body",
            "-X", "POST",
            "-H", f"Authorization: Bearer {token}",
            "-H", "Accept: application/vnd.github+json",
            "-H", "X-GitHub-Api-Version: 2022-11-28",
            "-H", "Content-Type: application/octet-stream",
            "-T", str(path),
            url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        if proc.returncode != 0:
            raise SystemExit(f"curl 上传失败 ({proc.returncode}): {proc.stderr[:500]}\n{proc.stdout[:500]}")
        asset = json.loads(proc.stdout)
    else:
        with open(path, "rb") as f:
            req = _req(url, token, "POST", data=f, headers={
                "Content-Type": "application/octet-stream", "Content-Length": str(size)})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                asset = json.loads(r.read())
    log(f"  上传完成 {time.time() - t0:.0f}s → {asset['browser_download_url']}")
    return asset


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="kunxiang1/easepi-r1-armbian")
    ap.add_argument("--token-file", default=os.environ.get("GH_TOKEN_FILE", "D:/Easepi-R1/.gh_token"))
    ap.add_argument("--run-id", default=None, help="默认取最近一次成功的、带镜像产物的运行")
    ap.add_argument("--tag", default=None, help="默认按 发行版/类型/分支/日期 生成")
    ap.add_argument("--name", default=None)
    ap.add_argument("--note", default="", help="追加到 Release 说明里的一行备注")
    ap.add_argument("--workdir", default=None, help="缓存目录（默认系统临时目录）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep", action="store_true", help="保留下载的 zip 和解压文件")
    args = ap.parse_args()

    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"token 文件为空: {args.token_file}")

    run = pick_run(args.repo, token, args.run_id)
    log(f"运行 #{run['run_number']} ({run['id']}) sha={run['head_sha'][:8]} 完成于 {run['updated_at']}")
    arts = api_json(f"/repos/{args.repo}/actions/runs/{run['id']}/artifacts", token)["artifacts"]
    arts = [a for a in arts if a["name"].startswith("easepi-r1-") and not a["expired"]]
    if not arts:
        raise SystemExit("该运行没有可用的镜像 artifact")
    art = max(arts, key=lambda a: a["size_in_bytes"])

    # easepi-r1-<branch>-<release>-<variant>-<dts>
    m = re.match(r"easepi-r1-(?P<branch>[^-]+)-(?P<release>[^-]+)-(?P<variant>[^-]+)-(?P<dts>.+)$", art["name"])
    branch, release, variant, dts = (m.group("branch"), m.group("release"), m.group("variant"), m.group("dts")) \
        if m else ("current", "trixie", "server", "mainline")
    # 构建时间用 UTC（与日志一致），tag 日期按北京时间
    stamp = time.strftime("%Y%m%d", time.gmtime(time.time() + 8 * 3600))
    tag = args.tag or f"easepi-r1-{release}-{variant}-{branch}-{stamp}"
    name = args.name or f"EasePi-R1 {release} {variant} ({branch}, {stamp})"

    tmp = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="easepi-rel-"))
    tmp.mkdir(parents=True, exist_ok=True)
    log(f"tag={tag}\n名称={name}\n临时目录={tmp}")
    if args.dry_run:
        log(f"--dry-run：将下载 artifact {art['name']} ({art['size_in_bytes'] / 1048576:.1f} MB) 并上传到上面的 tag，到此为止")
        return 0

    zip_path = download_artifact(args.repo, token, art, tmp)
    files = extract(zip_path, tmp)
    total = sum(f.stat().st_size for f in files)
    log(f"  待上传 {len(files)} 个文件，共 {total / 1048576:.1f} MB")

    body = (
        f"EasePi R1（RK3568）Armbian 镜像 — 由构建 #{run['run_number']} 产物补发\n\n"
        f"| 项目 | 值 |\n|---|---|\n"
        f"| 板型 | easepi-r1 (RK3568) |\n"
        f"| 发行版 | {release} |\n"
        f"| 镜像类型 | {variant} |\n"
        f"| 内核分支 | `{branch}` |\n"
        f"| dts | {dts} |\n"
        f"| commit | `{run['head_sha'][:8]}` |\n"
        f"| 构建运行 | [#{run['run_number']}]({run['html_url']}) |\n\n"
        "**烧写**：解压后用 BalenaEtcher / Rufus 写入 SD 卡或 eMMC，或\n"
        "`xz -dc *.img.xz | sudo dd of=/dev/sdX bs=4M status=progress conv=fsync`\n\n"
        "默认账号 `root` / `1234`（首次登录强制改密），校验见 `*.sha`。\n"
        + (f"\n{args.note}\n" if args.note else "")
    )
    rel = ensure_release(args.repo, token, tag, name, body)
    for f in sorted(files, key=lambda p: p.name):
        upload_asset(args.repo, token, rel, f)

    final = api_json(f"/repos/{args.repo}/releases/tags/{tag}", token)
    log("\n完成。Release: " + final["html_url"])
    for a in final["assets"]:
        log(f"  - {a['name']}  {a['size'] / 1048576:.1f} MB  {a['browser_download_url']}")

    if not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
