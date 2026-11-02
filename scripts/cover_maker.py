# -*- coding: utf-8 -*-
"""
封面与标题 (T15): 用素材打分 (shot_quality.py) 挑候选封面帧, 排上大字标题, 输出 N 张候选封面 + 对比拼图。

    python cover_maker.py <视频> -o <输出目录> [--titles "标题一" "标题二" ...] [--subtitle "副标题"]
                          [-n 6] [--format vertical|horizontal|square|portrait_3x4] [--font 剪映字体名]
                          [--analysis analysis.json] [--quality quality.json] [--every 0.5] [--json]

    # 只要标题生成提示 (标题文案由调用方 agent 写, 写好后再用 --titles 传回来)
    python cover_maker.py <视频> -o <输出目录> --prompt-only [--analysis analysis.json]

挑帧: 清晰 / 稳定 / 曝光好 (shot_quality 汇总分) + 有正脸、眼睛可见、脸大小和位置合适 (加分);
      跳过转场帧、黑帧, 以及 analysis.json 镜头切换点前后 --avoid 秒内的帧; 候选之间至少隔 --gap 秒。
排版: 画幅取 references/formats/<id>.json 的画布与安全区 (竖屏再收进封面中央 1080x1320, 见
      methodology/safe_area.md); 标题区在安全区的上带或下带二选一, 避开人脸;
      用 utils/text_metrics.fit 按真实字宽折行 / 缩字号 (methodology/layout.md), 白 / 黄字粗黑描边,
      背景亮时加渐变压暗, 并记录字色对背景的对比度。
输出 (目录里):
  cover_01.jpg ... cover_NN.jpg   候选封面 (画幅分辨率)
  contact_sheet.jpg              对比拼图 (缩略图约等于手机上的大小, 顺便检查小尺寸下标题是否看得清)
  title_prompt.txt               写标题的提示 (给调用方 agent)
  covers.json                    候选帧、分数、标题排版 (区域 / 字号 / 行)、对比度、警告
"""

import argparse
import json
import os
import sys
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shot_quality  # noqa: E402
from utils.cli_protocol import emit_result, make_result  # noqa: E402
from utils.text_metrics import (
    LINE_HEIGHT,
    PX_PER_SIZE,
    contrast,
    fit,
    font_file,
    luminance,
)  # noqa: E402

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORMATS_DIR = os.path.join(SKILL_ROOT, "references", "formats")
VERTICAL_COVER_SAFE_Y = 1320 / 1920  # 竖屏封面中央安全区 1080x1320 (safe_area.md)
TITLE_MAX_CJK = 8  # 主标题 <= 8 个字 (safe_area.md)
ZONE_SHARE = 0.42  # 标题区占安全区高度的比例
MIN_TITLE_PX = (
    0.09  # 一行标题的最小字高 / 画布短边 (1080 短边约 97px, 约等于 safe_area.md 的 >= 80pt)
)
FIT_ZOOM = 1.4  # fit 模式画面放大到画布宽的倍数 (左右裁掉一些, 画面不至于太小)

STYLES = {
    "white_black": {"name": "白字黑边", "fill": (255, 255, 255), "stroke": (0, 0, 0), "band": None},
    "yellow_black": {"name": "黄字黑边", "fill": (255, 225, 64), "stroke": (0, 0, 0), "band": None},
    "black_band": {
        "name": "黑底白字",
        "fill": (255, 255, 255),
        "stroke": None,
        "band": (0, 0, 0, 170),
    },
}
STYLE_ORDER = ["yellow_black", "white_black", "black_band"]


# ---------------------------------------------------------------- 画幅 / 坐标


def load_format(fmt: str) -> dict:
    path = fmt if fmt.endswith(".json") else os.path.join(FORMATS_DIR, f"{fmt}.json")
    if not os.path.exists(path):
        ids = sorted(f[:-5] for f in os.listdir(FORMATS_DIR) if f.endswith(".json"))
        raise ValueError(f"未知画幅 {fmt}, 可选: {', '.join(ids)}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cover_safe(fmt: dict) -> Tuple[float, float, float, float]:
    """封面标题可用区 [x0, y0, x1, y1] (transform 单位, 上为 +)。左右取对称 (居中排字不进按钮区)。"""
    s = fmt["safe"]
    half = min(-s["left"], s["right"])
    top, bottom = s["top"], s["bottom"]
    if fmt.get("id") == "vertical":
        top, bottom = min(top, VERTICAL_COVER_SAFE_Y), max(bottom, -VERTICAL_COVER_SAFE_Y)
    return (-half, bottom, half, top)


def to_px(box, w: int, h: int) -> Tuple[float, float, float, float]:
    """transform 框 [x0, y0, x1, y1] (y 上为 +) → 像素框 [left, top, right, bottom]。"""
    x0, y0, x1, y1 = box
    return ((x0 + 1) / 2 * w, (1 - y1) / 2 * h, (x1 + 1) / 2 * w, (1 - y0) / 2 * h)


def title_zones(fmt: dict) -> dict:
    x0, y0, x1, y1 = cover_safe(fmt)
    zh = (y1 - y0) * ZONE_SHARE
    return {"top": (x0, y1 - zh, x1, y1), "bottom": (x0, y0, x1, y0 + zh)}


def _overlap(a, b) -> float:
    """两个像素框的交集面积 / a 的面积。"""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    area = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return max(0.0, w) * max(0.0, h) / area


# ---------------------------------------------------------------- 挑帧


def cover_score(sample: dict) -> float:
    """封面分 (0–100) = 素材分 x 0.8 + 人脸加分 (正脸 10 / 两眼 5 / 大小合适 3 / 居中 2)。"""
    s = sample["score"] * 0.8
    face = sample.get("face")
    if face:
        s += 10 if face.get("facing") else 2
        if face.get("eyes") == 2:
            s += 5
        size = face.get("size", 0)
        if 0.15 <= size <= 0.55:
            s += 3
        cx = (face["box"][0] + face["box"][2]) / 2
        if abs(cx - 0.5) < 0.2:  # 主体居中, 裁切到竖屏不会丢脸
            s += 2
    return round(s, 1)


def pick_candidates(
    quality: dict,
    n: int = 6,
    scenes: Sequence[float] = (),
    min_gap_s: float = 1.5,
    avoid_s: float = 0.4,
) -> List[dict]:
    """
    从 shot_quality 结果里挑 n 个封面候选 (按封面分降序), 候选之间至少隔 min_gap_s 秒。
    跳过转场 / 黑帧 / 镜头切换点附近的帧; 有人脸的素材里没脸的帧扣分。不够 n 个时放宽间隔再补。
    """
    samples = quality.get("samples") or []
    any_face = any(s.get("face") for s in samples)
    bad = {shot_quality.ISSUES["transition"], shot_quality.ISSUES["black"]}
    pool = []
    for s in samples:
        if bad & set(s["issues"]) or any(abs(s["t"] - c) < avoid_s for c in scenes):
            continue
        cs = cover_score(s) - (10 if any_face and not s.get("face") else 0)
        pool.append(dict(s, cover_score=round(cs, 1)))
    pool.sort(key=lambda s: -s["cover_score"])
    picked: List[dict] = []
    for gap in (min_gap_s, min_gap_s / 3, 0.0):
        for s in pool:
            if len(picked) >= n:
                break
            if s in picked or any(abs(s["t"] - p["t"]) < gap for p in picked):
                continue
            picked.append(s)
    picked.sort(key=lambda s: -s["cover_score"])
    return picked[:n]


# ---------------------------------------------------------------- 渲染


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as e:
        raise RuntimeError("需要 Pillow: pip install Pillow") from e
    return Image, ImageDraw, ImageFont


def _font(path: Optional[str], px: float):
    _, _, ImageFont = _pil()
    if path:
        try:
            return ImageFont.truetype(path, max(8, int(round(px))))
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=max(8, int(round(px))))
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _face_px(face_box, sw, sh, left, top):
    """人脸框 (画面比例) → 画布像素框, 往上扩 40% 脸高 (头发)、往下扩 15% (下巴)。"""
    if not face_box:
        return None
    x0, y0, x1, y1 = face_box
    hh = (y1 - y0) * sh
    return (
        x0 * sw + left,
        max(0.0, top, y0 * sh - 0.4 * hh + top),
        x1 * sw + left,
        y1 * sh + 0.15 * hh + top,
    )


def fit_to_canvas(frame_bgr, w: int, h: int, face_box=None, top_free: float = 0.0):
    """
    整帧缩放放进画布 (不裁), 空出来的地方用同一帧放大 + 模糊 + 压暗铺底 (横屏素材做竖屏封面时用)。
    画面放在 top_free 像素以下 (给上方标题让位), 放不下时贴底。返回 (PIL 图, 人脸像素框)。
    """
    Image, _, _ = _pil()
    from PIL import ImageEnhance, ImageFilter

    bg, _ = crop_to_canvas(frame_bgr, w, h)
    bg = ImageEnhance.Brightness(bg.filter(ImageFilter.GaussianBlur(max(8, w // 40)))).enhance(0.55)
    import numpy as np

    rgb = Image.fromarray(np.ascontiguousarray(frame_bgr[:, :, ::-1]))
    fw, fh = rgb.size
    # 放大到画布宽的 FIT_ZOOM 倍 (左右裁一点, 跟着人脸), 但高度不超过标题区以下的空间
    k = min(w / fw * FIT_ZOOM, max(h - top_free, h * 0.4) / fh, h / fh)
    sw, sh = max(1, round(fw * k)), max(1, round(fh * k))
    cx = (face_box[0] + face_box[2]) / 2 * sw if face_box else sw / 2
    left = int(min(0, max(w - sw, w / 2 - cx))) if sw > w else (w - sw) // 2
    top = int(min(max(0.0, top_free), h - sh)) if top_free else (h - sh) // 2
    bg.paste(rgb.resize((sw, sh), Image.LANCZOS), (left, top))
    return bg, _face_px(face_box, sw, sh, left, top)


def crop_to_canvas(frame_bgr, w: int, h: int, face_box=None):
    """按画幅铺满裁切 (cover-fit), 有人脸时裁切中心跟着脸。返回 (PIL RGB 图, 裁切后人脸像素框或 None)。"""
    Image, _, _ = _pil()
    import numpy as np

    rgb = Image.fromarray(np.ascontiguousarray(frame_bgr[:, :, ::-1]))
    fw, fh = rgb.size
    k = max(w / fw, h / fh)
    sw, sh = max(w, round(fw * k)), max(h, round(fh * k))
    img = rgb.resize((sw, sh), Image.LANCZOS)
    cx, cy = sw / 2, sh / 2
    if face_box:
        cx = (face_box[0] + face_box[2]) / 2 * sw
        cy = (face_box[1] + face_box[3]) / 2 * sh
    left = int(min(max(0, cx - w / 2), sw - w))
    top = int(min(max(0, cy - h / 2), sh - h))
    img = img.crop((left, top, left + w, top + h))
    return img, _face_px(face_box, sw, sh, -left, -top)


def choose_zone(fmt: dict, face_px, w: int, h: int) -> str:
    """标题放上带还是下带: 和人脸 (含头发) 重叠少的那个; 一样时放上带。"""
    zones = title_zones(fmt)
    if not face_px:
        return "top"
    ov = {k: _overlap(to_px(z, w, h), face_px) for k, z in zones.items()}
    return min(("top", "bottom"), key=lambda k: (round(ov[k], 3), k != "top"))


def _mean_rgb(img, box) -> Tuple[float, float, float]:
    from PIL import ImageStat

    region = img.crop(tuple(int(round(v)) for v in box))
    return tuple(ImageStat.Stat(region).mean[:3])


def _scrim(img, box_px, side: str, strength: int = 170):
    """从画面边缘到标题区内侧的渐变压暗 (边缘最暗, 内侧渐隐到 0), 亮背景上白 / 黄字也读得清。"""
    Image, ImageDraw, _ = _pil()
    w, h = img.size
    pad = (box_px[3] - box_px[1]) * 0.35
    y0, y1 = (
        (0, int(min(h, box_px[3] + pad))) if side == "top" else (int(max(0, box_px[1] - pad)), h)
    )
    span = max(1, y1 - y0)
    layer = Image.new("L", (w, span), 0)
    d = ImageDraw.Draw(layer)
    for i in range(span):
        r = 1 - i / span if side == "top" else i / span  # r = 1 在画面边缘
        d.line([(0, i), (w, i)], fill=int(strength * r**0.6))
    img.paste(Image.new("RGB", (w, span), (0, 0, 0)), (0, y0), layer)


def layout_title(
    title: str,
    zone_px: Tuple[float, float, float, float],
    canvas_h: int,
    font: Optional[str] = None,
    base_px: Optional[float] = None,
    max_lines: int = 2,
    one_line_min_px: Optional[float] = None,
) -> dict:
    """
    用真实字宽把标题塞进 zone_px (像素框): 返回 {text, lines, size, px, w, h, fits, shrunk}。
    没有手动换行时先试一行: 一行的字号不低于 one_line_min_px 就用一行 (不在词中间折);
    否则按 text_metrics.fit 折成 max_lines 行、两行尽量等长。
    """
    zw, zh = zone_px[2] - zone_px[0], zone_px[3] - zone_px[1]
    unit = PX_PER_SIZE * canvas_h / 1920  # 剪映字号 1 对应的像素
    base_px = base_px or zh / (LINE_HEIGHT * max_lines) * 0.95
    size = base_px / unit
    content, s, bw, bh = "", 0.0, 0.0, 0.0
    if max_lines > 1 and "\n" not in title:
        floor = min(size, (one_line_min_px or base_px * 0.72) / unit)
        content, s, bw, bh = fit(
            title, font, size, zw, zh, max_lines=1, min_size=floor, canvas_h=canvas_h
        )
    if not content or "\n" in content or bw > zw + 0.5 or bh > zh + 0.5:
        content, s, bw, bh = fit(
            title, font, size, zw, zh, max_lines=max_lines, min_size=size * 0.6, canvas_h=canvas_h
        )
    return {
        "text": content,
        "lines": content.split("\n"),
        "size": s,
        "px": round(s * unit, 1),
        "w": round(bw, 1),
        "h": round(bh, 1),
        "fits": bw <= zw + 0.5 and bh <= zh + 0.5,
        "shrunk": round(s / size, 2),
    }


def _block_y(box, block_h: float, align_v: str = "center") -> Tuple[float, float]:
    """文字块在像素框里的纵向范围 (上, 下)。"""
    if align_v == "top":
        y = box[1]
    elif align_v == "bottom":
        y = box[3] - block_h
    else:
        y = box[1] + (box[3] - box[1] - block_h) / 2
    return y, y + block_h


def _draw_lines(draw, lines, font_path, px, box, style, align_v="center"):
    """在像素框里水平居中画多行文字, 返回实际墨迹框。"""
    f = _font(font_path, px)
    lh = px * LINE_HEIGHT
    y, _ = _block_y(box, lh * len(lines), align_v)
    sw = max(2, int(px * 0.09)) if style.get("stroke") else 0
    ink = [1e9, 1e9, -1e9, -1e9]
    for ln in lines:
        lw = f.getlength(ln)
        x = (box[0] + box[2]) / 2 - lw / 2
        yy = y + (lh - px) / 2
        kw = {"stroke_width": sw, "stroke_fill": style["stroke"]} if sw else {}
        draw.text((x, yy), ln, font=f, fill=style["fill"], **kw)
        bb = draw.textbbox((x, yy), ln, font=f, **({"stroke_width": sw} if sw else {}))
        ink = [min(ink[0], bb[0]), min(ink[1], bb[1]), max(ink[2], bb[2]), max(ink[3], bb[3])]
        y += lh
    return ink


def render_cover(
    frame_bgr,
    fmt: dict,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    style: str = "yellow_black",
    font: Optional[str] = None,
    face_box=None,
    mode: str = "auto",
):
    """
    一张封面: 按画幅裁切 → 选标题区 (避开人脸) → 量字排版 → 压暗 / 底条 → 画字。
    mode: fill = 铺满裁切; fit = 整帧放进画布、模糊铺底、画面让到标题区下方;
          auto = 先 fill, 上下两个标题区都压到人脸且素材和画幅比例差得多 (如横转竖) 时改 fit。
    返回 (PIL 图, 排版信息 dict)。
    """
    Image, ImageDraw, _ = _pil()
    w, h = fmt["canvas"]["width"], fmt["canvas"]["height"]
    zones = title_zones(fmt)
    if mode == "fit":
        img, face_px = fit_to_canvas(frame_bgr, w, h, face_box, to_px(zones["top"], w, h)[3])
    else:
        img, face_px = crop_to_canvas(frame_bgr, w, h, face_box)
    info = {
        "mode": "fit" if mode == "fit" else "fill",
        "zone": None,
        "title_layout": None,
        "subtitle_layout": None,
        "warnings": [],
        "contrast": None,
    }
    if not title:
        return img, info
    st = STYLES[style]
    zone = choose_zone(fmt, face_px, w, h)
    zbox = to_px(zones[zone], w, h)
    if mode == "auto" and face_px and _overlap(zbox, face_px) > 0.05:
        src_ar = frame_bgr.shape[1] / frame_bgr.shape[0]
        if abs(src_ar / (w / h) - 1) > 0.25:
            return render_cover(frame_bgr, fmt, title, subtitle, style, font, face_box, "fit")
    info["zone"] = zone
    info["zone_px"] = [round(v, 1) for v in zbox]
    if face_px and _overlap(zbox, face_px) > 0.05:
        info["warnings"].append("标题区和人脸重叠 (画面里没有足够的空白), 建议换一帧")

    tbox, sbox = zbox, None
    if subtitle:  # 标题 76% / 副标题 24%, 副标题挨着画面内侧
        cut = (zbox[3] - zbox[1]) * 0.76
        if zone == "top":
            tbox = (zbox[0], zbox[1], zbox[2], zbox[1] + cut)
            sbox = (zbox[0], zbox[1] + cut, zbox[2], zbox[3])
        else:
            sbox = (zbox[0], zbox[1], zbox[2], zbox[3] - cut)
            tbox = (zbox[0], zbox[3] - cut, zbox[2], zbox[3])
    tl = layout_title(title, tbox, h, font, one_line_min_px=MIN_TITLE_PX * min(w, h))
    info["title_layout"] = tl
    cjk = sum(1 for c in title if ord(c) > 0x2E80)
    if cjk > TITLE_MAX_CJK:
        info["warnings"].append(f"主标题 {cjk} 个字, 封面建议 <= {TITLE_MAX_CJK} 个字")
    if not tl["fits"]:
        info["warnings"].append("标题缩到下限仍放不下, 请缩短标题")
    sl = None
    if subtitle:
        sl = layout_title(subtitle, sbox, h, font, base_px=tl["px"] * 0.42, max_lines=1)
        info["subtitle_layout"] = sl
        if not sl["fits"]:
            info["warnings"].append("副标题放不下, 请缩短")

    va_t = "bottom" if (sl and zone == "top") else ("top" if sl else "center")
    va_s = "top" if zone == "top" else "bottom"
    bg = _mean_rgb(img, zbox)
    if st["band"] is not None:  # 半透明底条盖住标题 + 副标题
        blocks = [_block_y(tbox, tl["h"], va_t)] + ([_block_y(sbox, sl["h"], va_s)] if sl else [])
        pad_x, pad_y = tl["px"] * 0.35, tl["px"] * 0.2
        cx = (zbox[0] + zbox[2]) / 2
        bw = max(tl["w"], sl["w"] if sl else 0) + 2 * pad_x
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        y0, y1 = min(b[0] for b in blocks) - pad_y, max(b[1] for b in blocks) + pad_y
        ImageDraw.Draw(layer).rectangle([cx - bw / 2, y0, cx + bw / 2, y1], fill=st["band"])
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
        a = st["band"][3] / 255
        bg_eff = tuple(c * (1 - a) for c in bg)
    elif luminance(bg) > 0.25:  # 亮背景: 渐变压暗
        _scrim(img, zbox, zone)
        bg_eff = _mean_rgb(img, zbox)
    else:
        bg_eff = bg
    info["contrast"] = round(contrast(st["fill"], bg_eff), 2)
    if info["contrast"] < 3 and not st.get("stroke"):
        info["warnings"].append(f"字色对背景对比度 {info['contrast']}:1 < 3:1")

    draw = ImageDraw.Draw(img)
    fpath = font_file(font)
    ink = _draw_lines(draw, tl["lines"], fpath, tl["px"], tbox, st, va_t)
    tl["ink_px"] = [round(v, 1) for v in ink]
    if sl:
        sub_style = dict(st, fill=(255, 255, 255))  # 副标题统一白字
        sl["ink_px"] = [
            round(v, 1)
            for v in _draw_lines(draw, sl["lines"], fpath, sl["px"], sbox, sub_style, va_s)
        ]
    return img, info


def contact_sheet(images: Sequence, labels: Sequence[str], thumb_w: int = 300, cols: int = 3):
    """候选封面并排的对比拼图 (每张下面写编号 / 时间 / 分数 / 样式)。"""
    Image, ImageDraw, _ = _pil()
    if not images:
        raise ValueError("没有图片")
    w0, h0 = images[0].size
    th = round(thumb_w * h0 / w0)
    cols = max(1, min(cols, len(images)))
    rows = (len(images) + cols - 1) // cols
    lab_h, gap = 40, 12
    sheet = Image.new(
        "RGB", (cols * (thumb_w + gap) + gap, rows * (th + lab_h + gap) + gap), (24, 24, 24)
    )
    d = ImageDraw.Draw(sheet)
    f = _font(font_file(None), 18)
    for i, (im, lab) in enumerate(zip(images, labels)):
        r, c = divmod(i, cols)
        x, y = gap + c * (thumb_w + gap), gap + r * (th + lab_h + gap)
        sheet.paste(im.resize((thumb_w, th), Image.LANCZOS), (x, y))
        d.text((x + 4, y + th + 8), lab, font=f, fill=(230, 230, 230))
    return sheet


# ---------------------------------------------------------------- 标题提示


def title_prompt(quality: dict, fmt: dict, analysis: Optional[dict] = None, n: int = 5) -> str:
    """给调用方 agent 写标题用的提示。标题文案不由脚本生成。"""
    v = quality.get("video") or {}
    lines = [
        f"为这条视频写 {n} 个封面标题候选 (每行一个), 再写 1 个副标题。",
        f"- 画幅: {fmt.get('name', fmt.get('id'))} {fmt['canvas']['width']}x{fmt['canvas']['height']}; "
        f"视频时长 {v.get('duration', 0):.0f} 秒",
        f"- 主标题 <= {TITLE_MAX_CJK} 个汉字 (英文 <= 5 个词), 封面上要大到在手机缩略图里一眼读完; "
        "副标题 <= 14 个字, 补充主标题没说的信息",
        "- 只用一个停留理由, 五类各写一个: 场景型、问题型、结果先行、反常识、直接利益 "
        "(references/methodology/hook.md)",
        "- 标题的承诺必须在正片里兑现; 不要「大家好」「今天讲一下」式开场, 不要堆感叹号和表情",
        "- 可以带数字或对比 (3 个方法 / 从 0 到 1), 避开平台敏感词和绝对化用语 (最 / 第一 / 100%)",
    ]
    tr = (analysis or {}).get("transcript") or []
    text = "".join(seg.get("text", "") for seg in tr)
    if text:
        lines += ["", "视频内容 (转写节选):", text[:800] + ("…" if len(text) > 800 else "")]
    else:
        lines += ["", "(没有转写: 请根据视频主题 / 脚本自己概括内容)"]
    lines += [
        "",
        "输出格式: 每行一个标题, 不加编号和引号; 最后一行以「副标题:」开头。",
        "写好后运行: python cover_maker.py <视频> -o <目录> --titles 标题1 标题2 ... "
        '--subtitle "副标题"',
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程


def make_covers(
    video: str,
    out_dir: str,
    titles: Optional[Sequence[str]] = None,
    subtitle: Optional[str] = None,
    n: int = 6,
    fmt: str = "vertical",
    font: Optional[str] = None,
    analysis: Optional[dict] = None,
    quality: Optional[dict] = None,
    every_s: float = 0.5,
    min_gap_s: float = 1.5,
    avoid_s: float = 0.4,
    styles: Optional[Sequence[str]] = None,
    jpeg_quality: int = 92,
    mode: str = "auto",
) -> dict:
    """
    挑 n 个候选帧, 每帧配一个标题 (titles 轮流用) 和一种样式 (styles 轮流用), 写出封面 / 拼图 / 提示。
    titles 为空时只出无字的候选帧 + 标题提示。返回 covers.json 的内容。
    """
    f = load_format(fmt)
    styles = list(styles or STYLE_ORDER)
    for s in styles:
        if s not in STYLES:
            raise ValueError(f"未知样式 {s}, 可选: {', '.join(STYLES)}")
    os.makedirs(out_dir, exist_ok=True)
    quality = quality or shot_quality.score_video(video, every_s)
    scenes = (analysis or {}).get("scenes") or []
    cands = pick_candidates(quality, n, scenes, min_gap_s, avoid_s)
    if not cands:
        raise RuntimeError("没有可用的候选帧 (全是黑帧 / 转场?)")
    titles = [
        t.replace("\\n", "\n") for t in (titles or []) if t and t.strip()
    ]  # 「\\n」= 手动换行

    covers, images, labels = [], [], []
    for i, c in enumerate(cands):
        frame = shot_quality.grab_frame(video, c["t"])
        if frame is None:
            continue
        title = titles[i % len(titles)] if titles else None
        style = styles[i % len(styles)]
        face_box = (c.get("face") or {}).get("box")
        img, info = render_cover(frame, f, title, subtitle, style, font, face_box, mode)
        path = os.path.join(out_dir, f"cover_{len(covers) + 1:02d}.jpg")
        img.save(path, quality=jpeg_quality)
        images.append(img)
        labels.append(
            f"#{len(covers) + 1} {c['t']:.1f}s  {c['cover_score']:.0f}分  {STYLES[style]['name']}"
        )
        covers.append(
            {
                "index": len(covers) + 1,
                "file": path,
                "t": c["t"],
                "cover_score": c["cover_score"],
                "quality_score": c["score"],
                "face": c.get("face"),
                "issues": c["issues"],
                "title": title,
                "subtitle": subtitle if title else None,
                "style": style,
                **info,
            }
        )
    w = f["canvas"]["width"]
    cols = 3 if f["canvas"]["height"] >= w else 2
    sheet_path = os.path.join(out_dir, "contact_sheet.jpg")
    contact_sheet(images, labels, cols=cols).save(sheet_path, quality=88)
    prompt = title_prompt(quality, f, analysis)
    prompt_path = os.path.join(out_dir, "title_prompt.txt")
    with open(prompt_path, "w", encoding="utf-8") as fh:
        fh.write(prompt + "\n")
    res = {
        "source": os.path.abspath(video),
        "format": f["id"],
        "canvas": f["canvas"],
        "title_zones": {k: [round(v, 3) for v in z] for k, z in title_zones(f).items()},
        "face_detector": quality.get("face_detector"),
        "covers": covers,
        "contact_sheet": sheet_path,
        "title_prompt_file": prompt_path,
        "title_prompt": prompt,
        "warnings": sorted({w for c in covers for w in c["warnings"]}),
    }
    with open(os.path.join(out_dir, "covers.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)
    return res


def _load_json(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "ok" in data and "data" in data:  # --json 的信封
        data = data["data"]
    return data


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="封面候选: 挑帧 + 大字标题 + 对比拼图 + 标题提示")
    ap.add_argument("video")
    ap.add_argument("-o", "--out", required=True, help="输出目录")
    ap.add_argument("--titles", nargs="+", help="标题候选 (调用方写好的文案), 多个时轮流配给候选帧")
    ap.add_argument("--titles-file", help="标题文件, 每行一个 (「副标题:」开头的行当副标题)")
    ap.add_argument("--subtitle", help="副标题 (可选)")
    ap.add_argument("-n", type=int, default=6, help="候选封面张数 (默认 6)")
    ap.add_argument(
        "--format", default="vertical", help="画幅 id (references/formats) 或 json 路径"
    )
    ap.add_argument("--font", help="剪映字体名 (data/panel_fonts.csv), 默认系统中文字体")
    ap.add_argument("--styles", help="样式, 逗号分隔: " + ",".join(STYLES))
    ap.add_argument(
        "--mode",
        default="auto",
        choices=["auto", "fill", "fit"],
        help="fill 铺满裁切 / fit 整帧 + 模糊铺底 / auto 标题会压脸时改 fit",
    )
    ap.add_argument("--analysis", help="analysis.json (镜头切换点避让 + 转写给标题提示)")
    ap.add_argument("--quality", help="复用 shot_quality.py -o 的结果")
    ap.add_argument("--every", type=float, default=0.5, help="打分采样间隔秒")
    ap.add_argument("--gap", type=float, default=1.5, help="候选帧之间的最小间隔秒")
    ap.add_argument("--avoid", type=float, default=0.4, help="离镜头切换点至少多少秒")
    ap.add_argument("--prompt-only", action="store_true", help="只输出标题生成提示")
    ap.add_argument("--json", action="store_true", help="stdout 输出 {ok, code, reason, data}")
    args = ap.parse_args(argv)

    try:
        analysis, quality = _load_json(args.analysis), _load_json(args.quality)
        titles, subtitle = list(args.titles or []), args.subtitle
        if args.titles_file:
            with open(args.titles_file, encoding="utf-8") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if ln.startswith(("副标题:", "副标题：")):
                        subtitle = subtitle or ln[4:].strip()
                    elif ln:
                        titles.append(ln)
        if args.prompt_only:
            quality = quality or {"video": shot_quality.video_info(args.video)}
            prompt = title_prompt(quality, load_format(args.format), analysis)
            os.makedirs(args.out, exist_ok=True)
            path = os.path.join(args.out, "title_prompt.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(prompt + "\n")
            if args.json:
                emit_result(
                    make_result(True, "ok", "", {"title_prompt": prompt, "file": path}), True
                )
            else:
                print(prompt)
            return 0
        styles = [s.strip() for s in args.styles.split(",")] if args.styles else None
        res = make_covers(
            args.video,
            args.out,
            titles,
            subtitle,
            args.n,
            args.format,
            args.font,
            analysis,
            quality,
            args.every,
            args.gap,
            args.avoid,
            styles,
            mode=args.mode,
        )
    except (RuntimeError, ValueError, OSError) as e:
        if args.json:
            emit_result(make_result(False, "cover_maker_failed", str(e)), True)
        else:
            print(f"❌ {e}", file=sys.stderr)
        return 1

    if args.json:
        emit_result(make_result(True, "ok", "", res), True)
        return 0
    print(f"🖼  {len(res['covers'])} 张候选封面 ({res['format']}), 人脸检测 {res['face_detector']}")
    for c in res["covers"]:
        t = c["title"] or "(无标题)"
        size = f" 字号 {c['title_layout']['size']}" if c["title_layout"] else ""
        print(f"   #{c['index']} {c['t']:.1f}s 封面分 {c['cover_score']} {t} [{c['style']}]{size}")
    for w in res["warnings"]:
        print(f"   ⚠️ {w}")
    if not res["covers"] or not res["covers"][0]["title"]:
        print(f"   还没有标题: 按 {res['title_prompt_file']} 写好后用 --titles 再跑一次")
    print(f"✅ {res['contact_sheet']}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
