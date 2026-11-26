#!/bin/bash
# DramaNest 桌面图标 / 网关诊断脚本
# 用法：把本文件传到 NAS 任意目录，chmod +x diagnose.sh && ./diagnose.sh
# 把全部输出贴给开发者即可。

APP="dramanest"
BASE="/var/apps/$APP"

echo "========== 1. 应用目录结构 =========="
ls -la "$BASE" 2>&1
echo "--- target 指向 ---"
readlink -f "$BASE/target" 2>&1
echo "--- target 内容 ---"
ls -la "$BASE/target/" 2>&1 | head -40

echo
echo "========== 2. 入口配置 ui/config =========="
cat "$BASE/target/ui/config" 2>&1

echo
echo "========== 3. manifest =========="
cat "$BASE/manifest" 2>&1

echo
echo "========== 4. 图标文件 =========="
ls -la "$BASE/ICON.PNG" "$BASE/ICON_256.PNG" "$BASE/target/ui/images/" 2>&1

echo
echo "========== 5. 运行状态 =========="
"$BASE/cmd/main" status 2>&1; echo "status 退出码: $?"
echo "--- 进程 ---"
ps aux 2>/dev/null | grep -E '[d]ramanest' | head -10
echo "--- socket ---"
ls -la "$BASE/target/app.sock" 2>&1
echo "--- 应用日志尾部 ---"
tail -30 "$BASE/var/app.log" 2>&1

echo
echo "========== 6. 端口探测（端口模式，关键）=========="
PORT=$(sed -n 's/.*service_port[[:space:]]*=[[:space:]]*\([0-9]*\).*/\1/p' "$BASE/manifest" 2>/dev/null | head -1)
echo "manifest service_port: ${PORT:-未声明}"
if [ -n "${PORT:-}" ]; then
  curl -s -o /dev/null -w "GET http://127.0.0.1:$PORT/api/version -> HTTP %{http_code}\n" "http://127.0.0.1:$PORT/api/version" 2>&1
  curl -s -o /dev/null -w "GET http://127.0.0.1:$PORT/ -> HTTP %{http_code}\n" "http://127.0.0.1:$PORT/" 2>&1
  echo "--- 端口监听情况 ---"
  (ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null) | grep ":$PORT " 2>&1
fi
echo "--- 直连 socket（网关模式兼容）---"
curl -s --unix-socket "$BASE/target/app.sock" -o /dev/null -w "unix socket / -> HTTP %{http_code}\n" "http://localhost/" 2>&1

echo
echo "========== 7. fnOS 入口注册数据库 =========="
echo "--- 查找 appentry / desktop 相关配置 ---"
find /var/apps /etc /vol1/@appdata -maxdepth 4 \( -iname '*entry*' -o -iname '*desktop*' -o -iname '*launch*' -o -iname '*appcenter*' \) 2>/dev/null | grep -iE 'dramanest|entry|desktop' | head -40

echo
echo "========== 诊断结束 =========="
