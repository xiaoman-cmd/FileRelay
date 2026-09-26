#!/usr/bin/env bash
# ============================================================================
# 拉取一个【可重定位】的 CPython（python-build-standalone），打进 .app 实现零依赖。
#
# 产物：src/python-runtime/{bin,lib,include,share}，其中 bin/python3 可被 launcher
#       直接调用——它用相对路径定位 libpython 与标准库，拷到任何位置都能跑。
#
# 按本机架构下载对应 install_only 包（python-build-standalone 不提供 universal2，
# 故 Intel 机下 x86_64、Apple Silicon 下 aarch64；要跨架构分发就在对应机器各构建一次）。
#
# 版本可覆盖：PBS_VERSION / PBS_DATE 环境变量。
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"          # -> src/
HERE="$(pwd)"
OUT="$HERE/python-runtime"

VER="${PBS_VERSION:-3.13.15}"
DATE="${PBS_DATE:-20260924}"

case "$(uname -m)" in
  x86_64)        ARCH=x86_64 ;;
  arm64|aarch64) ARCH=aarch64 ;;
  *) echo "不支持的架构: $(uname -m)" >&2; exit 1 ;;
esac

if [ -x "$OUT/bin/python3" ]; then
  echo "已存在 $OUT/bin/python3，跳过下载"
else
  mkdir -p "$OUT"
  TMP="$(mktemp -d)"
  URL="https://github.com/indygreg/python-build-standalone/releases/download/$DATE/cpython-$VER+$DATE-$ARCH-apple-darwin-install_only.tar.gz"

  echo "下载 $URL"
  curl -fL --retry 3 --max-time 300 -o "$TMP/pbs.tar.gz" "$URL" \
    || { echo "下载失败（可能需要代理或手动下载该包并解压到 $OUT）" >&2; rm -rf "$TMP"; exit 1; }

  echo "解压…"
  tar -xzf "$TMP/pbs.tar.gz" -C "$TMP"

  # install_only 包顶层是 python/ 目录；把它里面的内容搬到 OUT（兼容无顶层目录的情况）
  if [ -d "$TMP/python" ]; then
    mv "$TMP/python/"* "$OUT/" 2>/dev/null || true
  else
    mv "$TMP/"* "$OUT/" 2>/dev/null || true
  fi
  rm -rf "$TMP"
fi

"$OUT/bin/python3" --version \
  || { echo "嵌入 Python 不可用，请检查 $OUT" >&2; exit 1; }

# ----------------------------------------------------------------- 二维码依赖
# 服务端 /qr.png 用 qrcode+Pillow 产出高清位图（菜单栏/网页端共用）；
# 缺它们会回退到 CoreImage 的 1px/模块低清码，放大后发虚——所以必须随运行时打进 bundle。
echo "确保二维码依赖（qrcode + Pillow）…"
"$OUT/bin/python3" -c "import qrcode, PIL" 2>/dev/null || \
  "$OUT/bin/python3" -m pip install --no-cache-dir --quiet qrcode Pillow \
    || { echo "二维码依赖安装失败（/qr.png 将回退低清码，不影响基本传输）" >&2; }

echo "完成：嵌入 Python 位于 $OUT/bin/python3（含 qrcode + Pillow）"
