#!/bin/bash
# 互传 · macOS / Linux 终端启动（零依赖，无需 .app / 菜单栏）
#   用法:  ./start.sh          前台运行，Ctrl+C 停止
#          PORT=9000 ./start.sh  指定首选端口（被占用自动顺延）
#   启动后自动打开本机控制台；手机/其他电脑用横幅里打印的局域网地址访问。
#   服务本体在 ../src/server.py（与 .app bundle 内是同一份源码）。
HERE="$(cd "$(dirname "$0")" && pwd)"
SERVER="$HERE/../src/server.py"
[ -f "$SERVER" ] || { echo "错误：找不到 $SERVER —— 请在项目目录结构下运行（scripts/ 与 src/ 需同级）"; exit 1; }

# 找 Python 3：AT_PYTHON 环境变量 → python3 → python。都没有就明说，别留下一个"没反应"。
PY=""
for c in "${AT_PYTHON:-}" \
         "$(command -v python3 2>/dev/null)" \
         "$(command -v python 2>/dev/null)"; do
  if [ -n "$c" ] && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "错误：没找到 Python 3（需 3.8+）。请先安装：https://www.python.org/downloads/"
  exit 1
fi

export PORT="${PORT:-8765}"
export PORT_SPAN="${PORT_SPAN:-20}"
export INBOX="${INBOX:-$HOME/Downloads/android-inbox}"
export OUTBOX="${OUTBOX:-$HOME/Downloads/android-outbox}"
export OPEN_CONSOLE=1

exec "$PY" "$SERVER"
