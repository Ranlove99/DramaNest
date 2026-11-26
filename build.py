#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DramaNest（剧巢）fpk 打包脚本。

用法：
    python3 build.py            # 同时构建 arm 与 x86 两个包
    python3 build.py arm        # 只构建 arm 包
    python3 build.py x86        # 只构建 x86 包

fpk 结构（gzip + tar）：
    manifest / cmd/* / config/* / wizard/* / ICON*.PNG / app.tgz
其中 app.tgz 为 gzip+tar，内含 dramanest.py 与 ui/*。
checksum = md5(app.tgz) 的原始字节。
"""

import hashlib
import io
import os
import shutil
import sys
import tarfile
import time
from gzip import GzipFile

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
DIST = os.path.join(ROOT, "dist")
APPNAME = "dramanest"
VERSION = "1.0"

# 固定时间戳，保证可复现构建（gzip 头 1f8b0808 00000000 02ff）
MTIME = 0


def _tar_gz_bytes(files, base, gzip_name="app.tgz"):
    """把 files[(arcname, diskpath)] 打成可复现的 gzip+tar 字节串。

    gzip_name 写入 gzip 头 FNAME 字段，使 FLG=0x08，与 fnOS 已验证包格式一致。
    """
    buf = io.BytesIO()
    with GzipFile(fileobj=buf, mode="wb", mtime=MTIME, compresslevel=9,
                  filename=gzip_name) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tf:
            # 与原包一致：显式写入各级目录项（fnOS 安装器依赖目录项识别结构）
            dirs = []
            for arcname, _ in files:
                parts = arcname.split("/")
                for i in range(1, len(parts)):
                    d = "/".join(parts[:i]) + "/"
                    if d not in dirs:
                        dirs.append(d)
            for d in dirs:
                ti = tarfile.TarInfo(d)
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                ti.mtime = MTIME
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = ""
                tf.addfile(ti)
            for arcname, diskpath in files:
                ti = tf.gettarinfo(diskpath, arcname=arcname)
                ti.mtime = MTIME
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = ""
                tf.addfile(ti, open(diskpath, "rb"))
    return buf.getvalue()


def _index_with_inline_css(ui_dir):
    """打包时把 style.css 内联进 index.html。

    统一网关只转发声明的 /app/dramanest 前缀，任何静态资源路径算错都会 404，
    表现出来就是一整页没有样式的裸 HTML。内联 CSS 后即使网关/缓存对
    style.css 做了什么奇怪的事，页面至少永远是完整样式。
    """
    src_html = os.path.join(ui_dir, "index.html")
    css_path = os.path.join(ui_dir, "style.css")
    if not os.path.isfile(css_path):
        return src_html
    with open(css_path, encoding="utf-8") as fh:
        css = fh.read()
    if "</style" in css.lower():  # css 里出现闭合标签就不内联，避免破坏 HTML
        return src_html
    with open(src_html, encoding="utf-8") as fh:
        html = fh.read()
    tag = '<link rel="stylesheet" href="style.css">'
    if tag not in html:
        return src_html
    html = html.replace(tag,
                        "<style>/* style.css 由 build.py 内联，避免网关下静态资源丢失 */\n"
                        + css + "\n</style>")
    os.makedirs(DIST, exist_ok=True)
    dst = os.path.join(DIST, ".index.inlined.html")
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(html)
    return dst


def collect_app_files():
    """收集 app.tgz 内容：主程序 + ui 全部文件。"""
    app_dir = os.path.join(SRC, "app")
    out = []
    out.append(("dramanest.py", os.path.join(app_dir, "dramanest.py")))
    # 与原包结构一致：app.tgz 内也带一份 config/（privilege、resource）
    app_cfg = os.path.join(app_dir, "config")
    if os.path.isdir(app_cfg):
        for n in sorted(os.listdir(app_cfg)):
            fp = os.path.join(app_cfg, n)
            if os.path.isfile(fp):
                out.append((os.path.join("config", n), fp))
    ui_dir = os.path.join(app_dir, "ui")
    inlined_index = None
    for root, dirs, names in os.walk(ui_dir):
        dirs.sort()
        names.sort()
        for n in names:
            full = os.path.join(root, n)
            arc = os.path.join("ui", os.path.relpath(full, ui_dir))
            if arc == os.path.join("ui", "index.html"):
                if inlined_index is None:
                    inlined_index = _index_with_inline_css(ui_dir)
                full = inlined_index
            out.append((arc, full))
    return out


def collect_fpk_files(platform, app_tgz_path):
    """收集 fpk 外层文件。"""
    out = []
    # manifest 单独处理（生成后临时落盘）
    out.append(("manifest", os.path.join(DIST, ".manifest.%s" % platform)))
    for sub in ("cmd", "config", "wizard"):
        base = os.path.join(SRC, sub)
        for root, dirs, names in os.walk(base):
            dirs.sort()
            names.sort()
            for n in names:
                full = os.path.join(root, n)
                arc = os.path.join(sub, os.path.relpath(full, base))
                out.append((arc, full))
    out.append(("ICON.PNG", os.path.join(SRC, "ICON.PNG")))
    out.append(("ICON_256.PNG", os.path.join(SRC, "ICON_256.PNG")))
    # 防火墙/端口协议文件（config/resource 的 port-config.protocol-file 引用）
    sc = os.path.join(SRC, "dramanest.sc")
    if os.path.isfile(sc):
        out.append(("dramanest.sc", sc))
    out.append(("app.tgz", app_tgz_path))
    return out


def build(platform):
    os.makedirs(DIST, exist_ok=True)
    print("[%s] 开始构建 ..." % platform)

    # 1) app.tgz
    app_files = collect_app_files()
    # 修正权限：cmd/* 755（app.tgz 内无 cmd，但 ui 保持普通权限）
    app_tgz = _tar_gz_bytes(app_files, SRC)
    app_tgz_path = os.path.join(DIST, ".app.tgz.%s" % platform)
    with open(app_tgz_path, "wb") as f:
        f.write(app_tgz)
    checksum = hashlib.md5(app_tgz).hexdigest()
    print("  app.tgz: %d 字节, md5=%s" % (len(app_tgz), checksum))

    # 2) manifest
    with open(os.path.join(SRC, "manifest.tmpl"), encoding="utf-8") as f:
        tmpl = f.read()
    manifest = tmpl.replace("{{PLATFORM}}", platform).replace("{{CHECKSUM}}", checksum)
    mf_path = os.path.join(DIST, ".manifest.%s" % platform)
    with open(mf_path, "w", encoding="utf-8") as f:
        f.write(manifest)

    # 3) cmd/* 权限 755
    cmd_dir = os.path.join(SRC, "cmd")
    for n in os.listdir(cmd_dir):
        p = os.path.join(cmd_dir, n)
        if os.path.isfile(p):
            os.chmod(p, 0o755)

    # 4) fpk
    fpk_files = collect_fpk_files(platform, app_tgz_path)
    fpk_bytes = _tar_gz_bytes(fpk_files, SRC)
    fpk_name = "DramaNest_%s_%s.fpk" % (VERSION, platform)
    fpk_path = os.path.join(DIST, fpk_name)
    with open(fpk_path, "wb") as f:
        f.write(fpk_bytes)
    sha = hashlib.sha256(fpk_bytes).hexdigest()
    print("  %s: %d 字节, sha256=%s" % (fpk_name, len(fpk_bytes), sha))

    # 清理临时文件
    for p in (app_tgz_path, mf_path):
        try:
            os.unlink(p)
        except OSError:
            pass
    return fpk_path


def verify(fpk_path):
    """验证 fpk：gzip 头、结构、checksum、cmd 权限。"""
    print("  校验 %s ..." % os.path.basename(fpk_path))
    with open(fpk_path, "rb") as f:
        head = f.read(10)
    assert head[:3] == b"\x1f\x8b\x08", "gzip 头错误"
    assert head[3] == 0x08, "FLG 错误（应为 FNAME 或 0）"
    assert head[4:8] == b"\x00\x00\x00\x00", "mtime 非 0，不可复现"
    assert head[8] == 0x02, "XFL 错误"
    with tarfile.open(fpk_path, "r:gz") as tf:
        names = tf.getnames()
        for must in ("manifest", "app.tgz", "cmd/main", "config/privilege",
                     "wizard/install", "ICON.PNG"):
            assert must in names, "缺少文件 " + must
        mf = tf.extractfile("manifest").read().decode("utf-8")
        declared = ""
        for line in mf.splitlines():
            if line.strip().startswith("checksum"):
                declared = line.split("=")[1].strip()
        app_bytes = tf.extractfile("app.tgz").read()
        actual = hashlib.md5(app_bytes).hexdigest()
        assert declared == actual, "checksum 不符: manifest=%s actual=%s" % (declared, actual)
        main_mode = tf.getmember("cmd/main").mode
        assert main_mode & 0o111, "cmd/main 无执行权限"
    print("  ✔ 校验通过（gzip 头 / 结构 / checksum / 执行权限）")


def main():
    targets = sys.argv[1:] or ["arm", "x86"]
    for p in targets:
        if p not in ("arm", "x86"):
            print("未知架构:", p)
            sys.exit(2)
    for p in targets:
        fpk = build(p)
        verify(fpk)
    print("\n构建完成，输出目录：%s" % DIST)


if __name__ == "__main__":
    main()
