#!/bin/bash
# FileRelay（文件互传）.app 启动器：后台起服务 + 拉起菜单栏（AppleScriptObjC，零依赖）
HERE="$(cd "$(dirname "$0")" && pwd)"
RES="$HERE/../Resources"
# Python 解释器：默认用 PATH 里的 python3，可用环境变量 AT_PYTHON 覆盖（如指向 venv）
PY="${AT_PYTHON:-}"
[ -n "$PY" ] && [ -x "$PY" ] || PY="$(command -v python3)"
export INBOX="$HOME/Downloads/android-inbox"
# 首选端口：若被占用，服务端会自动向后顺延（8765 → 8766 → …），实际端口见菜单栏
export PORT="${PORT:-8765}"
# 顺延范围（默认向后找 20 个）；想限制就改小，例如 5
export PORT_SPAN="${PORT_SPAN:-20}"

RUNTIME="$HOME/.androidtransfer/runtime"

cleanup() {
  # 服务端收到 SIGTERM 会自行清掉运行时文件（只清自己写的）
  pkill -f "Resources/server.py" 2>/dev/null
}
trap cleanup EXIT

# 清理残留的旧实例，避免端口被占用
pkill -f "Resources/server.py" 2>/dev/null
sleep 0.4
# 顺手删掉上一轮遗留的运行时文件：否则菜单栏可能读到已失效的旧端口
rm -f "$RUNTIME"

# 后台启动接收服务（脱离终端，随应用退出而结束）
# 端口默认 8765，被占用时服务端会自动顺延并把实际端口写进运行时文件
nohup "$PY" "$RES/server.py" >/tmp/androidtransfer.log 2>&1 &

# 等服务把实际端口写出来（最多 ~5 秒），菜单栏一起步就能拿到正确端口
for _ in $(seq 1 50); do
  [ -f "$RUNTIME" ] && break
  sleep 0.1
done

# 拉起菜单栏（AppleScriptObjC 自带，无需 pip）
osascript "$RES/start.applescript" "$RES"
