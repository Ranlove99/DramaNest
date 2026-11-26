#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DramaNest（剧巢）—— fnOS 原生视频下载应用。

下载 m3u8 / AES-128 加密及直链在线视频，用 ffmpeg 解密合成 MP4 到共享目录，
合并成标准 MP4 落到飞牛共享目录。全部使用 Python 标准库，无第三方依赖。

网关模式：优先监听 APP_DIR/app.sock（Unix Socket）供 fnOS 网关转发；
绑定失败回退 TCP，便于本机调试。
"""

import collections
import json
import os
import queue
import re
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

APPNAME = "dramanest"
PROJECT_URL = "https://github.com/Ranlove99/DramaNest"  # 项目主页（内置，非视频源）
PREFIX = "/app/" + APPNAME
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
VERSION = os.environ.get("TRIM_APPVER") or "1.0"

ETC_DIR = os.environ.get("TRIM_PKGETC") or "/var/apps/%s/etc" % APPNAME
VAR_DIR = os.environ.get("TRIM_PKGVAR") or "/var/apps/%s/var" % APPNAME
CONFIG_FILE = os.path.join(ETC_DIR, "app.json")
TASKS_FILE = os.path.join(VAR_DIR, "tasks.json")
LOG_FILE = os.path.join(VAR_DIR, "app.log")

for d in (ETC_DIR, VAR_DIR):
    os.makedirs(d, exist_ok=True)


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def default_config():
    return {
        "download_root": "/vol1/1000/Movie/DramaNest",
        "ffmpeg": "/usr/bin/ffmpeg",
        "workers": 2,
        "seg_workers": 6,
        "idle_timeout": 90,
        "ua": UA,
        "adult_enabled": False,
    }


def load_config():
    cfg = default_config()
    try:
        if os.path.isfile(CONFIG_FILE):
            with open(CONFIG_FILE, encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                cfg.update({k: v for k, v in saved.items() if k in cfg})
    except Exception as e:
        log("load config err: %s" % e)
    return cfg


def save_config(cfg):
    os.makedirs(ETC_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)


# ---------------- 任务存储 ----------------
_TASKS = {}
_TASKS_LOCK = threading.Lock()


def load_tasks():
    global _TASKS
    try:
        if os.path.isfile(TASKS_FILE):
            with open(TASKS_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                _TASKS = data
    except Exception as e:
        log("load tasks err: %s" % e)
    # 服务重启后，上次没跑完的任务不会自动续跑，标成暂停让用户点「继续」
    changed = False
    for t in _TASKS.values():
        if t.get("status") in ("downloading", "parsing", "queued"):
            t["status"] = "paused"
            t["progress"] = "已中断（服务重启），点「继续」恢复"
            for s in (t.get("ep_state") or []):
                if s.get("st") == "downloading":
                    s["st"] = "pending"
                    s["pct"] = 0
            changed = True
    if changed:
        save_tasks()


def save_tasks():
    # 每个线程使用独立的临时文件，避免并发下载时 os.replace 争抢同一个 .tmp
    try:
        with _TASKS_LOCK:
            snap = {k: dict(v) for k, v in _TASKS.items()}
        tmp = "%s.%d.tmp" % (TASKS_FILE, threading.get_ident())
        # 升级后 VAR_DIR 可能还没建（日志里的 tasks.json.tmp ENOENT 就是这来的）
        d = os.path.dirname(TASKS_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, TASKS_FILE)
    except Exception as e:
        log("save tasks err: %s" % e)


def _probe_writable(d):
    """真正建目录 + 写探针文件，确认可写（os.access 对共享目录不可靠）。"""
    os.makedirs(d, exist_ok=True)
    probe = os.path.join(d, ".w%d_%d" % (os.getpid(), threading.get_ident()))
    with open(probe, "wb") as fh:
        fh.write(b"ok")
    try:
        os.unlink(probe)
    except OSError:
        pass


def fallback_roots():
    return [
        "/var/apps/%s/share/data/DramaNest" % APPNAME,
        "/var/apps/%s/shares/data/DramaNest" % APPNAME,
        os.path.join(VAR_DIR, "downloads"),
        "/tmp/dramanest_downloads",
    ]


def _env_paths(name):
    """读取 fnOS 注入的路径列表环境变量（多个路径用 : 分隔）。"""
    out = []
    for p in (os.environ.get(name) or "").split(":"):
        p = p.strip()
        if not p.startswith("/"):
            continue
        p = os.path.normpath(p)
        if p not in out:
            out.append(p)
    return out


def authorized_paths():
    """用户在飞牛「应用设置 → 授权目录」里授予的目录。

    fnOS 通过 TRIM_DATA_ACCESSIBLE_PATHS 注入（冒号分隔）。manifest 里
    disable_authorization_path = false 时应用设置页才会出现授权入口。
    """
    return _env_paths("TRIM_DATA_ACCESSIBLE_PATHS")


def share_paths():
    """config/resource 声明的共享目录（TRIM_DATA_SHARE_PATHS + share 软链）。"""
    out = _env_paths("TRIM_DATA_SHARE_PATHS")
    for link in ("/var/apps/%s/share" % APPNAME, "/var/apps/%s/shares" % APPNAME):
        if not os.path.isdir(link):
            continue
        try:
            for n in sorted(os.listdir(link)):
                fp = os.path.normpath(os.path.join(link, n))
                if os.path.isdir(fp) and fp not in out:
                    out.append(fp)
        except Exception:
            pass
    return out


def preferred_roots():
    """落盘候选顺序：用户授权目录 > 共享目录 > 应用自有目录。"""
    out = []
    for base in authorized_paths() + share_paths():
        out.append(os.path.join(base, "DramaNest"))
        out.append(base)
    out += fallback_roots()
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def bootstrap_config():
    """首次启动：默认下载目录若不可写，自动挑一个真正可写的位置。

    默认值 /vol1/1000/Movie/DramaNest 只在「第一个存储空间的 1000 用户」下成立，
    别的机器上可能不存在，或者应用账号没有权限。此时按
    授权目录 → 共享目录 → 应用自有目录 的顺序找一个能写的。
    """
    if os.path.isfile(CONFIG_FILE):
        return
    cfg = default_config()
    try:
        _probe_writable(cfg["download_root"])
    except Exception:
        for p in preferred_roots():
            try:
                _probe_writable(p)
                cfg["download_root"] = p
                log("首次启动：默认目录不可写，下载目录自动设为 %s" % p)
                break
            except Exception:
                continue
    save_config(cfg)


def path_status(p):
    """检测某个目录：是否存在、可读、可写（可写要真写探针，不能只看 os.access）。"""
    raw = str(p or "").strip()
    if not raw:
        return {"path": "", "exists": False, "readable": False, "writable": False,
                "error": "路径为空"}
    if not raw.startswith("/"):
        return {"path": raw, "exists": False, "readable": False, "writable": False,
                "error": "必须是绝对路径"}
    p2 = os.path.normpath(raw)
    res = {"path": p2, "exists": os.path.isdir(p2), "readable": False,
           "writable": False, "error": "", "parent_exists": os.path.isdir(os.path.dirname(p2))}
    if not res["exists"]:
        res["error"] = "目录不存在" + ("" if res["parent_exists"] else "（上级目录也不存在，可能卷未挂载）")
        return res
    res["readable"] = os.access(p2, os.R_OK)
    try:
        _probe_writable(p2)
        res["writable"] = True
    except Exception as e:
        res["error"] = str(e)[:140]
    try:
        st = os.stat(p2)
        res["mode"] = oct(st.st_mode & 0o777)
    except Exception:
        pass
    return res


def candidate_paths(limit=60):
    """列出可供选择的目录：授权目录、共享目录、当前目录、各存储卷、应用自有目录。"""
    cfg = load_config()
    cur = (cfg.get("download_root") or "").strip()
    cands = authorized_paths() + share_paths()
    if cur:
        cands.append(cur)
    for i in range(1, 10):
        base = "/vol%d" % i
        if not os.path.isdir(base):
            continue
        cands.append(base)
        try:
            names = sorted(os.listdir(base))[:30]
        except Exception:
            names = []
        for n in names:
            p = os.path.join(base, n)
            if not os.path.isdir(p):
                continue
            cands.append(p)
            if n.isdigit():  # /vol1/1000 → 用户目录，再列一层共享目录
                try:
                    subs = sorted(os.listdir(p))[:40]
                except Exception:
                    subs = []
                for m in subs:
                    p2 = os.path.join(p, m)
                    if os.path.isdir(p2):
                        cands.append(p2)
    cands += fallback_roots()
    seen, out = set(), []
    for c in cands:
        c = os.path.normpath(c)
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def paths_snapshot():
    cfg = load_config()
    cur = (cfg.get("download_root") or "").strip()
    auth = authorized_paths()
    share = share_paths()
    items = [path_status(p) for p in candidate_paths()]
    # 当前目录排最前，其次授权/共享目录，再次可写，再次只读，最后不存在
    def rank(x):
        if x["path"] == os.path.normpath(cur or ""):
            return (0, 0)
        if x["path"] in auth:
            return (1, 0)
        if x["path"] in share:
            return (1, 1)
        if x["writable"]:
            return (2, 0)
        if x["exists"] and x["readable"]:
            return (3, 0)
        return (4, 0)
    items.sort(key=rank)
    for it in items:
        it["auth"] = it["path"] in auth
        it["share"] = it["path"] in share
    return {"current": cur, "app_user": APPNAME,
            "authorized": auth, "shares": share,
            "paths": items}


def resolve_download_root(cfg):
    """确认下载根目录可写；不可写时按顺序回退，避免任务直接失败。

    返回 (路径, 提示信息)；提示信息为空表示用的就是配置里的目录。
    """
    root = (cfg.get("download_root") or default_config()["download_root"]).strip()
    try:
        _probe_writable(root)
        return root, ""
    except Exception as e:
        first_err = e
    for fb in preferred_roots():
        try:
            _probe_writable(fb)
            warn = "下载目录 %s 不可用（%s），已自动改用 %s" % (root, first_err, fb)
            log(warn)
            return fb, warn
        except Exception:
            continue
    log("所有下载目录均不可写，最后尝试 %s" % root)
    return root, "下载目录均不可写，请到「设置」里改一个可写路径（或在飞牛应用设置里授权目录）"


# ---------------- 站点解析 ----------------
def http_json(url, cfg, post_data=None, referer=None, timeout=25):
    headers = {"User-Agent": cfg["ua"], "Accept": "application/json, text/plain, */*"}
    if referer:
        headers["Referer"] = referer
    data = None
    if post_data is not None:
        data = json.dumps(post_data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def sanitize(name):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name or "").strip()
    return name[:120] or "video"


# ---------------- CMS VOD 资源站 ----------------
def _normalize_site(x):
    """规整站点字段：保留 name/api/category/folder/disabled，丢弃其他字段。"""
    if not isinstance(x, dict):
        return None
    api = str(x.get("api") or "").strip()
    if not api.startswith(("http://", "https://")):
        return None
    cat = str(x.get("category") or "normal").lower()
    folder = str(x.get("folder") or "").strip()[:30]
    if not folder:
        folder = "成人内容" if cat == "adult" else "常用资源"
    return {"name": str(x.get("name") or "未命名").strip()[:60],
            "api": api, "category": "adult" if cat == "adult" else "normal",
            "folder": folder, "disabled": bool(x.get("disabled"))}


def cms_sites():
    """资源站配置：用户自定义优先，否则用随包内置列表。"""
    for path in (os.path.join(ETC_DIR, "cms_sites.json"),
                 os.path.join(HERE, "ui", "cms_sites.json")):
        try:
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    out, seen = [], set()
                    for x in data:
                        s = _normalize_site(x)
                        if s and s["api"] not in seen:
                            seen.add(s["api"])
                            out.append(s)
                    return out
        except Exception as e:
            log("cms sites load err (%s): %s" % (path, e))
    return []


def save_cms_sites(sites):
    path = os.path.join(ETC_DIR, "cms_sites.json")
    os.makedirs(ETC_DIR, exist_ok=True)
    clean, seen = [], set()
    for x in sites or []:
        s = _normalize_site(x)
        if s and s["api"] not in seen:
            seen.add(s["api"])
            clean.append(s)
    # 禁用的源统一排到最后，保持各自原有相对顺序
    clean = ([s for s in clean if not s["disabled"]] +
             [s for s in clean if s["disabled"]])
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, ensure_ascii=False, indent=2)
    return clean


def site_is_adult(api, cfg=None):
    """判断某个 API 是否属于成人分类；缺失配置时按普通处理。"""
    for s in cms_sites():
        if s["api"] == api:
            return s.get("category") == "adult"
    return False


def site_is_disabled(api):
    for s in cms_sites():
        if s["api"] == api:
            return bool(s.get("disabled"))
    return False


def check_adult_allowed(api):
    """成人内容受开关控制：关闭时拒绝访问。"""
    if site_is_adult(api) and not bool(load_config().get("adult_enabled")):
        raise ValueError("该资源站属于成人内容，请先在「设置」中开启成人内容开关")


def check_site_usable(api):
    """禁用的源不参与搜索/详情/下载，避免无谓的等待。"""
    check_adult_allowed(api)
    if site_is_disabled(api):
        raise ValueError("该资源站已被禁用，请在「设置 → 站点排序」中重新启用")


def cms_fetch(api, params, cfg, timeout=20):
    """请求苹果 CMS / 海洋 CMS 的 vod JSON 采集接口。"""
    api = (api or "").strip()
    if not api.startswith(("http://", "https://")):
        raise ValueError("资源站 API 地址无效")
    p = urllib.parse.urlparse(api)
    referer = "%s://%s/" % (p.scheme, p.netloc)
    q = "&".join("%s=%s" % (k, urllib.parse.quote(str(v))) for k, v in params.items())
    return http_json(api + ("&" if "?" in api else "?") + q, cfg,
                     referer=referer, timeout=timeout)


# ---------------- 源测活与评分 ----------------
_SITE_TEST = {"running": False, "total": 0, "done": 0, "started": 0.0,
              "finished_at": 0.0, "results": {}}
_SITE_TEST_LOCK = threading.Lock()


def site_score(ok, ms, count, classes):
    """0~100 分：能返回合法列表即 60 分起，速度、条目数、分类数加分。"""
    if not ok:
        return 0
    s = 60
    if ms <= 500:
        s += 25
    elif ms <= 1500:
        s += 15
    elif ms <= 3000:
        s += 8
    if count > 0:
        s += 10
    if classes > 0:
        s += 5
    return min(100, s)


def test_one_site(site, cfg, timeout=8):
    """单源测活：拉一页列表，校验结构并记录耗时。"""
    t0 = time.time()
    try:
        d = cms_fetch(site["api"], {"ac": "list", "pg": 1}, cfg, timeout=timeout)
        ms = int((time.time() - t0) * 1000)
        if not isinstance(d, dict):
            raise ValueError("返回不是 JSON 对象")
        lst = d.get("list")
        if not isinstance(lst, list):
            raise ValueError("缺少 list 字段，可能不是苹果 CMS 接口")
        cls = d.get("class") or []
        return {"api": site["api"], "ok": True, "ms": ms, "count": len(lst),
                "classes": len(cls) if isinstance(cls, list) else 0,
                "score": site_score(True, ms, len(lst), len(cls) if isinstance(cls, list) else 0),
                "err": ""}
    except Exception as e:
        return {"api": site["api"], "ok": False, "ms": int((time.time() - t0) * 1000),
                "count": 0, "classes": 0, "score": 0, "err": str(e)[:150]}


def start_site_test(apis=None, threads=8):
    """后台并发测活；已在进行中则直接复用当前任务。"""
    sites = cms_sites()
    if apis:
        want = set(apis)
        sites = [s for s in sites if s["api"] in want]
    with _SITE_TEST_LOCK:
        if _SITE_TEST["running"]:
            return False
        _SITE_TEST.update({"running": True, "total": len(sites), "done": 0,
                           "started": time.time(), "finished_at": 0.0, "results": {}})
    cfg = load_config()
    n = max(1, min(12, int(threads or 8)))

    def run():
        try:
            with ThreadPoolExecutor(max_workers=n) as ex:
                futs = [ex.submit(test_one_site, s, cfg) for s in sites]
                for f in as_completed(futs):
                    try:
                        r = f.result()
                    except Exception as e:
                        continue
                    with _SITE_TEST_LOCK:
                        _SITE_TEST["results"][r["api"]] = r
                        _SITE_TEST["done"] += 1
        finally:
            with _SITE_TEST_LOCK:
                _SITE_TEST["running"] = False
                _SITE_TEST["finished_at"] = time.time()
            log("site test finished: %d sites" % len(sites))

    threading.Thread(target=run, daemon=True).start()
    return True


def site_test_snapshot():
    with _SITE_TEST_LOCK:
        return {"running": _SITE_TEST["running"], "total": _SITE_TEST["total"],
                "done": _SITE_TEST["done"], "started": _SITE_TEST["started"],
                "finished_at": _SITE_TEST["finished_at"],
                "results": dict(_SITE_TEST["results"])}


def set_sites_disabled(apis, disabled=True):
    """禁用/启用源：写回配置文件，禁用的自动排到最后。"""
    want = set(apis or [])
    sites = cms_sites()
    for s in sites:
        if s["api"] in want:
            s["disabled"] = bool(disabled)
    return save_cms_sites(sites)


# ---------------- 全站搜索（汇总所有源） ----------------
_SEARCH = {"running": False, "total": 0, "done": 0, "keyword": "", "items": [],
           "errors": {}, "started": 0.0, "finished_at": 0.0, "sites": 0}
_SEARCH_LOCK = threading.Lock()


def _norm_title(t):
    """标题归一化，用于跨源合并同一部剧。"""
    return re.sub(r"[\s\-_·:：,，.。!！?？()（）\[\]【】《》\"'“”]+", "", str(t or "")).lower()


def search_one_site(site, keyword, cfg, per_site=20):
    d = cms_fetch(site["api"], {"ac": "list", "pg": 1, "wd": keyword}, cfg, timeout=10)
    rows = []
    for v in (d.get("list") or [])[:per_site]:
        rows.append({
            "site": site["name"], "api": site["api"],
            "id": str(v.get("vod_id") or ""),
            "title": str(v.get("vod_name") or ""),
            "type": str(v.get("type_name") or ""),
            "remarks": str(v.get("vod_remarks") or ""),
            "time": str(v.get("vod_time") or ""),
            "adult": site.get("category") == "adult",
        })
    return rows


def start_search_all(keyword, per_site=20, threads=8):
    """并发搜索所有可用源，按标题合并成「一部剧 + N 个源」。"""
    kw = (keyword or "").strip()
    if not kw:
        return False
    cfg = load_config()
    adult_ok = bool(cfg.get("adult_enabled"))
    sites = [s for s in cms_sites()
             if not s.get("disabled") and (adult_ok or s.get("category") != "adult")]
    if not sites:
        return False
    with _SEARCH_LOCK:
        if _SEARCH["running"]:
            return False
        _SEARCH.update({"running": True, "total": len(sites), "done": 0, "keyword": kw,
                        "items": [], "errors": {}, "started": time.time(),
                        "finished_at": 0.0, "sites": len(sites)})

    def run():
        merged, errs = {}, {}
        try:
            with ThreadPoolExecutor(max_workers=max(1, min(12, threads))) as ex:
                futs = {ex.submit(search_one_site, s, kw, cfg, per_site): s for s in sites}
                for f in as_completed(futs):
                    site = futs[f]
                    try:
                        rows = f.result()
                    except Exception as e:
                        errs[site["api"]] = str(e)[:100]
                        rows = []
                    for r in rows:
                        k = _norm_title(r["title"])
                        if not k:
                            continue
                        it = merged.get(k)
                        if not it:
                            it = {"title": r["title"], "type": r["type"],
                                  "remarks": r["remarks"], "time": r["time"],
                                  "adult": r["adult"], "sources": []}
                            merged[k] = it
                        it["sources"].append({"site": r["site"], "api": r["api"],
                                              "id": r["id"], "remarks": r["remarks"],
                                              "adult": r["adult"]})
                    with _SEARCH_LOCK:
                        _SEARCH["done"] += 1
                        _SEARCH["items"] = sorted(merged.values(),
                                                  key=lambda x: (-len(x["sources"]), x["title"]))
                        _SEARCH["errors"] = dict(errs)
        finally:
            with _SEARCH_LOCK:
                _SEARCH["running"] = False
                _SEARCH["finished_at"] = time.time()
            log("search-all '%s' done: %d titles / %d sites" % (kw, len(merged), len(sites)))

    threading.Thread(target=run, daemon=True).start()
    return True


def search_snapshot(limit=400):
    with _SEARCH_LOCK:
        return {"running": _SEARCH["running"], "total": _SEARCH["total"],
                "done": _SEARCH["done"], "keyword": _SEARCH["keyword"],
                "sites": _SEARCH["sites"], "started": _SEARCH["started"],
                "finished_at": _SEARCH["finished_at"],
                "items": list(_SEARCH["items"][:limit]), "count": len(_SEARCH["items"]),
                "errors": dict(_SEARCH["errors"])}


def cms_brief(it):
    """列表项只保留展示字段，不返回 vod_content（站点简介常含露骨文案）。"""
    try:
        vid = str(it.get("vod_id") or "").strip()
    except Exception:
        return None
    if not vid:
        return None
    return {
        "id": vid,
        "title": str(it.get("vod_name") or ("视频 " + vid))[:100],
        "type": str(it.get("type_name") or "")[:30],
        "remarks": str(it.get("vod_remarks") or "")[:40],
        "time": str(it.get("vod_time") or "")[:19],
        "from": str(it.get("vod_play_from") or "")[:80],
    }


def cms_list(api, cfg, page=1, keyword="", type_id=""):
    params = {"ac": "list", "pg": max(1, int(page))}
    if keyword:
        params["wd"] = keyword
    if type_id:
        params["type_id"] = type_id
    d = cms_fetch(api, params, cfg)
    items = [v for v in (cms_brief(x) for x in (d.get("list") or [])) if v]
    classes = [{"id": str(c.get("type_id") or ""), "name": str(c.get("type_name") or "")}
               for c in (d.get("class") or []) if isinstance(c, dict)]
    try:
        pages = max(1, int(d.get("pagecount") or 1))
    except Exception:
        pages = 1
    try:
        total = int(d.get("total") or 0)
    except Exception:
        total = 0
    return {"page": params["pg"], "pages": pages, "total": total,
            "videos": items, "class": classes[:60]}


_CMS_DETAIL_CACHE = {}
_CMS_DETAIL_LOCK = threading.Lock()


def cms_detail(api, cfg, vod_id, force=False):
    key = (api, str(vod_id))
    now = time.time()
    with _CMS_DETAIL_LOCK:
        hit = _CMS_DETAIL_CACHE.get(key)
        if hit and not force and now - hit[0] < 120:
            return hit[1]
    d = cms_fetch(api, {"ac": "detail", "ids": vod_id}, cfg, timeout=15)
    rows = d.get("list") or []
    if not rows:
        raise ValueError("资源站没有返回该视频的详情")
    v = rows[0]
    froms = [x for x in str(v.get("vod_play_from") or "").split("$$$") if x]
    blocks = str(v.get("vod_play_url") or "").split("$$$")
    sources = []
    for i, name in enumerate(froms):
        eps = []
        block = blocks[i] if i < len(blocks) else ""
        for seg in block.split("#"):
            seg = seg.strip()
            if not seg:
                continue
            if "$" in seg:
                ep_name, ep_url = seg.split("$", 1)
            else:
                ep_name, ep_url = "第%02d集" % (len(eps) + 1), seg
            ep_url = ep_url.strip()
            if ep_url:
                eps.append({"title": sanitize(ep_name.strip()), "url": ep_url,
                            "index": len(eps) + 1})
        if eps:
            sources.append({"from": name, "episodes": eps})
    result = {
        "id": str(vod_id),
        "title": sanitize(v.get("vod_name") or ("视频 " + str(vod_id))),
        "type": str(v.get("type_name") or ""),
        "remarks": str(v.get("vod_remarks") or ""),
        "sources": sources,
    }
    with _CMS_DETAIL_LOCK:
        _CMS_DETAIL_CACHE[key] = (now, result)
    return result


_RE_M3U8 = re.compile(r'https?://[^\s"\'<>\\]+?\.m3u8[^\s"\'<>\\]*', re.I)
_RE_MEDIA_URL = re.compile(r'(?:url|playurl|video_url|m3u8)\s*[:=]\s*["\']?(https?://[^"\'\s,;]+)', re.I)


def resolve_play_url(u, cfg, referer=""):
    """把 CMS 播放地址解析成 ffmpeg 可用的 m3u8/mp4 直链。

    资源站给的地址有两种：直接媒体文件，或需要二次跳转的播放页。
    """
    u = (u or "").strip()
    if not u:
        return None
    p = urllib.parse.urlparse(u)
    if p.path.lower().endswith((".m3u8", ".mp4", ".ts")):
        return {"url": u, "referer": referer or ("%s://%s/" % (p.scheme, p.netloc))}
    try:
        req = urllib.request.Request(
            u, headers={"User-Agent": cfg["ua"], "Referer": referer or u})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read(800000).decode("utf-8", "replace")
        m = _RE_M3U8.search(html)
        if m:
            return {"url": m.group(0), "referer": u}
        m = _RE_MEDIA_URL.search(html)
        if m:
            return {"url": m.group(1), "referer": u}
    except Exception as e:
        log("resolve play url err (%s): %s" % (u[:80], e))
    return None


# ---------------- 视频预览：直链解析 + 本地取流代理 ----------------
# 思路同 LibreTV：浏览器不能直接播 m3u8（Chrome 无原生 HLS），而且资源站的
# 分片/密钥普遍带 Referer 防盗链和跨域限制。这里把 m3u8 拉回来改写成走本地
# /px 代理，前端用 hls.js 播，既绕开跨域，也把 UA/Referer 补齐。
_PREVIEW_CACHE = {}
_PREVIEW_LOCK = threading.Lock()
_DIRECT_EXT = (".m3u8", ".mp4", ".ts", ".flv", ".mkv", ".m4v")


def _is_direct_media(u):
    """地址本身就是媒体文件（不是需要二次跳转的播放页）。"""
    return urllib.parse.urlparse(u or "").path.lower().endswith(_DIRECT_EXT)


def cms_links(api, cfg, vod_id, play_from=""):
    """返回某播放源每集的原始地址，供「显示视频链接」和播放器使用。

    这里不逐集解析（80 集要发 80 个请求），只标记哪些本来就是直链，
    播放页地址留给 cms_resolve_ep 按需解析并缓存。
    """
    d = cms_detail(api, cfg, vod_id)
    srcs = d.get("sources") or []
    if not srcs:
        raise ValueError("没有可用播放源")
    src = next((s for s in srcs if s["from"] == play_from), srcs[0])
    eps = [{"index": e["index"], "title": e["title"], "url": e["url"],
            "direct": _is_direct_media(e["url"])} for e in src["episodes"]]
    return {"id": d["id"], "title": d["title"], "from": src["from"],
            "type": d.get("type") or "", "remarks": d.get("remarks") or "",
            "sources": [s["from"] for s in srcs], "episodes": eps}


def cms_resolve_ep(api, cfg, vod_id, play_from, index, force=False):
    """把某一集解析成真正能播的直链；播放页地址会二次抓取，结果缓存 5 分钟。"""
    try:
        idx = int(index)
    except (TypeError, ValueError):
        raise ValueError("分集序号无效")
    key = (api, str(vod_id), play_from or "", idx)
    now = time.time()
    with _PREVIEW_LOCK:
        hit = _PREVIEW_CACHE.get(key)
        if hit and not force and now - hit[0] < 300:
            return {"url": hit[1], "referer": hit[2], "cached": True}
    info = cms_links(api, cfg, vod_id, play_from)
    ep = next((e for e in info["episodes"] if e["index"] == idx), None)
    if not ep:
        raise ValueError("没有第 %s 集" % idx)
    r = resolve_play_url(ep["url"], cfg)
    if not r:
        raise ValueError("该集地址无法解析（可能是播放页失效或需要登录）")
    with _PREVIEW_LOCK:
        _PREVIEW_CACHE[key] = (now, r["url"], r.get("referer") or "")
    return {"url": r["url"], "referer": r.get("referer") or "",
            "raw": ep["url"], "title": ep["title"], "cached": False}


def px_url(u, referer=""):
    """把上游地址包装成本地代理地址。

    必须带 PREFIX：飞牛上应用挂在 /app/dramanest/ 下，写根路径 /px 会被网关
    当成别的应用，浏览器直接 404。
    """
    q = "u=" + urllib.parse.quote(u or "", safe="")
    if referer:
        q += "&r=" + urllib.parse.quote(referer, safe="")
    return PREFIX + "/px?" + q


def px_rewrite(text, base, referer=""):
    """改写 m3u8：分片、子播放列表、EXT-X-KEY/EXT-X-MAP 的 URI 全部指向本地代理。

    AES-128 的密钥也走代理，解密仍由 hls.js 在浏览器里完成。
    """
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if s.startswith("#"):
            # 只改 URI="..." 这种属性，注释本身原样保留
            out.append(re.sub(r'URI="([^"]+)"',
                              lambda m: 'URI="%s"' % px_url(
                                  urllib.parse.urljoin(base, m.group(1)), referer),
                              line))
            continue
        out.append(px_url(urllib.parse.urljoin(base, s), referer))
    return "\n".join(out) + "\n"


def parse_huangguo(url, cfg):
    """解析黄果短剧系统：/video/<id>/ 或带 ep-N。

    该系统的站点会频繁更换域名，
    但 API 结构一致，因此从输入的 URL 自动提取站点根域名，不写死任何域名。
    """
    m = re.search(r"/video/(\d+)(?:/ep-(\d+))?", url)
    if not m:
        return None
    vid = m.group(1)
    only_ep = int(m.group(2)) if m.group(2) else None
    # 从输入 URL 提取站点根（协议 + 主机）
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc:
        return None
    base = "%s://%s" % (parsed.scheme or "https", parsed.netloc)
    ref = "%s/video/%s/" % (base, vid)
    # 取详情拿标题和总集数
    try:
        info = http_json("%s/api/videos/%s/play" % (base, vid), cfg, referer=ref)
        d = info.get("data", {}) or {}
        raw_title = re.sub(r"\s*第\s*\d+\s*集.*$", "", d.get("page_h1") or d.get("page_title") or "")
        raw_title = re.sub(r"\s*-\s*短剧视频在线观看\s*\|\s*.*$", "", raw_title).strip()
        if not raw_title:
            raw_title = "video_%s" % vid
        desc = d.get("page_desc") or ""
        em = re.search(r"更新至(\d+)集", desc)
        total = int(em.group(1)) if em else 1
    except Exception as e:
        log("site info err (%s): %s" % (base, e))
        raw_title, total = "video_%s" % vid, (only_ep or 1)

    eps = list(range(1, total + 1)) if not only_ep else [only_ep]
    result = []
    for ep in eps:
        try:
            pb = http_json("%s/api/videos/%s/episodes/%d/playback" % (base, vid, ep),
                           cfg, post_data={}, referer=ref)
            data = pb.get("data", {}) or {}
            streams = data.get("streams", {}) or {}
            m3u8 = streams.get("main") or streams.get("hevc") or streams.get("preview")
            cdn = data.get("cdn", {}) or {}
            if not m3u8:
                continue
            result.append({
                "title": "%s - S01E%02d" % (raw_title, ep),
                "series": sanitize(raw_title),
                "m3u8": m3u8,
                "auth_key": cdn.get("authKey") or "",
                "auth_value": cdn.get("authValue") or "",
                "referer": ref,
            })
        except Exception as e:
            log("ep %d err: %s" % (ep, e))
    if result:
        return {"series": sanitize(raw_title), "episodes": result}
    return None


def parse_generic(url, cfg):
    """通用 m3u8 直链。"""
    title = "video"
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "title" in qs:
        title = qs["title"][0]
    return {
        "series": sanitize(title),
        "episodes": [{
            "title": sanitize(title),
            "series": sanitize(title),
            "m3u8": url,
            "auth_key": "",
            "auth_value": "",
            "referer": urllib.parse.urlparse(url).scheme + "://" + urllib.parse.urlparse(url).netloc + "/",
        }],
    }


def parse_source(url, cfg):
    url = (url or "").strip()
    if not url:
        raise ValueError("地址不能为空")
    # 黄果短剧系统：任何域名的 /video/<id>/ 路径都按其 API 结构解析
    if re.search(r"/video/\d+", url):
        r = parse_huangguo(url, cfg)
        if r:
            return r
    if ".m3u8" in url or url.lower().startswith("http"):
        return parse_generic(url, cfg)
    raise ValueError("无法识别的地址，请粘贴视频页地址或 m3u8 直链")


# ---------------- 整站扫描 ----------------
def site_base(url):
    """从任意站点地址提取协议 + 主机，不写死任何域名。"""
    u = (url or "").strip()
    if not u:
        raise ValueError("请填写站点地址")
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    p = urllib.parse.urlparse(u)
    if not p.netloc:
        raise ValueError("站点地址无效")
    return "%s://%s" % (p.scheme.lower(), p.netloc)


def _video_brief(it, base):
    """只保留展示需要的字段；刻意不返回 description，避免站点简介里的
    露骨文案被渲染到前端列表。"""
    try:
        vid = int(it.get("id"))
    except Exception:
        return None
    return {
        "id": vid,
        "title": it.get("title") or ("视频 %s" % vid),
        "episodes": int(it.get("episode_count") or it.get("total_episodes") or 0),
        "duration": str(it.get("duration") or ""),
        "score": it.get("score"),
        "tags": list(it.get("tags") or [])[:4],
        "finished": bool(it.get("is_finished")),
        "hot": int(it.get("hot") or 0),
        "url": "%s/video/%d/" % (base, vid),
    }


def scan_page(base, cfg, page, page_size=100, timeout=30):
    """扫描单页，供前端逐页调用，避免单次请求超过网关超时。"""
    d = http_json("%s/api/videos?page=%d&size=%d" % (base, page, page_size),
                  cfg, referer=base + "/", timeout=timeout)
    data = d.get("data") or {}
    pg = data.get("pagination") or {}
    videos = [v for v in (_video_brief(it, base) for it in (data.get("items") or [])) if v]
    try:
        pages = max(1, int(pg.get("pages") or 1))
    except Exception:
        pages = 1
    return {"site": base, "page": page, "pages": pages,
            "total": int(pg.get("total") or 0), "videos": videos,
            "finished": page >= pages}


def scan_site(base, cfg, max_pages=0, page_size=100, timeout=30):
    """服务端一次性扫描（API 调试用）；正常前端走 scan_page 逐页拉取。"""
    videos = {}
    page = 1
    pages = 1
    while page <= pages:
        try:
            r = scan_page(base, cfg, page, page_size, timeout)
        except Exception as e:
            if page == 1:
                raise ValueError("无法访问站点列表接口：%s" % e)
            log("scan page %d err: %s" % (page, e))
            break
        pages = r["pages"]
        if max_pages:
            pages = min(pages, int(max_pages))
        for v in r["videos"]:
            videos[v["id"]] = v
        log("scan page %d/%d: +%d, total %d" % (page, pages, len(r["videos"]), len(videos)))
        page += 1
    result = sorted(videos.values(), key=lambda x: x["id"], reverse=True)
    return {"site": base, "total": len(result), "videos": result}


# ---------------- 下载执行 ----------------
_STOP_FLAGS = {}
_PAUSE_FLAGS = {}
_RUNNING = set()
_RUNNING_LOCK = threading.Lock()
_DL_COND = threading.Condition()
_DL_ACTIVE = 0


class TaskCancelled(Exception):
    """整个任务被取消。"""


class TaskPaused(Exception):
    """整个任务被暂停，可原地继续。"""


class EpPaused(Exception):
    """单集被暂停，跳过本集继续下一集。"""


def dl_limit(cfg):
    try:
        return max(1, min(8, int(cfg.get("workers") or 2)))
    except Exception:
        return 2


def acquire_slot(cfg, task, stop_flag, pause_flag):
    """全局并发闸门：最多同时跑 workers 个 ffmpeg。

    返回 "ok" / "cancelled" / "paused"，排队期间也能响应取消与暂停。
    """
    global _DL_ACTIVE
    limit = dl_limit(cfg)
    with _DL_COND:
        while _DL_ACTIVE >= limit:
            if stop_flag.is_set():
                return "cancelled"
            if pause_flag.is_set():
                return "paused"
            task["status"] = "queued"
            task["progress"] = "排队中（当前 %d/%d 个下载进行中）" % (_DL_ACTIVE, limit)
            _DL_COND.wait(timeout=1.5)
        if stop_flag.is_set():
            return "cancelled"
        if pause_flag.is_set():
            return "paused"
        _DL_ACTIVE += 1
    return "ok"


def release_slot():
    global _DL_ACTIVE
    with _DL_COND:
        _DL_ACTIVE = max(0, _DL_ACTIVE - 1)
        _DL_COND.notify_all()


def human_size(num):
    try:
        num = float(num or 0)
    except Exception:
        return ""
    if num < 1024:
        return "%d B" % num
    if num < 1024 ** 2:
        return "%.1f KB" % (num / 1024)
    if num < 1024 ** 3:
        return "%.1f MB" % (num / (1024 ** 2))
    return "%.2f GB" % (num / (1024 ** 3))


def human_speed(bps):
    if not bps or bps <= 0:
        return ""
    return human_size(bps) + "/s"


def human_duration(sec):
    try:
        sec = int(sec)
    except Exception:
        return "—"
    if sec < 0:
        return "—"
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return "%02d:%02d:%02d" % (h, m, s) if h else "%02d:%02d" % (m, s)


def probe_duration(ep, cfg):
    """读取 m3u8 的 EXTINF 求总时长，用于计算本集百分比。失败返回 0。"""
    url = ep.get("m3u8")
    if not url:
        return 0

    def fetch(u):
        headers = {"User-Agent": cfg["ua"]}
        if ep.get("referer"):
            headers["Referer"] = ep["referer"]
        if ep.get("auth_key") and ep.get("auth_value"):
            headers[ep["auth_key"]] = ep["auth_value"]
        req = urllib.request.Request(u, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.read(600000).decode("utf-8", "replace")

    try:
        text = fetch(url)
        dur = sum(float(x) for x in re.findall(r"#EXTINF:([0-9.]+)", text))
        if dur > 0:
            return dur
        # 主播放列表：跟随第一个子列表
        m = re.search(r"(?m)^\s*(?!#)(https?://[^\r\n]+\.m3u8[^\r\n]*|[^#\r\n]+\.m3u8[^\r\n]*)\s*$", text)
        if m:
            sub = urllib.parse.urljoin(url, m.group(1).strip())
            t2 = fetch(sub)
            return sum(float(x) for x in re.findall(r"#EXTINF:([0-9.]+)", t2))
    except Exception as e:
        log("probe duration err: %s" % e)
    return 0


class FFmpegError(RuntimeError):
    """ffmpeg 失败，附带 stderr 尾部，便于定位（退出码 234 这类没有上下文很难查）。"""

    def __init__(self, msg, detail=""):
        super().__init__(msg)
        self.detail = detail


def kill_ffmpeg(proc):
    """杀掉整个进程组。只杀直接子进程时，孙进程仍占着管道，读线程会永远阻塞。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


_MUXER_BY_EXT = {".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".ts": "mpegts",
                 ".mkv": "matroska", ".flv": "flv", ".webm": "webm", ".avi": "avi"}


def _muxer_for(path):
    """按真实输出文件名推断封装格式。

    必须显式传给 ffmpeg：写的是 xxx.mp4.part，扩展名是 .part，
    ffmpeg 猜不出格式会直接报 "Unable to choose an output format"（rc=234）。
    """
    return _MUXER_BY_EXT.get(os.path.splitext(path)[1].lower(), "mp4")


def _ff_args(cfg, ep, out_path, variant="normal", src=None, fmt=""):
    """构造 ffmpeg 参数。

    variant:
      normal   普通直连
      robust   健壮直连（加断线重连、放宽协议白名单）
      local    从本地分段合成，保留 aac_adtstoasc
      localraw 从本地分段合成，不加 bsf（fMP4 分片用）
    注意：输入是本地文件时不能带 -reconnect* / -headers / -user_agent，
    ffmpeg 会直接报 "Option not found"（rc=8）——这正是之前本地兜底全挂的原因。
    """
    url = src or ep.get("m3u8") or ""
    local_src = bool(src) and not url.lower().startswith(("http://", "https://"))
    cmd = [cfg["ffmpeg"], "-nostdin", "-loglevel", "error", "-stats"]
    if variant == "robust" and not local_src:
        cmd += ["-rw_timeout", "15000000",
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                "-http_persistent", "0"]
    if not local_src:
        headers = "Referer: %s\r\n" % (ep.get("referer") or "")
        if ep.get("auth_key") and ep.get("auth_value"):
            headers += "%s: %s\r\n" % (ep["auth_key"], ep["auth_value"])
        cmd += ["-headers", headers, "-user_agent", cfg["ua"]]
    cmd += ["-i", url, "-c", "copy"]
    if variant in ("normal", "local"):
        cmd += ["-bsf:a", "aac_adtstoasc"]
    if fmt:
        cmd += ["-f", fmt]
    cmd += ["-y", out_path]
    return cmd


def run_ffmpeg(task, ep, out_path, cfg, stop_flag, pause_flag=None, st=None,
               variant="normal", src=None):
    """下载单集。写入 .part 临时文件，成功后才改名，避免半成品冒充成片。

    期间同时响应：任务取消 / 任务暂停 / 单集暂停；带空闲看门狗（默认 90 秒无输出即终止），
    失败时把 ffmpeg stderr 尾部一起抛出并写日志。
    """
    tmp_path = out_path + ".part"
    cmd = _ff_args(cfg, ep, out_path=tmp_path, variant=variant, src=src,
                   fmt=_muxer_for(out_path))
    log("ffmpeg[%s]: %s" % (variant, " ".join(cmd)))
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
    except Exception as e:
        raise RuntimeError("无法创建下载目录 %s（%s），请检查设置里的下载根目录" % (
            os.path.dirname(out_path), e))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, start_new_session=True)
    # 预先读取本集总时长，用于百分比；失败则退化为按集数进度
    total_dur = ep.get("_dur") or probe_duration(ep, cfg)
    if total_dur:
        ep["_dur"] = total_dur
    started = time.time()
    task["mode_text"] = {"normal": "ffmpeg 直连", "robust": "ffmpeg 重连",
                         "local": "本地合成", "localraw": "本地合成"}.get(variant, "ffmpeg")
    last_sec, last_size, last_sample = 0, 0, started
    last = 0
    err = None
    rc = None
    tail = collections.deque(maxlen=25)
    idle = float(cfg.get("idle_timeout") or 90)
    last_act = [started]

    # 用队列 + 超时读取，而不是直接迭代 stdout：
    # 被 kill 的子进程若还有孙进程占着管道，迭代会永久阻塞在 EOF 上。
    q = queue.Queue()

    def reader():
        try:
            for ln in proc.stdout:
                q.put(ln)
        except Exception:
            pass
        finally:
            q.put(None)

    threading.Thread(target=reader, daemon=True).start()
    try:
        while True:
            try:
                line = q.get(timeout=1.0)
            except queue.Empty:
                line = ""
            if line is None:
                break
            line = line.rstrip()
            if line:
                last_act[0] = time.time()
                if "time=" not in line:
                    tail.append(line)
            if stop_flag.is_set():
                err = "cancelled"
                break
            if pause_flag is not None and pause_flag.is_set():
                err = "paused"
                break
            if st is not None and st.get("st") == "paused":
                err = "ep_paused"
                break
            # 看门狗：ffmpeg 直连 HLS 没有默认超时，坏 CDN 会让它永久挂住
            if time.time() - last_act[0] > idle:
                tail.append("[看门狗] %.0f 秒无任何输出，强制终止" % idle)
                err = "idle"
                break
            # ffmpeg -stats: frame=... size=1234kB time=00:00:12.34 bitrate=... speed=45x
            tm = re.search(r"time=(\d+):(\d+):(\d+)", line)
            if tm:
                sec = int(tm.group(1)) * 3600 + int(tm.group(2)) * 60 + int(tm.group(3))
                now = time.time()
                if sec >= last + 2 or now - task.get("_last_prog", 0) > 3:
                    try:
                        cur_size = os.path.getsize(tmp_path) if os.path.isfile(tmp_path) else 0
                    except Exception:
                        cur_size = 0
                    rate = 0
                    if now - last_sample >= 0.8 and cur_size >= last_size:
                        rate = (cur_size - last_size) / (now - last_sample)
                        last_size, last_sample = cur_size, now
                    sm = re.search(r"speed=\s*([\d.]+)x", line)
                    task["speed_text"] = human_speed(rate)
                    task["size_text"] = human_size(cur_size)
                    task["elapsed"] = human_duration(now - started)
                    task["ff_speed"] = (sm.group(1) + "x") if sm else ""
                    pct = None
                    if total_dur and sec > 0:
                        pct = min(99, int(sec * 100 / total_dur))
                        remain = (now - started) * (total_dur - sec) / sec
                        task["eta"] = human_duration(remain)
                    task["ep_pct"] = pct
                    if st is not None:
                        st["pct"] = pct if pct is not None else 0
                        st["size"] = human_size(cur_size)
                        st["sp"] = task["speed_text"]
                    task["progress"] = "%s (%s) %s%s" % (
                        ep.get("title", ""), human_duration(sec),
                        task["size_text"], (" · " + task["speed_text"]) if task["speed_text"] else "")
                    task["_last_prog"] = now
                    last = sec
                    save_tasks()
    finally:
        if err:
            kill_ffmpeg(proc)
        try:
            rc = proc.wait(timeout=20)
        except Exception:
            kill_ffmpeg(proc)
            rc = -1
        if err or rc != 0:
            try:
                os.path.isfile(tmp_path) and os.remove(tmp_path)
            except Exception:
                pass
    if err == "cancelled":
        raise TaskCancelled()
    if err == "paused":
        raise TaskPaused()
    if err == "ep_paused":
        raise EpPaused()
    if err == "idle":
        raise FFmpegError("下载卡住（%.0f 秒无数据），已强制重启" % idle, "\n".join(list(tail)[-8:]))
    if rc != 0:
        detail = "\n".join(list(tail)[-8:])
        hint = ""
        for ln in reversed(list(tail)):
            if ln.strip():
                hint = ln.strip()[:110]
                break
        log("ffmpeg 失败 rc=%s ep=%s detail:\n%s" % (rc, ep.get("title"), "\n".join(tail)))
        raise FFmpegError("ffmpeg 退出码 %s%s" % (rc, ("：" + hint) if hint else ""), detail)
    if not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) < 1024:
        raise FFmpegError("输出文件为空")
    os.replace(tmp_path, out_path)


# ---------------- 分段/直连下载（不依赖 ffmpeg） ----------------
def _http_open(u, cfg, ep, timeout=25):
    headers = {"User-Agent": cfg["ua"]}
    if ep.get("referer"):
        headers["Referer"] = ep["referer"]
    if ep.get("auth_key") and ep.get("auth_value"):
        headers[ep["auth_key"]] = ep["auth_value"]
    req = urllib.request.Request(u, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def _http_bytes(u, cfg, ep, timeout=25, limit=8 * 1024 * 1024):
    with _http_open(u, cfg, ep, timeout) as resp:
        return resp.read(limit)


def _fetch_to(u, path, cfg, ep, retries=3, stop_flag=None):
    """下单个分片到本地，返回字节数。"""
    last = None
    for i in range(retries):
        if stop_flag is not None and stop_flag.is_set():
            raise TaskCancelled()
        try:
            data = _http_bytes(u, cfg, ep)
            if not data:
                raise RuntimeError("空响应")
            with open(path, "wb") as fh:
                fh.write(data)
            return len(data)
        except TaskCancelled:
            raise
        except Exception as e:
            last = e
            time.sleep(0.6 * (i + 1))
    raise RuntimeError("分片下载失败：%s" % last)


def hls_local_download(ep, cfg, stop_flag, task=None, st=None):
    """把 m3u8 和全部分片并发抓到本地，再让 ffmpeg 从本地合成。

    并发是关键：一集动辄 150~200 个分片，串行下载要好几分钟，
    6 路并发能压到十几秒。AES 密钥仍交给 ffmpeg 解密（key 改写成绝对地址）。
    返回 (本地 m3u8 路径, 临时目录)。
    """
    base = ep["m3u8"]
    text = _http_bytes(base, cfg, ep, limit=2 * 1024 * 1024).decode("utf-8", "replace")
    if not text.lstrip().startswith("#EXTM3U"):
        head = text.lstrip()[:60].replace("\n", " ")
        raise RuntimeError("该地址返回的不是 m3u8（开头是「%s」），无法按分片下载" % head)
    if "#EXT-X-STREAM-INF" in text:
        cand = [l.strip() for l in text.splitlines()
                if l.strip() and not l.strip().startswith("#")]
        if cand:
            base = urllib.parse.urljoin(base, cand[-1])
            text = _http_bytes(base, cfg, ep, limit=2 * 1024 * 1024).decode("utf-8", "replace")
            if not text.lstrip().startswith("#EXTM3U"):
                raise RuntimeError("子播放列表不是 m3u8，源站可能已失效")

    tmpdir = tempfile.mkdtemp(prefix="dd_hls_")
    plan, segs = [], []
    pending = []
    n = 0
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("#EXT-X-KEY"):
            m = re.search(r'URI="([^"]+)"', s)
            if m:
                s = s.replace(m.group(1), urllib.parse.urljoin(base, m.group(1)))
            plan.append(("line", s))
            continue
        if s.startswith("#EXT-X-MAP"):
            m = re.search(r'URI="([^"]+)"', s)
            if m:
                name = "init_%05d.mp4" % n
                remote = urllib.parse.urljoin(base, m.group(1))
                plan.append(("map", s, name, remote))
                segs.append((name, remote))
                n += 1
            else:
                plan.append(("line", s))
            continue
        if s.startswith("#"):
            if s.startswith(("#EXT-X-ENDLIST", "#EXT-X-PLAYLIST-TYPE", "#EXT-X-DISCONTINUITY")):
                plan.append(("line", s))
            elif s.startswith("#EXTINF"):
                pending.append(s)
            continue
        name = "seg_%05d.ts" % n
        remote = urllib.parse.urljoin(base, s)
        plan.append(("seg", name, remote, pending))
        segs.append((name, remote))
        pending = []
        n += 1

    if not segs:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError("m3u8 里没有可用分片")

    total = len(segs)
    workers = max(1, min(16, int(cfg.get("seg_workers") or 6)))
    done = [0, 0]      # 已完成片数 / 已下字节数
    lock = threading.Lock()

    def one(item):
        name, u = item
        size = _fetch_to(u, os.path.join(tmpdir, name), cfg, ep, 3, stop_flag)
        with lock:
            done[0] += 1
            done[1] += size
            if task is not None:
                task["mode_text"] = "本地分段"
                task["progress"] = "本地分段 %d/%d 片 · %s" % (
                    done[0], total, human_size(done[1]))
                task["size_text"] = human_size(done[1])
            if st is not None:
                st["pct"] = min(95, int(done[0] * 100 / total))
            if done[0] % 5 == 0:
                save_tasks()
        return size

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(one, it) for it in segs]
            for f in as_completed(futs):
                f.result()      # 任一失败直接抛出，交给上层重试
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    out_lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:10",
                 "#EXT-X-MEDIA-SEQUENCE:0"]
    for item in plan:
        if item[0] == "line":
            out_lines.append(item[1])
        elif item[0] == "map":
            out_lines.append(item[1].replace(
                re.search(r'URI="([^"]+)"', item[1]).group(1), item[2]))
        else:
            out_lines.extend(item[3])
            out_lines.append(item[1])
    out_lines.append("#EXT-X-ENDLIST")
    local = os.path.join(tmpdir, "local.m3u8")
    with open(local, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out_lines) + "\n")
    log("本地分段完成：%d 片 / %s，并发 %d" % (total, human_size(done[1]), workers))
    return local, tmpdir


_MEDIA_FILE_EXT = (".mp4", ".m4v", ".ts", ".flv", ".mkv", ".mov", ".avi", ".webm")


def direct_download(task, ep, out_path, cfg, stop_flag, pause_flag=None, st=None):
    """原链接下载：不经过 ffmpeg，把地址的字节流原样落盘。

    两个用处：① 地址本身就是 mp4/ts 等媒体文件时最快；
    ② ffmpeg 打不开（编码/协议不兼容）但 HTTP 能取到时的兜底。
    先嗅探开头：是 m3u8 或网页就立刻失败，不浪费时间。
    """
    url = (ep.get("m3u8") or "").strip()
    if not url:
        raise RuntimeError("没有可用的下载地址")
    tmp_path = out_path + ".part"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    try:
        resp = _http_open(url, cfg, ep, timeout=30)
    except Exception as e:
        raise RuntimeError("原链接打不开：%s" % e)
    got = 0
    try:
        total_len = int(resp.headers.get("Content-Length") or 0)
        first = resp.read(65536)
        if not first:
            raise RuntimeError("原链接返回空内容")
        head = first.lstrip()[:16].upper()
        if head.startswith(b"#EXTM3U"):
            raise RuntimeError("该地址是 m3u8 播放列表，不是可直下的媒体文件")
        if head.startswith(b"<!DOCTYPE") or head.startswith(b"<HTML"):
            raise RuntimeError("该地址返回的是网页，不是视频文件")
        started = time.time()
        last_save = 0.0
        with open(tmp_path, "wb") as fh:
            fh.write(first)
            got = len(first)
            while True:
                if stop_flag.is_set():
                    raise TaskCancelled()
                if pause_flag is not None and pause_flag.is_set():
                    raise TaskPaused()
                if st is not None and st.get("st") == "paused":
                    raise EpPaused()
                chunk = resp.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - last_save >= 1.0:
                    last_save = now
                    rate = got / max(0.5, now - started)
                    pct = int(got * 100 / total_len) if total_len else 0
                    task["mode_text"] = "原链接"
                    task["speed_text"] = human_speed(rate)
                    task["size_text"] = human_size(got)
                    task["elapsed"] = human_duration(now - started)
                    task["ep_pct"] = pct
                    if total_len and rate > 0:
                        task["eta"] = human_duration((total_len - got) / rate)
                    if st is not None:
                        st["pct"] = min(99, pct)
                        st["size"] = human_size(got)
                        st["sp"] = task["speed_text"]
                    task["progress"] = "原链接下载 %s%s" % (
                        human_size(got),
                        (" / " + human_size(total_len)) if total_len else "")
                    save_tasks()
    except (TaskPaused, TaskCancelled, EpPaused):
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise RuntimeError("原链接下载失败：%s" % e)
    finally:
        try:
            resp.close()
        except Exception:
            pass
    if got < 1024:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise RuntimeError("原链接只下到 %d 字节，内容不完整" % got)
    os.replace(tmp_path, out_path)


def _resolve_ep_url(task, ep, cfg):
    """重新解析本集播放地址（签名 URL 会过期，换域名也靠这个救回来）。"""
    u = (task.get("url") or "").strip()
    if u.lower().split("?")[0].endswith(".m3u8"):
        return u  # 本来就是直链，无需重解析
    if task.get("source") == "cms" and task.get("cms_api") and ep.get("idx"):
        try:
            det = cms_detail(task["cms_api"], cfg, task["cms_id"], force=True)
            sources = det.get("sources") or []
            chosen = next((s for s in sources if s["from"] == task.get("play_from")),
                          sources[0] if sources else None)
            if chosen:
                idx = int(ep["idx"])
                if 1 <= idx <= len(chosen["episodes"]):
                    r = resolve_play_url(chosen["episodes"][idx - 1]["url"], cfg)
                    if r:
                        return r["url"]
        except Exception as e:
            log("cms 重新解析失败：%s" % e)
    if task.get("url"):
        try:
            fresh = parse_source(task["url"], cfg)
            i = int(ep.get("_i") or 0)
            if i < len(fresh["episodes"]):
                r = resolve_play_url(fresh["episodes"][i]["url"], cfg)
                if r:
                    return r["url"]
        except Exception as e:
            log("url 重新解析失败：%s" % e)
    return None


def download_episode(task, ep, out_path, cfg, stop_flag, pause_flag, st):
    """单集下载的降级梯子，按地址类型选顺序。

    m3u8：ffmpeg 普通 → ffmpeg 健壮 → 刷新地址 → 本地分段并发兜底 → 原链接直下
    直链媒体：原链接直下 → ffmpeg 普通 → ffmpeg 健壮 → 刷新地址
    """
    low = (ep.get("m3u8") or "").strip().lower().split("?")[0]
    is_hls = low.endswith(".m3u8") or not low.endswith(_MEDIA_FILE_EXT)
    plan = (["normal", "robust", "refresh", "local", "direct"] if is_hls
            else ["direct", "normal", "robust", "refresh"])
    last = None
    for i, mode in enumerate(plan):
        if stop_flag.is_set():
            raise TaskCancelled()
        if pause_flag is not None and pause_flag.is_set():
            raise TaskPaused()
        if st is not None and st.get("st") == "paused":
            raise EpPaused()
        try:
            if mode == "refresh":
                fresh = _resolve_ep_url(task, ep, cfg)
                if not fresh:
                    raise FFmpegError("刷新播放地址失败")
                ep["m3u8"] = fresh
                run_ffmpeg(task, ep, out_path, cfg, stop_flag, pause_flag, st, "normal")
            elif mode == "local":
                local_m3u8, tmpdir = hls_local_download(ep, cfg, stop_flag, task, st)
                try:
                    # 先带 aac_adtstoasc 合成（TS 分片必须），失败再去掉 bsf（fMP4 分片）
                    for v in ("local", "localraw"):
                        try:
                            run_ffmpeg(task, ep, out_path, cfg, stop_flag, pause_flag, st,
                                       v, src=local_m3u8)
                            break
                        except FFmpegError as fe:
                            if v == "localraw":
                                raise
                            log("本地合成(bsf)失败，去掉 bsf 重试：%s" % fe)
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)
            elif mode == "direct":
                direct_download(task, ep, out_path, cfg, stop_flag, pause_flag, st)
            else:
                run_ffmpeg(task, ep, out_path, cfg, stop_flag, pause_flag, st, mode)
            if i > 0:
                log("ep %s 第 %d 次尝试(%s)成功" % (ep.get("title"), i + 1, mode))
            return
        except (TaskPaused, TaskCancelled, EpPaused):
            raise
        except Exception as e:
            last = e
            log("ep %s 第 %d 次尝试(%s)失败：%s" % (ep.get("title"), i + 1, mode, e))
            if st is not None:
                st["err"] = str(e)[:140]
            task["last_error"] = str(e)[:160]
            save_tasks()
            if i < len(plan) - 1:
                time.sleep(min(6, 2 * (i + 1)))
    raise last if last else RuntimeError("下载失败")


def start_worker(tid):
    """同一任务只允许一个 worker 线程在跑，重复调用直接返回。"""
    with _RUNNING_LOCK:
        if tid in _RUNNING:
            return False
        _RUNNING.add(tid)
    threading.Thread(target=_worker_entry, args=(tid,), daemon=True).start()
    return True


def _worker_entry(tid):
    try:
        worker(tid)
    except Exception as e:
        log("worker %s crashed: %s" % (tid, e))
        t = _TASKS.get(tid)
        if t:
            t["status"] = "error"
            t["progress"] = "失败：%s" % e
            save_tasks()
    finally:
        with _RUNNING_LOCK:
            _RUNNING.discard(tid)


def is_running(tid):
    with _RUNNING_LOCK:
        return tid in _RUNNING


def ensure_ep_state(task):
    """每集一条状态记录，供前端显示与单集暂停/启动。"""
    eps = task.get("episodes") or []
    st = task.get("ep_state")
    if not isinstance(st, list) or len(st) != len(eps):
        st = [{"t": e.get("title", ""), "st": "pending"} for e in eps]
        task["ep_state"] = st
    else:
        for i, e in enumerate(eps):
            if not st[i].get("t"):
                st[i]["t"] = e.get("title", "")
    task["total"] = len(eps)
    return st


def _reset_downloading_to_pending(task):
    for s in (task.get("ep_state") or []):
        if s.get("st") == "downloading":
            s["st"] = "pending"
            s["pct"] = 0


def worker(task_id):
    task = _TASKS.get(task_id)
    if not task:
        return
    stop_flag = _STOP_FLAGS.setdefault(task_id, threading.Event())
    pause_flag = _PAUSE_FLAGS.setdefault(task_id, threading.Event())
    stop_flag.clear()
    cfg = load_config()
    root, dir_warn = resolve_download_root(cfg)
    if dir_warn:
        task["dir_warn"] = dir_warn
    slot = acquire_slot(cfg, task, stop_flag, pause_flag)
    if slot != "ok":
        if slot == "paused":
            task["status"] = "paused"
            task["progress"] = "已暂停（排队中）"
        else:
            task["status"] = "cancelled"
            task["progress"] = "已取消（排队中）"
        save_tasks()
        return
    try:
        # CMS 资源站任务：在下载线程内解析分集，避免 POST 请求超时
        if task.get("source") == "cms" and task.get("episodes") is None:
            task["status"] = "parsing"
            task["progress"] = "正在解析资源站分集..."
            save_tasks()
            det = cms_detail(task["cms_api"], cfg, task["cms_id"])
            sources = det["sources"]
            if not sources:
                raise ValueError("该视频没有可用播放源")
            chosen = next((s for s in sources if s["from"] == task.get("play_from")), sources[0])
            idxs = task.get("ep_indices") or list(range(1, len(chosen["episodes"]) + 1))

            def resolve_from(src):
                out = []
                for idx in idxs:
                    try:
                        idx = int(idx)
                    except Exception:
                        continue
                    if idx < 1 or idx > len(src["episodes"]):
                        continue
                    src_ep = src["episodes"][idx - 1]
                    resolved = resolve_play_url(src_ep["url"], cfg)
                    if not resolved:
                        log("cms ep %s unresolved: %s" % (idx, src_ep["url"][:80]))
                        continue
                    out.append({
                        "title": "%s - E%02d" % (det["title"], idx),
                        "series": det["title"],
                        "m3u8": resolved["url"],
                        "auth_key": "",
                        "auth_value": "",
                        "referer": resolved["referer"],
                        "idx": idx,
                    })
                return out

            eps = resolve_from(chosen)
            # 首选播放源整片解析失败时，自动换其它播放源再试一次
            if not eps and len(sources) > 1:
                for alt in sources:
                    if alt is chosen:
                        continue
                    log("换播放源重试：%s" % alt["from"])
                    eps = resolve_from(alt)
                    if eps:
                        task["play_from"] = alt["from"]
                        break
            if not eps:
                raise ValueError("所有分集播放地址均无法解析（已尝试 %d 个播放源）" % len(sources))
            task["episodes"] = eps
            task["series"] = det["title"]
            task["title"] = det["title"]
            task["total"] = len(eps)
            save_tasks()

        # 若存的是 URL 则先解析
        if task.get("episodes") is None and task.get("url"):
            task["status"] = "parsing"
            task["progress"] = "正在解析站点..."
            save_tasks()
            parsed = parse_source(task["url"], cfg)
            task["series"] = parsed["series"]
            task["episodes"] = parsed["episodes"]
            task["title"] = parsed["series"]
            task["total"] = len(parsed["episodes"])
            save_tasks()

        ep_state = ensure_ep_state(task)
        total = task["total"]
        outdir = os.path.join(root, task.get("series", "video"))
        task["status"] = "downloading"
        task.pop("paused", None)
        done = sum(1 for s in ep_state if s.get("st") == "done")

        for i, ep in enumerate(task.get("episodes", [])):
            if stop_flag.is_set():
                raise TaskCancelled()
            if pause_flag.is_set():
                raise TaskPaused()
            st = ep_state[i]
            if st.get("st") == "done":
                continue
            if st.get("st") == "paused":
                continue
            fname = sanitize(ep.get("title") or ("%s - E%02d" % (task.get("series"), i + 1))) + ".mp4"
            out_path = os.path.join(outdir, fname)
            st["st"] = "downloading"
            st["pct"] = 0
            st.pop("err", None)
            task["progress"] = "正在下载 %d/%d：%s" % (i + 1, total, fname)
            task["current"] = i + 1
            task["ep_pct"] = None
            task["speed_text"] = ""
            task["size_text"] = ""
            task["eta"] = "—"
            task["elapsed"] = "00:00"
            task["ff_speed"] = ""
            task["pct"] = int(done * 100 / total) if total else 0
            save_tasks()
            try:
                ep.setdefault("_i", i)
                download_episode(task, ep, out_path, cfg, stop_flag, pause_flag, st)
                st["st"] = "done"
                st["pct"] = 100
                st["sp"] = ""
                st.pop("err", None)
                try:
                    st["size"] = human_size(os.path.getsize(out_path))
                except Exception:
                    pass
                done += 1
                task["pct"] = int(done * 100 / total) if total else 100
                task["speed_text"] = ""
                task["ep_pct"] = None
                save_tasks()
            except EpPaused:
                st["st"] = "paused"
                st["pct"] = 0
                st["sp"] = ""
                save_tasks()
            except (TaskPaused, TaskCancelled):
                raise
            except Exception as e:
                st["st"] = "error"
                st["err"] = str(e)[:140]
                st["sp"] = ""
                task["last_error"] = str(e)[:200]
                save_tasks()

        # 收尾：区分全部完成 / 部分完成
        done = sum(1 for s in ep_state if s.get("st") == "done")
        bad = sum(1 for s in ep_state if s.get("st") == "error")
        paused_cnt = sum(1 for s in ep_state if s.get("st") == "paused")
        pending = sum(1 for s in ep_state if s.get("st") in ("pending", "downloading"))
        task["pct"] = int(done * 100 / total) if total else 100
        task["speed_text"] = ""
        task["ep_pct"] = None
        task["eta"] = "—"
        if stop_flag.is_set():
            task["status"] = "cancelled"
            task["progress"] = "已取消（完成 %d/%d 集）" % (done, total)
        elif pause_flag.is_set():
            _reset_downloading_to_pending(task)
            task["status"] = "paused"
            task["progress"] = "已暂停（完成 %d/%d 集）" % (done, total)
        elif bad or paused_cnt or pending:
            task["status"] = "partial"
            parts = ["完成 %d/%d 集" % (done, total)]
            if bad:
                parts.append("失败 %d" % bad)
            if paused_cnt:
                parts.append("暂停 %d" % paused_cnt)
            if pending:
                parts.append("未开始 %d" % pending)
            task["progress"] = " · ".join(parts)
        else:
            task["status"] = "done"
            task["progress"] = "完成 %d/%d 集" % (done, total)
            task["pct"] = 100
        log("task %s finished: %s" % (task_id, task["progress"]))
    except TaskPaused:
        _reset_downloading_to_pending(task)
        task["status"] = "paused"
        task["progress"] = "已暂停（第 %d/%d 集）" % (task.get("current") or 0, task.get("total") or 0)
        task["speed_text"] = ""
        task["ep_pct"] = None
        log("task %s paused" % task_id)
    except TaskCancelled:
        task["status"] = "cancelled"
        task["progress"] = "已取消"
        task["speed_text"] = ""
        task["ep_pct"] = None
        log("task %s cancelled" % task_id)
    except Exception as e:
        task["status"] = "error"
        task["progress"] = "失败：%s" % e
        log("task %s error: %s" % (task_id, e))
    finally:
        release_slot()
        task.pop("_last_prog", None)
        save_tasks()


# ---------------- HTTP Handler ----------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # 记录每个请求，便于排查前端资源路径问题
        try:
            log("HTTP %s" % (fmt % args))
        except Exception:
            pass

    def _send(self, code, body, ctype="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _path(self):
        p = urllib.parse.urlparse(self.path).path
        if p == PREFIX:
            return "/"
        if p.startswith(PREFIX + "/"):
            return p[len(PREFIX):]
        return p

    def do_GET(self):
        p = self._path()
        if p in ("/", "/index.html"):
            return self._serve_ui("index.html")
        # 浏览器会自动请求 /favicon.ico，直接给应用图标，避免控制台里一条 404
        if p in ("/favicon.ico", "/apple-touch-icon.png"):
            return self._serve_ui("images/icon_64.png")
        if p == "/app.js":
            return self._serve_ui("app.js")
        if p == "/style.css":
            return self._serve_ui("style.css")
        if p == "/hls.min.js":
            return self._serve_ui("hls.min.js")
        if p.startswith("/images/"):
            return self._serve_ui(p.lstrip("/"))
        if p == "/px":
            return self._proxy()
        if p == "/api/version":
            return self._json({"version": VERSION, "app": APPNAME,
                               "project_url": PROJECT_URL})
        if p == "/api/config":
            return self._json(load_config())
        if p == "/api/cms/sites":
            return self._json({"sites": cms_sites(),
                               "adult_enabled": bool(load_config().get("adult_enabled"))})
        if p == "/api/cms/sites/test":
            return self._json(site_test_snapshot())
        if p == "/api/cms/search-all":
            return self._json(search_snapshot())
        if p == "/api/paths":
            return self._json(paths_snapshot())
        if p == "/api/cms/sites/export":
            body = json.dumps(cms_sites(), ensure_ascii=False, indent=2)
            return self._send(200, body, "application/json; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="dramanest_sites.json"'})
        if p == "/api/tasks":
            with _TASKS_LOCK:
                items = sorted(_TASKS.values(), key=lambda t: t.get("created", 0), reverse=True)
            out = []
            for t in items:
                x = {k: v for k, v in t.items() if k != "episodes" and not k.startswith("_")}
                out.append(x)
            cfg = load_config()
            return self._json({"tasks": out, "active": _DL_ACTIVE, "limit": dl_limit(cfg)})
        if p.startswith("/api/task/"):
            tid = p[len("/api/task/"):]
            t = _TASKS.get(tid)
            return self._json(t) if t else self._json({"error": "not found"}, 404)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = self._path()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return self._json({"error": "invalid json"}, 400)

        if p == "/api/cms/sites":
            sites = data.get("sites")
            if not isinstance(sites, list):
                return self._json({"error": "sites 必须是数组"}, 400)
            clean = save_cms_sites(sites)
            return self._json({"ok": True, "sites": clean})

        if p == "/api/cms/sites/import":
            """导入源列表：mode=replace 覆盖，mode=merge 追加去重。"""
            try:
                payload = data.get("sites")
                if isinstance(data.get("json"), str):
                    payload = json.loads(data["json"])
                if not isinstance(payload, list):
                    return self._json({"error": "导入内容必须是站点数组"}, 400)
                mode = str(data.get("mode") or "merge").lower()
                incoming = [_normalize_site(x) for x in payload]
                incoming = [x for x in incoming if x]
                if mode == "replace":
                    final = save_cms_sites(incoming)
                else:
                    merged = cms_sites() + incoming
                    final = save_cms_sites(merged)
                return self._json({"ok": True, "count": len(final), "sites": final})
            except Exception as e:
                return self._json({"error": "解析失败：%s" % e}, 400)

        if p == "/api/cms/sites/test":
            apis = data.get("apis")
            if isinstance(apis, list) and apis:
                started = start_site_test([str(a) for a in apis],
                                          threads=int(data.get("threads") or 8))
            else:
                started = start_site_test(threads=int(data.get("threads") or 8))
            snap = site_test_snapshot()
            snap["ok"] = True
            snap["started_new"] = started
            return self._json(snap)

        if p == "/api/cms/sites/disable":
            apis = data.get("apis")
            if not isinstance(apis, list) or not apis:
                return self._json({"error": "请提供要操作的源"}, 400)
            clean = set_sites_disabled([str(a) for a in apis], bool(data.get("disabled", True)))
            return self._json({"ok": True, "sites": clean})

        if p == "/api/cms/search-all":
            kw = str(data.get("keyword") or "").strip()
            if not kw:
                return self._json({"error": "请输入关键词"}, 400)
            started = start_search_all(kw,
                                       per_site=int(data.get("per_site") or 20),
                                       threads=int(data.get("threads") or 8))
            snap = search_snapshot()
            snap["ok"] = True
            snap["started_new"] = started
            if not started and not snap["running"]:
                snap["error"] = "没有可用的源（可能全部被禁用）"
            return self._json(snap)

        if p == "/api/paths/probe":
            raw = str(data.get("path") or "").strip()
            if not raw.startswith("/"):
                return self._json({"error": "请填写绝对路径（以 / 开头）"}, 400)
            raw = os.path.normpath(raw)
            if data.get("create"):
                try:
                    os.makedirs(raw, exist_ok=True)
                except Exception as e:
                    st = path_status(raw)
                    st["error"] = "创建失败：%s" % e
                    st["ok"] = False
                    return self._json(st)
            st = path_status(raw)
            st["ok"] = bool(st["writable"])
            return self._json(st)

        if p == "/api/cms/list":
            try:
                api = str(data.get("api") or "").strip()
                check_site_usable(api)
                res = cms_list(api, load_config(),
                               page=int(data.get("page") or 1),
                               keyword=str(data.get("keyword") or "").strip(),
                               type_id=str(data.get("type_id") or "").strip())
                res["ok"] = True
                return self._json(res)
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if p == "/api/cms/detail":
            try:
                api = str(data.get("api") or "").strip()
                check_site_usable(api)
                res = cms_detail(api, load_config(), str(data.get("id") or "").strip())
                res["ok"] = True
                return self._json(res)
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if p == "/api/cms/links":
            try:
                api = str(data.get("api") or "").strip()
                check_site_usable(api)
                res = cms_links(api, load_config(), str(data.get("id") or "").strip(),
                                str(data.get("from") or "").strip())
                res["ok"] = True
                return self._json(res)
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if p == "/api/cms/resolve":
            try:
                api = str(data.get("api") or "").strip()
                check_site_usable(api)
                res = cms_resolve_ep(api, load_config(), str(data.get("id") or "").strip(),
                                     str(data.get("from") or "").strip(),
                                     data.get("index"), bool(data.get("force")))
                res["ok"] = True
                return self._json(res)
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if p == "/api/cms/download":
            try:
                api = str(data.get("api") or "").strip()
                vid = str(data.get("id") or "").strip()
                if not api.startswith(("http://", "https://")) or not vid:
                    return self._json({"error": "缺少资源站 API 或视频 ID"}, 400)
                check_site_usable(api)
                eps = data.get("episodes") or []
                if not isinstance(eps, list):
                    eps = []
                if len(eps) > 500:
                    return self._json({"error": "单次最多 500 集，请分批下载"}, 400)
                indices = []
                for x in eps:
                    try:
                        indices.append(int(x))
                    except Exception:
                        pass
                key = api + "|" + vid
                if data.get("skip_existing", True):
                    for t in _TASKS.values():
                        if t.get("status") == "done" and t.get("cms_key") == key:
                            return self._json({"ok": True, "created": 0, "skipped": 1})
                tid = uuid.uuid4().hex[:12]
                task = {
                    "id": tid, "source": "cms", "cms_api": api, "cms_id": vid,
                    "cms_key": key,
                    "play_from": str(data.get("play_from") or ""),
                    "ep_indices": indices,
                    "title": str(data.get("title") or ""),
                    "series": str(data.get("title") or ""),
                    "status": "queued", "progress": "排队中",
                    "created": time.time(), "total": len(indices),
                    "current": 0, "episodes": None,
                }
                _TASKS[tid] = task
                save_tasks()
                start_worker(tid)
                log("cms task %s: api=%s id=%s eps=%d" % (tid, api, vid, len(indices)))
                return self._json({"ok": True, "created": 1, "id": tid})
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if p == "/api/config":
            cfg = load_config()
            cfg.update({k: v for k, v in data.items() if k in default_config()})
            save_config(cfg)
            return self._json({"ok": True, "config": cfg})

        if p == "/api/parse":
            try:
                cfg = load_config()
                parsed = parse_source(data.get("url", ""), cfg)
                return self._json({"ok": True, "series": parsed["series"],
                                   "episodes": [{"title": e["title"]} for e in parsed["episodes"]]})
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if p == "/api/download":
            url = data.get("url", "").strip()
            if not url and not data.get("m3u8"):
                return self._json({"error": "请填写地址"}, 400)
            tid = uuid.uuid4().hex[:12]
            task = {
                "id": tid, "url": url, "m3u8": data.get("m3u8"),
                "title": data.get("title") or "", "status": "queued",
                "progress": "排队中", "created": time.time(), "total": 0, "current": 0,
                "episodes": None,
            }
            # 若是直接给的 m3u8，构造单集
            if not url and data.get("m3u8"):
                g = parse_generic(data["m3u8"], load_config())
                task["episodes"] = g["episodes"]
                task["series"] = data.get("title") or g["series"]
                task["total"] = len(g["episodes"])
            _TASKS[tid] = task
            save_tasks()
            start_worker(tid)
            return self._json({"ok": True, "id": tid})

        if p == "/api/scan":
            try:
                cfg = load_config()
                base = site_base(data.get("url"))
                if data.get("page"):
                    res = scan_page(base, cfg, max(1, int(data["page"])))
                else:
                    res = scan_site(base, cfg, max_pages=int(data.get("max_pages") or 0))
                res["ok"] = True
                return self._json(res)
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if p == "/api/download-batch":
            try:
                cfg = load_config()
                raw_urls = data.get("urls") or []
                if not isinstance(raw_urls, list) or not raw_urls:
                    return self._json({"error": "请先勾选视频"}, 400)
                if len(raw_urls) > 300:
                    return self._json({"error": "单次最多 300 个，请分批下载"}, 400)
                base = site_base(data.get("base") or str(raw_urls[0]))
                # 按视频 ID 去重，而不是完整 URL：同一部剧换域名后仍应识别为已下载
                existing = set()
                if data.get("skip_existing", True):
                    for t in _TASKS.values():
                        if t.get("status") == "done":
                            m0 = re.search(r"/video/(\d+)", str(t.get("url") or ""))
                            if m0:
                                existing.add(m0.group(1))
                created, skipped = [], 0
                for item in raw_urls:
                    if isinstance(item, dict):
                        u = str(item.get("url") or "").strip()
                        title = str(item.get("title") or "")
                    else:
                        u, title = str(item).strip(), ""
                    if not u:
                        continue
                    if re.fullmatch(r"\d+", u):
                        u = "%s/video/%s/" % (base, u)
                    if not re.search(r"/video/\d+", u):
                        continue
                    u = site_base(u) + urllib.parse.urlparse(u).path
                    mid = re.search(r"/video/(\d+)", u)
                    if mid and mid.group(1) in existing:
                        skipped += 1
                        continue
                    tid = uuid.uuid4().hex[:12]
                    task = {
                        "id": tid, "url": u, "m3u8": None,
                        "title": title, "status": "queued",
                        "progress": "排队中", "created": time.time(),
                        "total": 0, "current": 0, "episodes": None,
                    }
                    _TASKS[tid] = task
                    created.append(tid)
                    start_worker(tid)
                save_tasks()
                log("batch created %d tasks (skipped %d)" % (len(created), skipped))
                return self._json({"ok": True, "created": len(created),
                                    "skipped": skipped, "ids": created})
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if p.startswith("/api/pause/"):
            tid = p[len("/api/pause/"):]
            t = _TASKS.get(tid)
            if not t:
                return self._json({"error": "任务不存在"}, 404)
            _PAUSE_FLAGS.setdefault(tid, threading.Event()).set()
            if not is_running(tid):
                # 已停下的任务（排队/部分完成）直接置为暂停态
                if t.get("status") in ("queued", "parsing", "partial", "downloading"):
                    t["status"] = "paused"
                    t["progress"] = "已暂停"
                    save_tasks()
            return self._json({"ok": True})

        if p.startswith("/api/resume/"):
            tid = p[len("/api/resume/"):]
            t = _TASKS.get(tid)
            if not t:
                return self._json({"error": "任务不存在"}, 404)
            _PAUSE_FLAGS.setdefault(tid, threading.Event()).clear()
            _STOP_FLAGS.setdefault(tid, threading.Event()).clear()
            # 暂停态的分集恢复为待下载
            for s in (t.get("ep_state") or []):
                if s.get("st") == "paused":
                    s["st"] = "pending"
            if t.get("status") in ("paused", "error", "cancelled", "partial"):
                t["status"] = "queued"
                t["progress"] = "排队中"
                save_tasks()
            start_worker(tid)
            return self._json({"ok": True})

        if p.startswith("/api/retry/"):
            tid = p[len("/api/retry/"):]
            t = _TASKS.get(tid)
            if not t:
                return self._json({"error": "任务不存在"}, 404)
            if is_running(tid):
                return self._json({"error": "任务正在运行，请先暂停"}, 400)
            _PAUSE_FLAGS.setdefault(tid, threading.Event()).clear()
            _STOP_FLAGS.setdefault(tid, threading.Event()).clear()
            # 重试：把所有未完成的集数（失败/暂停/未开始）重置为待下载
            n = 0
            for s in (t.get("ep_state") or []):
                if s.get("st") != "done":
                    s["st"] = "pending"
                    s["pct"] = 0
                    s.pop("err", None)
                    n += 1
            t["status"] = "queued"
            t["progress"] = "重试中（%d 集待下载）" % n if n else "全部已完成"
            save_tasks()
            if n:
                start_worker(tid)
            return self._json({"ok": True, "pending": n})

        if p.startswith("/api/ep/"):
            tid = p[len("/api/ep/"):]
            t = _TASKS.get(tid)
            if not t:
                return self._json({"error": "任务不存在"}, 404)
            try:
                i = int(data.get("index"))
            except Exception:
                return self._json({"error": "缺少分集序号"}, 400)
            st = t.get("ep_state") or []
            if i < 0 or i >= len(st):
                return self._json({"error": "分集序号超出范围"}, 400)
            act = str(data.get("action") or "").lower()
            if act == "pause":
                if st[i].get("st") == "done":
                    return self._json({"error": "该集已完成"}, 400)
                st[i]["st"] = "paused"
                st[i]["pct"] = 0
                save_tasks()
                return self._json({"ok": True})
            if act == "start":
                st[i]["st"] = "pending"
                st[i]["pct"] = 0
                st[i].pop("err", None)
                # 启动单集时同时恢复任务级暂停，否则 worker 一进来就退出
                _PAUSE_FLAGS.setdefault(tid, threading.Event()).clear()
                _STOP_FLAGS.setdefault(tid, threading.Event()).clear()
                if t.get("status") in ("paused", "error", "cancelled", "partial", "queued"):
                    t["status"] = "queued"
                save_tasks()
                start_worker(tid)
                return self._json({"ok": True})
            return self._json({"error": "action 只能是 pause 或 start"}, 400)

        if p.startswith("/api/cancel/"):
            tid = p[len("/api/cancel/"):]
            _PAUSE_FLAGS.setdefault(tid, threading.Event()).clear()
            flag = _STOP_FLAGS.get(tid)
            if flag:
                flag.set()
            t = _TASKS.get(tid)
            if t and not is_running(tid):
                t["status"] = "cancelled"
                t["progress"] = "已取消"
                save_tasks()
            return self._json({"ok": True})

        if p.startswith("/api/delete/"):
            tid = p[len("/api/delete/"):]
            flag = _STOP_FLAGS.setdefault(tid, threading.Event())
            flag.set()
            _TASKS.pop(tid, None)
            save_tasks()
            return self._json({"ok": True})

        if p == "/api/clear-done":
            for tid in list(_TASKS):
                if _TASKS[tid].get("status") in ("done", "error", "cancelled"):
                    _TASKS.pop(tid, None)
            save_tasks()
            return self._json({"ok": True})

        return self._json({"error": "not found"}, 404)

    def _proxy(self):
        """本地取流代理：补 UA/Referer 绕过防盗链，并把 m3u8 改写成代理地址。

        浏览器直接向上游要分片会被跨域和 Referer 校验挡掉，这里统一代取。
        非 m3u8（ts/mp4/密钥）按字节流转发，支持 Range，便于拖动进度条。
        """
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        u = (qs.get("u") or [""])[0]
        ref = (qs.get("r") or [""])[0]
        if not u.startswith(("http://", "https://")):
            return self._json({"error": "地址无效"}, 400)
        cfg = load_config()
        pp = urllib.parse.urlparse(u)
        headers = {"User-Agent": cfg.get("ua") or UA, "Accept": "*/*"}
        headers["Referer"] = ref or ("%s://%s/" % (pp.scheme, pp.netloc))
        rng = self.headers.get("Range")
        if rng:
            headers["Range"] = rng
        try:
            req = urllib.request.Request(u, headers=headers)
            resp = urllib.request.urlopen(req, timeout=25)
        except Exception as e:
            return self._json({"error": "取流失败：%s" % e}, 502)
        try:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            is_hls = ("mpegurl" in ctype) or pp.path.lower().endswith(".m3u8")
            if is_hls:
                raw = resp.read()
                text = raw.decode("utf-8", "replace")
                if text.lstrip().startswith("#EXTM3U"):
                    body = px_rewrite(text, resp.geturl(), ref).encode("utf-8")
                    return self._send(200, body, "application/vnd.apple.mpegurl",
                                      {"Access-Control-Allow-Origin": "*"})
                return self._send(200, raw, ctype or "application/octet-stream",
                                  {"Access-Control-Allow-Origin": "*"})
            # 分片/整片：流式转发，避免把几百 MB 读进内存
            self.send_response(getattr(resp, "status", 200) or 200)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            cl = resp.headers.get("Content-Length")
            if cl:
                self.send_header("Content-Length", cl)
            cr = resp.headers.get("Content-Range")
            if cr:
                self.send_header("Content-Range", cr)
            self.end_headers()
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 用户拖动进度条会中断上一个分片请求，正常现象
        except Exception as e:
            log("proxy stream err (%s): %s" % (u[:90], e))
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _serve_ui(self, rel):
        path = os.path.join(HERE, "ui", rel)
        if not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        ext = os.path.splitext(rel)[1].lower()
        ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
                 ".ico": "image/x-icon"}.get(ext, "application/octet-stream")
        with open(path, "rb") as fh:
            self._send(200, fh.read(), ctype)


class UnixHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True

    def __init__(self, sock_path, handler):
        self.sock_path = sock_path
        super().__init__(sock_path, handler)

    def server_bind(self):
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass
        socketserver.TCPServer.server_bind(self)
        os.chmod(self.sock_path, 0o777)


def main():
    load_tasks()
    bootstrap_config()
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default="app.sock")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()

    # 端口模式：监听 TCP，供 fnOS 端口入口 / 浏览器直连。
    # 统一网关模式：同时监听 Unix Socket，二者共用同一 Handler，双兼容。
    servers = []
    if args.port:
        try:
            tcp = ThreadingHTTPServer((args.host, args.port), Handler)
            tcp.daemon_threads = True
            servers.append(tcp)
            log("listening TCP %s:%d" % (args.host, args.port))
        except OSError as e:
            log("TCP %s:%d 绑定失败(端口被占用?)，回退仅 unix socket: %s"
                % (args.host, args.port, e))

    sp = args.socket if os.path.isabs(args.socket) else os.path.join(HERE, args.socket)
    try:
        unix = UnixHTTPServer(sp, Handler)
        servers.append(unix)
        log("listening unix %s" % sp)
    except Exception as e:
        log("unix socket 监听失败(可忽略, 端口模式仍可用): %s" % e)

    if not servers:
        log("ERROR: 无可用监听方式")
        sys.exit(1)

    # 除第一个 server 在主线程跑，其余放后台线程
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
