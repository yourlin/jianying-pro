# -*- coding: utf-8 -*-
"""
文字排版测量: 用剪映缓存里的真实字体文件量字宽, 把文字折行 / 缩字号塞进一个版面区域。

剪映的 max_line_width 对英文、带空格的文字常常不折行 (2026-10-23 实测「Comment in Chinese ↓」整行越过圆形),
所以排版时由这里算好换行, 直接写进文字内容。

单位: 画布像素 (1080x1920 竖屏时 x 方向 1px = 1/540 transform 单位)。
字号换算: 剪映 size 1 ≈ 6.4 画布像素 (1920 高画布, 用挥墨体 / 启功行楷 / 英文实测 6.15–6.6)。
"""
import csv
import glob
import os
from functools import lru_cache
from typing import List, Optional, Tuple

try:
    from PIL import ImageFont
except ImportError:  # 没有 Pillow 时退化成按字数估算
    ImageFont = None

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EFFECT_CACHE = [
    os.path.expanduser("~/Movies/JianyingPro/User Data/Cache/effect"),
    os.path.expanduser("~/Library/Containers/com.lemon.lvpro/Data/Movies/JianyingPro/User Data/Cache/effect"),
]
SYSTEM_FONTS = ["/System/Library/Fonts/Hiragino Sans GB.ttc", "/System/Library/Fonts/PingFang.ttc",
                "C:/Windows/Fonts/msyh.ttc"]
PX_PER_SIZE = 6.4
LINE_HEIGHT = 1.3  # 行高 / 字高
PAD = 1.06  # 描边、花字边缘的余量

_BREAK_AFTER = set("，。！？；：、,.!?;:·  」）》”’")  # 只能跟在前一个字后面 (行首不能是它)
_OPEN = set("「（《“‘([")  # 只能跟着后一个字 (行尾不能是它)
_NATURAL = set("，。！？；：、,.!?;:·  ")  # 在这些后面断行最自然


@lru_cache(maxsize=None)
def _font_rows() -> dict:
    path = os.path.join(SKILL_ROOT, "data", "panel_fonts.csv")
    rows = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = [ln for ln in f if not ln.startswith("#")]
        for r in csv.DictReader(lines):
            if r.get("name"):
                rows.setdefault(r["name"], r)
    return rows


@lru_cache(maxsize=None)
def font_file(name: Optional[str]) -> Optional[str]:
    """剪映字体名 → 本地字体文件 (Cache/effect/<id>/<md5>/*.ttf|otf)。没下载过返回系统中文字体。"""
    row = _font_rows().get(name or "")
    if row:
        for base in EFFECT_CACHE:
            for pat in ("*.ttf", "*.otf", "*.TTF", "*.OTF", "*.ttc"):
                hits = glob.glob(os.path.join(base, row["resource_id"], "*", pat))
                if hits:
                    return hits[0]
    return next((p for p in SYSTEM_FONTS if os.path.exists(p)), None)


@lru_cache(maxsize=256)
def _font(path: Optional[str]):
    if ImageFont is None or not path:
        return None
    try:
        return ImageFont.truetype(path, 100)
    except OSError:
        return None


def line_width(text: str, font: Optional[str], size: float, canvas_h: float = 1920) -> float:
    """一行文字的宽度 (画布像素)。"""
    px = size * PX_PER_SIZE * canvas_h / 1920
    f = _font(font_file(font))
    if f is None:  # 估算: 中文 1 字宽, 英文 0.55 字宽
        units = sum(1.0 if ord(c) > 0x2E80 else 0.55 for c in text)
        return units * px * PAD
    return f.getlength(text) / 100 * px * PAD


def block_size(text: str, font: Optional[str], size: float, canvas_h: float = 1920) -> Tuple[float, float]:
    """多行文字块的 (宽, 高), 画布像素。"""
    lines = text.split("\n")
    px = size * PX_PER_SIZE * canvas_h / 1920
    return max(line_width(ln, font, size, canvas_h) for ln in lines), px * LINE_HEIGHT * len(lines)


def _tokens(text: str) -> List[str]:
    """折行单位: 英文按词 (带后面的空格), 中文按字, 标点跟着前一个字。"""
    out, cur = [], ""
    for ch in text:
        if ord(ch) > 0x2E80 and ch not in _BREAK_AFTER:
            if cur and not (cur[-1] in _OPEN):
                out.append(cur)
                cur = ""
            cur += ch
        elif ch == " ":
            cur += ch
            out.append(cur)
            cur = ""
        else:
            if cur and ord(cur[-1]) > 0x2E80 and ch not in _BREAK_AFTER and cur[-1] not in _OPEN:
                out.append(cur)
                cur = ""
            cur += ch
    if cur:
        out.append(cur)
    return out


def wrap(text: str, font: Optional[str], size: float, max_w: float, canvas_h: float = 1920) -> List[str]:
    """按真实字宽折行; 优先在标点 / 空格 / 「·」处断。"""
    lines = []
    for raw in text.split("\n"):
        toks, cur = _tokens(raw), ""
        for t in toks:
            trial = cur + t
            if cur and line_width(trial.rstrip(), font, size, canvas_h) > max_w:
                lines.append(cur.rstrip())
                cur = t.lstrip()
            else:
                cur = trial
        lines.append(cur.rstrip())
    return lines


def balance(text: str, lines: List[str], font, size, max_w, canvas_h=1920) -> List[str]:
    """两行时尽量等长 (避免「一长一短剩一个词」)。"""
    if len(lines) != 2 or "\n" in text:
        return lines
    toks = _tokens(text)
    best, best_d = lines, None
    for i in range(1, len(toks)):
        a, b = "".join(toks[:i]).rstrip(" ·"), "".join(toks[i:]).strip(" ·")
        wa, wb = line_width(a, font, size, canvas_h), line_width(b, font, size, canvas_h)
        cost = abs(wa - wb) + (0 if a[-1:] in _NATURAL or b[:1] in _OPEN or toks[i - 1].endswith(" ") else 0.6 * max_w)
        if max(wa, wb) <= max_w and (best_d is None or cost < best_d):
            best, best_d = [a, b], cost
    return best


def fit(text: str, font: Optional[str], size: float, box_w: float, box_h: float, *, max_lines: int = 2,
        min_size: Optional[float] = None, canvas_h: float = 1920) -> Tuple[str, float, float, float]:
    """
    把文字塞进 box_w x box_h (画布像素): 先折行, 放不下就按 4% 一档缩字号, 最小到 min_size (默认原字号的 70%)。
    返回 (带换行的文字, 字号, 实际宽, 实际高)。缩到下限仍放不下时返回下限字号的结果 (调用方应报警告)。
    """
    min_size = min_size or size * 0.7
    if "\n" not in text and max_lines > 1:  # 短文字先试一行 (字号最多缩到 88%), 一行放得下就不折
        one = fit(text, font, size, box_w, box_h, max_lines=1, min_size=max(min_size, size * 0.88), canvas_h=canvas_h)
        if "\n" not in one[0] and one[2] <= box_w and one[3] <= box_h:
            return one
    s = size
    while True:
        lines = balance(text, wrap(text, font, s, box_w, canvas_h), font, s, box_w, canvas_h)
        content = "\n".join(lines)
        w, h = block_size(content, font, s, canvas_h)
        if (len(lines) <= max_lines and w <= box_w and h <= box_h) or s <= min_size:
            return content, round(s, 2), w, h
        s = max(min_size, s * 0.96)


@lru_cache(maxsize=None)
def font_name_by_id(resource_id: str) -> Optional[str]:
    for name, r in _font_rows().items():
        if r.get("resource_id") == str(resource_id):
            return name
    return None


def luminance(rgb) -> float:
    """相对亮度 (WCAG), rgb 0-255 或 0-1。"""
    vals = [v / 255 if max(rgb) > 1 else v for v in rgb[:3]]
    lin = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in vals]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)
