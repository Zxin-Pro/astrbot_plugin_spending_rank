# -*- coding: utf-8 -*-
"""SpendingRank 本地渲染器 —— 纯 Pillow 绘制榜单图片。

为什么需要它
------------
插件原来只会调远端 t2i 渲染服务（html_render / t2i_endpoint）。那套服务一
限流就是 429、一没节点就是 503，插件当场出不了图，/排行榜 只能回一条报错。
这里的实现**不联网、不依赖浏览器**，只要求装了 Pillow（AstrBot 自带），
所以永远不会因为外部服务抽风而失败。

版式用 Pillow 重画：深蓝→青绿渐变底、前三名金银铜高亮、主指标发光。
支持单榜与双榜并排（一张图里左右两个榜），分辨率按 2 倍超采样绘制再缩回。

对外入口：
    render_panels(panels, meta, ...) -> bytes     # 一个或多个面板并排
    render_leaderboard(rows, meta, ...) -> bytes  # 单榜，兼容旧调用
"""
from __future__ import annotations

import os
from io import BytesIO
from typing import Any, Iterable, Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------- 调色板

GOLD = (255, 216, 130)
CYAN = (111, 211, 255)
MINT = (110, 231, 183)
TEXT = (232, 238, 245)
TEXT_NAME = (242, 247, 252)
TEXT_TOP1_NAME = (255, 246, 226)
SILVER = (207, 224, 238)
BRONZE = (232, 165, 107)

BG_STOPS = ((0.0, (16, 23, 37)), (0.55, (22, 35, 58)), (1.0, (18, 48, 60)))

SCALE = 2  # 超采样倍数


# ---------------------------------------------------------------- 字体

_CJK_BOLD = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyhbd.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
)

_CJK_REGULAR = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyh.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
)

_MONO_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "C:/Windows/Fonts/consolab.ttf",
    "/System/Library/Fonts/Menlo.ttc",
)

_MONO_REGULAR = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "C:/Windows/Fonts/consola.ttf",
    "/System/Library/Fonts/Menlo.ttc",
)

_font_cache: dict[tuple[str, int], Any] = {}


def _first_existing(paths: Iterable[str]) -> str:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return ""


def _load(path: str, size: int):
    """加载字体；.ttc 优先取简体中文那一个 face（index=2），失败再回落到 0。"""
    key = (path, size)
    hit = _font_cache.get(key)
    if hit is not None:
        return hit

    font = None
    if path.lower().endswith(".ttc"):
        for index in (2, 0, 1, 3):
            try:
                font = ImageFont.truetype(path, size, index=index)
                break
            except Exception:
                continue
    if font is None:
        try:
            font = ImageFont.truetype(path, size)
        except Exception:
            font = ImageFont.load_default(size=size)

    _font_cache[key] = font
    return font


def _font(size: int, bold: bool = False, mono: bool = False):
    """按用途取字体，自动在本机找最合适的一套。"""
    size = int(size)
    if mono:
        path = _first_existing(_MONO_BOLD if bold else _MONO_REGULAR)
        if path:
            return _load(path, size)
    path = _first_existing(_CJK_BOLD if bold else _CJK_REGULAR)
    if path:
        return _load(path, size)
    for cand in (_CJK_REGULAR + _MONO_REGULAR):
        if os.path.exists(cand):
            return _load(cand, size)
    return ImageFont.load_default(size=size)


# ---------------------------------------------------------------- 小工具

def _s(v: float) -> int:
    """逻辑尺寸 → 绘制尺寸（乘超采样倍数）。"""
    return int(round(v * SCALE))


def _ellipsis(text: str, font, max_w: int) -> str:
    """单行超宽就截断加省略号。"""
    text = str(text or "")
    if not text:
        return ""
    if font.getlength(text) <= max_w:
        return text
    ell = "…"
    ell_w = font.getlength(ell)
    if ell_w > max_w:
        return ""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.getlength(text[:mid]) + ell_w <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + ell


def _lerp(c1: Sequence[int], c2: Sequence[int], t: float) -> tuple[int, int, int]:
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def _stop_color(stops: Sequence[tuple[float, Sequence[int]]], t: float) -> tuple[int, int, int]:
    t = min(max(t, 0.0), 1.0)
    for i in range(len(stops) - 1):
        p0, c0 = stops[i]
        p1, c1 = stops[i + 1]
        if t <= p1:
            span = p1 - p0
            local = 0.0 if span <= 0 else (t - p0) / span
            return _lerp(c0, c1, local)
    return tuple(stops[-1][1])  # type: ignore[return-value]


def _vertical_gradient(size: tuple[int, int], stops) -> Image.Image:
    """竖直渐变。先画 256 高的小图再放大，Pillow 的双线性插值负责抹平色阶。"""
    w, h = size
    w, h = max(1, w), max(1, h)
    strip_h = min(256, h)
    strip = Image.new("RGB", (1, strip_h))
    px = strip.load()
    for y in range(strip_h):
        px[0, y] = _stop_color(stops, y / max(1, strip_h - 1))
    return strip.resize((w, h), Image.Resampling.BILINEAR)


def _gradient_text(
    target: Image.Image,
    xy: tuple[int, int],
    text: str,
    font,
    stops,
    anchor: str = "la",
) -> None:
    """用竖直渐变填充文字（名次的金银铜就是这样画出来的）。"""
    if not text:
        return

    # mask 一律按 "la" 锚点画（这样文字恰好铺满 mask），
    # 再用 "lm" 的 bbox 把这块 mask 对回调用方给的中心点。
    box = font.getbbox(text)                      # anchor="la"
    w = int(box[2] - box[0])
    h = int(box[3] - box[1])
    if w <= 0 or h <= 0:
        return

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text((-box[0], -box[1]), text, font=font, fill=255)

    rel = font.getbbox(text, anchor=anchor)       # 相对锚点的偏移
    target.paste(
        _vertical_gradient((w, h), stops),
        (xy[0] + int(rel[0]), xy[1] + int(rel[1])),
        mask,
    )


def _glow_text(
    img: Image.Image,
    xy: tuple[int, int],
    text: str,
    font,
    color: Sequence[int],
    anchor: str = "ra",
    radius: float = 5.0,
    alpha: int = 130,
) -> None:
    """带柔和发光的文字（主指标列）。"""
    if not text:
        return
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(xy, text, font=font, fill=(*color, alpha), anchor=anchor)
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(_s(radius))))
    ImageDraw.Draw(img).text(xy, text, font=font, fill=(*color, 255), anchor=anchor)


def _draw_kv(d, x, y, label, value, f_label, f_value, value_color, label_color) -> int:
    """画「标签 + 值」，返回下一个元素的起始 x。"""
    d.text((x, y), label, font=f_label, fill=label_color, anchor="lm")
    x += int(f_label.getlength(label)) + _s(5)
    d.text((x, y), value, font=f_value, fill=(*value_color, 255), anchor="lm")
    x += int(f_value.getlength(value)) + _s(20)
    return x


# ---------------------------------------------------------------- 版式规划

def _plan(panels: Sequence[dict], show_cost: bool, image_width: int) -> dict:
    """把宽度、字体、列宽一次算好，绘制与高度计算共用同一份。"""
    n = max(1, len(panels))
    dual = n >= 2
    base = int(image_width or 880)

    if dual:
        # 双榜：每栏至少 600，否则用户列会窄到只能放省略号
        panel_w = max(600, int(round(base * 0.76)))
        pad_x = 28
        panel_gap = 24
        fs = dict(brand=30, sub=17, title=19, head=16, name=21, num=19,
                  cost=21, rank=23, foot=16)
        geo = dict(rank=46, gap=10, row_v=11, head_v=12, row_pad_h=20, title_h=38,
                   radius=14, head_gap=20)
        col_w = dict(tokens=96, cost=104, requests=84)
    else:
        panel_w = max(760, base)
        pad_x = 40
        panel_gap = 0
        fs = dict(brand=34, sub=19, title=0, head=17, name=23, num=20,
                  cost=23, rank=25, foot=17)
        geo = dict(rank=62, gap=12, row_v=12, head_v=13, row_pad_h=22, title_h=0,
                   radius=14, head_gap=22)
        col_w = dict(tokens=128, cost=138, requests=92)

    total_w = pad_x * 2 + panel_w * n + panel_gap * (n - 1)

    # 面板内部可用宽度
    inner_pad = 1 + geo["row_pad_h"]
    inner_w = panel_w - 2 * inner_pad

    # 每一栏的列：双榜只放「主指标 + 次指标」，单榜放三列
    def columns_for(panel: dict) -> list[dict]:
        metric = str(panel.get("metric") or "tokens")
        cols: list[dict] = []
        if dual or not show_cost:
            first = "tokens" if metric == "tokens" else "cost"
            second = "cost" if metric == "tokens" else "tokens"
            cols.append(dict(key=first, w=col_w[first], primary=True,
                             label="TOKEN" if first == "tokens" else "消耗"))
            if show_cost or second != "cost":
                cols.append(dict(key=second, w=col_w[second], primary=False,
                                 label="消耗" if second == "cost" else "TOKEN"))
        else:
            # 单榜：主指标紧挨用户列排最左，再跟请求，次指标放最后
            first = "tokens" if metric == "tokens" else "cost"
            second = "cost" if first == "tokens" else "tokens"
            cols.append(dict(key=first, w=col_w[first], primary=True,
                             label="TOKEN" if first == "tokens" else "消耗"))
            cols.append(dict(key="requests", w=col_w["requests"], primary=False, label="请求"))
            cols.append(dict(key=second, w=col_w[second], primary=False,
                             label="消耗" if second == "cost" else "TOKEN"))
        return cols

    return dict(
        n=n, dual=dual, panel_w=panel_w, pad_x=pad_x, panel_gap=panel_gap,
        total_w=total_w, inner_w=inner_w, inner_pad=inner_pad, fs=fs, geo=geo,
        columns_for=columns_for, show_cost=show_cost,
    )


def _fit_columns(plan: dict, cols: list[dict], panel: dict) -> None:
    """按该榜实际最长的数字放宽列宽。

    金额位数是会变的（$6.5327 和 $818.0253 差出好几个字符），列宽写死时
    长金额会把它的 $ 挤进左边一列，看起来像两个数字粘在一起。
    """
    rows = panel.get("rows") or []
    if not rows:
        return

    fs = plan["fs"]
    f_cost = _font(_s(fs["cost"]), bold=True, mono=True)
    f_cur = _font(_s(int(fs["cost"] * 0.78)), mono=True)
    f_num = _font(_s(fs["num"]), mono=True)
    cur_w = f_cur.getlength("$")

    need = {"cost": 0.0, "tokens": 0.0}
    for row in rows:
        cost_txt = str(row.get("cost_display") or "0")
        need["cost"] = max(need["cost"], cur_w + f_cost.getlength(cost_txt) + _s(7))
        need["tokens"] = max(
            need["tokens"], f_num.getlength(str(row.get("tokens_display") or "0")) + _s(6)
        )

    for col in cols:
        want = need.get(col["key"], 0.0)
        if want > 0:
            col["w"] = max(col["w"], int(want / SCALE) + 2)


def _col_positions(plan: dict, panel: dict, x0: int, x1: int) -> list[dict]:
    """从右往左排列表头/数字列，返回带右边界坐标的列信息。"""
    geo = plan["geo"]
    gap = _s(geo["gap"])
    right = x1 - _s(plan["inner_pad"])
    cols = plan["columns_for"](panel)
    _fit_columns(plan, cols, panel)
    out = []
    for col in reversed(cols):
        w = _s(col["w"])
        out.append(dict(col, x_right=right, w=w))
        right -= w + gap
    out.reverse()
    return out


def _name_span(plan: dict, cols: list[dict], x0: int) -> tuple[int, int]:
    """用户列可用的 x 起点与宽度。"""
    geo = plan["geo"]
    gap = _s(geo["gap"])
    name_x = x0 + _s(plan["inner_pad"]) + _s(geo["rank"]) + gap
    if cols:
        limit = cols[0]["x_right"] - cols[0]["w"] - gap
    else:
        limit = x0 + _s(plan["panel_w"] - plan["inner_pad"])
    return name_x, max(_s(50), limit - name_x)


# ---------------------------------------------------------------- 面板绘制

TOP_STOPS = {
    1: ((0.0, (255, 242, 207)), (0.55, (255, 216, 130)), (1.0, (224, 165, 60))),
    2: ((0.0, (255, 255, 255)), (0.55, (207, 224, 238)), (1.0, (147, 169, 189))),
    3: ((0.0, (255, 220, 180)), (0.55, (232, 165, 107)), (1.0, (192, 122, 60))),
}
TOP_TINT = {1: GOLD, 2: SILVER, 3: BRONZE}
TOP_ALPHA = {1: 50, 2: 40, 3: 40}

_DIM = (232, 238, 245, 102)
_SOFT = (232, 238, 245, 158)


def _draw_panel(d, layer, plan: dict, panel: dict, x0: int, y0: int,
                x1: int, row_count: int, show_cost: bool) -> int:
    """画一个榜单面板，返回面板高度（绘制尺寸）。"""
    fs, geo = plan["fs"], plan["geo"]
    gap = _s(geo["gap"])
    row_h = _s(int(fs["name"] * 1.42) + 2 * geo["row_v"])
    head_h = _s(int(fs["head"] * 1.42) + 2 * geo["head_v"])
    title_h = _s(geo["title_h"])
    panel_h = title_h + head_h + row_count * row_h + _s(2)

    # 面板底板
    d.rounded_rectangle(
        [x0, y0, x1, y0 + panel_h],
        radius=_s(geo["radius"]),
        fill=(255, 255, 255, 14),
        outline=(255, 255, 255, 24),
        width=_s(1),
    )

    left = x0 + _s(plan["inner_pad"])
    right = x1 - _s(plan["inner_pad"])
    cols = _col_positions(plan, panel, x0, x1)
    name_x, name_w = _name_span(plan, cols, x0)

    # 面板标题（双榜才有）
    if title_h:
        ty = y0 + title_h // 2
        title = str(panel.get("title") or "")
        f_title = _font(_s(fs["title"]), bold=True)
        accent = MINT if str(panel.get("metric")) == "cost" else CYAN
        d.rectangle([left, ty - _s(9), left + _s(3), ty + _s(9)], fill=(*accent, 235))
        d.text((left + _s(12), ty), title, font=f_title, fill=(*TEXT_NAME, 255), anchor="lm")
        sub = str(panel.get("subtitle") or "")
        if sub:
            d.text((right, ty), sub, font=_font(_s(fs["head"])), fill=_DIM, anchor="rm")

    # 表头
    head_y = y0 + title_h
    hy = head_y + head_h // 2
    f_head = _font(_s(fs["head"]))
    d.text((left, hy), "排名", font=f_head, fill=_DIM, anchor="lm")
    d.text((name_x, hy), "用户", font=f_head, fill=_DIM, anchor="lm")
    for col in cols:
        d.text((col["x_right"], hy), col["label"], font=f_head, fill=_DIM, anchor="rm")

    row_top = head_y + head_h
    d.rectangle([x0 + _s(1), row_top, x1 - _s(1), row_top + _s(1)], fill=(255, 255, 255, 24))

    f_rank = _font(_s(fs["rank"]), bold=True, mono=True)
    f_name = _font(_s(fs["name"]))
    f_name_b = _font(_s(fs["name"]), bold=True)
    f_num = _font(_s(fs["num"]), mono=True)
    f_cost = _font(_s(fs["cost"]), bold=True, mono=True)
    f_cur = _font(_s(int(fs["cost"] * 0.78)), mono=True)

    rows = list(panel.get("rows") or [])
    for i, row in enumerate(rows):
        ry0 = row_top + i * row_h
        ry1 = ry0 + row_h
        cy = (ry0 + ry1) // 2
        rank = int(row.get("rank") or (i + 1))

        if 1 <= rank <= 3:
            tint = TOP_TINT[rank]
            d.rectangle([x0 + _s(1), ry0, x0 + _s(5), ry1], fill=(*tint, 235))
            band_x0, band_x1 = x0 + _s(1), x1 - _s(1)
            band_w = band_x1 - band_x0
            steps = 32
            for k in range(steps):
                bx0 = band_x0 + band_w * k // steps
                bx1 = band_x0 + band_w * (k + 1) // steps
                alpha = int(TOP_ALPHA[rank] * (1.0 - k / steps))
                if alpha > 0:
                    d.rectangle([bx0, ry0, bx1, ry1], fill=(*tint, alpha))

        # 名次
        if 1 <= rank <= 3:
            _gradient_text(layer, (left, cy), str(rank), f_rank, TOP_STOPS[rank], anchor="lm")
        else:
            d.text((left, cy), str(rank), font=f_rank, fill=_DIM, anchor="lm")

        # 用户名
        f_use = f_name_b if rank == 1 else f_name
        name = _ellipsis(row.get("name") or "匿名用户", f_use, name_w)
        color = TEXT_TOP1_NAME if rank == 1 else TEXT_NAME
        d.text((name_x, cy), name, font=f_use, fill=(*color, 255), anchor="lm")

        # 指标列
        for col in cols:
            key = col["key"]
            if key == "cost":
                txt = str(row.get("cost_display") or "0")
                if col["primary"]:
                    d.text((col["x_right"] - f_cost.getlength(txt) - _s(3), cy), "$",
                           font=f_cur, fill=(*MINT, 190), anchor="rm")
                    _glow_text(layer, (col["x_right"], cy), txt, f_cost, MINT, anchor="rm")
                else:
                    d.text((col["x_right"] - f_cost.getlength(txt) - _s(3), cy), "$",
                           font=f_cur, fill=(232, 238, 245, 120), anchor="rm")
                    d.text((col["x_right"], cy), txt, font=f_cost, fill=_SOFT, anchor="rm")
            elif key == "tokens":
                txt = str(row.get("tokens_display") or "0")
                if col["primary"]:
                    _glow_text(layer, (col["x_right"], cy), txt, f_num, CYAN, anchor="rm")
                else:
                    d.text((col["x_right"], cy), txt, font=f_num, fill=_SOFT, anchor="rm")
            else:
                d.text((col["x_right"], cy), str(row.get("requests_display") or "0"),
                       font=f_num, fill=_SOFT, anchor="rm")

        if i < len(rows) - 1:
            d.rectangle([x0 + _s(1), ry1, x1 - _s(1), ry1 + _s(1)], fill=(255, 255, 255, 14))

    if not rows:
        d.text(((left + right) // 2, row_top + row_count * row_h // 2), "今天还没有人上榜",
               font=_font(_s(fs["name"])), fill=(232, 238, 245, 110), anchor="mm")

    return panel_h


# ---------------------------------------------------------------- 主入口

def render_panels(
    panels: Sequence[dict],
    meta: dict,
    *,
    brand: str = "WorldCodes",
    image_width: int = 880,
    show_cost: bool = True,
    image_format: str = "png",
    image_quality: int = 92,
) -> bytes:
    """把一到两个榜单面板画进同一张图，返回编码后的字节。

    panels 每项：{"title": 面板标题, "metric": "tokens"|"cost", "rows": [...]}
    两个面板时会左右并排（左边通常是 Token 榜，右边是消耗榜）。
    """
    clean = [p for p in (panels or []) if isinstance(p, dict)]
    if not clean:
        clean = [dict(title="", metric="tokens", rows=[])]

    plan = _plan(clean, show_cost, image_width)
    fs, geo = plan["fs"], plan["geo"]

    row_n = max(1, max([len(p.get("rows") or []) for p in clean] + [1]))
    row_h = _s(int(fs["name"] * 1.42) + 2 * geo["row_v"])
    head_h = _s(int(fs["head"] * 1.42) + 2 * geo["head_v"])
    title_h = _s(geo["title_h"])
    panel_h = title_h + head_h + row_n * row_h + _s(2)

    header_h = _s(int(fs["brand"] * 1.2) + 16 + 2)
    footer_h = _s(20 + 16) + _s(1) + _s(int(fs["foot"] * 1.42))
    total_h = _s(34) + header_h + _s(geo["head_gap"]) + panel_h + footer_h + _s(38)

    W, H = _s(plan["total_w"]), total_h
    img = Image.new("RGB", (W, H))
    img.paste(_vertical_gradient((W, H), BG_STOPS))

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    pad_x = _s(plan["pad_x"])
    right_edge = _s(plan["total_w"] - plan["pad_x"])

    # ---------- 顶栏：品牌 + 副标题 ----------
    brand = str(brand or "")
    split = min(5, len(brand))
    f_brand = _font(_s(fs["brand"]), bold=True)
    base_y = _s(34)
    anchor_y = base_y + _s(int(fs["brand"] * 0.62))
    head_cut, tail_cut = brand[:split], brand[split:]
    d.text((pad_x, anchor_y), head_cut, font=f_brand, fill=(*CYAN, 255), anchor="lm")
    if tail_cut:
        d.text((pad_x + f_brand.getlength(head_cut), anchor_y), tail_cut,
               font=f_brand, fill=(*GOLD, 255), anchor="lm")

    f_sub = _font(_s(fs["sub"]))
    date_txt = str(meta.get("date", ""))
    top_n = max([len(p.get("rows") or []) for p in clean] + [0])
    sub_room = right_edge - (pad_x + f_brand.getlength(brand)) - _s(16)
    if len(clean) > 1:
        kinds = "全站双榜"
    elif str(clean[0].get("metric")) == "cost":
        kinds = "全站消耗榜"
    else:
        kinds = "全站 Token 榜"
    sub = "TOP {}".format(top_n)
    for candidate in (
        "{}  ·  {}  ·  TOP {}".format(kinds, date_txt, top_n),
        "{}  ·  TOP {}".format(date_txt, top_n),
    ):
        if f_sub.getlength(candidate) <= sub_room:
            sub = candidate
            break
    d.text((right_edge, anchor_y), sub, font=f_sub, fill=(232, 238, 245, 150), anchor="rm")

    sep_y = _s(34) + header_h - _s(2)
    d.rectangle([pad_x, sep_y, right_edge, sep_y + _s(1)], fill=(255, 255, 255, 34))

    # ---------- 各面板 ----------
    board_top = _s(34) + header_h + _s(geo["head_gap"])
    for idx, panel in enumerate(clean):
        x0 = pad_x + idx * (_s(plan["panel_w"]) + _s(plan["panel_gap"]))
        x1 = x0 + _s(plan["panel_w"])
        _draw_panel(d, layer, plan, panel, x0, board_top, x1, row_n, show_cost)

    # ---------- 底部汇总 ----------
    foot_top = board_top + panel_h + _s(20)
    d.rectangle([pad_x, foot_top, right_edge, foot_top + _s(1)], fill=(255, 255, 255, 34))
    fy = foot_top + _s(16) + _s(int(fs["foot"] * 0.7))

    f_foot = _font(_s(fs["foot"]))
    f_foot_b = _font(_s(fs["foot"]), bold=True)
    grey = (232, 238, 245, 128)

    x = pad_x
    x = _draw_kv(d, x, fy, "上榜", "{} 人".format(meta.get("total_users", top_n)),
                 f_foot, f_foot_b, GOLD, grey)
    x = _draw_kv(d, x, fy, "合计 Token", str(meta.get("total_tokens_display", "0")),
                 f_foot, f_foot_b, GOLD, grey)
    if show_cost:
        x = _draw_kv(d, x, fy, "合计消耗", "$" + str(meta.get("total_cost_display", "0")),
                     f_foot, f_foot_b, GOLD, grey)

    updated = str(meta.get("updated_at", ""))
    hhmm = updated[11:16] if len(updated) >= 16 else updated
    for candidate in ("数据更新于 " + updated, "更新于 " + hhmm, "更新 " + hhmm):
        if x + _s(16) + f_foot.getlength(candidate) <= right_edge:
            d.text((right_edge, fy), candidate, font=f_foot,
                   fill=(232, 238, 245, 130), anchor="rm")
            break

    # ---------- 合成 ----------
    out = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    if SCALE != 1:
        out = out.resize((plan["total_w"], total_h // SCALE), Image.Resampling.LANCZOS)

    fmt = str(image_format or "png").lower()
    buf = BytesIO()
    if fmt in ("jpg", "jpeg"):
        q = min(max(int(image_quality or 92), 30), 100)
        out.save(buf, format="JPEG", quality=q, subsampling=0, optimize=True)
    else:
        out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_leaderboard(
    rows: Sequence[dict],
    meta: dict,
    *,
    brand: str = "WorldCodes",
    image_width: int = 880,
    show_cost: bool = True,
    image_format: str = "png",
    image_quality: int = 92,
) -> bytes:
    """单榜入口（兼容旧调用）。"""
    return render_panels(
        [dict(title="", metric="tokens", rows=list(rows or []))],
        meta,
        brand=brand,
        image_width=image_width,
        show_cost=show_cost,
        image_format=image_format,
        image_quality=image_quality,
    )
