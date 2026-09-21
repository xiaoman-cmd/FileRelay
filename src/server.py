#!/usr/bin/env python3
"""互传 · 局域网文件 / 剪贴板收发服务。

零必需依赖，只用标准库。qrcode + Pillow 仅在生成二维码位图时用到，缺失会自动降级。
服务端可跑在 macOS / Windows / Linux 上；客户端是浏览器 —— 手机、平板、电脑都行，
对方不需要安装任何东西。
"""
import http.server
import socketserver
import os
import sys
import socket
import io
import time
import json
import signal
import subprocess
import secrets
import tempfile
import threading
import mimetypes
import zipfile
import shutil
import platform
import urllib.parse
from pathlib import Path

# ================================================================ 平台层
# 全部平台差异集中在这一块，业务代码里不再出现 sys.platform 判断。
# 要支持新平台、或改某个平台能力，只改这里 —— 别把分支散回业务逻辑里去。
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")
IS_LINUX = not (IS_MAC or IS_WIN)      # 其余按 Linux 处理（POSIX 兼容平台都能用 xdg-open/notify-send）

HOST_KIND = "mac" if IS_MAC else ("win" if IS_WIN else "linux")
HOST_LABEL = {"mac": "macOS", "win": "Windows", "linux": "Linux"}[HOST_KIND]

# 展示用应用名（通知标题、页面标题）。与 .app bundle 名无关，改它不影响打包路径。
APP_NAME = os.environ.get("APP_NAME", "FileRelay")


def _host_name():
    """本机名，页面上的「从 X 收」用它。取不到就退回平台名，绝不返回空串 ——
    空串会让页面出现「从  收」这种破句。"""
    try:
        n = (platform.node() or "").strip()
        if n:
            # Windows 的 node() 一般是 NetBIOS 名；POSIX 上可能带 .local 后缀
            return n.split(".")[0][:32]
    except Exception:
        pass
    return HOST_LABEL


HOST_NAME = _host_name()


def open_in_file_manager(path):
    """在系统文件管理器里打开目录。Windows 的 explorer 即使成功也返回非 0，故一律不检查返回码。"""
    p = str(path)
    try:
        if IS_WIN:
            subprocess.Popen(["explorer", p])
        elif IS_MAC:
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
        return True
    except Exception as e:
        print(f"[open] 打开目录失败 {p}: {e!r}", flush=True)
        return False


def open_url(url):
    """用系统默认浏览器打开 URL（start.sh / start.bat 启动时自动打开本机控制台用）。"""
    try:
        if IS_WIN:
            os.startfile(url)  # noqa: PTH654 —— Windows 打开 URL 的原生方式
        elif IS_MAC:
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url])
        return True
    except Exception as e:
        print(f"[open] 打开浏览器失败 {url}: {e!r}", flush=True)
        return False


# ---------------------------------------------------------------- 剪贴板
# Windows 刻意不用 clip.exe：它按控制台代码页解释标准输入，中文会变成乱码。
# 直接调 Win32 API 长一点，但没有编码歧义、不依赖外部进程、不需要 PowerShell。

def _win_clip_setup():
    """集中声明 argtypes/restype —— 不声明的话，64 位下 HANDLE 会被当成 int32 截断。"""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    u.OpenClipboard.argtypes = [wintypes.HWND]
    u.OpenClipboard.restype = wintypes.BOOL
    u.EmptyClipboard.restype = wintypes.BOOL
    u.CloseClipboard.restype = wintypes.BOOL
    u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u.SetClipboardData.restype = wintypes.HANDLE
    u.GetClipboardData.argtypes = [wintypes.UINT]
    u.GetClipboardData.restype = wintypes.HANDLE
    k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k.GlobalAlloc.restype = wintypes.HGLOBAL
    k.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k.GlobalLock.restype = wintypes.LPVOID
    k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k.GlobalUnlock.restype = wintypes.BOOL
    k.GlobalFree.argtypes = [wintypes.HGLOBAL]
    k.GlobalFree.restype = wintypes.HGLOBAL
    return ctypes, u, k


def _win_clip_open(u):
    """剪贴板可能被别的进程短暂占用 —— Win32 上这是常态，要重试而不是直接放弃。"""
    for _ in range(5):
        if u.OpenClipboard(None):
            return True
        time.sleep(0.05)
    return False


def _win_clip_write(text):
    try:
        ctypes, u, k = _win_clip_setup()
    except Exception:
        return False
    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
    if not _win_clip_open(u):
        return False
    try:
        if not u.EmptyClipboard():
            return False
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        h = k.GlobalAlloc(GMEM_MOVEABLE, size)
        if not h:
            return False
        p = k.GlobalLock(h)
        if not p:
            k.GlobalFree(h)
            return False
        ctypes.memmove(p, buf, size)
        k.GlobalUnlock(h)
        if not u.SetClipboardData(CF_UNICODETEXT, h):
            k.GlobalFree(h)
            return False
        return True          # 成功后内存所有权归系统，不能再 GlobalFree
    except Exception:
        return False
    finally:
        u.CloseClipboard()


def _win_clip_read():
    try:
        ctypes, u, k = _win_clip_setup()
    except Exception:
        return ""
    CF_UNICODETEXT = 13
    if not _win_clip_open(u):
        return ""
    try:
        h = u.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""        # 剪贴板里不是文本（图片/文件）时就是这个结果，属正常
        p = k.GlobalLock(h)
        if not p:
            return ""
        try:
            return ctypes.wstring_at(p)
        finally:
            k.GlobalUnlock(h)
    except Exception:
        return ""
    finally:
        u.CloseClipboard()


_LINUX_CLIP = {}        # 缓存探测结果：未命中键=没探测过，False=确认不可用


def _linux_clip(read):
    """Wayland 优先，其次 X11。都没有返回 None —— 调用方降级即可，不该报错：
    剪贴板工具在多数桌面发行版是可选安装的。"""
    key = "r" if read else "w"
    if key in _LINUX_CLIP:
        return _LINUX_CLIP[key] or None
    if read:
        cands = [["wl-paste", "--no-newline"],
                 ["xclip", "-selection", "clipboard", "-o"],
                 ["xsel", "--clipboard", "--output"]]
    else:
        cands = [["wl-copy"],
                 ["xclip", "-selection", "clipboard"],
                 ["xsel", "--clipboard", "--input"]]
    for c in cands:
        if shutil.which(c[0]):
            _LINUX_CLIP[key] = c
            return c
    _LINUX_CLIP[key] = False
    return None


def _ps_quote(s):
    """PowerShell 单引号字符串转义：内部单引号写成两个。"""
    return "'" + str(s).replace("'", "''") + "'"


def _win_toast(title, msg):
    """Windows 通知只能借 PowerShell 调 WinRT（标准库没有等价能力）。
    局限要说清楚：未注册 AppUserModelID 时，部分系统版本会静默不显示 —— 所以这是
    「尽力而为」，失败不报错。真正可靠的反馈是控制台页面上的「最近收到」，那个一定准。"""
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x=$t.GetElementsByTagName('text');"
        f"$x.Item(0).AppendChild($t.CreateTextNode({_ps_quote(title)}))|Out-Null;"
        f"$x.Item(1).AppendChild($t.CreateTextNode({_ps_quote(msg)}))|Out-Null;"
        "$toast=[Windows.UI.Notifications.ToastNotification]::new($t);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier().Show($toast);"
    )
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),   # 别闪黑框
    )


PORT = int(os.environ.get("PORT", "8765"))
PORT_SPAN = int(os.environ.get("PORT_SPAN", "20"))   # 端口被占用时向前探测的范围


def _pick_base_dir():
    """选一个用来存放收件 / 发件目录的父目录。

    默认用「下载」。但 Windows 上「下载」可能被 OneDrive 重定向到
    C:\\Users\\<用户>\\OneDrive\\Downloads —— 这时 Path.home()/"Downloads" 并不存在，
    原来的写法会直接 mkdir 出一个新目录：目录建起来了、服务也「正常」，
    用户却永远找不到收到的文件。这是最典型的一类静默错位，所以只在父目录
    确实存在时才采用它。
    """
    home = Path.home()
    cands = [home / "Downloads", home / "下载"]
    if IS_WIN:
        cands += [home / "OneDrive" / "Downloads", home / "Desktop"]
    else:
        cands += [home / "Desktop", home / "桌面"]
    cands += [home / "Documents", home]       # 兜底：主目录一定存在
    for c in cands:
        try:
            if c.is_dir():
                return c
        except OSError:
            continue
    return home


_BASE_DIR = _pick_base_dir()
INBOX = Path(os.environ.get("INBOX", _BASE_DIR / "android-inbox"))
OUTBOX = Path(os.environ.get("OUTBOX", _BASE_DIR / "android-outbox"))
TOKEN = os.environ.get("TOKEN", "").strip()
TOKEN_FILE = Path.home() / ".androidtransfer_token"
# 请求体上限：/upload 与 /outbox 投递 2 GiB，纯文本接口 1 MiB。
# 不设上限时全量 read() 入内存，一个恶意大 Content-Length 就能把进程打到 OOM。
MAX_UPLOAD = 2 * 1024 * 1024 * 1024
MAX_TEXT = 1 * 1024 * 1024
# 运行时信息（实际端口等）写在这里，控制台 / 菜单栏读它来定位服务
# AT_RUNTIME_DIR 只为测试预留：起临时实例时改到别处，免得覆盖正式实例的 runtime / 开关文件
RUNTIME_DIR = Path(os.environ.get("AT_RUNTIME_DIR", str(Path.home() / ".androidtransfer")))
RUNTIME_FILE = RUNTIME_DIR / "runtime"
# 目录建不出来时只告警、不中断启动 —— 服务起得来，页面才有机会把问题显示给用户
for _d in (INBOX, OUTBOX):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[目录] 无法创建 {_d}: {e!r}", flush=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(BASE_DIR, "index.html")


def resolve_token():
    """TOKEN=auto 时从文件读取/生成稳定口令，避免每次重启都变。"""
    global TOKEN
    if TOKEN == "auto":
        if TOKEN_FILE.exists():
            TOKEN = TOKEN_FILE.read_text().strip()
        else:
            TOKEN = secrets.token_hex(4)
            TOKEN_FILE.write_text(TOKEN)
    return TOKEN


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def current_url():
    """手机应访问的完整地址（开 TOKEN 时带上 ?token=，二维码/复制地址可直接用）。"""
    url = f"http://{get_lan_ip()}:{PORT}/"
    if TOKEN:
        url += f"?token={TOKEN}"
    return url


# ---------------------------------------------------------------- 端口自动选择
# 场景：8765 被别的程序占了（或上一次残留实例还在），原来会直接崩溃/静默失败。
# 现在依次探测 preferred、preferred+1 …，取第一个能真正绑上的端口，并把实际
# 端口写进运行时文件，菜单栏据此定位服务（换端口后二维码/复制地址自动跟随）。
def _listen_sockopt(s):
    """给监听/探测用的 socket 设平台正确的选项。

    ★ 这里有一个**只在 Windows 上暴露**的坑，改这个函数前务必读完 ★

    POSIX 的 SO_REUSEADDR 语义是「允许复用处于 TIME_WAIT 的地址」，但仍然**不允许**
    绑定到已被活动监听占用的端口。所以「设 SO_REUSEADDR → 试 bind → 失败即被占」
    这套探测法在 macOS/Linux 上成立。

    Windows 不是这个语义。它允许第二个 socket 直接绑定到已被占用的端口，甚至把流量
    抢过去。微软文档原话：the second socket has overtaken the port and behavior
    regarding which socket will receive packets is undetermined。

    照搬 POSIX 写法会让「端口是否空闲」这个判断**永远返回 True**：端口顺延机制静默失效，
    两个实例抢同一端口，症状是「文件有时收到有时收不到」这种极难定位的间歇故障。
    Windows 上必须用 SO_EXCLUSIVEADDRUSE 才能拿到独占语义。
    （证据：Microsoft Learn「Using SO_EXCLUSIVEADDRUSE」bind 行为矩阵；
      bitcoin/bitcoin PR #36305 为解决同类问题做了同样的改动。）
    """
    if IS_WIN:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def port_is_free(port):
    """能否在本机所有网卡上绑定该端口（与 TransferServer 的绑定方式一致）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _listen_sockopt(s)
        s.bind(("", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port(preferred, span=PORT_SPAN):
    """返回第一个可用端口；全部占用时抛错。"""
    for i in range(span):
        cand = preferred + i
        if cand > 65535:
            break
        if port_is_free(cand):
            return cand
    raise SystemExit(f"端口 {preferred}~{min(preferred + span - 1, 65535)} 全部被占用，无法启动")


def write_runtime():
    """把实际端口 / PID / 访问地址写入运行时文件，供菜单栏读取。"""
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        RUNTIME_FILE.write_text(
            f"PORT={PORT}\nPID={os.getpid()}\nURL={current_url()}\n", encoding="utf-8")
    except Exception as e:
        print(f"[runtime] 写入失败: {e!r}", flush=True)


def clear_runtime():
    """退出时清理运行时文件（只删自己写的，避免误删新实例的）。"""
    try:
        if RUNTIME_FILE.exists() and f"PID={os.getpid()}" in RUNTIME_FILE.read_text(encoding="utf-8"):
            RUNTIME_FILE.unlink()
    except Exception:
        pass


# ---------------------------------------------------------------- 高清二维码
# CoreImage 的 CIQRCodeGenerator 输出只有「1 模块 = 1 像素」（约 25~33px），
# 菜单栏把它放大到几百像素时模块边界会糊/锯齿。这里用 qrcode+Pillow 按整数倍
# box_size 生成位图，并让输出边长贴近显示端的目标像素（Retina 下约 1:1），
# 这样任何缩放插值都几乎不失真，模块边界保持锐利。
QR_PX = int(os.environ.get("QR_PX", "720"))    # 目标边长（像素）
QR_BORDER = 4                                  # quiet zone 模块数（规范要求 >= 4）
_qr_cache = {}


def qr_png_bytes(target_px=None):
    """返回高清二维码 PNG 字节；依赖缺失时返回 None，调用方回退。"""
    px = int(target_px or QR_PX)
    url = current_url()
    key = (url, px, QR_BORDER)
    if key in _qr_cache:
        return _qr_cache[key]
    try:
        import qrcode
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                           border=QR_BORDER, box_size=4)
        qr.add_data(url)
        qr.make(fit=True)
        # 选最接近目标的整数倍 box：输出边长 = 总模块数 × box
        total = qr.modules_count + 2 * QR_BORDER
        qr.box_size = max(2, round(px / total))
        img = qr.make_image(fill_color="black", back_color="white").get_image()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
    except Exception as e:
        print(f"[qr] 生成失败: {e!r}", flush=True)
        return None
    _qr_cache.clear()
    _qr_cache[key] = data
    return data


def _safe(text):
    return text.replace("\\", "/").replace('"', "'").replace("\n", " ")


def notify(title, msg):
    """系统通知：三平台各一条路径。任何失败都静默 —— 通知是锦上添花，不该影响主流程。
    Windows 那条是尽力而为（原因见 _win_toast 注释）；没弹出来不影响任何功能。"""
    try:
        if IS_MAC:
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{_safe(msg)[:200]}" with title "{_safe(title)}" sound name "Glass"'],
                check=False, timeout=5,
            )
        elif IS_LINUX:
            subprocess.run(["notify-send", "-a", APP_NAME, title, msg],
                           check=False, timeout=5)
        elif IS_WIN:
            _win_toast(title, msg)
    except Exception:
        pass


def write_state(kind, name, extra=""):
    try:
        (INBOX / "__last.json").write_text(
            json.dumps({"kind": kind, "name": name, "time": time.strftime("%H:%M:%S"), "extra": extra},
                       ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 发件箱（本机 → 对方）
# 形态：异步队列 + 对方拉取，不是真推送 —— 局域网 http 下浏览器没有被动接收的通道
# （Web Push 要 HTTPS+SW+FCM，SSE/WebSocket 一样要求页面常驻前台），所以本机只负责
# "把东西放进队列"，对方打开页面或回到前台时自动拉走。
#
# 条目 = 本体文件 + 旁挂的隐藏元数据文件：
#     <发件箱>/报告.pdf
#     <发件箱>/.报告.pdf.json
# 本体直接躺在目录里（不另建 id 子目录）是有意的：用户在文件管理器里看得见待发内容，
# 也能直接把文件拖进这个目录 —— "拖进文件夹即发送"，这条路径不需要任何 UI。
# 元数据以点开头：macOS / Linux 上这就是隐藏（Finder 与文件管理器默认不显示）；
# Windows 不吃这套约定，所以每次写完还要调 mark_hidden() 补设隐藏属性位。
META_PREFIX = "."
META_SUFFIX = ".json"
OUTBOX_SCAN = float(os.environ.get("OUTBOX_SCAN", "2"))            # 目录扫描周期（秒）
OUTBOX_TTL = int(os.environ.get("OUTBOX_TTL", str(24 * 3600)))     # 条目保留时长（秒）
OUTBOX_MAX = int(os.environ.get("OUTBOX_MAX", "200"))              # 队列上限，超出丢最旧
PHONE_ONLINE_WINDOW = float(os.environ.get("PHONE_ONLINE", "20"))  # 多久内拉过算"对方在线"
TEXT_SNIFF_LIMIT = 65536               # 小于此、能按 UTF-8 解出且无 NUL 的，当作文本条目

FLAGS_DIR = RUNTIME_DIR / "flags"
FLAG_CLIP_READ = FLAGS_DIR / "clip_read"          # 存在 → 允许对方读本机剪贴板（默认禁止）
FLAG_NO_CLIP_WRITE = FLAGS_DIR / "no_clip_write"  # 存在 → 对方文字不自动写进本机剪贴板（默认写）

_phone_seen = 0.0   # 最近一次对方拉取发件箱的时间戳


def meta_path(name):
    return OUTBOX / (META_PREFIX + name + META_SUFFIX)


def mark_hidden(path):
    """把文件标记为隐藏（只有 Windows 需要真的做什么）。

    整个项目用「以点开头」表达隐藏，那是 POSIX 约定 —— Finder 和 Linux 文件管理器
    默认不显示。Windows 的隐藏是一个独立的文件属性位，跟文件名无关：不设的话用户在
    资源管理器里会看到一堆 `.报告.pdf.json` 这种怪文件，而它们本应是不可见的实现细节。
    """
    if not IS_WIN:
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
        k.GetFileAttributesW.restype = ctypes.c_uint32
        k.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        k.SetFileAttributesW.restype = ctypes.c_int
        s = str(path)
        attrs = k.GetFileAttributesW(s)
        if attrs == 0xFFFFFFFF:            # INVALID_FILE_ATTRIBUTES
            return
        # 只加隐藏位，其余属性（ARCHIVE 等）原样保留，别覆盖成只剩隐藏
        k.SetFileAttributesW(s, attrs | 0x02)      # FILE_ATTRIBUTE_HIDDEN
    except Exception:
        pass        # 隐藏只影响观感，失败不该牵连发件箱主流程


def read_meta(name):
    try:
        return json.loads(meta_path(name).read_text(encoding="utf-8"))
    except Exception:
        return None


def write_meta(name, obj):
    try:
        p = meta_path(name)
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        mark_hidden(p)
    except Exception as e:
        print(f"[outbox] 元数据写入失败 {name}: {e!r}", flush=True)


def sniff_text(p):
    """内容是 UTF-8 文本就返回全文，否则 None。按内容判断而不是按后缀 ——
    这样拖进来的 .txt/.md/.py 在手机上能直接复制，图片视频走纯下载。"""
    try:
        if p.stat().st_size > TEXT_SNIFF_LIMIT:
            return None
        raw = p.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _note(name, st):
    text = sniff_text(OUTBOX / name)
    return {
        "text": text is not None,
        "preview": " ".join(text.split())[:80] if text else "",
        "mtime": st.st_mtime,
    }


def outbox_entries():
    """当前队列，按 mtime 降序。条目最多 OUTBOX_MAX 个，直接扫目录即可。"""
    items = []
    try:
        files = [p for p in OUTBOX.iterdir()
                 if p.is_file() and not p.name.startswith(META_PREFIX)]
    except FileNotFoundError:
        return []
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        m = read_meta(p.name) or {}
        items.append({
            "id": p.name, "name": p.name, "size": st.st_size, "mtime": int(st.st_mtime),
            "text": bool(m.get("text")), "preview": m.get("preview", ""),
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def drop_entry(name):
    """删掉一个条目（本体 + 元数据）。用 unlink 逐个删，不做递归删除。"""
    gone = False
    for p in (OUTBOX / name, meta_path(name)):
        try:
            if p.is_file():
                p.unlink()
                gone = True
        except OSError:
            pass
    return gone


def scan_outbox():
    """登记新文件、刷新被改动的条目、清理孤儿元数据与过期条目。返回新登记的名字列表。"""
    fresh = []
    now = time.time()
    try:
        files = [p for p in OUTBOX.iterdir()
                 if p.is_file() and not p.name.startswith(META_PREFIX)]
    except FileNotFoundError:
        return []

    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        m = read_meta(p.name)
        if m is None:
            write_meta(p.name, _note(p.name, st))
            fresh.append(p.name)
        elif abs(float(m.get("mtime", 0)) - st.st_mtime) > 1:
            # 同名文件被改过（用户重新拖了一份）→ 视作新条目，重新排到列表最前
            write_meta(p.name, _note(p.name, st))
            fresh.append(p.name)

    for mp in OUTBOX.glob(META_PREFIX + "*" + META_SUFFIX):
        owner = mp.name[len(META_PREFIX):-len(META_SUFFIX)]
        if not (OUTBOX / owner).is_file():
            # 孤儿元数据：本体已经不在（被手机 ack 掉，或用户手动删了）
            try:
                mp.unlink()
            except OSError:
                pass
        else:
            try:
                if now - mp.stat().st_mtime > OUTBOX_TTL:
                    drop_entry(owner)
            except OSError:
                pass

    # 队列超上限：丢掉最旧的
    for it in outbox_entries()[OUTBOX_MAX:]:
        drop_entry(it["id"])

    return fresh


def outbox_worker():
    """后台线程：周期扫描目录，让"拖进文件夹"也能被登记，顺带清理过期条目。"""
    scan_outbox()                      # 启动时静默登记，避免重启就刷一屏通知
    while True:
        time.sleep(OUTBOX_SCAN)
        try:
            fresh = scan_outbox()
        except Exception as e:
            print(f"[outbox] 扫描异常: {e!r}", flush=True)
            continue
        if not fresh:
            continue
        online = _phone_seen and (time.time() - _phone_seen) < PHONE_ONLINE_WINDOW
        who = fresh[0] if len(fresh) == 1 else f"{len(fresh)} 个文件"
        if online:
            notify("已放入发件箱", who)
        else:
            # 对方页面没开着就没有通知通道，只能明说"在排队"，否则用户会以为发送失败
            notify("已放入发件箱（等对方取件）", f"{who} · 对方页面未打开")


def flag_on(p):
    return p.exists()


# ---------------- 传文字历史（clipboard.log）：滚动保留 + 可清空 ----------------
# 曾经是纯 append 无限增长，但传文字是临时剪贴板语义 —— 用户可能把密码、验证码
# 当普通文字发过来，历史只是「留个底方便找」，不该永久明文躺在盘上。
CLIP_LOG_SEP = "=" * 40
CLIP_LOG_MAX = 200   # 只保留最近 200 条


def clip_log_path():
    return INBOX / "clipboard.log"


def clip_log_write(ts, text):
    """追加一条传文字记录并滚动裁剪到最近 CLIP_LOG_MAX 条。

    条目格式与旧版逐字节兼容（[ts]\\n正文\\n分隔线\\n），旧文件直接接着滚动。
    正文里恰好出现同样分隔线的概率极低；万一出现也只是把一条拆成两条计数，
    不影响「只保留最近 N 条」的语义。
    """
    entries = []
    try:
        old = clip_log_path().read_text(encoding="utf-8", errors="replace")
        entries = [e for e in old.split("\n" + CLIP_LOG_SEP + "\n") if e.strip()]
    except OSError:
        pass  # 首次写入或文件暂时读不了 → 当空历史处理
    entries.append(f"[{ts}]\n{text}")
    if len(entries) > CLIP_LOG_MAX:
        entries = entries[-CLIP_LOG_MAX:]
    with open(clip_log_path(), "w", encoding="utf-8") as f:
        f.write(("\n" + CLIP_LOG_SEP + "\n").join(entries) + "\n" + CLIP_LOG_SEP + "\n")


def set_clipboard(text):
    """把文本写进系统剪贴板。返回是否成功 —— 调用方要如实转达失败，
    不能假装写进去了：「手机说发送成功、电脑上却 Cmd+V 无效」是最难排查的一类反馈。"""
    if IS_MAC:
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False, timeout=5)
            return True
        except Exception:
            return False
    if IS_WIN:
        return _win_clip_write(text)
    cmd = _linux_clip(read=False)
    if not cmd:
        return False          # 没装 xclip/wl-copy：这是正常的降级，不是错误
    try:
        subprocess.run(cmd, input=text.encode("utf-8"), check=False, timeout=5)
        return True
    except Exception:
        return False


def read_clipboard():
    """读系统剪贴板里的文本。剪贴板装的是图片/文件、或工具缺失，一律返回空串。"""
    if IS_MAC:
        try:
            r = subprocess.run(["pbpaste"], capture_output=True, timeout=5)
            return r.stdout.decode("utf-8", "replace")
        except Exception:
            return ""
    if IS_WIN:
        return _win_clip_read()
    cmd = _linux_clip(read=True)
    if not cmd:
        return ""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=5)
        return r.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def content_disposition(name, inline=False):
    """HTTP header 的值只能 latin-1 —— 中文文件名直接塞进去会抛 UnicodeEncodeError，
    客户端只看到连接被断开。按 RFC 5987 用 filename* 传 UTF-8，另留一个纯 ASCII 的
    filename= 兜底（老客户端用得到，所以它必须真的只有 ASCII）。"""
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace('"', "_")
    kind = "inline" if inline else "attachment"
    return f'{kind}; filename="{ascii_name}"; filename*=UTF-8\'\'{urllib.parse.quote(name)}'


# 由 main() 赋值。/shutdown 用它优雅停止 serve_forever —— 比 os._exit 干净：
# serve_forever 正常返回后，main 的 finally 仍会执行 clear_runtime()。
_httpd = None


# ---------------------------------------------------------------- 文件名清洗
# Windows 文件名限制比 POSIX 严得多。原来只剥了路径分隔符和首尾点，在 macOS 上够用，
# 但上传 `会议记录:2024.pdf` 或 `CON.txt` 这类名字时 Windows 上 open() 直接抛 OSError
# → 请求 500，而用户只看到「上传失败」、不知道是自己文件名的问题。
# 这里统一按最严格的一套清洗：同一个名字在三个平台上应该得到同样的结果。
_BAD_NAME_CHARS = '<>:"/\\|?*'
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def sanitize_name(raw):
    """把来自客户端或 URL 的名字变成三平台都能安全落盘的文件名。"""
    name = urllib.parse.unquote(raw or "")
    # 反斜杠同样是路径分隔符（Windows 客户端会带过来），先归一化再取 basename
    name = os.path.basename(name.replace("\\", "/"))
    for ch in _BAD_NAME_CHARS:
        name = name.replace(ch, "_")
    # 控制字符（含 NUL、换行）一律换掉 —— 它们能让某些文件系统直接出错
    name = "".join("_" if ord(c) < 32 else c for c in name)
    # 首尾的点和空格：Windows 会**静默丢弃**结尾的点和空格，于是「我们以为写入的名字」
    # 和「真正落盘的名字」不一致，之后按名字回查就永远找不到
    name = name.strip().strip(".")
    if not name:
        return "upload.bin"
    # 保留设备名要按主名判断 —— CON.txt 和 CON 一样不合法
    if name.split(".", 1)[0].upper() in _WIN_RESERVED:
        name = "_" + name
    # 超长截断：Windows 全路径上限 260，且中文按多字节参与计算，留足余量
    if len(name) > 150:
        base, ext = os.path.splitext(name)
        name = base[:150 - len(ext)] + ext
    return name


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默日志

    def _ok(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _token_ok(self):
        if not TOKEN:
            return True
        return self.headers.get("X-Token", "") == TOKEN

    def _token_ok_qs(self):
        """浏览器原生下载（<a href>）带不了自定义请求头，所以这几条路由额外接受 ?token=。"""
        if not TOKEN:
            return True
        if self.headers.get("X-Token", "") == TOKEN:
            return True
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return qs.get("token", [""])[0] == TOKEN

    def _is_local(self):
        """请求是否来自本机。

        用于「只该由宿主机自己发起」的操作：打开目录、改剪贴板开关、退出服务。
        这些若远端也能调，同网段任何人都能让你的电脑莫名弹出文件夹、开启剪贴板读取、
        或者直接把服务关掉。TOKEN 默认是空的，不能指望它兜底，所以这里按来源 IP 拦。
        """
        try:
            ip = self.client_address[0]
        except Exception:
            return False
        return ip.startswith("127.") or ip in ("::1", "localhost")

    def _text(self, body, ctype="text/plain; charset=utf-8"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # 路径与 query 分离：新接口要用 ?token=，旧分支照旧按整串比较路径
        path = self.path.partition("?")[0]
        if path in ("/", "/index.html", "/upload"):
            self._serve_page()
        elif path == "/list":
            files = []
            for p in sorted(INBOX.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if p.is_file() and p.name != "__last.json":
                    files.append({"name": p.name, "size": p.stat().st_size,
                                  "mtime": int(p.stat().st_mtime)})
            self._ok({"files": files[:200]})
        elif path == "/info":
            # 仅本机可访问，且不再返回 token：这是手册"发件箱同样受口令保护"承诺的
            # 前提 —— 否则开了 TOKEN，同网段任何人 GET /info 就把口令直接拿走。
            if not self._is_local():
                self.send_error(403, "local only")
                return
            self._ok({"ip": get_lan_ip(), "port": PORT, "inbox": str(INBOX),
                      "outbox": str(OUTBOX)})
        elif path == "/addr":
            # 开启 TOKEN 时返回可直接访问的完整地址，菜单栏复制/二维码即可直接扫
            body = current_url().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/open":
            # 在宿主机文件管理器里打开收件 / 发件目录。原来只有 macOS 菜单栏能做，
            # 跨平台后由控制台页面调用。只允许本机 —— 否则同网段任何人都能让
            # 你的电脑莫名弹出文件夹窗口。
            if not self._is_local():
                self.send_error(403, "local only")
                return
            qs2 = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            which = (qs2.get("dir", ["inbox"])[0] or "inbox").lower()
            target = OUTBOX if which == "outbox" else INBOX
            self._ok({"ok": open_in_file_manager(target), "path": str(target)})
        elif path in ("/doc", "/manual-en.html") or urllib.parse.unquote(path) == "/使用手册.html":
            # 使用说明（中文 /doc 与别名 /使用手册.html）与英文版 /manual-en.html。
            # 原来由 AppleScript 用 `open` 打开这个文件，跨平台后改由控制台页面链接过来
            # —— Windows / Linux 上压根没有 AppleScript。/使用手册.html 别名是为了让
            # 英文手册里「中文版」的相对回跳（href="使用手册.html"）在 HTTP 下也能走通；
            # 浏览器会把中文路径编码成 %E4%BD%BF…，所以这里按解码后的值比较。
            fname = "manual-en.html" if path == "/manual-en.html" else "使用手册.html"
            try:
                with open(os.path.join(BASE_DIR, fname), "rb") as f:
                    data = f.read()
            except FileNotFoundError:
                self.send_error(404, "doc missing")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path in ("/ui-console.png", "/ui-phone.png", "/ui-console-en.png", "/ui-phone-en.png"):
            # 手册配图。手册有两种打开方式：HTTP 的 /doc 与菜单栏 `open` 的 file:// ——
            # file:// 下绝对路径 /doc-img/... 会指到文件系统根，所以手册里用相对文件名，
            # 这里就在根路径伺服。白名单制：不拼路径、不通配，防 ../ 穿越。
            img = path.lstrip("/")
            try:
                with open(os.path.join(BASE_DIR, img), "rb") as f:
                    data = f.read()
            except FileNotFoundError:
                self.send_error(404, "no such image")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/favicon.png":
            # 浏览器标签页图标。静态资源、不含任何数据，无需口令 —— 开着 TOKEN 时
            # <link rel="icon"> 的自动请求不带 query，不该因此 403 弄脏日志。
            try:
                with open(os.path.join(BASE_DIR, "favicon.png"), "rb") as f:
                    data = f.read()
            except FileNotFoundError:
                self.send_error(404, "no icon")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path.startswith("/qr"):
            # 高清二维码 PNG，可选 ?px=720 指定目标边长
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                want_px = int(qs.get("px", [QR_PX])[0])
            except (ValueError, TypeError):
                want_px = QR_PX
            data = qr_png_bytes(want_px)
            if not data:
                # 注意：send_error 的状态行只能 latin-1，message 必须用 ASCII
                self.send_error(503, "qrcode unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/last":
            try:
                d = json.loads((INBOX / "__last.json").read_text(encoding="utf-8"))
                body = f"{d.get('kind','')}\n{d.get('name','')}\n{d.get('time','')}".encode("utf-8")
            except Exception:
                body = b"NONE\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/outbox":
            if not self._token_ok_qs():
                # 注意：message 只能是 ASCII（会进状态行，latin-1 编码）
                self.send_error(403, "bad token")
                return
            global _phone_seen
            _phone_seen = time.time()      # 有手机在拉 = 手机在线，菜单栏据此决定提示文案
            items = outbox_entries()
            self._ok({"ok": True, "items": items, "total": len(items)})
        elif path == "/outbox.zip":
            if not self._token_ok_qs():
                self.send_error(403, "bad token")
                return
            self._outbox_zip()
        elif path.startswith("/outbox/"):
            if not self._token_ok_qs():
                self.send_error(403, "bad token")
                return
            self._outbox_send(urllib.parse.unquote(path[len("/outbox/"):]))
        elif path == "/clip/now":
            # 本机剪贴板里可能有密码 → 默认关闭，靠开关哨兵文件放行
            if not self._token_ok_qs():
                self.send_error(403, "bad token")
                return
            if not flag_on(FLAG_CLIP_READ):
                self.send_error(403, "clipboard read disabled")
                return
            self._text(read_clipboard().encode("utf-8"))
        elif path == "/status":
            self._status()
        else:
            self.send_error(404)

    def _serve_page(self):
        try:
            with open(INDEX, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            # send_error 的 message 会进 HTTP 状态行，只能是 latin-1 —— 必须写 ASCII
            self.send_error(500, "index.html missing")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = self.path.partition("?")[0]
        if not self._token_ok_qs():
            # 注意：message 只能是 ASCII（进状态行，latin-1 编码）
            self.send_error(403, "bad token")
            return
        if path == "/upload":
            self._upload()
        elif path == "/clip":
            self._clip()
        elif path == "/outbox":
            self._outbox_post()
        elif path == "/outbox/ack":
            self._outbox_ack()
        elif path == "/flags":
            self._flags()
        elif path == "/cliplog/clear":
            self._cliplog_clear()
        elif path == "/shutdown":
            self._shutdown()
        else:
            self.send_error(404)

    def _safe_name(self, raw):
        # 具体清洗逻辑在模块级 sanitize_name 里，那里解释了为什么不能只剥路径和首尾点
        return sanitize_name(raw)

    def _upload(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            # 同上：ASCII，避免状态行 UnicodeEncodeError
            self.send_error(400, "empty body")
            return
        if length > MAX_UPLOAD:
            # 不读 body 直接拒绝；必须断开连接，否则未消费的请求体会污染下一条 keep-alive 请求
            self.close_connection = True
            self.send_error(413, "payload too large")
            return
        name = self._safe_name(self.headers.get("X-Filename", "upload.bin"))
        dest = INBOX / name
        if dest.exists():
            base, ext = os.path.splitext(name)
            i = 1
            while dest.exists():
                dest = INBOX / f"{base}({i}){ext}"
                i += 1
        data = self.rfile.read(length)
        with open(dest, "wb") as f:
            f.write(data)
        kb = f"{len(data)/1024:.1f} KB"
        notify(APP_NAME, f"收到文件：{dest.name}（{kb}）")
        write_state("file", dest.name, kb)
        self._ok({"ok": True, "file": dest.name, "size": len(data)})

    def _clip(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_TEXT:
            self.close_connection = True
            self.send_error(413, "text too large")
            return
        text = self.rfile.read(length).decode("utf-8", "replace")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        clip_log_write(ts, text)
        # 顺手写进系统剪贴板：手机复制 → 电脑上直接 Cmd+V，这才是「传文字」的完整语义
        off = flag_on(FLAG_NO_CLIP_WRITE)
        pasted = (not off) and set_clipboard(text)
        # 三种结果必须分清：用户主动关掉 / 写失败 / 正常写入。含糊地一律报「已收到」，
        # 会让「手机说发送成功、电脑上粘不出来」变成一桩查不出所以然的无头案。
        if off:
            hint = "（剪贴板写入已关闭）"
        elif pasted:
            hint = "，已写入剪贴板"
        else:
            hint = "（未能写入剪贴板）"
        notify(APP_NAME, f"收到 {len(text)} 字" + hint)
        write_state("clip", f"{len(text)} 字", text[:50])
        self._ok({"ok": True, "len": len(text)})

    # ------------------------------ 发件箱（本机 → 对方） ------------------------------

    def _outbox_send(self, raw_name):
        name = self._safe_name(raw_name)
        p = OUTBOX / name
        # 只认发件箱目录下的直接子文件：_safe_name 已剥掉路径，这里再确认一次
        if not p.is_file() or p.parent != OUTBOX:
            self.send_error(404, "not found")
            return
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        inline = qs.get("inline", ["0"])[0] in ("1", "true", "yes")
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") and "charset" not in ctype:
            ctype += "; charset=utf-8"
        size = p.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", content_disposition(name, inline))
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with open(p, "rb") as f:
                while True:
                    chunk = f.read(65536)      # 分块：大文件不能整个读进内存
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass      # 手机取消下载是正常操作，别让线程抛栈

    def _outbox_zip(self):
        items = outbox_entries()
        if not items:
            self.send_error(404, "outbox empty")
            return
        fd, tmp = tempfile.mkstemp(prefix="androidtransfer-outbox-", suffix=".zip")
        os.close(fd)
        try:
            # ZIP_STORED：队列里多是图片/视频，二次压缩收益极小，纯浪费时间
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
                for it in items:
                    p = OUTBOX / it["id"]
                    if p.is_file():
                        z.write(p, arcname=it["id"])
            zname = f"{APP_NAME}-outbox-" + time.strftime("%Y%m%d-%H%M") + ".zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", content_disposition(zname))
            self.send_header("Content-Length", str(os.path.getsize(tmp)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(tmp, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                os.unlink(tmp)             # 临时文件用完即删（unlink，非递归）
            except OSError:
                pass

    def _outbox_ack(self):
        """手机确认已收到 → 从队列里删掉条目（本体 + 元数据）。不带 ids 表示清空全部。"""
        length = int(self.headers.get("Content-Length", 0) or 0)
        ids = []
        if length > 0:
            if length > MAX_TEXT:
                self.close_connection = True
                self.send_error(413, "body too large")
                return
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8", "replace") or "{}")
                ids = [str(x) for x in (data.get("ids") or [])]
            except Exception:
                ids = []
        targets = ids if ids else [it["id"] for it in outbox_entries()]
        removed = 0
        for n in targets:
            if drop_entry(self._safe_name(n)):
                removed += 1
        self._ok({"ok": True, "removed": removed})

    def _outbox_post(self):
        """往发件箱投递一个文件（宿主机自己的控制台页面用）。

        为什么需要这个投递口：macOS 上的「发送文件」靠菜单栏的 NSOpenPanel 直接拷文件，
        而 Windows / Linux 没有菜单栏 —— 浏览器页面是唯一的图形入口，可浏览器又不能
        直接写服务器文件系统。所以必须有一个 HTTP 投递口。

        与 /upload 的两点差异都是刻意的：
          1. 目标是 OUTBOX，不是 INBOX；
          2. 同名文件**覆盖**，不加 (1) 后缀 —— 发件箱的语义是「最新这份要发过去」，
             加后缀只会让对方看到一串看起来一模一样的条目。菜单栏的发送面板也是覆盖语义。
        """
        if not self._is_local():
            self.send_error(403, "local only")
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            self.send_error(400, "empty body")
            return
        if length > MAX_UPLOAD:
            self.close_connection = True
            self.send_error(413, "payload too large")
            return
        name = sanitize_name(self.headers.get("X-Filename", "outbox.txt"))
        dest = OUTBOX / name
        try:
            data = self.rfile.read(length)
            with open(dest, "wb") as f:
                f.write(data)
            # 立刻登记，不必等后台扫描的下一个 2 秒周期 —— 否则用户放完文件、
            # 页面刷新却看不到它，会以为没生效
            write_meta(name, _note(name, dest.stat()))
        except OSError as e:
            print(f"[outbox] 投递失败 {name}: {e!r}", flush=True)
            self.send_error(500, "write failed")
            return
        self._ok({"ok": True, "file": name, "size": len(data)})

    # ------------------------- 宿主机本地操作（仅本机可调） -------------------------

    @staticmethod
    def _set_flag(path, on):
        """哨兵文件：存在＝开。用 touch / unlink 表达，文件内容本身没有意义。"""
        try:
            if on:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                mark_hidden(path)
            elif path.exists():
                path.unlink()
            return True
        except OSError as e:
            print(f"[flags] 写入 {path} 失败: {e!r}", flush=True)
            return False

    def _cliplog_clear(self):
        """清空传文字历史（clipboard.log）。

        只允许本机操作 —— 与 /flags、/shutdown 同级：访客没有权限替主机
        删记录。清空 = 覆写为空文件（不删文件本身，服务端下次照常追加）。
        """
        if not self._is_local():
            self.send_error(403, "local only")
            return
        try:
            with open(clip_log_path(), "w", encoding="utf-8") as f:
                f.write("")
            self._ok({"ok": True})
        except OSError:
            self.send_error(500, "clear failed")

    def _flags(self):
        """改两个剪贴板开关。

        原来只有 macOS 菜单栏能改（直接 touch / rm 哨兵文件）。跨平台之后控制台页面
        也要能改，所以补这个接口。仍然走哨兵文件、且服务端每次请求现读 —— 保持
        「改完立刻生效、不用重启」的既有行为，不引入需要重启的状态。
        """
        if not self._is_local():
            self.send_error(403, "local only")
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8", "replace") or "{}")
        except Exception:
            self.send_error(400, "bad json")
            return
        if "clip_read" in data:
            self._set_flag(FLAG_CLIP_READ, bool(data["clip_read"]))
        if "clip_write" in data:
            # 语义是反的：no_clip_write 这个哨兵文件存在 = 关闭写剪贴板
            self._set_flag(FLAG_NO_CLIP_WRITE, not bool(data["clip_write"]))
        self._ok({"ok": True,
                  "clip_read": flag_on(FLAG_CLIP_READ),
                  "clip_write": not flag_on(FLAG_NO_CLIP_WRITE)})

    def _shutdown(self):
        """请求服务退出。

        为什么非要有这个接口：Windows 不发送 SIGTERM，os.kill(pid, SIGTERM) 会走
        TerminateProcess 直接结束进程，**不会触发 Python 的 signal handler** ——
        于是 clear_runtime() 永远不执行，运行时文件里留着失效的 PID 与旧端口，
        下次启动或控制台读到的就是错的端口。走 HTTP 是唯一可靠的优雅退出路径。
        """
        if not self._is_local():
            self.send_error(403, "local only")
            return
        self._ok({"ok": True, "bye": True})
        # 先回响应、再关服务：反过来的话客户端只会看到连接被重置，像是请求失败了
        threading.Thread(target=self._delayed_shutdown, daemon=True).start()

    @staticmethod
    def _delayed_shutdown():
        time.sleep(0.25)
        try:
            if _httpd is not None:
                _httpd.shutdown()   # 让 serve_forever 干净返回 → main 的 finally 负责清理
        except Exception:
            pass

    def _status(self):
        """状态快照：纯文本 key=value，键与数值全 ASCII，值可为 UTF-8。
        AppleScript 菜单栏与浏览器控制台页都读这一份，所以新增字段只加不改。"""
        seen = int(time.time() - _phone_seen) if _phone_seen else -1
        lines = [
            f"PENDING={len(outbox_entries())}",
            f"SEEN={seen}",
            f"ONLINE={1 if 0 <= seen < PHONE_ONLINE_WINDOW else 0}",
            f"CLIPREAD={1 if flag_on(FLAG_CLIP_READ) else 0}",
            f"CLIPWRITE={0 if flag_on(FLAG_NO_CLIP_WRITE) else 1}",
            f"OUTBOX={OUTBOX}",
            # 身份字段：控制台页 / 移动端页面上所有「对面那台机器」的文案都从这里取，
            # 页面里不再写死任何平台名（这是整个通用化改造的关键一环）
            f"HOST={HOST_NAME}",
            f"HOSTKIND={HOST_KIND}",
            f"HOSTLABEL={HOST_LABEL}",
            f"APP={APP_NAME}",
            f"INBOXDIR={INBOX}",
            f"OUTBOXDIR={OUTBOX}",
        ]
        try:
            d = json.loads((INBOX / "__last.json").read_text(encoding="utf-8"))
            lines += [f"LASTKIND={d.get('kind', '')}", f"LASTNAME={d.get('name', '')}",
                      f"LASTTIME={d.get('time', '')}"]
        except Exception:
            lines += ["LASTKIND=", "LASTNAME=", "LASTTIME="]
        self._text(("\n".join(lines) + "\n").encode("utf-8"))


class TransferServer(socketserver.ThreadingTCPServer):
    """端口选项不能靠 allow_reuse_address 类属性一刀切 —— 它在 Windows 上的语义是反的
    （设了等于允许别的进程抢占本端口），所以改为在 server_bind 里分平台显式设置。
    详见 _listen_sockopt 的注释。"""

    allow_reuse_address = False     # 实际选项由 server_bind 按平台设置
    daemon_threads = True

    def server_bind(self):
        _listen_sockopt(self.socket)
        # allow_reuse_address 已置 False，父类这段只会做纯地址绑定，正好复用
        socketserver.TCPServer.server_bind(self)


def bind_server(retries=3, wait=1.0):
    """带重试的端口绑定，进一步容忍端口尚未完全释放的情况。"""
    last = None
    for i in range(retries):
        try:
            return TransferServer(("", PORT), Handler)
        except OSError as e:
            last = e
            if i < retries - 1:
                print(f"端口 {PORT} 暂不可用，{wait:.0f}s 后重试… ({e})")
                time.sleep(wait)
    raise last


def _graceful(signum, frame):  # noqa: ARG001
    """收到 SIGTERM/SIGINT 时清掉运行时文件再退出（pkill 走的就是这条路）。"""
    clear_runtime()
    sys.exit(0)


def main():
    global PORT, _httpd
    resolve_token()

    preferred = PORT
    PORT = pick_port(preferred)
    if PORT != preferred:
        print(f"[端口] {preferred} 已被占用，自动改用 {PORT}")
        notify(APP_NAME, f"端口 {preferred} 被占用，已自动改用 {PORT}")
    write_runtime()

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)
    # Windows 上 SIGTERM 不会被投递（进程是被 TerminateProcess 直接干掉的），但关闭
    # 控制台窗口时会发 SIGBREAK —— 那是 Windows 上唯一还能做清理的时机。
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, _graceful)
        except (ValueError, OSError):
            pass

    # 目录扫描线程：让"往发件箱里拖文件"也能被登记，并定期清理过期条目
    threading.Thread(target=outbox_worker, daemon=True).start()

    url = current_url()
    print("=" * 60)
    print(f"{APP_NAME} 服务已启动  ·  {HOST_NAME}（{HOST_LABEL}）")
    print(f"其他设备浏览器打开: {url}")
    print(f"本机控制台:         http://127.0.0.1:{PORT}/")
    print(f"收件目录: {INBOX}")
    print(f"发件目录: {OUTBOX}")
    print(f"运行时信息: {RUNTIME_FILE}")
    print("=" * 60)
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii()
    except Exception:
        pass
    # start.sh / start.bat 启动时设 OPEN_CONSOLE=1：直接把本机控制台拉到用户面前。
    # （.app 场景不设它 —— 菜单栏有自己的入口，避免双重弹窗。）
    if os.environ.get("OPEN_CONSOLE") == "1":
        open_url(f"http://127.0.0.1:{PORT}/")
    try:
        with bind_server() as httpd:
            _httpd = httpd        # /shutdown 需要它来优雅停机
            httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        clear_runtime()
        print("\n已停止")


if __name__ == "__main__":
    main()
