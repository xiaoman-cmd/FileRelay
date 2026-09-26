#!/bin/bash
# FileRelay（文件互传）.app 启动器：后台起服务 + 拉起菜单栏（AppleScriptObjC，零依赖）
#
# 为什么重写（解决"双击没反应"）：
#   1) GUI 双击启动只继承极简 PATH，python3 往往不在里面（Homebrew 装在 /usr/local、
#      Apple Silicon 的 /opt/homebrew、pyenv 在 ~/.pyenv）。这里做多路径探测，找不到就
#      弹一个【可见对话框】说明，而不是让菜单栏静默"假死"。
#   2) 本程序是菜单栏代理（LSUIElement=true），不会弹主窗口。首次启动弹一次提示，
#      告诉用户去屏幕右上角看图标，避免误以为程序没打开。
#   3) 服务端若在 5 秒内没就绪，弹【可见对话框】并附日志路径，便于排查。

HERE="$(cd "$(dirname "$0")" && pwd)"
RES="$HERE/../Resources"

# 启动器诊断日志：CLI 下看不见 stdout，统一写这里，方便排查"没反应"
LOG_LAUNCHER="$HOME/Library/Logs/FileRelay-launch.log"
mkdir -p "$(dirname "$LOG_LAUNCHER")" 2>/dev/null
log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_LAUNCHER" 2>/dev/null; }

# ----------------------------------------------------- 可见对话框（无窗环境也安全）
# 通过临时 UTF-8 文件把中文/换行原样传给 AppleScript，避免引号转义与乱码
dialog() {
  local title="$1" msg="$2" tmpf
  tmpf="$(mktemp /tmp/_fr_dialog.XXXXXX)" || return 1
  printf '%s' "$msg" >"$tmpf"
  osascript <<EOF 2>/dev/null
set f to POSIX file "$tmpf"
try
    set msgText to read f as «class utf8»
on error
    set msgText to "文件互传 启动遇到问题，请查看 $HOME/Library/Logs/FileRelay-launch.log"
end try
display dialog msgText with title "$title" buttons {"知道了"} default button 1
EOF
  rm -f "$tmpf"
}

# ----------------------------------------------------- 1. 找 Python 解释器
# 优先级：① bundle 自带嵌入式 Python（零依赖，最优先）② AT_PYTHON ③ 系统/常见路径探测
PY="${AT_PYTHON:-}"
# bundle 自带的嵌入式 Python（已打进 Resources/python，无需用户安装任何东西）
EMBED="$RES/python/bin/python3"
if [ -z "$PY" ] && [ -x "$EMBED" ]; then
  PY="$EMBED"
fi

probe_python() {
  local cand="$1"
  [ -n "$cand" ] && [ -x "$cand" ] || return 1
  local v mj mi
  v="$("$cand" --version 2>&1)" || return 1
  mj="$(printf '%s' "$v" | grep -oE '[0-9]+' | head -1)"
  mi="$(printf '%s' "$v" | grep -oE '[0-9]+\.[0-9]+' | head -1 | cut -d. -f2)"
  [ -n "$mj" ] || return 1
  [ "$mj" -ge 3 ] 2>/dev/null || return 1
  # 需要 Python 3.8+（服务端用到 3.8 语法/标准库）
  if [ "$mj" -eq 3 ] && [ "${mi:-0}" -lt 8 ] 2>/dev/null; then return 1; fi
  return 0
}

if [ -z "$PY" ] || ! probe_python "$PY"; then
  PY=""
  for cand in \
    /usr/bin/python3 \
    /usr/local/bin/python3 \
    /opt/homebrew/bin/python3 \
    /opt/local/bin/python3 \
    "$HOME/.pyenv/shims/python3" \
    "$(command -v python3 2>/dev/null)" ; do
    [ -z "$cand" ] && continue
    if probe_python "$cand"; then PY="$cand"; break; fi
  done
fi

if [ -z "$PY" ]; then
  log "FATAL: 未找到 Python 3.8+"
  dialog "文件互传 · 启动失败" \
"文件互传 无法启动：本机没有找到 Python 3.8 及以上版本。

请先安装 Python 3.8+：
  • 官网 python.org 下载安装包，或
  • Homebrew：brew install python
装好后重新打开本程序。"
  exit 1
fi
log "使用 Python: $PY ($("$PY" --version 2>&1))"

# 不写 .pyc 缓存：① bundle 保持干净、可放在只读的系统卷 /Applications 上；
# ② 避免解释器每次运行往 .app 里写文件（既污染校验，又可能因无写权限启动失败）
export PYTHONDONTWRITEBYTECODE=1

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

# ----------------------------------------------------- 2. 后台启动接收服务
# 端口默认 8765，被占用时服务端会自动顺延并把实际端口写进运行时文件
nohup "$PY" "$RES/server.py" >/tmp/androidtransfer.log 2>&1 &
SERVER_PID=$!

# 等服务把实际端口写出来（最多 ~5 秒）；若进程已退出说明启动失败
for _ in $(seq 1 50); do
  [ -f "$RUNTIME" ] && break
  kill -0 "$SERVER_PID" 2>/dev/null || break
  sleep 0.1
done

if [ ! -f "$RUNTIME" ]; then
  log "FATAL: 服务在 5 秒内未就绪（server.py 进程仍在=$(kill -0 "$SERVER_PID" 2>/dev/null && echo 是 || echo 否)）"
  dialog "文件互传 · 服务未启动" \
"文件互传 服务启动失败。

诊断日志：$HOME/Library/Logs/FileRelay-launch.log
服务日志：/tmp/androidtransfer.log

常见原因：
  • Python 版本低于 3.8（上面日志里有实际版本）
  • 8765~8784 端口被别的程序占用"
  exit 1
fi
log "服务已就绪：$(cat "$RUNTIME" 2>/dev/null | tr '\n' ' ')"

# ----------------------------------------------------- 3. 首次启动提示
# 菜单栏程序不会弹主窗口，容易误以为"没反应"。首启弹一次，指向右上角图标。
HINT_FLAG="$HOME/.androidtransfer/.launched"
if [ ! -f "$HINT_FLAG" ]; then
  dialog "文件互传 已启动" \
"文件互传 已在菜单栏启动。

屏幕右上角（状态栏）会出现一个小小的图标，点它即可显示二维码、收发文件。

这是菜单栏程序，不会弹出主窗口——这是正常现象。"
  touch "$HINT_FLAG" 2>/dev/null
fi

# ----------------------------------------------------- 4. 拉起菜单栏（AppleScriptObjC，零依赖）
# 菜单栏进程退出码：0 = 用户主动退出（quitApp）；非 0 = 异常崩溃。
# 无论哪种，launcher 都随它退出（trap 会清掉 server）。异常时把线索写进诊断日志，
# 避免"整个 app 静默消失、用户以为没反应"却毫无头绪。
osascript "$RES/start.applescript" "$RES"
RC=$?
if [ "$RC" -ne 0 ]; then
  log "菜单栏进程退出异常（osascript rc=$RC）。若你并未点「退出」，请查看崩溃报告：~/Library/Logs/DiagnosticReports/osascript*.ips"
fi
