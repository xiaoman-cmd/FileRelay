#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 macOS Big Sur 风格应用图标（文件互传）。

设计：圆角矩形 + 对角渐变底 + 左上柔光 + 底部内阴影 + 纯白「下箭头落入托盘」符号。
几何严格按 Apple 图标规范：1024 画布，内容区 824x824 居中，圆角半径 185.4。

用法:
    python3 make_icon.py            # 生成预览对比图 + 主方案 1024 png
    python3 make_icon.py --icns     # 额外生成 AppIcon.icns
"""
import sys, os, shutil, subprocess, math
from PIL import Image, ImageDraw, ImageChops, ImageFilter

S = 1024                 # 最终边长
SS = 4                   # 超采样倍数
C = S * SS
PAD = 100 * SS           # 内容区留边
W = C - 2 * PAD          # 内容区边长 (=824*SS)
R = int(185.4 * SS)      # 圆角半径

# 三套配色（上亮 → 下深）
THEMES = {
    "blue":   ((0x6E, 0xA4, 0xFF), (0x33, 0x46, 0xDB)),
    "violet": ((0x8C, 0x7B, 0xFF), (0x4A, 0x2F, 0xD6)),
    "teal":   ((0x5B, 0xD6, 0xE8), (0x1F, 0x6F, 0xE0)),
}


def lerp(a, b, t):
    return a + (b - a) * t


def symbol_shapes(d, X, Y, fill, tip_fill=None):
    """白色符号：下箭头 + 落入托盘 + 底线。坐标按 1024 设计空间定义。"""
    d.rounded_rectangle([X(474), Y(296), X(550), Y(516)], radius=int(38 * SS), fill=fill)
    d.polygon([(X(512), Y(614)), (X(406), Y(498)), (X(618), Y(498))], fill=fill)
    d.rounded_rectangle([X(300), Y(672), X(724), Y(730)], radius=int(29 * SS), fill=fill)
    d.rounded_rectangle([X(378), Y(786), X(646), Y(812)], radius=int(13 * SS),
                        fill=tip_fill if tip_fill else fill)


def build(theme):
    top, bot = THEMES[theme]

    # ---- 1. 轻微对角线性渐变底（旋转 15°，源图放大到刚好覆盖裁剪区，无拉伸/黑角）----
    ang = 15.0
    K = int(W * (math.cos(math.radians(ang)) + math.sin(math.radians(ang))) + 4)
    gr = Image.linear_gradient("L").resize((K, K), Image.BICUBIC)
    gr = gr.rotate(ang, resample=Image.BICUBIC, expand=True)
    cw, ch = gr.size
    crop_l, crop_t = (cw - W) // 2, (ch - W) // 2
    gr = gr.crop((crop_l, crop_t, crop_l + W, crop_t + W))
    grad = gr.point(lambda v: int(255 * (v / 255) ** 0.92))
    r = grad.point(lambda v: int(lerp(top[0], bot[0], v / 255)))
    g = grad.point(lambda v: int(lerp(top[1], bot[1], v / 255)))
    b = grad.point(lambda v: int(lerp(top[2], bot[2], v / 255)))
    bg = Image.merge("RGB", (r, g, b)).convert("RGBA")

    # ---- 2. 左上柔光（径向高光，中心落在左上角）----
    g2 = ImageChops.invert(Image.radial_gradient("L").resize((2 * W, 2 * W), Image.BICUBIC))
    g2 = g2.crop((W, W, 2 * W, 2 * W))
    g2 = g2.point(lambda v: int(255 * (v / 255) ** 2.2 * 0.50))
    ov = Image.new("RGBA", (W, W), (255, 255, 255, 0))
    ov.putalpha(g2)
    bg = Image.alpha_composite(bg, ov)

    # ---- 3. 底部内阴影（增加立体感）----
    dk = Image.linear_gradient("L").resize((W, W), Image.BICUBIC)
    dk = dk.point(lambda v: int((v / 255) ** 2.4 * 72))
    ov = Image.new("RGBA", (W, W), (10, 20, 60, 0))
    ov.putalpha(dk)
    bg = Image.alpha_composite(bg, ov)

    # ---- 4. 符号：先画柔和投影，再叠纯白 ----
    X = lambda v: int(v * SS - PAD)
    Y = lambda v: int(v * SS - PAD)

    sh = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    symbol_shapes(ImageDraw.Draw(sh), X, Y, (0, 6, 40, 110))
    sh = sh.filter(ImageFilter.GaussianBlur(SS * 5))
    sh = ImageChops.offset(sh, int(SS * 2), int(SS * 7))
    bg = Image.alpha_composite(bg, sh)

    sym = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    symbol_shapes(ImageDraw.Draw(sym), X, Y, (255, 255, 255, 255), (255, 255, 255, 135))
    bg = Image.alpha_composite(bg, sym)

    # ---- 5. 圆角裁切 ----
    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, W - 1], radius=R, fill=255)
    out = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    out.paste(bg, (PAD, PAD), mask)
    return out.resize((S, S), Image.LANCZOS)


def preview(imgs, path, bg_dark=True):
    """拼一张对比预览：三套配色 + 各尺寸缩略。"""
    bgc = (24, 26, 32, 255) if bg_dark else (245, 246, 248, 255)
    fgc = (255, 255, 255) if bg_dark else (28, 30, 36)
    pad, big = 48, 320
    small_sizes = [128, 64, 32, 16]
    W_ = pad * 2 + big * len(imgs) + pad * (len(imgs) - 1)
    H_ = pad * 2 + big + 56 + 150
    canvas = Image.new("RGBA", (W_, H_), bgc)
    d = ImageDraw.Draw(canvas)
    try:
        from PIL import ImageFont
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 22)
    except Exception:
        font = None

    for i, (name, im) in enumerate(imgs.items()):
        x = pad + i * (big + pad)
        big_im = im.resize((big, big), Image.LANCZOS)
        canvas.paste(big_im, (x, pad), big_im)
        d.text((x + 4, pad + big + 18), "theme: " + name, fill=fgc, font=font)

        # 小尺寸一排（验证 16px 可辨识度）
        sx = x + 4
        for s in small_sizes:
            thumb = im.resize((s, s), Image.LANCZOS)
            y = pad + big + 56 + (120 - s) // 2
            canvas.paste(thumb, (sx, y), thumb)
            sx += s + 18
    canvas.convert("RGB").save(path, quality=95)
    return path


def make_icns(src_png, out_icns):
    iconset = "/tmp/AppIcon.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    os.makedirs(iconset, exist_ok=True)
    pairs = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)]
    for base, scale in pairs:
        px = base * scale
        nm = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
        subprocess.run(["sips", "-z", str(px), str(px), src_png, "--out", os.path.join(iconset, nm)],
                       check=True, capture_output=True)
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", out_icns], check=True, capture_output=True)
    shutil.rmtree(iconset, ignore_errors=True)
    return out_icns


def make_ico(src_png, out_ico):
    """Windows .ico（多尺寸打包进单文件，资源管理器各视图都清晰）。
    纯 PIL 实现，不依赖 macOS 的 sips/iconutil —— 跨平台构建也能出。"""
    img = Image.open(src_png).convert("RGBA")
    img.save(out_ico, format="ICO",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return out_ico


# ---------------------------------------------------------------- 菜单栏图标
# 状态栏图标 = 「迷你 app 图标」：同样的紫罗兰渐变圆角磁贴 + 白色下箭头符号，
# 与 Dock/Finder 里的应用图标一眼同源。
#
# 为什么不沿用单色模板图（template image）：实测把该符号做成 18px 单色图后，
# 与系统 SF Symbol `arrow.down.to.line`（细箭头 + 一条横线）在真实显示尺寸下
# 几乎无法区分 —— 用户会直接判定「图标没换」。加渐变磁贴后色彩即标识，深色、
# 浅色菜单栏都能一眼认出。
#
# 小尺寸符号单独调形：更粗的竖杆、更宽的箭头、更厚的托盘，并去掉 app 图标里
# 那条装饰细线（18px 下必然糊成一片）。
MS_SHAFT = (43.5, 4.0, 56.5, 40.0)          # 竖杆；小尺寸下加粗到 13/100
MS_HEAD = ((50.0, 58.0), (25.0, 34.0), (75.0, 34.0))   # 箭头三角（宽 50）
MS_TRAY = (21.0, 68.0, 79.0, 82.0)          # 托盘；加厚到 14/100
MS_BOX = (21.0, 4.0, 79.0, 82.0)            # 符号包围盒（宽 58 × 高 78）

SYM_BOX_FULL = (300, 296, 724, 812)   # app 图标用（含底部细线）
SYM_BOX_MENU = (300, 296, 724, 730)   # 旧版菜单单色图用


def _font(size):
    """优先用系统中文黑体，避免中文渲染成方块。"""
    from PIL import ImageFont
    for p in ("/System/Library/Fonts/PingFang.ttc",
              "/System/Library/Fonts/Supplemental/Songti.ttc",
              "/Library/Fonts/Arial Unicode.ttf",
              "/System/Library/Fonts/Supplemental/Arial.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return None


def _draw_menu_glyph(d, cx, cy, h, fill=(255, 255, 255, 255), width_ratio=1.0):
    """在 (cx, cy) 居中绘制高度 h 的小尺寸优化符号（单位 → 像素）。"""
    x0, y0, x1, y1 = MS_BOX
    bw, bh = x1 - x0, y1 - y0
    k = h / bh
    ox = cx - (bw * k) / 2 - x0 * k
    oy = cy - (bh * k) / 2 - y0 * k
    P = lambda vx, vy: (vx * k + ox, vy * k + oy)

    sx0, sy0, sx1, sy1 = MS_SHAFT
    p0, p1 = P(sx0, sy0), P(sx1, sy1)
    # 以中线为轴按 width_ratio 微调竖杆宽度
    mid = (p0[0] + p1[0]) / 2
    half = (p1[0] - p0[0]) / 2 * width_ratio
    d.rounded_rectangle([mid - half, p0[1], mid + half, p1[1]],
                        radius=max(0.3, 6.5 * k * width_ratio), fill=fill)
    d.polygon([P(*MS_HEAD[0]), P(*MS_HEAD[1]), P(*MS_HEAD[2])], fill=fill)
    t0, t1 = P(MS_TRAY[0], MS_TRAY[1]), P(MS_TRAY[2], MS_TRAY[3])
    d.rounded_rectangle([t0[0], t0[1], t1[0], t1[1]], radius=max(0.3, 4.5 * k), fill=fill)


def _tile_bg(px_big, theme):
    """与应用图标同源的 15° 对角渐变底（旋转后中心裁剪，无拉伸/黑角）。"""
    top, bot = THEMES[theme]
    ang = 15.0
    K = int(px_big * (math.cos(math.radians(ang)) + math.sin(math.radians(ang))) + 4)
    gr = Image.linear_gradient("L").resize((K, K), Image.BICUBIC)
    gr = gr.rotate(ang, resample=Image.BICUBIC, expand=True)
    cw, ch = gr.size
    crop_l, crop_t = (cw - px_big) // 2, (ch - px_big) // 2
    gr = gr.crop((crop_l, crop_t, crop_l + px_big, crop_t + px_big))
    grad = gr.point(lambda v: int(255 * (v / 255) ** 0.92))
    r = grad.point(lambda v: int(lerp(top[0], bot[0], v / 255)))
    g = grad.point(lambda v: int(lerp(top[1], bot[1], v / 255)))
    b = grad.point(lambda v: int(lerp(top[2], bot[2], v / 255)))
    return Image.merge("RGB", (r, g, b)).convert("RGBA")


def build_menu_icon(px, style="tile", theme="violet", fill=(0, 0, 0, 255),
                    glyph_ratio=0.73, corner_ratio=0.225):
    """生成菜单栏状态项图标。

    px    : 目标像素边长（18 与 36 各出一份，后者供 Retina 使用）
    style : "tile" 迷你 app 图标（默认）｜"mono" 单色模板图（随菜单栏反色）
    """
    SS2 = 8                      # 小图必须超采样，否则圆角/斜边发毛
    big = px * SS2
    im = Image.new("RGBA", (big, big), (0, 0, 0, 0))

    if style == "mono":
        d = ImageDraw.Draw(im)
        _draw_menu_glyph(d, big / 2, big / 2, big * 0.84, fill)
        return im.resize((px, px), Image.LANCZOS)

    # --- tile：与应用图标同源的渐变圆角磁贴 + 白色符号 ---
    bg = _tile_bg(big, theme)
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, big - 1, big - 1], radius=int(big * corner_ratio), fill=255)
    bg.putalpha(mask)
    d = ImageDraw.Draw(bg)
    # 符号在磁贴内居中，略微下移以平衡视觉重心（箭头向下、托盘在下）
    _draw_menu_glyph(d, big / 2, big / 2 + big * 0.012, big * glyph_ratio,
                     (255, 255, 255, 255))
    return bg.resize((px, px), Image.LANCZOS)


def menu_preview(path, app_icon=None, theme="violet"):
    """模拟菜单栏真实观感：深色行与浅色行各排一行，
    展示「迷你 app 图标磁贴」在 18px / Retina 36px 下的样子，并附单色版对照。"""
    dark = (44, 44, 46, 255)
    light = (236, 236, 238, 255)
    W2, H2 = 780, 300
    row_h = H2 // 2
    canvas = Image.new("RGBA", (W2, H2), dark)
    d = ImageDraw.Draw(canvas)
    d.rectangle([0, row_h, W2, H2], fill=light)
    f = _font(16)
    f2 = _font(14)
    labels = [
        ("18px 实尺寸（1x）", lambda col: build_menu_icon(18, "tile", theme)),
        ("18px 最近邻 4x", lambda col: build_menu_icon(18, "tile", theme).resize((72, 72), Image.NEAREST)),
        ("Retina 36px 实尺寸（1x 显示）", lambda col: build_menu_icon(36, "tile", theme).resize((36, 36), Image.LANCZOS)),
        ("单色模板版 18px", lambda col: build_menu_icon(18, "mono", theme, fill=col)),
        ("应用图标（同源，参考）", lambda col: (app_icon.resize((72, 72), Image.LANCZOS)
                                              if app_icon is not None else Image.new("RGBA", (1, 1)))),
    ]
    for i, (col, lab_col) in enumerate([((255, 255, 255, 255), (238, 238, 242)),
                                        ((0, 0, 0, 255), (36, 36, 42))]):
        y0 = i * row_h
        x = 30
        for text, fn in labels:
            im = fn(col)
            side = im.size[0]
            canvas.paste(im, (x, y0 + (row_h - side) // 2 - 14), im)
            d.text((x, y0 + row_h - 34), text.split("（")[0], fill=lab_col, font=f2)
            x += max(side, 96) + 26
    canvas.convert("RGB").save(path, quality=95)
    return path


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    # --out <dir>：产物输出目录（构建脚本传 dist/，src/ 里只放源码）。缺省输出到脚本所在目录。
    out = here
    if "--out" in sys.argv:
        cand = sys.argv[sys.argv.index("--out") + 1]
        out = os.path.abspath(cand)
        os.makedirs(out, exist_ok=True)
    # --no-preview：只产出构建真正需要的图标，跳过配色对比/预览图。
    # 构建脚本用这个开关，避免每次构建都在产物目录里撒一堆只能在挑配色时才有用的图。
    no_preview = "--no-preview" in sys.argv
    imgs = {k: build(k) for k in THEMES}

    if not no_preview:
        for k, im in imgs.items():
            im.save(os.path.join(out, f"icon_preview_{k}.png"))
        preview(imgs, os.path.join(out, "图标方案对比.png"))

    # 主方案：--theme <name> 指定，默认 blue
    main_theme = "blue"
    if "--theme" in sys.argv:
        cand = sys.argv[sys.argv.index("--theme") + 1]
        if cand not in THEMES:
            raise SystemExit(f"未知配色 {cand}，可选：{', '.join(THEMES)}")
        main_theme = cand

    main_png = os.path.join(out, "AppIcon_1024.png")
    imgs[main_theme].save(main_png)
    print(f"主方案({main_theme}) 1024 ->", main_png)
    if not no_preview:
        print("对比图 ->", os.path.join(out, "图标方案对比.png"))

    if "--icns" in sys.argv:
        icns = os.path.join(out, "AppIcon.icns")
        make_icns(main_png, icns)
        print(f"icns({main_theme}) ->", icns, os.path.getsize(icns), "bytes")

    if "--ico" in sys.argv:
        ico = os.path.join(out, "AppIcon.ico")
        make_ico(main_png, ico)
        print(f"ico({main_theme}) ->", ico, os.path.getsize(ico), "bytes")

    if "--menu-icon" in sys.argv:
        style = "tile"
        if "--menu-style" in sys.argv:
            style = sys.argv[sys.argv.index("--menu-style") + 1]
        if style not in ("tile", "mono"):
            raise SystemExit("--menu-style 只能是 tile 或 mono")
        for px, fn in ((18, "MenuIcon.png"), (36, "MenuIcon@2x.png")):
            build_menu_icon(px, style=style, theme=main_theme).save(os.path.join(out, fn))
            print(f"菜单栏图标 -> {fn} ({px}x{px}, {style})")
        # 记录风格：tile 是彩色磁贴（不可当模板图，否则会被压成纯黑方块）；
        # mono 是透明模板图（需要 setTemplate:true 才能随菜单栏反色）
        with open(os.path.join(out, "MenuIcon.style"), "w") as f:
            f.write(style)
        print(f"菜单栏图标风格 -> {style}")
        if not no_preview:
            menu_preview(os.path.join(out, "菜单栏图标预览.png"),
                         app_icon=imgs[main_theme], theme=main_theme)
            print("菜单栏预览 -> 菜单栏图标预览.png")
