#!/bin/bash
# ============================================================================
# 互传 · 一键构建（macOS .app）
#
#   图标重建 → 组装 .app bundle → ditto 打包 zip → 同步 /Applications → 重启
#
# 目录约定：
#   src/    构建源码   server.py / index.html / 使用手册.html / make_icon.py
#                      Info.plist / start.applescript / launcher.sh
#   scripts/ 跨平台启动脚本   start.sh（mac/Linux）/ start.bat（Windows）
#   docs/   文档与报告（不参与构建）
#   tests/  selftest.py 离线自测
#   dist/   构建产物   FileRelay.app + .app.zip + 图标（AppIcon.* / MenuIcon.*）
#
#   注意：.app 是纯产物，不要直接改 bundle 里的文件 —— 改 src/ 源码再跑本脚本。
# ============================================================================
set -euo pipefail
umask 022

cd "$(dirname "$0")"
HERE="$(pwd)"
SRC="$HERE/src"        # 构建源码
DIST="$HERE/dist"      # 构建产物（.app / zip / 图标）

APP_NAME="FileRelay"
APP="$DIST/$APP_NAME.app"
ZIP="$DIST/$APP_NAME.app.zip"
INSTALL_DIR="/Applications"
THEME="${THEME:-violet}"

DO_ICON=1        # 是否重建图标
DO_INSTALL=1     # 是否同步到 /Applications
DO_SIGN=0        # 是否 ad-hoc 签名
DO_CLEAN=0       # 只清理
FORCE_ICON=0

# 系统改写 bundle 时会产生 bundle container 临时文件 (.BC.T_*) 与 Finder 元数据，
# 它们不是构建产出，比对时必须排除，否则会误报"安装校验失败"
DIFF_X=(-x '.BC.T_*' -x '.DS_Store' -x '._*' -x '.localized' -x '__pycache__')

# ------------------------------------------------------------------ 输出
if [ -t 1 ]; then
  B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[0m'
  GRN=$'\033[32m'; YLW=$'\033[33m'; RED=$'\033[31m'; CYN=$'\033[36m'
else
  B=""; DIM=""; R=""; GRN=""; YLW=""; RED=""; CYN=""
fi
step() { printf '%s▸ %s%s\n' "$CYN$B" "$1" "$R"; }
ok()   { printf '  %s✓%s %s\n' "$GRN" "$R" "$1"; }
warn() { printf '  %s!%s %s\n' "$YLW" "$R" "$1"; }
die()  { printf '  %s✗ %s%s\n' "$RED" "$1" "$R" >&2; exit 1; }

# 系统改写 bundle 时会留下 .BC.T_* 影子文件。实测每次构建漏 ~12 个，长期累计过 120 个；
# 更糟的是有一次运行中，新内容**只**落进了影子、真名文件仍是旧版 —— 所以这里的清理不是
# 洁癖，是防线：清完紧跟着的逐文件校验才可信。
# 同样用 mv 进废纸篓（可回滚），不做递归删除。
SHADOW_TRASH=""
sweep_shadow() {
  root="$1"; n=0; f=""
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [ -z "$SHADOW_TRASH" ]; then
      SHADOW_TRASH="$HOME/.Trash/$APP_NAME-影子文件-$(date +%Y%m%d-%H%M%S)"
      mkdir -p "$SHADOW_TRASH"
    fi
    mv "$f" "$SHADOW_TRASH/" 2>/dev/null && n=$((n + 1))
  done < <(find "$root" -name '.BC.T_*' 2>/dev/null)
  if [ "$n" -gt 0 ]; then
    warn "清掉 $n 个 .BC.T_* 影子文件 → $SHADOW_TRASH"
  fi
  return 0
}

usage() {
  cat <<EOF
${B}FileRelay（文件互传）· 构建脚本${R}

用法：./build.sh [选项]

  ${B}(无参数)${R}        全套：图标 → 组装 → 打包 zip → 装到 /Applications 并重启
  --no-install     只构建，不碰 /Applications
  --no-icon        跳过图标重建（只改了 server.py / index.html 时用，快很多）
  --force-icon     强制重建图标（默认仅在缺失或 make_icon.py 更新时重建）
  --sign           ad-hoc 签名（换台 Mac 分发时有用；默认不签）
  --clean          删除全部构建产物（源码保留）
  -h, --help       显示本帮助

环境变量：
  THEME=violet     图标配色（blue / violet / teal）
EOF
}

for a in "$@"; do
  case "$a" in
    --no-install) DO_INSTALL=0 ;;
    --no-icon)    DO_ICON=0 ;;
    --force-icon) FORCE_ICON=1 ;;
    --sign)       DO_SIGN=1 ;;
    --clean)      DO_CLEAN=1 ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "未知参数：$a" >&2; usage >&2; exit 2 ;;
  esac
done

# python：默认 PATH 里的 python3，可用环境变量 AT_PYTHON 覆盖。
# 注意：图标重建需要 Pillow —— 默认解释器没装时，设 AT_PYTHON 指向带 Pillow 的
# 解释器，或用 --no-icon 跳过图标步骤。
PY="${AT_PYTHON:-$(command -v python3 || true)}"
[ -n "$PY" ] || die "找不到 python3"

# ============================================================== 清理模式
if [ "$DO_CLEAN" = 1 ]; then
  step "清理构建产物"
  # 移入废纸篓而非 rm -rf：删错了能捞回来（尤其是 .app 本身）
  TRASH_DIR="$HOME/.Trash/$APP_NAME-构建产物-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$TRASH_DIR"
  n=0
  # 用具名相对路径（不要用 $APP / $ZIP 这类绝对路径 —— 拼上 $DIST/ 就成了不存在的位置）
  for f in "$APP_NAME.app" "$APP_NAME.app.zip" ".$APP_NAME.app.bak" AppIcon.icns AppIcon_1024.png AppIcon.ico \
           MenuIcon.png MenuIcon@2x.png MenuIcon.style \
           icon_preview_blue.png icon_preview_teal.png icon_preview_violet.png \
           图标方案对比.png 菜单栏图标预览.png 扫码窗口预览.png 状态栏按钮实测.png \
           __pycache__; do
    [ -e "$DIST/$f" ] || continue
    mv "$DIST/$f" "$TRASH_DIR/" && ok "移入废纸篓 dist/${f}" && n=$((n + 1))
  done
  if [ "$n" = 0 ]; then
    rmdir "$TRASH_DIR" 2>/dev/null || true
    printf '\n没有构建产物可清理（源码一律不动）。\n'
  else
    printf '\n已清理 %s 项 → %s\n源码已保留，跑 %s./build.sh%s 可重建。\n' \
      "$n" "$TRASH_DIR" "$B" "$R"
  fi
  exit 0
fi

# ============================================================== 0. 前置检查
step "检查源码"
need=(server.py index.html 使用手册.html manual-en.html make_icon.py Info.plist start.applescript launcher.sh ui-console.png ui-phone.png ui-console-en.png ui-phone-en.png)
missing=()
for f in "${need[@]}"; do [ -f "$SRC/$f" ] || missing+=("$f"); done
[ ${#missing[@]} -eq 0 ] || die "缺少源文件（src/）：${missing[*]}"
ok "${#need[@]} 个源文件齐全"

for f in server.py make_icon.py; do
  # 用 compile() 在内存里查语法：不写 .pyc、不留 __pycache__，因此完全不需要删除操作
  "$PY" -c "import sys; compile(open(sys.argv[1], encoding='utf-8').read(), sys.argv[1], 'exec')" "$SRC/$f" \
    || die "$f 语法错误"
done
ok "Python 语法检查通过（无副作用，不留 __pycache__）"

plutil -lint "$SRC/Info.plist" >/dev/null || die "Info.plist 格式无效"
ok "Info.plist 格式有效"

# AppleScript 编译检查：写进临时文件（osacompile 没有纯检查模式），失败即中止
osacompile -o /tmp/atbuild_start_check.scpt "$SRC/start.applescript" 2>/tmp/atbuild_as.err \
  || { cat /tmp/atbuild_as.err >&2; die "start.applescript 编译失败"; }
ok "start.applescript 编译检查通过"

# ============================================================== 0.5 嵌入 Python（零依赖）
step "嵌入 Python 运行时"
if [ ! -x "$SRC/python-runtime/bin/python3" ]; then
  echo "  src/python-runtime 缺失，尝试自动下载（python-build-standalone）"
  bash "$SRC/fetch-python.sh" || die "无法获取嵌入式 Python，请手动运行 src/fetch-python.sh"
fi
[ -x "$SRC/python-runtime/bin/python3" ] || die "嵌入式 Python 缺失：$SRC/python-runtime/bin/python3"
ok "嵌入式 Python 就绪（$( "$SRC/python-runtime/bin/python3" --version 2>&1 )）"
# 二维码依赖自检：缺 qrcode/Pillow 时 /qr.png 会回退低清码，给出明确提醒
if "$SRC/python-runtime/bin/python3" -c "import qrcode, PIL" 2>/dev/null; then
  ok "二维码依赖就绪（qrcode + Pillow，/qr.png 出高清码）"
else
  warn "未检测到 qrcode/Pillow，正在补装（否则二维码会发虚）"
  "$SRC/python-runtime/bin/python3" -m pip install --no-cache-dir --quiet qrcode Pillow \
    || warn "补装失败，请手动：src/python-runtime/bin/python3 -m pip install qrcode Pillow"
fi

# 嵌入源是运行时产物，server.py 跑过会往里面写 __pycache__。构建前清掉，避免被 ditto
# 带进 bundle、又污染逐文件校验。运行时由 launcher 设 PYTHONDONTWRITEBYTECODE=1 不再产生。
find "$SRC/python-runtime" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true

# ============================================================== 1. 图标
mkdir -p "$DIST"
step "图标"
if [ "$DO_ICON" = 0 ]; then
  warn "按 --no-icon 跳过"
elif [ "$FORCE_ICON" = 0 ] && [ -f "$DIST/AppIcon.icns" ] \
     && [ "$DIST/AppIcon.icns" -nt "$SRC/make_icon.py" ]; then
  ok "AppIcon.icns 比 make_icon.py 新，跳过（要强制用 --force-icon）"
else
  # --no-preview 让图标脚本不产出配色对比/预览图 —— 那些只在人工挑配色时有用，
  # 不该每次构建都往产物目录里撒一份（也因此这里不再需要任何清理删除）
  # --out dist/：产物一律落 dist/，src/ 里只放源码
  "$PY" "$SRC/make_icon.py" --theme "$THEME" --icns --menu-icon --no-preview --out "$DIST" \
    >/tmp/_build_icon.log 2>&1 \
    || { tail -20 /tmp/_build_icon.log; die "图标生成失败（详见 /tmp/_build_icon.log）"; }
  for f in AppIcon.icns MenuIcon.png MenuIcon@2x.png MenuIcon.style; do
    [ -s "$DIST/$f" ] || die "图标产物缺失：$f"
  done
  ok "生成 AppIcon.icns（$(du -h "$DIST/AppIcon.icns" | cut -f1)）+ 菜单栏图标（$(cat "$DIST/MenuIcon.style" 2>/dev/null || echo '?')）"
  ok "已跳过配色预览图（AppIcon_1024.png 保留，它是 icns 的母版）"
fi
for f in AppIcon.icns MenuIcon.png MenuIcon@2x.png MenuIcon.style; do
  [ -s "$DIST/$f" ] || die "缺少图标文件 dist/$f（去掉 --no-icon 重跑）"
done

# ============================================================== 2. 组装 bundle
step "组装 $APP_NAME.app"
# 暂存目录放 /tmp（不是工作区）：万一清理被打断，残留只落在 /tmp，不会脏了用户的工作区。
# 清理用「逐个列出的文件 + rmdir」，不用 rm -rf（递归删除可能被批量删除保护拦下，反把脚本掐死）。
STAGE="$(mktemp -d /tmp/atbuild.XXXXXX)"
stage_clean(){
  [ -d "$STAGE" ] || return 0
  rm -f "$STAGE/$APP_NAME.app/Contents/Info.plist" \
        "$STAGE/$APP_NAME.app/Contents/MacOS/AndroidTransfer" \
        "$STAGE/$APP_NAME.app/Contents/Resources/"* 2>/dev/null
  rmdir "$STAGE/$APP_NAME.app/Contents/MacOS" \
        "$STAGE/$APP_NAME.app/Contents/Resources" \
        "$STAGE/$APP_NAME.app/Contents" "$STAGE/$APP_NAME.app" "$STAGE" 2>/dev/null || true
}
trap stage_clean EXIT
mkdir -p "$STAGE/$APP_NAME.app/Contents/MacOS"
RES="$STAGE/$APP_NAME.app/Contents/Resources"
mkdir -p "$RES"

cp "$SRC/Info.plist"         "$STAGE/$APP_NAME.app/Contents/Info.plist"
cp "$SRC/launcher.sh"        "$STAGE/$APP_NAME.app/Contents/MacOS/AndroidTransfer"
chmod 755                    "$STAGE/$APP_NAME.app/Contents/MacOS/AndroidTransfer"

cp "$SRC/start.applescript" "$SRC/server.py" "$SRC/index.html" "$SRC/使用手册.html" "$SRC/manual-en.html" "$SRC/favicon.png" "$RES/"
cp "$SRC/ui-console.png" "$SRC/ui-phone.png" "$SRC/ui-console-en.png" "$SRC/ui-phone-en.png" "$RES/"
cp "$DIST/AppIcon.icns" "$DIST/MenuIcon.png" "$DIST/MenuIcon@2x.png" "$DIST/MenuIcon.style" "$RES/"
chmod 644 "$RES"/* "$STAGE/$APP_NAME.app/Contents/Info.plist"

# 嵌入的 Python 运行时（零依赖）：整目录拷进 Resources/python。
# 必须放在上面的 chmod 644 之后，否则 bin/python3 会被改成败可执行。
ditto "$SRC/python-runtime" "$RES/python"
# 拷完立即清掉任何 __pycache__，保证 STAGE 干净、校验可比
find "$RES/python" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true

# 逐项验收，任何一项缺失都不落地
for f in Contents/Info.plist Contents/MacOS/AndroidTransfer \
         Contents/Resources/server.py Contents/Resources/index.html \
         Contents/Resources/使用手册.html Contents/Resources/manual-en.html \
         Contents/Resources/start.applescript \
         Contents/Resources/favicon.png Contents/Resources/ui-console.png \
         Contents/Resources/ui-phone.png Contents/Resources/ui-console-en.png \
         Contents/Resources/ui-phone-en.png \
         Contents/Resources/AppIcon.icns Contents/Resources/MenuIcon.png \
         Contents/Resources/MenuIcon@2x.png Contents/Resources/MenuIcon.style; do
  [ -s "$STAGE/$APP_NAME.app/$f" ] || die "bundle 缺文件：$f"
done
[ -x "$STAGE/$APP_NAME.app/Contents/MacOS/AndroidTransfer" ] || die "启动器无执行权限"
ok "10 个文件就位，权限正确"

if [ "$DO_SIGN" = 1 ]; then
  codesign --force --deep --sign - "$STAGE/$APP_NAME.app" >/dev/null 2>&1 \
    && ok "ad-hoc 签名完成" || warn "ad-hoc 签名失败（不影响本机运行）"
fi

# 落地：就地覆盖（ditto 合并进现有 bundle），**不要** rm -rf 整目录。
#
# 为什么不备份再硬删：
#   1) `rm -rf Xxx.app` 会触发批量删除保护（实测 53~59 个文件 > 阈值 50）而被中途掐断；
#   2) 更糟的是它发生在「旧 bundle 已移走、新 bundle 刚就位」之后 —— 脚本一死就留下
#      半成品 + 一个隐藏的 .bak 残留目录，环境反而是脏的。实测踩过一次，应用直接没了。
#   3) 覆盖式天然不误删。唯一代价：将来若改了文件名，旧文件会留下 —— 下面的校验专门抓它，
#      失败时把差异逐条打出来，照着清理即可。
ditto "$STAGE/$APP_NAME.app" "$APP" || die "写入 bundle 失败"
# 清理旧 bundle 里可能残留的 __pycache__（上一轮构建遗留），否则逐文件校验仍会报差异
find "$APP/Contents/Resources/python" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
sweep_shadow "$APP"
if ! diff -rq "${DIFF_X[@]}" "$STAGE/$APP_NAME.app" "$APP" >/dev/null 2>&1; then
  warn "bundle 校验不一致（下面列出的多为遗留在 bundle 里的旧文件）："
  diff -rq "${DIFF_X[@]}" "$STAGE/$APP_NAME.app" "$APP" 2>&1 | sed 's/^/    /' | head -20
  die "校验失败，请按上面提示清理 $APP 后重跑"
fi
ok "bundle 已更新并通过逐文件校验（$(du -sh "$APP" | cut -f1)）"

# ============================================================== 3. 打包 zip
step "打包 zip"
# 不删旧 zip：ditto 写到临时名再 mv 覆盖 —— mv 是重命名，不涉及任何删除操作
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP.tmp" || die "ditto 打包失败"
mv -f "$ZIP.tmp" "$ZIP" || die "替换 zip 失败"
ok "$(basename "$ZIP")（$(du -h "$ZIP" | cut -f1)）"

# ============================================================== 4. 安装
if [ "$DO_INSTALL" = 0 ]; then
  printf '\n%s构建完成%s（按 --no-install 未同步到 %s）\n' "$B$GRN" "$R" "$INSTALL_DIR"
  exit 0
fi

step "同步到 $INSTALL_DIR"
DEST="$INSTALL_DIR/$APP_NAME.app"

# 判定"是否在跑"必须锚定 bundle 内的启动器本身。
# 千万别用 pgrep -f "$APP_NAME.app"：那样任何命令行里提到过该路径的进程（比如刚跑完 diff 的
# shell）都会被误判成实例，实测踩过一次，会误杀无关进程并输出假警报。
LAUNCHER_MATCH="$APP_NAME\.app/Contents/MacOS/AndroidTransfer"
is_running() { pgrep -f "$LAUNCHER_MATCH" >/dev/null 2>&1; }

if is_running; then
  warn "检测到运行中的实例，先停掉"
  # 实际命令行里路径夹着 `..`（Contents/MacOS/../Resources/xxx），故用 `.*` 兜住两种形态
  pkill -f "$APP_NAME\.app.*MacOS/AndroidTransfer" 2>/dev/null || true
  pkill -f "$APP_NAME\.app.*Resources/server\.py" 2>/dev/null || true
  pkill -f "$APP_NAME\.app.*start\.applescript"   2>/dev/null || true
  for _ in $(seq 1 40); do is_running || break; sleep 0.1; done
  if is_running; then
    warn "启动器仍在运行，稍后可能抢端口（菜单栏点「退出」可手动收尾）"
  else
    ok "已停止"
  fi
fi

[ -w "$INSTALL_DIR" ] || die "$INSTALL_DIR 不可写（需把 app 拖进去过一次以授权）"

# 覆盖式安装：ditto 就地替换同名文件，不做整目录 rm -rf。
# 好处：不误删、不需要整目录删除权限；代价是若将来 bundle 里删改了文件名，旧文件会留在
# DEST 里 —— 下面的比对正是用来抓这种情况的（失败会明确报出差异）。
ditto "$APP" "$DEST" || die "拷贝到 $INSTALL_DIR 失败"
sweep_shadow "$DEST"
if ! diff -rq "${DIFF_X[@]}" "$APP" "$DEST" >/dev/null 2>&1; then
  warn "安装后校验不一致，差异如下（含 DEST 里多出的残留文件）："
  diff -rq "${DIFF_X[@]}" "$APP" "$DEST" 2>&1 | sed 's/^/    /' | head -20
  die "安装校验失败，请按上面提示手动清理 $DEST 后重跑"
fi
ok "已安装并通过逐文件校验"

step "启动"
# 不删运行时文件（.app 启动器自己会清）。改为要求它的 mtime 不早于本次启动时刻，
# 否则轮询会立刻读到上一次运行遗留的旧端口 —— 比删文件更稳，也少一次删除操作。
RUNTIME="$HOME/.androidtransfer/runtime"
LAUNCH_TS=$(date +%s)
open "$DEST"
for _ in $(seq 1 80); do
  if [ -f "$RUNTIME" ] && [ "$(stat -f %m "$RUNTIME" 2>/dev/null || echo 0)" -ge "$LAUNCH_TS" ]; then break; fi
  sleep 0.1
done

PORT="$(grep -o 'PORT=[0-9]*' "$RUNTIME" 2>/dev/null | cut -d= -f2 || true)"
if [ -z "$PORT" ]; then
  die "启动超时：8 秒内没写出运行时文件，看 /tmp/androidtransfer.log"
fi
ADDR="$(curl -s --max-time 3 "http://127.0.0.1:$PORT/addr" || true)"
[ -n "$ADDR" ] || die "服务未响应（端口 $PORT），看 /tmp/androidtransfer.log"

printf '\n%s构建并安装完成%s\n' "$B$GRN" "$R"
printf '  手机访问  %s%s%s\n' "$B" "$ADDR" "$R"
printf '  收件目录  ~/Downloads/android-inbox\n'
printf '  %s菜单栏图标 → 显示二维码，手机扫码即可。%s\n' "$DIM" "$R"
