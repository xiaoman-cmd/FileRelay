#!/usr/bin/env python3
"""互传 · 服务端离线自测（零依赖，只用标准库）。

用法：python3 selftest.py [关键字]

起一个完全隔离的临时实例（独立端口 / runtime / 收件发件目录），逐条验证接口行为，
跑完自动清理。不碰正在运行的真实实例，也不碰 ~/Downloads 里的数据。

两个刻意的设计：
  * 在 PATH 最前面插一个假 bin 目录，拦截 open / osascript / notify-send 等，
    这样既能验证「服务确实去调了系统命令、传对了参数」，又不会真的弹出 Finder 窗口；
  * 每个断言都打印实际值 —— 失败时一眼看出差在哪，而不是只给一句 FAIL。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT_BASE = int(os.environ.get("SELFTEST_PORT", "18765"))

_results = []
_ONLY = sys.argv[1] if len(sys.argv) > 1 else ""


def check(name, ok, detail=""):
    """记一条断言。name 里带关键字可用于 --filter。"""
    if _ONLY and _ONLY not in name:
        return True
    _results.append((name, bool(ok), detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"   [{detail}]" if detail else ""))
    return bool(ok)


def req(port, path, method="GET", body=None, headers=None, timeout=6):
    """发一个请求，返回 (status, bytes)。4xx/5xx 不抛异常，交给断言去判断。"""
    data = body.encode("utf-8") if isinstance(body, str) else body
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def status_map(port):
    """把 /status 的 key=value 快照解析成 dict。"""
    _, raw = req(port, "/status")
    out = {}
    for line in raw.decode("utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def make_fake_bin(tmp):
    """造一批同名假命令放在 PATH 最前，把调用参数记进 calls.log。

    这样既能断言「服务确实调了系统命令、参数传对了」，又不会真的弹出 Finder 窗口，
    也不会覆盖用户当前的剪贴板 —— 测试不该留下这种副作用。
    """
    fakebin = os.path.join(tmp, "bin")
    store = os.path.join(tmp, "clipstore")
    os.makedirs(fakebin, exist_ok=True)
    os.makedirs(store, exist_ok=True)
    log = os.path.join(tmp, "calls.log")
    for cmd in ("open", "osascript", "notify-send", "xdg-open", "explorer"):
        p = os.path.join(fakebin, cmd)
        with open(p, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "{cmd} $*" >> {log}\nexit 0\n')
        os.chmod(p, 0o755)

    # pbcopy / pbpaste 也是假的，但**有状态**：写进文件、从文件读。
    # 于是「服务端写 → 服务端读」的完整编码链路可以验证（中文 + emoji 逐字节比对），
    # 同时完全不碰用户的真实剪贴板。
    with open(os.path.join(fakebin, "pbcopy"), "w", encoding="utf-8") as f:
        f.write(f'#!/bin/sh\ncat > {store}/clip.txt\n'
                f'printf "%s\\n" "pbcopy" >> {log}\nexit 0\n')
    with open(os.path.join(fakebin, "pbpaste"), "w", encoding="utf-8") as f:
        f.write(f'#!/bin/sh\ncat {store}/clip.txt 2>/dev/null\n'
                f'printf "%s\\n" "pbpaste" >> {log}\nexit 0\n')
    for cmd in ("pbcopy", "pbpaste"):
        os.chmod(os.path.join(fakebin, cmd), 0o755)
    return fakebin, log


def read_calls(log):
    try:
        with open(log, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def wait_port(tmp, proc, tries=100):
    rf = os.path.join(tmp, "runtime")
    for _ in range(tries):
        if proc.poll() is not None:
            print("服务进程提前退出，输出如下：")
            print(proc.stdout.read())
            raise SystemExit(1)
        if os.path.exists(rf):
            with open(rf, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("PORT="):
                        return int(line.strip().split("=", 1)[1])
        time.sleep(0.1)
    raise SystemExit("服务未在 10 秒内写出 runtime 文件")


def main():
    tmp = tempfile.mkdtemp(prefix="attest-")
    fakebin, log = make_fake_bin(tmp)
    inbox, outbox = os.path.join(tmp, "in"), os.path.join(tmp, "out")

    env = dict(os.environ)
    env.update(PORT=str(PORT_BASE), PORT_SPAN="20", AT_RUNTIME_DIR=tmp,
               INBOX=inbox, OUTBOX=outbox)
    env["PATH"] = fakebin + os.pathsep + env.get("PATH", "")

    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "..", "src", "server.py")],
                            env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        port = wait_port(tmp, proc)
        print(f"\n临时实例已就绪：端口 {port}   隔离目录 {tmp}\n")

        print("【身份字段】")
        st = status_map(port)
        check("status 含 HOST 字段", "HOST" in st, f"HOST={st.get('HOST')!r}")
        check("HOST 不为空", bool(st.get("HOST", "").strip()))
        check("status 含 HOSTKIND", st.get("HOSTKIND") in ("mac", "win", "linux"),
              f"HOSTKIND={st.get('HOSTKIND')!r}")
        check("status 含 HOSTLABEL", bool(st.get("HOSTLABEL", "").strip()),
              f"HOSTLABEL={st.get('HOSTLABEL')!r}")
        check("status 含 APP", bool(st.get("APP", "").strip()), f"APP={st.get('APP')!r}")
        check("status 含 INBOXDIR/OUTBOXDIR",
              bool(st.get("INBOXDIR")) and bool(st.get("OUTBOXDIR")))
        check("初始 PENDING=0", st.get("PENDING") == "0", f"PENDING={st.get('PENDING')}")

        print("\n【剪贴板开关（/flags，哨兵文件）】")
        flags = os.path.join(tmp, "flags")
        s, _ = req(port, "/flags", "POST", json.dumps({"clip_read": True}),
                   {"Content-Type": "application/json"})
        check("POST /flags 返回 200", s == 200, f"status={s}")
        check("clip_read 哨兵文件已创建", os.path.exists(os.path.join(flags, "clip_read")))
        check("status CLIPREAD=1（改完立刻生效，无需重启）",
              status_map(port).get("CLIPREAD") == "1")
        req(port, "/flags", "POST", json.dumps({"clip_read": False}),
            {"Content-Type": "application/json"})
        check("关闭后 CLIPREAD=0", status_map(port).get("CLIPREAD") == "0")
        req(port, "/flags", "POST", json.dumps({"clip_write": False}),
            {"Content-Type": "application/json"})
        check("clip_write=false → CLIPWRITE=0", status_map(port).get("CLIPWRITE") == "0")
        check("no_clip_write 哨兵文件已创建（语义是反的）",
              os.path.exists(os.path.join(flags, "no_clip_write")))
        req(port, "/flags", "POST", json.dumps({"clip_write": True}),
            {"Content-Type": "application/json"})
        check("恢复后 CLIPWRITE=1", status_map(port).get("CLIPWRITE") == "1")
        check("no_clip_write 哨兵文件已删除",
              not os.path.exists(os.path.join(flags, "no_clip_write")))

        print("\n【打开目录（/open，走假 open 命令）】")

        def wait_log(substr, timeout=3.0):
            """轮询等假命令把调用写进日志。固定 sleep 会 flaky（Popen 调度忙时
            0.4s 不够）—— 条件满足立即返回，超时才认输，确定性代替赌运气。"""
            dl = time.monotonic() + timeout
            while time.monotonic() < dl:
                if substr in read_calls(log):
                    return True
                time.sleep(0.05)
            return False

        s, raw = req(port, "/open?dir=inbox")
        check("GET /open?dir=inbox 返回 200", s == 200, f"status={s}")
        try:
            okflag = json.loads(raw).get("ok")
        except Exception:
            okflag = None
        check("/open 报告打开成功", okflag is True, f"ok={okflag}")
        # open_in_file_manager 用的是 Popen（不阻塞，这是对的 —— 打开目录不该卡住请求），
        # 所以假命令把调用写进日志需要一点时间，轮询等它。
        check("确实调用了系统打开命令，且指向收件目录",
              wait_log(inbox), read_calls(log).strip().splitlines()[-1] if read_calls(log).strip() else "无调用")
        req(port, "/open?dir=outbox")
        check("dir=outbox 指向发件目录", wait_log(outbox))

        print("\n【上传：跨平台文件名清洗】")
        cases = [
            ("普通中文名", "报告.pdf", "报告.pdf"),
            ("Windows 非法字符 ':'", "会议记录:2024.pdf", "会议记录_2024.pdf"),
            ("Windows 保留设备名", "CON.txt", "_CON.txt"),
            ("小写保留名", "nul", "_nul"),
            ("路径穿越尝试", "../../etc/passwd", "passwd"),
            ("Windows 反斜杠路径", "C:\\Users\\x\\机密.txt", "机密.txt"),
        ]
        for label, raw_name, expect in cases:
            fn = urllib.parse.quote(raw_name)
            s, body = req(port, "/upload", "POST", f"content-of-{label}".encode(),
                          {"X-Filename": fn})
            landed = sorted(os.listdir(inbox)) if os.path.isdir(inbox) else []
            check(f"{label}：{raw_name!r} → {expect!r}",
                  s == 200 and expect in landed,
                  f"status={s} 目录={landed}")
            # 清掉，避免影响下一条的名字冲突逻辑
            for f in landed:
                if f != "clipboard.log":
                    try:
                        os.unlink(os.path.join(inbox, f))
                    except OSError:
                        pass

        print("\n【传文字（/clip）】")
        s, _ = req(port, "/clip", "POST", "中文测试 emoji 🎉")
        check("POST /clip 返回 200", s == 200, f"status={s}")
        cl = os.path.join(inbox, "clipboard.log")
        got = open(cl, encoding="utf-8").read() if os.path.exists(cl) else ""
        check("文字已落进 clipboard.log", "中文测试 emoji 🎉" in got)
        check("写剪贴板命令被调用（假 pbcopy）", "pbcopy" in read_calls(log))

        print("\n【传文字历史：滚动 200 条 + 一键清空】")
        SEP = "=" * 40
        # 直接往日志里预置 250 条旧格式记录，验证写入新记录后被裁到 200 条
        with open(cl, "w", encoding="utf-8") as f:
            for i in range(250):
                f.write(f"[2026-01-01 00:00:{i:02d}]\n旧记录{i}\n{SEP}\n")
        req(port, "/clip", "POST", "滚动测试")
        got = open(cl, encoding="utf-8").read()
        cnt = got.count(SEP)
        check("超过上限后只保留最近 200 条", cnt == 200, f"实际 {cnt} 条")
        check("保留的是最新的（最早的 250 条里前 49 条被裁掉）",
              "旧记录249" in got and "旧记录49" not in got and "旧记录48" not in got)
        entries_now = [e for e in got.split("\n" + SEP + "\n") if e.strip()]
        check("新记录在文件末尾", entries_now[-1].endswith("滚动测试"),
              f"最后一条={entries_now[-1]!r}")
        s, _ = req(port, "/cliplog/clear", "POST")
        check("POST /cliplog/clear 返回 200", s == 200, f"status={s}")
        check("清空后日志为空文件", os.path.exists(cl) and open(cl, encoding="utf-8").read() == "")
        req(port, "/clip", "POST", "清空后的第一条")
        got = open(cl, encoding="utf-8").read()
        check("清空后可照常追加", "清空后的第一条" in got and got.count(SEP) == 1)

        print("\n【浏览器标签页图标（/favicon.png）】")
        s, raw = req(port, "/favicon.png")
        check("GET /favicon.png 返回 200", s == 200, f"status={s}")
        check("内容是 PNG（魔数 \\x89PNG）", raw[:4] == b"\x89PNG", f"头={raw[:4]!r}")

        print("\n【剪贴板端到端往返】")
        # 关键：让被测方**自己写、自己读**。这是唯一可信的剪贴板测法 ——
        # 「从外部写 → 服务端读」在沙箱环境里拿到的是隔离粘贴板，会得到假阴性，
        # 让人误判接口坏了（这个坑实际踩过）。
        req(port, "/flags", "POST", json.dumps({"clip_read": True}),
            {"Content-Type": "application/json"})
        probe = "中文测试 emoji 🎉 换行\\n第二行"
        req(port, "/clip", "POST", probe)
        s, raw = req(port, "/clip/now")
        got = raw.decode("utf-8", "replace")
        check("写进去再读出来，逐字节一致",
              s == 200 and got == probe, f"status={s} 读回={got!r}")
        req(port, "/flags", "POST", json.dumps({"clip_read": False}),
            {"Content-Type": "application/json"})
        s, _ = req(port, "/clip/now")
        check("开关关闭后 /clip/now 返回 403（剪贴板常含密码，默认就该关）",
              s == 403, f"status={s}")

        print("\n【发件箱队列】")
        s, raw = req(port, "/outbox")
        j = json.loads(raw)
        check("GET /outbox 正常", s == 200 and j.get("ok") is True)
        check("初始队列为空", j.get("total") == 0, f"total={j.get('total')}")
        with open(os.path.join(outbox, "你好.txt"), "w", encoding="utf-8") as f:
            f.write("hello")
        time.sleep(2.6)          # 后台扫描周期是 2s
        s, raw = req(port, "/outbox")
        j = json.loads(raw)
        check("拖进发件箱的文件被登记", j.get("total") == 1, f"total={j.get('total')}")
        check("元数据文件已生成",
              os.path.exists(os.path.join(outbox, ".你好.txt.json")))
        check("status PENDING 同步更新", status_map(port).get("PENDING") == "1")
        meta_calls = read_calls(log)
        check("登记后发出了通知", "notify-send" in meta_calls or "osascript" in meta_calls,
              "（Windows 通知借 PowerShell，不走这两个命令）")

        print("\n【宿主机投递（POST /outbox）】")
        s, _ = req(port, "/outbox", "POST", "第一版内容",
                   {"X-Filename": urllib.parse.quote("说明.txt")})
        check("POST /outbox 返回 200", s == 200, f"status={s}")
        note = os.path.join(outbox, "说明.txt")
        check("文件已落进发件箱", os.path.exists(note))
        s, raw = req(port, "/outbox")
        names = [x["name"] for x in json.loads(raw).get("items", [])]
        check("立刻出现在队列里（不必等 2 秒扫描周期）", "说明.txt" in names, f"队列={names}")

        # 重名必须覆盖。发件箱的语义是「最新这份要发过去」—— 若像收件箱那样加 (1) 后缀，
        # 对方会看到一串看起来一模一样的条目，根本分不清该取哪个。
        req(port, "/outbox", "POST", "第二版内容",
            {"X-Filename": urllib.parse.quote("说明.txt")})
        with open(note, encoding="utf-8") as f:
            body = f.read()
        check("同名投递是覆盖而不是复制副本", body == "第二版内容", f"内容={body!r}")
        s, raw = req(port, "/outbox")
        cnt = sum(1 for x in json.loads(raw).get("items", []) if x["name"].startswith("说明"))
        check("队列里仍然只有一份", cnt == 1, f"份数={cnt}")

        print("\n【使用说明路由】")
        s, raw = req(port, "/doc")
        check("GET /doc 返回 HTML", s == 200 and b"<html" in raw.lower(), f"status={s}")
        s, raw = req(port, "/manual-en.html")
        check("GET /manual-en.html 返回英文手册", s == 200 and b"User Guide" in raw, f"status={s}")
        s, raw = req(port, "/%E4%BD%BF%E7%94%A8%E6%89%8B%E5%86%8C.html")
        check("GET /使用手册.html 别名同 /doc", s == 200 and b"<html" in raw.lower(), f"status={s}")

        print("\n【优雅退出（/shutdown）】")
        rf = os.path.join(tmp, "runtime")
        s, _ = req(port, "/shutdown", "POST")
        check("POST /shutdown 返回 200", s == 200, f"status={s}")
        gone = False
        for _ in range(60):
            if proc.poll() is not None:
                gone = True
                break
            time.sleep(0.1)
        check("服务进程已退出", gone, f"returncode={proc.poll()}")
        check("runtime 文件已被清理（这正是 Windows 上 SIGTERM 做不到的事）",
              not os.path.exists(rf))

    finally:
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)

    total = len(_results)
    failed = [n for n, ok, _ in _results if not ok]
    print("\n" + "=" * 62)
    if failed:
        print(f"结果：{total - len(failed)}/{total} 通过，失败 {len(failed)} 项：")
        for n in failed:
            print(f"  ✗ {n}")
        sys.exit(1)
    print(f"结果：{total}/{total} 全部通过")
    print("=" * 62)


if __name__ == "__main__":
    main()
