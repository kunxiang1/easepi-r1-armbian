#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把本仓库（板级支持 + GitHub Actions 工作流）发布到 GitHub，并触发一次 CI 构建。

用法:
    python scripts/publish.py --token-file D:\\Easepi-R1\\.gh_token
    python scripts/publish.py --token-file ... --repo easepi-r1-armbian --dispatch
    python scripts/publish.py --token-file ... --status

只用标准库；token 不打印。
"""
import argparse
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {".git", "output", "__pycache__"}


def req(token, method, path, payload=None):
    url = API + path
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", "Bearer " + token)
    r.add_header("Accept", "application/vnd.github+json")
    r.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        r.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(r, timeout=60, context=ctx) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"message": body[:400]}


def collect_files():
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT).replace("\\", "/")
            with open(full, "rb") as f:
                out.append((rel, f.read()))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token-file", default=os.path.join(os.path.dirname(ROOT), ".gh_token"))
    ap.add_argument("--repo", default="easepi-r1-armbian")
    ap.add_argument("--private", action="store_true")
    ap.add_argument("--fork-upstream", action="store_true", help="同时 fork armbian/build")
    ap.add_argument("--dispatch", action="store_true", help="发布后触发 CI")
    ap.add_argument("--status", action="store_true", help="只查看最近一次运行")
    ap.add_argument("--branch", default="current")
    ap.add_argument("--release", default="trixie")
    ap.add_argument("--variant", default="server")
    ap.add_argument("--dts", default="mainline")
    args = ap.parse_args()

    if not os.path.isfile(args.token_file):
        sys.exit("找不到 token 文件: %s" % args.token_file)
    token = open(args.token_file, encoding="utf-8").read().strip()
    if not token:
        sys.exit("token 文件是空的")

    st, me = req(token, "GET", "/user")
    if st != 200:
        sys.exit("token 无效（%s: %s）" % (st, me.get("message")))
    owner = me["login"]
    print("登录身份: %s" % owner)

    scopes = ""
    # 探测 token 权限
    r = urllib.request.Request(API + "/user")
    r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            scopes = resp.headers.get("x-oauth-scopes", "")
    except Exception:
        pass

    repo_full = "%s/%s" % (owner, args.repo)

    if args.status:
        show_status(token, repo_full)
        return

    # 1) 建仓库
    st, info = req(token, "GET", "/repos/" + repo_full)
    if st == 404:
        st, info = req(token, "POST", "/user/repos", {
            "name": args.repo,
            "description": "Armbian build support + CI for LinkEase EasePi R1 (RK3568)",
            "private": bool(args.private),
            "has_issues": True,
            "has_wiki": False,
        })
        if st not in (200, 201):
            sys.exit("建仓库失败（%s: %s）" % (st, info.get("message")))
        print("已创建仓库: %s" % info["html_url"])
        repo_created = True
    else:
        print("仓库已存在: %s" % info["html_url"])
        repo_created = False

    # 2) fork 上游（可选）
    if args.fork_upstream:
        st, f = req(token, "POST", "/repos/armbian/build/forks", {})
        if st in (200, 201, 202):
            print("已 fork armbian/build -> %s（后台创建，稍等生效）" % f.get("full_name", owner + "/build"))
        elif st == 422:
            print("armbian/build 的 fork 已存在，跳过")
        else:
            print("fork 失败（%s: %s）" % (st, f.get("message")))

    # 3) 一次提交推全部文件（Git Data API，避免多次 push）
    files = collect_files()
    print("待发布文件 %d 个" % len(files))

    default_branch = info.get("default_branch") or "main"

    def get_head():
        st, r = req(token, "GET", "/repos/%s/git/ref/heads/%s" % (repo_full, default_branch))
        return (r["object"]["sha"], None) if st == 200 else (None, st)

    parent_commit, _ = get_head()

    # 空仓库先落一个初始提交，否则 Git Data API 会报 409 "Git Repository is empty"
    if not parent_commit:
        print("仓库为空，先建初始提交…")
        boot = dict((rel, data) for rel, data in files).get("README.md") or b"# easepi-r1-armbian\n"
        st, _r = req(token, "PUT", "/repos/%s/contents/README.md" % repo_full, {
            "message": "init",
            "content": base64.b64encode(boot).decode(),
        })
        if st not in (200, 201):
            sys.exit("初始化仓库失败（%s: %s）" % (st, _r.get("message")))
        parent_commit, _ = get_head()
        if not parent_commit:
            sys.exit("初始化后仍取不到 %s 分支 HEAD" % default_branch)

    base_tree = None
    st2, cm = req(token, "GET", "/repos/%s/git/commits/%s" % (repo_full, parent_commit))
    if st2 == 200:
        base_tree = cm["tree"]["sha"]

    tree = []
    for rel, data in files:
        st, blob = req(token, "POST", "/repos/%s/git/blobs" % repo_full, {
            "content": base64.b64encode(data).decode(),
            "encoding": "base64",
        })
        if st not in (200, 201):
            sys.exit("上传 %s 失败（%s: %s）" % (rel, st, blob.get("message")))
        tree.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    tree_payload = {"tree": tree}
    if base_tree:
        tree_payload["base_tree"] = base_tree
    st, new_tree = req(token, "POST", "/repos/%s/git/trees" % repo_full, tree_payload)
    if st not in (200, 201):
        sys.exit("建 tree 失败（%s: %s）" % (st, new_tree.get("message")))

    commit_payload = {
        "message": "easepi-r1: board support for armbian/build + CI\n\n"
                   "config/boards/easepi-r1.conf  (modeled on upstream easepi-a2.conf)\n"
                   "patch/kernel/archive/rockchip64-6.18/dt/  (dts backport, kernel <6.19)\n"
                   "GitHub Actions: build server/minimal/desktop images",
        "tree": new_tree["sha"],
        "parents": [parent_commit] if parent_commit else [],
    }
    st, commit = req(token, "POST", "/repos/%s/git/commits" % repo_full, commit_payload)
    if st not in (200, 201):
        sys.exit("建 commit 失败（%s: %s）" % (st, commit.get("message")))

    st, _ = req(token, "PATCH", "/repos/%s/git/refs/heads/%s" % (repo_full, default_branch),
                 {"sha": commit["sha"], "force": False})
    if st not in (200, 201):
        sys.exit("更新分支失败（%s）" % st)
    print("已提交: %s/commit/%s" % (info["html_url"], commit["sha"][:8]))

    # 4) 触发 CI
    if args.dispatch:
        payload = {
            "ref": default_branch,
            "inputs": {
                "branch": args.branch,
                "release": args.release,
                "variant": args.variant,
                "dts": args.dts,
                "armbian_ref": "main",
            },
        }
        st, res = req(token, "POST", "/repos/%s/actions/workflows/build.yml/dispatches" % repo_full, payload)
        if st in (204, 200):
            print("已触发 CI: branch=%s release=%s variant=%s dts=%s"
                  % (args.branch, args.release, args.variant, args.dts))
            time.sleep(8)
            show_status(token, repo_full)
        else:
            print("触发失败（%s: %s）" % (st, res.get("message")))
            if scopes:
                print("当前 token scopes: %s" % scopes)

    print()
    print("仓库地址: %s" % info["html_url"])
    print("Actions : %s/actions" % info["html_url"])


def show_status(token, repo_full):
    st, runs = req(token, "GET", "/repos/%s/actions/runs?per_page=5" % repo_full)
    if st != 200:
        print("取运行记录失败（%s: %s）" % (st, runs.get("message")))
        return
    items = runs.get("workflow_runs") or []
    if not items:
        print("还没有构建运行（可能 Actions 尚未启用，或刚触发还在排队）")
        return
    for r in items:
        print("[%s] #%s %s | %s | %s" % (
            r["name"], r["run_number"], r["status"],
            r.get("conclusion") or "-", r["html_url"]))
        if r.get("status") != "completed":
            print("    正在跑: %s  开始于 %s" % (r.get("status"), r.get("run_started_at")))


if __name__ == "__main__":
    main()
