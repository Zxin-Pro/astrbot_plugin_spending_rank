# -*- coding: utf-8 -*-
"""SpendingRank 本地渲染器 —— 纯 Pillow 绘制榜单图片。

为什么需要它
------------
插件原来只会调远端 t2i 渲染服务（html_render / t2i_endpoint）。那套服务一
限流就是 429、一没节点就是 503，插件当场出不了图，/排行榜 只能回一条报错。
这里的实现**不联网、不依赖浏览器**，只要求装了 Pillow（AstrBot 自带），
所以永远不会因为外部服务抽风而失败。

把 template.html 的版式用 Pillow 重画了一遍：同一个深蓝→青绿渐变、同样的
列宽比例、同样的前三名高亮。分辨率按 2 倍超采样绘制再缩回，圆角和文字都
没有锯齿。

对外只有一个函数：render_leaderboard(...) -> bytes
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

# 版式常量（未乘 SCALE，单位 = 最终图上的像素）
PAD_X = 40
PAD_TOP = 34
PAD_BOTTOM = 38
RADIUS = 14
HEADER_GAP = 22
FOOTER_GAP = 20
FOOTER_PAD = 16

BRAND_SIZE = 34
SUB_SIZE = 19
HEAD_SIZE = 17
NAME_SIZE = 23
NUM_SIZE = 20
COST_SIZE = 23
RANK_SIZE = 25
FOOT_SIZE = 17

ROW_PAD_H = 22
ROW_PAD_V = 12
HEAD_PAD_V = 13
COL_GAP = 12

COL_RANK = 62
COL_TOKENS = 128
COL_REQ = 92
COL_COST = 138

COL_TOKENS_NC = 150
COL_REQ_NC = 108


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
    """带柔和发光的文字（消耗列）。"""
    if not text:
        return
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(xy, text, font=font, fill=(*color, alpha), anchor=anchor)
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(_s(radius))))
    ImageDraw.Draw(img).text(xy, text, font=font, fill=(*color, 255), anchor=anchor)


# ---------------------------------------------------------------- 主渲染

def _layout(row_count: int, show_cost: bool, width: int) -> dict:
    """把所有尺寸算出来，绘制和高度计算共用一份，避免两边对不上。"""
    row_h = _s(int(NAME_SIZE * 1.42) + 2 * ROW_PAD_V)
    head_h = _s(int(HEAD_SIZE * 1.42) + 2 * HEAD_PAD_V)
    header_h = _s(int(BRAND_SIZE * 1.2) + 16 + 2)
    footer_text_h = _s(int(FOOT_SIZE * 1.42))
    footer_h = _s(FOOTER_GAP + FOOTER_PAD) + _s(1) + footer_text_h

    board_h = head_h + row_count * row_h + _s(2)
    total_h = _s(PAD_TOP) + header_h + _s(HEADER_GAP) + board_h + footer_h + _s(PAD_BOTTOM)

    inner_left = _s(PAD_X + 1 + ROW_PAD_H)
    inner_right = _s(width - PAD_X - 1 - ROW_PAD_H)
    inner_w = inner_right - inner_left
    gap = _s(COL_GAP)

    if show_cost:
        fixed = COL_RANK + COL_TOKENS + COL_REQ + COL_COST
    else:
        fixed = COL_RANK + COL_TOKENS_NC + COL_REQ_NC
    name_w = inner_w - _s(fixed) - 4 * gap

    return {
        "w": _s(width),
        "h": total_h,
        "row_h": row_h,
        "head_h": head_h,
        "header_h": header_h,
        "board_top": _s(PAD_TOP) + header_h + _s(HEADER_GAP),
        "board_h": board_h,
        "inner_left": inner_left,
        "inner_right": inner_right,
        "name_w": name_w,
        "gap": gap,
        "max_name_w": max(_s(60), name_w),
    }


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
    """把榜单数据画成图片，返回编码后的字节。"""
    # 宽度下限 760：再窄的话「用户」列会被压到十几个像素，名字全变成省略号
    width = max(760, int(image_width or 880))
    rows = list(rows or [])
    # 空榜时也留一行位置，用来画「暂无数据」，避免整块面板塌成一条缝
    L = _layout(max(1, len(rows)), show_cost, width)

    W, H = L["w"], L["h"]
    img = Image.new("RGB", (W, H))
    img.paste(_vertical_gradient((W, H), BG_STOPS))

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # ---------- 顶部：品牌 + 日期 ----------
    brand = str(brand or "")
    split = min(5, len(brand))
    f_brand = _font(_s(BRAND_SIZE), bold=True)
    base_y = _s(PAD_TOP)
    anchor_y = base_y + _s(int(BRAND_SIZE * 0.62))
    head_cut = brand[:split]
    tail_cut = brand[split:]
    d.text((_s(PAD_X), anchor_y), head_cut, font=f_brand, fill=(*CYAN, 255), anchor="lm")
    if tail_cut:
        dx = _s(PAD_X) + f_brand.getlength(head_cut)
        d.text((dx, anchor_y), tail_cut, font=f_brand, fill=(*GOLD, 255), anchor="lm")

    f_sub = _font(_s(SUB_SIZE))
    date_txt = str(meta.get("date", ""))
    sub_right = _s(width - PAD_X)
    # 副标题右对齐；留够和品牌字之间的距离，挤不下就逐级缩短
    sub_room = sub_right - (_s(PAD_X) + f_brand.getlength(brand)) - _s(16)
    sub = "TOP {}".format(len(rows))
    for candidate in (
        "全站 Token 榜  ·  {}  ·  TOP {}".format(date_txt, len(rows)),
        "{}  ·  TOP {}".format(date_txt, len(rows)),
    ):
        if f_sub.getlength(candidate) <= sub_room:
            sub = candidate
            break
    d.text((sub_right, anchor_y), sub, font=f_sub, fill=(232, 238, 245, 150), anchor="rm")

    # 顶部隔线
    sep_y = _s(PAD_TOP) + L["header_h"] - _s(2)
    d.rectangle([_s(PAD_X), sep_y, _s(width - PAD_X), sep_y + _s(1)], fill=(255, 255, 255, 34))

    # ---------- 榜单面板 ----------
    top = L["board_top"]
    d.rounded_rectangle(
        [_s(PAD_X), top, _s(width - PAD_X), top + L["board_h"]],
        radius=_s(RADIUS),
        fill=(255, 255, 255, 14),
        outline=(255, 255, 255, 24),
        width=_s(1),
    )

    left = L["inner_left"]
    right = L["inner_right"]
    gap = L["gap"]
    name_w = L["max_name_w"]

    # 列边界（右对齐列共用）
    if show_cost:
        col_tok_r = right - _s(COL_COST) - _s(COL_REQ) - 2 * gap
        col_req_r = right - _s(COL_COST) - gap
        col_cost_r = right
    else:
        col_tok_r = right - _s(COL_REQ_NC) - gap
        col_req_r = right
        col_cost_r = right
    name_x = left + _s(COL_RANK) + gap

    # ---------- 表头 ----------
    head_y = top + _s(1)
    d.rectangle([_s(PAD_X) + _s(1), head_y, _s(width - PAD_X) - _s(1), head_y + L["head_h"]],
                fill=(255, 255, 255, 9))
    hy = head_y + L["head_h"] // 2
    f_head = _font(_s(HEAD_SIZE))
    dim = (232, 238, 245, 102)
    d.text((left, hy), "排名", font=f_head, fill=dim, anchor="lm")
    d.text((name_x, hy), "用户", font=f_head, fill=dim, anchor="lm")
    d.text((col_tok_r, hy), "TOKEN", font=f_head, fill=dim, anchor="rm")
    d.text((col_req_r, hy), "请求", font=f_head, fill=dim, anchor="rm")
    if show_cost:
        d.text((col_cost_r, hy), "消耗", font=f_head, fill=dim, anchor="rm")

    # 表头与首行之间的分隔线
    row_top = head_y + L["head_h"]
    d.rectangle([_s(PAD_X) + _s(1), row_top, _s(width - PAD_X) - _s(1), row_top + _s(1)],
                fill=(255, 255, 255, 24))

    # ---------- 数据行 ----------
    f_rank = _font(_s(RANK_SIZE), bold=True, mono=True)
    f_name = _font(_s(NAME_SIZE))
    f_name_b = _font(_s(NAME_SIZE), bold=True)
    f_num = _font(_s(NUM_SIZE), mono=True)
    f_cost = _font(_s(COST_SIZE), bold=True, mono=True)

    top_stops = {
        1: ((0.0, (255, 242, 207)), (0.55, (255, 216, 130)), (1.0, (224, 165, 60))),
        2: ((0.0, (255, 255, 255)), (0.55, (207, 224, 238)), (1.0, (147, 169, 189))),
        3: ((0.0, (255, 220, 180)), (0.55, (232, 165, 107)), (1.0, (192, 122, 60))),
    }
    top_tint = {1: GOLD, 2: SILVER, 3: BRONZE}
    top_alpha = {1: 50, 2: 40, 3: 40}

    for i, row in enumerate(rows):
        y0 = row_top + i * L["row_h"]
        y1 = y0 + L["row_h"]
        cy = (y0 + y1) // 2
        rank = int(row.get("rank") or (i + 1))

        if 1 <= rank <= 3:
            tint = top_tint[rank]
            a = top_alpha[rank]
            # 左侧 4px 色条
            d.rectangle([_s(PAD_X) + _s(1), y0, _s(PAD_X) + _s(5), y1], fill=(*tint, 235))
            # 由左向右淡出的高亮底色（铺满整行，与 template.html 的 inset 位置一致）
            band_x0 = _s(PAD_X) + _s(1)
            band_x1 = _s(width - PAD_X) - _s(1)
            band_w = band_x1 - band_x0
            steps = 40
            for k in range(steps):
                x0 = band_x0 + band_w * k // steps
                x1 = band_x0 + band_w * (k + 1) // steps
                ratio = 1.0 - k / steps
                alpha = int(a * ratio)
                if alpha <= 0:
                    continue
                d.rectangle([x0, y0, x1, y1], fill=(*tint, alpha))

        # 排名
        if 1 <= rank <= 3:
            _gradient_text(layer, (left, cy), str(rank), f_rank, top_stops[rank], anchor="lm")
        else:
            d.text((left, cy), str(rank), font=f_rank, fill=(232, 238, 245, 102), anchor="lm")

        # 用户名
        name = _ellipsis(row.get("name") or "匿名用户", f_name_b if rank == 1 else f_name, name_w)
        name_color = TEXT_TOP1_NAME if rank == 1 else TEXT_NAME
        d.text((name_x, cy), name, font=(f_name_b if rank == 1 else f_name),
               fill=(*name_color, 255), anchor="lm")

        # Token / 请求
        d.text((col_tok_r, cy), row.get("tokens_display") or "0", font=f_num,
               fill=(232, 238, 245, 158), anchor="rm")
        d.text((col_req_r, cy), row.get("requests_display") or "0", font=f_num,
               fill=(232, 238, 245, 158), anchor="rm")

        # 消耗
        if show_cost:
            cost = str(row.get("cost_display") or "0")
            d.text((col_cost_r - f_cost.getlength(cost) - _s(4), cy), "$", font=_font(_s(18), mono=True),
                   fill=(*MINT, 190), anchor="rm")
            _glow_text(layer, (col_cost_r, cy), cost, f_cost, MINT, anchor="rm")

        # 行分隔线
        if i < len(rows) - 1:
            d.rectangle([_s(PAD_X) + _s(1), y1, _s(width - PAD_X) - _s(1), y1 + _s(1)],
                        fill=(255, 255, 255, 14))

    if not rows:
        d.text(
            ((left + right) // 2, row_top + L["row_h"] // 2),
            "今天还没有人上榜",
            font=_font(_s(NAME_SIZE)),
            fill=(232, 238, 245, 110),
            anchor="mm",
        )

    # ---------- 底部汇总 ----------
    foot_top = top + L["board_h"] + _s(FOOTER_GAP)
    d.rectangle([_s(PAD_X), foot_top, _s(width - PAD_X), foot_top + _s(1)],
                fill=(255, 255, 255, 34))
    fy = foot_top + _s(FOOTER_PAD) + _s(int(FOOT_SIZE * 0.7))

    f_foot = _font(_s(FOOT_SIZE))
    f_foot_b = _font(_s(FOOT_SIZE), bold=True)
    grey = (232, 238, 245, 128)

    x = _s(PAD_X)
    x = _draw_kv(d, x, fy, "上榜", "{} 人".format(meta.get("total_users", len(rows))), f_foot, f_foot_b, GOLD, grey)
    x = _draw_kv(d, x, fy, "合计 Token", str(meta.get("total_tokens_display", "0")), f_foot, f_foot_b, GOLD, grey)
    if show_cost:
        x = _draw_kv(d, x, fy, "合计消耗", "$" + str(meta.get("total_cost_display", "0")), f_foot, f_foot_b, GOLD, grey)

    # 时间戳右对齐；和左侧汇总挤在一起时逐级缩短，宁可少显示也不要叠字
    right_edge = _s(width - PAD_X)
    updated = str(meta.get("updated_at", ""))
    hhmm = updated[11:16] if len(updated) >= 16 else updated
    stamp = ""
    for candidate in ("数据更新于 " + updated, "更新于 " + hhmm, "更新 " + hhmm):
        if x + _s(16) + f_foot.getlength(candidate) <= right_edge:
            stamp = candidate
            break
    if stamp:
        d.text((right_edge, fy), stamp, font=f_foot, fill=(232, 238, 245, 130), anchor="rm")

    # ---------- 合成 ----------
    out = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    if SCALE != 1:
        out = out.resize((width, H // SCALE), Image.Resampling.LANCZOS)

    fmt = str(image_format or "png").lower()
    buf = BytesIO()
    if fmt in ("jpg", "jpeg"):
        q = min(max(int(image_quality or 92), 30), 100)
        out.save(buf, format="JPEG", quality=q, subsampling=0, optimize=True)
    else:
        out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _draw_kv(d, x, y, label, value, f_label, f_value, value_color, label_color) -> int:
    """画「标签 + 值」，返回下一个元素的起始 x。"""
    d.text((x, y), label, font=f_label, fill=label_color, anchor="lm")
    x += int(f_label.getlength(label)) + _s(5)
    d.text((x, y), value, font=f_value, fill=(*value_color, 255), anchor="lm")
    x += int(f_value.getlength(value)) + _s(20)
    return x
