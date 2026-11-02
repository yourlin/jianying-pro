"""
动态图形模板库 (T12): 参数化 HTML 动效 -> 视频素材。

    python motion_graphics.py list [--json]
    python motion_graphics.py html bar_chart --data '{"items":[{"label":"A","value":3}]}' --out chart.html
    python motion_graphics.py render counter --data '{"to":12800,"suffix":"+"}' --out n.mov --aspect portrait
    python motion_graphics.py render map_route --data route.json --out r.mp4 --background "#101820"

模板 (assets/motion_graphics/*.html, 纯 SVG + 原生 JS, 不依赖 CDN, 可离线渲染):
  bar_chart 柱状图增长 / line_chart 折线图增长 / counter 数字滚动 / callout 箭头·圈注标注 /
  progress_bar 进度条·环形进度 / map_route 地图路线 / list_reveal 列表逐条出现
--data: JSON 字符串或 JSON 文件路径。画幅: --aspect landscape|portrait|square 或 --size 1080x1920。
背景: --background transparent | #RRGGBB (默认 auto: .mov/.webm 透明, .mp4 黑底)。

录制 (rules/web-vfx.md 的协议: 页面播放完设 window.animationFinished = true):
  --mode frames (默认): playwright 逐帧调用 window.__mgSeek(t) 截图 (支持透明), ffmpeg 编码,
      .mov = ProRes 4444 带透明, .webm = VP9 带透明, .mp4 = H.264 (无透明)
  --mode record: 走现有 web_recorder.record_web_animation (实时录屏, 固定 1920x1080, 无透明)
需要 playwright (pip install playwright && playwright install chromium) 和 ffmpeg; 缺失时报错并说明。
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.cli_protocol import emit_result, make_result  # noqa: E402

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(SKILL_ROOT, "assets", "motion_graphics")
RUNTIME_FILE = os.path.join(TEMPLATE_DIR, "_runtime.js")
ASPECTS = {"landscape": (1920, 1080), "portrait": (1080, 1920), "square": (1080, 1080)}
DEFAULT_FONT = '"PingFang SC","Microsoft YaHei","Noto Sans CJK SC","Source Han Sans SC",sans-serif'
DEFAULT_PALETTE = ["#4F8CFF", "#FF8A3D", "#2CC9A0", "#F25C78", "#A77BFF", "#FFC93C", "#3CC8F2"]
ALPHA_EXTS = {".mov", ".webm"}
VIDEO_EXTS = {".mov", ".webm", ".mp4"}


class MGValidationError(ValueError):
    """模板参数不合法。"""


class MGDependencyError(RuntimeError):
    """缺少 playwright / ffmpeg。"""


# ------------------------------------------------------------------ 校验工具
_COLOR_RE = re.compile(
    r"^(#[0-9a-fA-F]{3,4}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8}|"
    r"rgba?\(\s*[\d.%\s,/]+\)|hsla?\(\s*[\d.%\s,/deg]+\)|[a-zA-Z]{3,20})$"
)
_PATH_RE = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9eE.,+\-\s]+$")
_FONT_RE = re.compile(r"^[\w\s,\"'\-一-鿿]+$")


def _err(path: str, msg: str):
    raise MGValidationError(f"{path}: {msg}")


def _num(v, path, lo=None, hi=None, default=None, required=False) -> Optional[float]:
    if v is None:
        if required:
            _err(path, "必填")
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        _err(path, f"必须是数字, 收到 {v!r}")
    if lo is not None and v < lo:
        _err(path, f"不能小于 {lo}, 收到 {v}")
    if hi is not None and v > hi:
        _err(path, f"不能大于 {hi}, 收到 {v}")
    return float(v)


def _str(v, path, max_len=80, default="", required=False) -> str:
    if v is None or v == "":
        if required:
            _err(path, "必填")
        return default
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = f"{v:g}"
    if not isinstance(v, str):
        _err(path, f"必须是字符串, 收到 {type(v).__name__}")
    v = v.strip()
    if len(v) > max_len:
        _err(path, f"最长 {max_len} 字, 收到 {len(v)} 字")
    return v


def _color(v, path, default=None) -> Optional[str]:
    if v is None or v == "":
        return default
    if not isinstance(v, str) or not _COLOR_RE.match(v.strip()):
        _err(path, f"颜色格式不对 (#RRGGBB / rgb() / 颜色名), 收到 {v!r}")
    return v.strip()


def _bool(v, path, default) -> bool:
    if v is None:
        return default
    if not isinstance(v, bool):
        _err(path, f"必须是 true/false, 收到 {v!r}")
    return v


def _list(v, path, lo, hi) -> list:
    if not isinstance(v, list):
        _err(path, "必须是数组")
    if not lo <= len(v) <= hi:
        _err(path, f"需要 {lo}~{hi} 项, 收到 {len(v)} 项")
    return v


def _choice(v, path, options, default):
    if v is None:
        return default
    if v not in options:
        _err(path, f"可选 {'/'.join(options)}, 收到 {v!r}")
    return v


def _svg_path(v, path) -> Optional[str]:
    if v is None or v == "":
        return None
    if not isinstance(v, str) or not _PATH_RE.match(v) or v.strip()[:1] not in "Mm":
        _err(path, "必须是以 M 开头的 SVG path (只含命令字母和数字)")
    if len(v) > 20000:
        _err(path, "SVG path 太长 (最多 20000 字符)")
    return v.strip()


def _xy(v, path) -> Tuple[float, float]:
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        _err(path, "必须是 [x, y] (画面比例 0~1)")
    return (_num(v[0], f"{path}[0]", 0, 1), _num(v[1], f"{path}[1]", 0, 1))


# ------------------------------------------------------------------ 各模板的 data 校验
def _v_bar_chart(d: dict) -> dict:
    items = []
    for i, it in enumerate(_list(d.get("items"), "items", 1, 20)):
        p = f"items[{i}]"
        if not isinstance(it, dict):
            _err(p, "必须是 {label, value}")
        items.append(
            {
                "label": _str(it.get("label"), f"{p}.label", 30),
                "value": _num(it.get("value"), f"{p}.value", 0, required=True),
                "color": _color(it.get("color"), f"{p}.color"),
            }
        )
    mx = _num(d.get("max"), "max", 0)
    if mx is not None and mx < max(it["value"] for it in items):
        _err("max", "不能小于最大的 value")
    return {
        "items": items,
        "title": _str(d.get("title"), "title", 40),
        "unit": _str(d.get("unit"), "unit", 8),
        "max": mx,
        "orientation": _choice(
            d.get("orientation"), "orientation", ("auto", "vertical", "horizontal"), "auto"
        ),
        "decimals": int(_num(d.get("decimals"), "decimals", 0, 4, 0)),
        "show_values": _bool(d.get("show_values"), "show_values", True),
    }


def _v_line_chart(d: dict) -> dict:
    labels = [
        _str(x, f"labels[{i}]", 16) for i, x in enumerate(_list(d.get("labels"), "labels", 2, 60))
    ]
    series = []
    for i, s in enumerate(_list(d.get("series"), "series", 1, 5)):
        p = f"series[{i}]"
        if not isinstance(s, dict):
            _err(p, "必须是 {name, values}")
        vals = _list(s.get("values"), f"{p}.values", len(labels), len(labels))
        series.append(
            {
                "name": _str(s.get("name"), f"{p}.name", 20, default=f"系列{i + 1}"),
                "values": [_num(v, f"{p}.values[{j}]", required=True) for j, v in enumerate(vals)],
                "color": _color(s.get("color"), f"{p}.color"),
            }
        )
    lo, hi = _num(d.get("min"), "min"), _num(d.get("max"), "max")
    if lo is not None and hi is not None and hi <= lo:
        _err("max", "必须大于 min")
    return {
        "labels": labels,
        "series": series,
        "title": _str(d.get("title"), "title", 40),
        "unit": _str(d.get("unit"), "unit", 8),
        "min": lo,
        "max": hi,
        "decimals": int(_num(d.get("decimals"), "decimals", 0, 4, 0)),
        "show_values": _bool(d.get("show_values"), "show_values", True),
    }


def _v_counter(d: dict) -> dict:
    to = _num(d.get("to"), "to", -1e15, 1e15, required=True)
    return {
        "from": _num(d.get("from"), "from", -1e15, 1e15, 0.0),
        "to": to,
        "decimals": int(_num(d.get("decimals"), "decimals", 0, 4, 0)),
        "prefix": _str(d.get("prefix"), "prefix", 6),
        "suffix": _str(d.get("suffix"), "suffix", 8),
        "label": _str(d.get("label"), "label", 40),
        "title": _str(d.get("title"), "title", 40),
        "separator": _bool(d.get("separator"), "separator", True),
    }


def _v_callout(d: dict) -> dict:
    marks = []
    for i, m in enumerate(_list(d.get("marks"), "marks", 1, 8)):
        p = f"marks[{i}]"
        if not isinstance(m, dict):
            _err(p, "必须是对象")
        typ = _choice(m.get("type"), f"{p}.type", ("circle", "arrow", "box", "underline"), "circle")
        out = {
            "type": typ,
            "x": _num(m.get("x"), f"{p}.x", 0, 1, required=True),
            "y": _num(m.get("y"), f"{p}.y", 0, 1, required=True),
            "text": _str(m.get("text"), f"{p}.text", 30),
            "color": _color(m.get("color"), f"{p}.color"),
            "at": _num(m.get("at"), f"{p}.at", 0, 60),
        }
        if typ == "circle":
            out["r"] = _num(m.get("r"), f"{p}.r", 0.01, 0.5, 0.1)
        elif typ == "box":
            out["w"] = _num(m.get("w"), f"{p}.w", 0.01, 1, 0.3)
            out["h"] = _num(m.get("h"), f"{p}.h", 0.01, 1, 0.2)
        elif typ == "underline":
            out["w"] = _num(m.get("w"), f"{p}.w", 0.01, 1, 0.3)
        else:
            if m.get("from") is None:
                _err(f"{p}.from", "箭头需要起点 [x, y]")
            out["from"] = list(_xy(m.get("from"), f"{p}.from"))
            if out["from"] == [out["x"], out["y"]]:
                _err(f"{p}.from", "起点和终点不能相同")
        marks.append(out)
    return {"marks": marks, "title": _str(d.get("title"), "title", 40)}


def _v_progress_bar(d: dict) -> dict:
    raw = d.get("items")
    if raw is None:
        if d.get("value") is None:
            _err("value", "需要 value (0~100) 或 items")
        raw = [{"label": d.get("label"), "value": d.get("value"), "color": d.get("color")}]
    items = []
    for i, it in enumerate(_list(raw, "items", 1, 6)):
        p = f"items[{i}]"
        if not isinstance(it, dict):
            _err(p, "必须是 {label, value}")
        items.append(
            {
                "label": _str(it.get("label"), f"{p}.label", 30),
                "value": _num(it.get("value"), f"{p}.value", 0, 100, required=True),
                "color": _color(it.get("color"), f"{p}.color"),
            }
        )
    return {
        "items": items,
        "style": _choice(d.get("style"), "style", ("bar", "ring"), "bar"),
        "show_percent": _bool(d.get("show_percent"), "show_percent", True),
        "title": _str(d.get("title"), "title", 40),
    }


def _v_map_route(d: dict) -> dict:
    pts = []
    for i, p in enumerate(_list(d.get("points"), "points", 2, 30)):
        path = f"points[{i}]"
        if isinstance(p, (list, tuple)):
            x, y = _xy(p, path)
            pts.append({"x": x, "y": y, "label": ""})
        elif isinstance(p, dict):
            pts.append(
                {
                    "x": _num(p.get("x"), f"{path}.x", 0, 1, required=True),
                    "y": _num(p.get("y"), f"{path}.y", 0, 1, required=True),
                    "label": _str(p.get("label"), f"{path}.label", 20),
                }
            )
        else:
            _err(path, "必须是 {x, y, label} 或 [x, y]")
    return {
        "points": pts,
        "path": _svg_path(d.get("path"), "path"),
        "outline": _svg_path(d.get("outline"), "outline"),
        "title": _str(d.get("title"), "title", 40),
        "smooth": _bool(d.get("smooth"), "smooth", True),
        "marker": _bool(d.get("marker"), "marker", True),
    }


def _v_list_reveal(d: dict) -> dict:
    items = []
    for i, it in enumerate(_list(d.get("items"), "items", 1, 12)):
        p = f"items[{i}]"
        if isinstance(it, str):
            it = {"text": it}
        if not isinstance(it, dict):
            _err(p, "必须是字符串或 {text, sub}")
        items.append(
            {
                "text": _str(it.get("text"), f"{p}.text", 40, required=True),
                "sub": _str(it.get("sub"), f"{p}.sub", 60),
                "color": _color(it.get("color"), f"{p}.color"),
            }
        )
    return {
        "items": items,
        "title": _str(d.get("title"), "title", 40),
        "numbered": _bool(d.get("numbered"), "numbered", True),
        "highlight_last": _bool(d.get("highlight_last"), "highlight_last", False),
    }


# 模板名 -> (说明, 校验函数, 默认时长函数 (data -> 秒))
TEMPLATES: Dict[str, Tuple[str, Any, Any]] = {
    "bar_chart": ("柱状图增长 (横竖屏自动切换横/竖向柱)", _v_bar_chart, lambda d: 5.0),
    "line_chart": ("折线图增长 (多系列, 末端数值跟随)", _v_line_chart, lambda d: 6.0),
    "counter": ("数字滚动 (前后缀、千分位、小数)", _v_counter, lambda d: 4.0),
    "callout": (
        "箭头 / 圈注 / 方框 / 下划线 标注",
        _v_callout,
        lambda d: 2.0 + 0.8 * len(d["marks"]),
    ),
    "progress_bar": ("进度条 / 环形进度", _v_progress_bar, lambda d: 4.0),
    "map_route": ("地图路线 (简化 SVG 路径 + 标注点)", _v_map_route, lambda d: 6.0),
    "list_reveal": ("列表逐条出现", _v_list_reveal, lambda d: 2.0 + 0.7 * len(d["items"])),
}


# ------------------------------------------------------------------ 配置
def _luminance(color: str) -> Optional[float]:
    m = re.match(r"^#([0-9a-fA-F]{6})", color or "")
    if not m:
        m3 = re.match(r"^#([0-9a-fA-F]{3})$", color or "")
        if not m3:
            return None
        hexs = "".join(c * 2 for c in m3.group(1))
    else:
        hexs = m.group(1)
    r, g, b = (int(hexs[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def resolve_size(aspect: Optional[str] = None, size: Optional[str] = None) -> Tuple[int, int]:
    if size:
        m = re.match(r"^(\d+)\s*[xX*]\s*(\d+)$", str(size).strip())
        if not m:
            _err("size", f"格式为 宽x高, 如 1080x1920, 收到 {size!r}")
        w, h = int(m.group(1)), int(m.group(2))
        if not (64 <= w <= 4096 and 64 <= h <= 4096):
            _err("size", "宽高需在 64~4096")
        if w % 2 or h % 2:
            _err("size", "宽高需为偶数 (视频编码要求)")
        return w, h
    if aspect is None:
        return ASPECTS["landscape"]
    if aspect not in ASPECTS:
        _err("aspect", f"可选 {'/'.join(ASPECTS)}, 收到 {aspect!r}")
    return ASPECTS[aspect]


def resolve_background(background: Optional[str], out_path: Optional[str] = None) -> str:
    """auto: 输出 .mov/.webm 透明, 其他 (mp4/html) 按扩展名决定 (html 透明, mp4 黑底)。"""
    bg = (background or "auto").strip()
    if bg == "auto":
        ext = os.path.splitext(out_path or "")[1].lower()
        return "#000000" if ext == ".mp4" else "transparent"
    if bg == "transparent":
        return bg
    return _color(bg, "background")


def build_theme(background: str, theme: Optional[dict] = None) -> dict:
    t = dict(theme or {})
    for k in t:
        if k not in ("primary", "accent", "text", "muted", "track", "label_bg", "font", "palette"):
            _err(f"theme.{k}", "未知主题项")
    lum = None if background == "transparent" else _luminance(background)
    light_bg = lum is not None and lum > 0.6
    text = "#1B1F27" if light_bg else "#FFFFFF"
    out = {
        "primary": _color(t.get("primary"), "theme.primary", "#4F8CFF"),
        "accent": _color(t.get("accent"), "theme.accent", "#FFC93C" if not light_bg else "#FF5A36"),
        "text": _color(t.get("text"), "theme.text", text),
        "muted": _color(
            t.get("muted"),
            "theme.muted",
            "rgba(27,31,39,0.55)" if light_bg else "rgba(255,255,255,0.65)",
        ),
        "track": _color(
            t.get("track"),
            "theme.track",
            "rgba(27,31,39,0.12)" if light_bg else "rgba(255,255,255,0.22)",
        ),
        "label_bg": _color(
            t.get("label_bg"),
            "theme.label_bg",
            "rgba(255,255,255,0.85)" if light_bg else "rgba(0,0,0,0.55)",
        ),
        "on_primary": "#FFFFFF",
        "bg_solid": background if background != "transparent" else "#FFFFFF",
    }
    font = t.get("font")
    if font is not None and (not isinstance(font, str) or not _FONT_RE.match(font)):
        _err("theme.font", "字体名只能含文字、空格、逗号、引号、连字符")
    out["font"] = font or DEFAULT_FONT
    pal = t.get("palette")
    if pal is not None:
        pal = [
            _color(c, f"theme.palette[{i}]")
            for i, c in enumerate(_list(pal, "theme.palette", 1, 12))
        ]
    out["palette"] = pal or ([out["primary"]] + [c for c in DEFAULT_PALETTE if c != out["primary"]])
    return out


def build_config(
    template: str,
    data: Any,
    aspect: Optional[str] = None,
    size: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    background: Optional[str] = None,
    duration: Optional[float] = None,
    hold: float = 1.0,
    theme: Optional[dict] = None,
    out_path: Optional[str] = None,
    manual: bool = False,
) -> dict:
    """校验参数并生成注入页面的 MG_CONFIG。参数不合法抛 MGValidationError (消息含字段路径)。"""
    if template not in TEMPLATES:
        raise MGValidationError(f"未知模板: {template} (可选: {', '.join(TEMPLATES)})")
    if not isinstance(data, dict):
        raise MGValidationError("data 必须是 JSON 对象")
    _, validate, default_dur = TEMPLATES[template]
    clean = validate(data)
    if width or height:
        w, h = resolve_size(size=f"{int(width or 1920)}x{int(height or 1080)}")
    else:
        w, h = resolve_size(aspect, size)
    hold = _num(hold, "hold", 0, 10, 1.0)
    dur = _num(duration, "duration", 1, 60) or round(default_dur(clean) + hold, 2)
    if dur <= hold + 0.3:
        _err("duration", f"需大于 hold + 0.3 秒 ({hold + 0.3:g})")
    bg = resolve_background(background, out_path)
    return {
        "template": template,
        "width": w,
        "height": h,
        "duration": dur,
        "hold": hold,
        "background": bg,
        "theme": build_theme(bg, theme),
        "data": clean,
        "manual": bool(manual),
    }


def _embed_json(obj) -> str:
    """JSON 嵌入 <script>: 转义 </ 和 <!-- 及 U+2028/2029, 防止提前闭合标签。"""
    s = json.dumps(obj, ensure_ascii=False)
    return (
        s.replace("</", "<\\/")
        .replace("<!--", "<\\!--")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_html(template: str, data: Any, **opts) -> str:
    """渲染出自包含的 HTML (运行时与配置已内联), 可直接用浏览器打开预览。opts 同 build_config。"""
    cfg = build_config(template, data, **opts)
    return html_from_config(cfg)


def html_from_config(cfg: dict) -> str:
    tpl_path = os.path.join(TEMPLATE_DIR, f"{cfg['template']}.html")
    with open(tpl_path, "r", encoding="utf-8") as f:
        html = f.read()
    with open(RUNTIME_FILE, "r", encoding="utf-8") as f:
        runtime = f.read()
    if "/*MG_CONFIG*/null" not in html or "/*MG_RUNTIME*/" not in html:
        raise RuntimeError(f"模板缺少占位符: {tpl_path}")
    html = html.replace("/*MG_CONFIG*/null", _embed_json(cfg), 1)
    return html.replace("/*MG_RUNTIME*/", runtime.replace("</script", "<\\/script"), 1)


def write_html(template: str, data: Any, out_path: str, **opts) -> str:
    html = render_html(template, data, out_path=out_path, **opts)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return os.path.abspath(out_path)


def list_templates() -> list:
    return [{"name": k, "description": v[0]} for k, v in TEMPLATES.items()]


# ------------------------------------------------------------------ 录制
def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise MGDependencyError(
            "需要 playwright 才能录制动效: pip install playwright && playwright install chromium"
        ) from e
    return sync_playwright


def _require_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise MGDependencyError("需要 ffmpeg 才能编码视频 (并加入 PATH)")
    return exe


def encoder_args(out_path: str, transparent: bool) -> list:
    ext = os.path.splitext(out_path)[1].lower()
    if ext == ".mov":
        if transparent:
            return ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le"]
        return ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18"]
    if ext == ".webm":
        pix = "yuva420p" if transparent else "yuv420p"
        return ["-c:v", "libvpx-vp9", "-pix_fmt", pix, "-b:v", "0", "-crf", "28"]
    if ext == ".mp4":
        if transparent:
            raise MGValidationError(
                "mp4 不支持透明背景: 输出改用 .mov / .webm, 或指定 --background 颜色"
            )
        return ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-movflags", "+faststart"]
    raise MGValidationError(
        f"输出格式只支持 {'/'.join(sorted(VIDEO_EXTS))}, 收到 {ext or '(无扩展名)'}"
    )


def _record_frames(html_path: str, cfg: dict, out_path: str, fps: int) -> None:
    """逐帧截图 (window.__mgSeek) -> ffmpeg (stdin image2pipe)。"""
    sync_playwright = _require_playwright()
    ffmpeg = _require_ffmpeg()
    transparent = cfg["background"] == "transparent"
    enc = encoder_args(out_path, transparent)
    n = max(1, int(round(cfg["duration"] * fps)))
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps)]
    cmd += ["-c:v", "png", "-i", "-", *enc, "-r", str(fps), out_path]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": cfg["width"], "height": cfg["height"]})
            page.goto("file://" + os.path.abspath(html_path))
            page.wait_for_function("() => window.__mgReady === true", timeout=15000)
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for i in range(n):
                    page.evaluate("t => window.__mgSeek(t)", i / fps)
                    png = page.screenshot(type="png", omit_background=transparent)
                    proc.stdin.write(png)
            finally:
                proc.stdin.close()
                err = proc.stderr.read().decode("utf-8", "ignore")
                proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(f"ffmpeg 编码失败: {err.strip()[-500:]}")
        finally:
            browser.close()


def _record_realtime(html_path: str, cfg: dict, out_path: str) -> None:
    """走现有 web_recorder (实时录屏, 1920x1080, 无透明)。"""
    _require_playwright()
    from web_recorder import record_web_animation

    tmp_webm = os.path.splitext(out_path)[0] + ".rec.webm"
    if not record_web_animation(html_path, tmp_webm, max_duration=cfg["duration"] + 10):
        raise RuntimeError("web_recorder 录制失败")
    if out_path.lower().endswith(".webm"):
        shutil.move(tmp_webm, out_path)
        return
    ffmpeg = _require_ffmpeg()
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", tmp_webm, "-t", f"{cfg['duration']:.3f}"]
    cmd += [*encoder_args(out_path, False), out_path]
    res = subprocess.run(cmd, capture_output=True, text=True)
    os.remove(tmp_webm)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg 转码失败: {res.stderr.strip()[-500:]}")


def render_video(
    template: str,
    data: Any,
    out_path: str,
    mode: str = "frames",
    fps: int = 30,
    keep_html: bool = False,
    **opts,
) -> dict:
    """
    渲染模板为视频。返回 {"output", "html", "width", "height", "duration", "fps", "transparent", "mode"}。
    缺 playwright / ffmpeg 抛 MGDependencyError; 参数不合法抛 MGValidationError。
    """
    if mode not in ("frames", "record"):
        raise MGValidationError(f"mode 只能是 frames / record, 收到 {mode}")
    fps = int(_num(fps, "fps", 1, 60, 30))
    if mode == "record":
        opts.setdefault("background", "#000000")
    cfg = build_config(template, data, out_path=out_path, manual=(mode == "frames"), **opts)
    if mode == "record" and cfg["background"] == "transparent":
        raise MGValidationError(
            "record 模式录不出透明背景, 请指定 --background 颜色或用 --mode frames"
        )
    if mode == "record" and (cfg["width"], cfg["height"]) != (1920, 1080):
        raise MGValidationError(
            "record 模式 (web_recorder) 只支持 1920x1080, 其他画幅请用 --mode frames"
        )
    encoder_args(out_path, cfg["background"] == "transparent")  # 提前校验输出格式
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    work = tempfile.mkdtemp(prefix="mg_")
    html_path = os.path.join(work, f"{template}.html")
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_from_config(cfg))
        if mode == "frames":
            _record_frames(html_path, cfg, out_path, fps)
        else:
            _record_realtime(html_path, cfg, out_path)
        kept = None
        if keep_html:
            kept = os.path.splitext(out_path)[0] + ".html"
            shutil.copy2(html_path, kept)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {
        "output": out_path,
        "html": kept,
        "width": cfg["width"],
        "height": cfg["height"],
        "duration": cfg["duration"],
        "fps": fps,
        "transparent": cfg["background"] == "transparent",
        "mode": mode,
    }


# ------------------------------------------------------------------ CLI
def load_data_arg(value: Optional[str]) -> Any:
    """--data: JSON 字符串 / JSON 文件路径 / 空 (= {})。"""
    if not value:
        return {}
    v = value.strip()
    if v[:1] in "{[":
        try:
            return json.loads(v)
        except json.JSONDecodeError as e:
            raise MGValidationError(f"--data 不是合法 JSON: {e}") from e
    if not os.path.isfile(v):
        raise MGValidationError(f"--data 既不是 JSON 也不是文件: {v}")
    with open(v, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            raise MGValidationError(f"{v} 不是合法 JSON: {e}") from e


def _add_common(sp):
    sp.add_argument("template", help=f"模板: {', '.join(TEMPLATES)}")
    sp.add_argument("--data", help="JSON 字符串或 JSON 文件路径")
    sp.add_argument("--out", required=True, help="输出路径")
    sp.add_argument("--aspect", choices=list(ASPECTS), help="画幅预设 (默认 landscape)")
    sp.add_argument("--size", help="自定义尺寸 宽x高, 如 1080x1350")
    sp.add_argument("--background", default="auto", help="transparent / #RRGGBB / auto")
    sp.add_argument("--duration", type=float, help="总时长 (秒, 默认按模板和数据估算)")
    sp.add_argument("--hold", type=float, default=1.0, help="动画结束后静止的秒数")
    sp.add_argument("--theme", help='主题 JSON, 如 {"primary":"#FF5A36","font":"Arial"}')
    sp.add_argument("--json", action="store_true", help="stdout 输出 {ok, code, reason, data}")


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="动态图形模板库: 参数化 HTML 动效 -> 视频")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="列出模板")
    ls.add_argument("--json", action="store_true")
    _add_common(sub.add_parser("html", help="只生成 HTML (浏览器打开即可预览)"))
    rp = sub.add_parser("render", help="渲染为视频 (.mov/.webm 透明, .mp4)")
    _add_common(rp)
    rp.add_argument("--mode", choices=["frames", "record"], default="frames")
    rp.add_argument("--fps", type=int, default=30)
    rp.add_argument("--keep-html", action="store_true", help="同时保留渲染用的 HTML")
    args = ap.parse_args(argv)

    if args.cmd == "list":
        items = list_templates()
        if args.json:
            emit_result(make_result(True, "ok", "", {"templates": items}), True)
        else:
            for it in items:
                print(f"  {it['name']:<14}{it['description']}")
        return 0

    try:
        data = load_data_arg(args.data)
        theme = load_data_arg(args.theme) if args.theme else None
        opts = dict(
            aspect=args.aspect,
            size=args.size,
            background=args.background,
            duration=args.duration,
            hold=args.hold,
            theme=theme,
        )
        if args.cmd == "html":
            path = write_html(args.template, data, args.out, **opts)
            res = {"output": path}
        else:
            res = render_video(
                args.template,
                data,
                args.out,
                mode=args.mode,
                fps=args.fps,
                keep_html=args.keep_html,
                **opts,
            )
    except MGValidationError as e:
        return _fail(args.json, "invalid_input", str(e), 2)
    except MGDependencyError as e:
        return _fail(args.json, "missing_dependency", str(e), 3)
    except RuntimeError as e:
        return _fail(args.json, "render_failed", str(e), 1)
    if args.json:
        emit_result(make_result(True, "ok", "", res), True)
    else:
        print(f"✅ {res['output']}")
    return 0


def _fail(as_json: bool, code: str, reason: str, rc: int) -> int:
    if as_json:
        emit_result(make_result(False, code, reason), True)
    else:
        print(f"❌ {reason}")
    return rc


if __name__ == "__main__":
    sys.exit(_main())
