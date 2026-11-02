"""
剧本编译器: 剧本 (script.json, 只写内容) + 规范 (场景 / 风格 / 画幅) + 素材分析 -> 分镜表 (storyboard.json)。

    python script_compiler.py script.json [-o storyboard.json] [--format horizontal] [--style tech] [--build]

剧本里不写坐标和字号, 只写「讲什么、每段出现什么元素」(格式见 references/script_schema.md);
位置、字体、花字、字号、运镜幅度、转场、配乐都由规范决定。换画幅 / 风格只要改 --format / --style 重新编译。
"""

import argparse
import json
import os
import sys
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from utils.user_prefs import UserPrefsError, apply_user_prefs  # noqa: E402

FORMAT_ALIASES = {
    "9:16": "vertical",
    "16:9": "horizontal",
    "3:4": "portrait_3x4",
    "1:1": "square",
    "竖屏": "vertical",
    "横屏": "horizontal",
    "小红书": "portrait_3x4",
    "方形": "square",
}
STYLE_ALIASES = {
    "国风": "guofeng",
    "国风古韵": "guofeng",
    "科技": "tech",
    "清新": "fresh",
    "清新文艺": "fresh",
    "综艺": "variety",
    "综艺活泼": "variety",
    "商务": "business",
    "商务简约": "business",
}
SCENE_ALIASES = {
    "口播": "talking_head",
    "口播知识": "talking_head",
    "vlog": "vlog",
    "Vlog": "vlog",
    "教程": "tutorial",
    "教程录屏": "tutorial",
    "带货": "product",
    "带货种草": "product",
    "解说": "commentary",
    "影视解说": "commentary",
    "卡点": "beat_montage",
    "卡点混剪": "beat_montage",
    "剧情": "drama",
    "剧情短片": "drama",
    "宣传片": "promo",
    "宣传": "promo",
    "预告片": "promo",
    "游戏宣传片": "promo",
    "产品宣传片": "promo",
    "MV": "music_video",
    "mv": "music_video",
    "音乐视频": "music_video",
    "歌曲MV": "music_video",
    "音乐MV": "music_video",
}


class ScriptError(ValueError):
    pass


def load_profile(kind: str, key: str) -> dict:
    """kind: formats / styles / scenes。"""
    alias = {"formats": FORMAT_ALIASES, "styles": STYLE_ALIASES, "scenes": SCENE_ALIASES}[kind]
    key = alias.get(key, key)
    path = os.path.join(SKILL, "references", kind, f"{key}.json")
    if not os.path.exists(path):
        options = sorted(
            os.path.splitext(f)[0] for f in os.listdir(os.path.dirname(path)) if f.endswith(".json")
        )
        raise ScriptError(
            f"没有这个{ {'formats': '画幅', 'styles': '风格', 'scenes': '场景'}[kind] }: {key} (可选: {'、'.join(options)})"
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 时间锚点: 剧本可以用台词片段定位, 不用手抄时间戳
# ---------------------------------------------------------------------------


def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch.isalnum() or "一" <= ch <= "鿿")


class Transcript:
    def __init__(self, segments: List[dict]):
        self.segments = segments or []
        self.words = [w for s in self.segments for w in (s.get("words") or [])]

    def find(self, phrase: str, after: float = 0.0) -> Optional[Tuple[float, float]]:
        """台词片段 -> (开始, 结束) 秒; 按词拼接后做不区分大小写 / 标点的匹配。"""
        target = _norm(phrase)
        if not target:
            return None
        ws = [w for w in self.words if w["s"] >= after - 0.01]
        for i in range(len(ws)):
            acc = ""
            for j in range(i, min(len(ws), i + 60)):
                acc += _norm(ws[j]["w"])
                if acc.startswith(target) or target in acc and len(acc) - len(target) < 4:
                    return ws[i]["s"], ws[j]["e"]
                if not target.startswith(acc) and target not in acc:
                    break
        for s in self.segments:  # 退而求其次: 句子级
            if s["start"] >= after - 0.01 and target in _norm(s.get("text", "")):
                return s["start"], s["end"]
        return None

    @property
    def end(self) -> float:
        return max([w["e"] for w in self.words] + [s["end"] for s in self.segments] + [0.0])


def resolve_time(
    spec, tr: Transcript, after: float = 0.0, default: Optional[Tuple[float, float]] = None
):
    """[s, e] / {"from": "台词", "to": "台词"} / "台词" -> (s, e)。"""
    if spec is None:
        return default
    if isinstance(spec, (list, tuple)):
        return float(spec[0]), float(spec[1])
    if isinstance(spec, str):
        spec = {"from": spec}
    s = e = None
    if "from" in spec:
        hit = tr.find(spec["from"], after)
        if not hit:
            raise ScriptError(f"在转写里找不到台词「{spec['from']}」")
        s, e = hit
    if "to" in spec:
        hit = tr.find(spec["to"], s or after)
        if not hit:
            raise ScriptError(f"在转写里找不到台词「{spec['to']}」")
        e = hit[1]
    if "start" in spec:
        s = float(spec["start"])
    if "end" in spec:
        e = float(spec["end"])
    return float(s), float(e)


# ---------------------------------------------------------------------------
# 编译
# ---------------------------------------------------------------------------


def _ranges_minus(ranges: List[List[float]], a: float, b: float) -> List[List[float]]:
    """从保留区间里剪掉 (a, b)。"""
    out = []
    for s, e in ranges:
        if e <= a or s >= b:
            out.append([s, e])
            continue
        if s < a:
            out.append([s, round(a, 3)])
        if e > b:
            out.append([round(b, 3), e])
    return out


def _ranges_split(ranges: List[List[float]], a: float, b: float) -> List[List[float]]:
    """在 a、b 两处把保留区间切开 (加速段要单独成一段)。"""
    out = []
    for s, e in ranges:
        pts = [s] + [x for x in (round(a, 3), round(b, 3)) if s + 0.05 < x < e - 0.05] + [e]
        out.extend([x, y] for x, y in zip(pts, pts[1:]))
    return out


class Compiler:
    def __init__(
        self,
        script: dict,
        base_dir: str,
        fmt: Optional[str] = None,
        style: Optional[str] = None,
        lang: Optional[str] = None,
    ):
        self.sc = script
        self.base = base_dir
        self.lang = lang or script.get("lang")
        self.montage_cfg = script.get("montage")
        self.plan: Optional[dict] = (
            None  # 混剪模式 (montage) 的镜头计划 (beat_snap.plan_beat_montage)
        )
        self.music_start = 0.0
        self.lyrics: List[dict] = []  # 歌词行 (成片时间)
        self.speed: List[dict] = []  # 加速段 (教程等待段)
        self.waits_done: List[dict] = []
        self.transitions: Optional[List[Optional[dict]]] = None  # 混剪模式按切点给的转场
        self.section_times: Dict[str, Tuple[float, float]] = (
            {}
        )  # 混剪模式: 段落 id -> 实际 (开始, 结束)
        self.fmt = load_profile("formats", fmt or script.get("format", "vertical"))
        self.style = load_profile("styles", style or script.get("style", "guofeng"))
        self.scene = load_profile("scenes", script.get("scene", "talking_head"))
        # 个人偏好覆盖层 (~/.config/jianying-pro/style_overrides.json, JY_USER_PREFS=off 关闭)
        try:
            self.scene, self.style, self.fmt, self.user_prefs = apply_user_prefs(
                self.scene, self.style, self.fmt
            )
        except UserPrefsError as e:
            raise ScriptError(str(e)) from e
        self.analysis = self._load_analysis()
        self.tr = Transcript(self.analysis.get("transcript") or [])
        self.faces = self.analysis.get("faces") or {}
        self.notes: List[str] = [f"个人偏好: {a}" for a in self.user_prefs]
        self.camera: List[dict] = []
        self.pip_ranges: List[Tuple[float, float]] = []
        self.no_sub: List[Tuple[float, float]] = list(
            map(tuple, (script.get("subtitles") or {}).get("exclude", []))
        )
        self.insert_mode = script.get("insert_mode") or self.scene.get("inserts", {}).get(
            "default", "pip"
        )
        self.boxes: List[Tuple[float, float, str, Tuple[float, float, float, float]]] = (
            []
        )  # 版面占用 (时间, 名称, 区域)

    def _path(self, p: str) -> str:
        p = (self.sc.get("sources") or {}).get(p, p)
        return p if os.path.isabs(p) else os.path.join(self.base, p)

    def localize(self, el: dict) -> dict:
        """多语言版本共用一条时间线, 只换文字: 元素的 i18n / sub_i18n / label_i18n, 或剧本 translations 对照表。"""
        if not self.lang:
            return el
        table = (self.sc.get("translations") or {}).get(self.lang) or {}
        out = dict(el)
        for key in ("text", "sub", "label", "title", "tag"):
            if out.get(key) is None:
                continue
            own = out.get("i18n" if key == "text" else f"{key}_i18n") or {}
            out[key] = own.get(self.lang, table.get(out[key], out[key]))
        if out.get("words"):
            own = out.get("words_i18n") or {}
            out["words"] = own.get(self.lang) or [table.get(w, w) for w in out["words"]]
        if out.get("sub") and out.get("text") and out["sub"] == out["text"]:
            out.pop("sub")  # 原版副标题就是这种语言的翻译: 换完以后不重复出现
        return out

    def scene_beat(self, b: dict) -> dict:
        """剧本段落对应的场景段落 (按 section / purpose 匹配场景 beats 的 name)。"""
        key = b.get("section") or b.get("purpose") or ""
        for sb_ in self.scene.get("beats") or []:
            if sb_.get("name") and (sb_["name"] == key or key.startswith(sb_["name"])):
                return sb_
        return {}

    def _load_analysis(self) -> dict:
        p = self.sc.get("analysis")
        if not p:
            return {}
        p = p if os.path.isabs(p) else os.path.join(self.base, p)
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)

    # -- 样式 --
    def role(self, name: str, **override) -> dict:
        """风格里的角色 -> 文字参数, 位置取画幅的 slot。"""
        r = dict(self.style["roles"].get(name) or {})
        slot = self.fmt["slots"].get(override.pop("slot", None) or r.pop("slot", "") or "", {})
        r.pop("slot", None)
        out = {
            "role": name,
            **{k: v for k, v in r.items() if not k.startswith("_") and k != "font_zh"},
        }
        if "x" in slot:
            out.setdefault("x", slot["x"])
        if "y" in slot:
            out.setdefault("y", slot["y"])
        if slot.get("max_line_width"):
            out.setdefault("max_line_width", slot["max_line_width"])
        out.update(override)
        return out

    def _on_bright(self, t: dict) -> dict:
        """风格的花字不适合亮背景时 (如国风墨色 / 金箔), 标注类改成白字黑描边。"""
        if not self.style.get("flower_on_bright_background", True) and t.get("role") in (
            "label",
            "keyword",
        ):
            t.pop("flower", None)
            t["border"] = True
        return t

    # -- 主轨 --
    def keep_ranges(self) -> List[List[float]]:
        """
        按节奏剪气口: 句子之间的停顿 >= 阈值时剪掉中间部分 (两侧各留 keep_breath 秒); 片尾留 0.35s。
        场景有 pacing.wait (教程) 时, 超过 wait.max_s 的等待段改为跳剪 (只留头尾) 或加速, 见 apply_waits。
        混剪模式 (montage) 没有主视频: 整条成片时间轴就是 [0, 总时长]。
        """
        if self.plan is not None:
            return [[0.0, round(float(self.plan["total_duration"]), 3)]]
        pacing = self.sc.get("pacing", "natural")
        p = self.scene.get("pacing", {})
        th = {"compact": p.get("compact", 0.45), "natural": p.get("natural", 0.8)}.get(pacing)
        dur = (self.analysis.get("video") or {}).get("duration") or self.tr.end
        end = min(dur, self.tr.end + 0.35) if self.tr.words else dur
        wcfg = self.wait_cfg()
        if not th or not self.tr.segments:
            return self.apply_waits([[0.0, round(end, 3)]])
        breath = float(p.get("keep_breath", 0.25))
        ranges, cur = [], 0.0
        segs = self.tr.segments
        for a, b in zip(segs, segs[1:]):
            gap_s, gap_e = a["end"], b["start"]
            if wcfg and gap_e - gap_s >= float(wcfg.get("max_s", 5.0)):
                continue  # 长等待交给 apply_waits (保留操作的头尾, 不是只留 2 个气口)
            if gap_e - gap_s >= th:
                cut_s, cut_e = round(gap_s + breath, 3), round(gap_e - breath, 3)
                if cut_e - cut_s >= 0.2:
                    ranges.append([round(cur, 3), cut_s])
                    cur = cut_e
        ranges.append([round(cur, 3), round(end, 3)])
        return self.apply_waits(ranges)

    # -- 等待段 (教程录屏: 加载 / 生成 / 长时间无旁白) --
    def wait_cfg(self) -> Optional[dict]:
        cfg = (self.scene.get("pacing") or {}).get("wait")
        if not cfg:
            return None
        mode = self.sc.get("waits_mode") or cfg.get("mode", "cut")
        if mode in ("keep", "保留", "none"):
            return None
        mode = {"跳剪": "cut", "剪掉": "cut", "加速": "speedup"}.get(mode, mode)
        return {**cfg, "mode": mode}

    def find_waits(self) -> List[Tuple[float, float]]:
        """
        等待段来源 (按优先级): 剧本 waits ([[s, e]] / [{start, end}] / 含 waits 或 dead_zones 的 JSON 路径,
        比如 quality_review 的 report.json) > analysis.json 的 waits > 转写里超过 wait.max_s 的无旁白空档。
        """
        cfg = self.wait_cfg() or {}
        th = float(cfg.get("max_s", 5.0))
        src = self.sc.get("waits")
        if src is None:
            src = self.analysis.get("waits")
        if isinstance(src, str):
            with open(self._path(src), "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data = data.get("waits") or data.get("dead_zones") or []
            src = data
        out = []
        if src is not None:
            for w in src:
                a, b = (w["start"], w["end"]) if isinstance(w, dict) else (w[0], w[1])
                out.append((float(a), float(b)))
        else:
            segs = self.tr.segments
            for a, b in zip(segs, segs[1:]):
                out.append((float(a["end"]), float(b["start"])))
        return sorted((a, b) for a, b in out if b - a >= th - 1e-6)

    def apply_waits(self, ranges: List[List[float]]) -> List[List[float]]:
        """超过阈值的等待段: cut = 跳剪 (只留头 keep_head / 尾 keep_tail 秒); speedup = 中间加速到约 target_s 秒。"""
        self.speed, self.waits_done = [], []
        cfg = self.wait_cfg()
        if not cfg:
            return ranges
        head, tail = float(cfg.get("keep_head", 1.0)), float(cfg.get("keep_tail", 1.0))
        lo, hi = (list(cfg.get("speed") or [2.0, 8.0]) * 2)[:2]
        target = float(cfg.get("target_s", 2.0))
        for ws, we in self.find_waits():
            a, b = round(ws + head, 3), round(we - tail, 3)
            if b - a < 0.5:
                continue
            if cfg["mode"] == "speedup":
                raw = b - a
                k = min(float(hi), max(float(lo), raw / target))
                if (
                    raw / k > 2 * target
                ):  # 加速到上限还太长: 只保留能加速成 2×target 秒的部分, 其余跳剪
                    ranges = _ranges_minus(ranges, a + k * 2 * target, b)
                    b = round(a + k * 2 * target, 3)
                ranges = _ranges_split(ranges, a, b)
                self.speed.append({"range": [a, b], "speed": round(k, 2)})
                self.waits_done.append({"range": [ws, we], "mode": "speedup", "speed": round(k, 2)})
            else:
                ranges = _ranges_minus(ranges, a, b)
                self.waits_done.append(
                    {"range": [ws, we], "mode": "cut", "removed": round(b - a, 3)}
                )
        return ranges

    def add_camera(self, move: str, at: float, dur: float, note: str = "", ui: bool = False, **kw):
        allowed = self.scene.get("camera", {}).get("allowed")
        if allowed and move not in allowed:
            self.notes.append(f"运镜 {move} 不在场景「{self.scene['name']}」的白名单里, 已跳过")
            return
        if self.scene.get("camera", {}).get("only_on") == "ui_insert" and not ui and move != "pip":
            self.notes.append(
                f"场景「{self.scene['name']}」只在进出产品 UI 时推拉 (段落写 ui_insert: true), "
                f"{at:.1f}s 的 {move} 已跳过"
            )
            return
        cam = self.style.get("camera", {})
        if move == "punch_in":
            if not cam.get("punch"):
                return  # 风格不允许强调推近 (如商务)
            kw.setdefault("amount", cam["punch"])
        elif move in ("push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down"):
            kw.setdefault("amount", cam.get("amount", 0.06))
        if move == "pip":
            kw.setdefault("scale", self.fmt["pip"]["scale"])
            kw.setdefault("pos", self.fmt["pip"]["pos"])
            self.pip_ranges.append((at, at + dur))
        self.camera.append(
            {"at": round(at, 3), "dur": round(dur, 3), "move": move, "note": note, **kw}
        )

    def _overlaps_pip(self, s: float, e: float) -> bool:
        return any(s < b and e > a for a, b in self.pip_ranges)

    # -- 圆形讲解人版式 (insert_mode = "circle") --
    @property
    def circle(self) -> Optional[dict]:
        return self.fmt.get("circle_layout") if self.insert_mode == "circle" else None

    def card_role(self, name: str, **override) -> dict:
        r = dict(
            (self.style.get("card_roles") or {}).get(name) or self.style["roles"].get("label") or {}
        )
        r.pop("slot", None)
        return {"role": "label", **r, **override}

    def page(self, s: float, e: float, shot: dict, note: str):
        """插卡页: 底卡铺满 (盖住主画面) + 和主视频同步的圆形讲解人 (圆形蒙版 + 描边, 静音)。"""
        lay = self.circle
        shot.setdefault("overlays", []).append(
            {
                "source": self.style.get("card_background", "paper:#F5F1E8"),
                "start": round(s, 3),
                "dur": round(e - s, 3),
                "fit": "cover",
                "scale": [1, 1],
                "motion": "fade",
                "track": "CardBG",
            }
        )
        v = self.analysis.get("video") or {}
        mw, mh = float(v.get("width") or 1080), float(v.get("height") or 1920)
        cw, ch = float(self.fmt["canvas"]["width"]), float(self.fmt["canvas"]["height"])
        box = self.faces.get("box") or [0.3, 0.3, 0.7, 0.6]
        fx, fy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        # 圆的直径 (占素材高度): 头 + 肩, 但不能超过素材的宽 / 高 (超出部分没有画面, 圆的左右会被截平)
        d_px = min(max(0.35, (box[3] - box[1]) * 1.6) * mh, 0.94 * min(mw, mh))
        mask_size = d_px / mh
        fit = min(cw / mw, ch / mh)  # 剪映 scale 1 = 素材适应画布
        sp = lay["speaker"]
        scale = sp["diameter_px"] / (mask_size * mh * fit)
        r = d_px / 2  # 圆心离素材边至少 r, 整个圆都落在画面内
        off_x = max(
            -(mw / 2 - r), min(mw / 2 - r, (fx - 0.5) * mw)
        )  # 蒙版圆心相对素材中心的偏移 (素材像素, 右 / 上为正)
        off_y = max(-(mh / 2 - r), min(mh / 2 - r, (0.5 - fy) * mh))
        cx, cy = sp["center"]
        pos = [
            round(cx - off_x * fit * scale / (cw / 2), 4),
            round(cy - off_y * fit * scale / (ch / 2), 4),
        ]
        shot["overlays"].append(
            {
                "source": "main",
                "source_in": round(s, 3),
                "start": round(s, 3),
                "dur": round(e - s, 3),
                "pos": pos,
                "scale": [round(scale, 4), round(scale, 4)],
                "motion": "fade",
                "mute": True,
                "track": "Speaker",
                "mask": {
                    "name": "圆形",
                    "size": round(mask_size, 3),
                    "center_x": round(off_x, 1),
                    "center_y": round(off_y, 1),
                },
                "mask_stroke": {
                    "style": sp.get("stroke", {}).get("style", "单层"),
                    "color": self.style.get("stroke_color", "#FFFFFF"),
                    "size": sp.get("stroke", {}).get("size", 30),
                },
            }
        )
        rx, ry = (sp["diameter_px"] / 2 + 8) / (cw / 2), (sp["diameter_px"] / 2 + 8) / (
            ch / 2
        )  # 含描边
        self.occupy(s, e, "圆形讲解人", (cx - rx, cy - ry, cx + rx, cy + ry))
        shot["note"] = (shot.get("note", "") + f"; {note}").strip("; ")

    # -- 版面网格 --
    def region(self, name: str) -> Tuple[float, float, float, float]:
        return tuple(self.circle_spec["regions"][name])

    @property
    def circle_spec(self) -> dict:
        return self.circle or self.fmt.get("circle_layout") or {}

    def occupy(self, s: float, e: float, what: str, box):
        self.boxes.append((s, e, what, tuple(round(v, 4) for v in box)))

    def place(self, t: dict, region, *, align: Optional[str] = None, max_lines: int = 2) -> dict:
        """
        把文字放进版面区域: 按真实字体量宽, 折行 / 缩字号塞进区域, 再按对齐方式定位。
        region: 区域名 (circle_layout.regions) 或 [x0, y0, x1, y1]。放不下时记警告。
        """
        from utils.text_metrics import fit

        name = region if isinstance(region, str) else t.get("track", "text")
        x0, y0, x1, y1 = self.region(region) if isinstance(region, str) else region
        align = align or (self.circle_spec.get("region_align") or {}).get(name, "center")
        cw, ch = float(self.fmt["canvas"]["width"]), float(self.fmt["canvas"]["height"])
        bw, bh = (x1 - x0) * cw / 2, (y1 - y0) * ch / 2
        k = float(t.get("scale", 1))
        content, size, w, h = fit(
            t["content"],
            t.get("font"),
            float(t.get("size", 8)) * k,
            bw,
            bh,
            max_lines=max_lines,
            canvas_h=ch,
        )
        if w > bw + 1 or h > bh + 1:
            self.notes.append(
                f"⚠️ 「{t['content'][:12]}」放不进区域 {name} (需要 {w:.0f}x{h:.0f}px, 区域 {bw:.0f}x{bh:.0f}px)"
            )
        t.update(
            content=content,
            size=round(size / k, 2),
            max_line_width=round(min(1.0, w / cw + 0.04), 3),
            wrap_chars=0,
            y=round((y0 + y1) / 2, 4),
        )
        hw, hh = w / cw, h / ch  # 半宽 / 半高 (transform 单位)
        if align == "left":
            t.update(x=round(x0 + hw, 4), align=0)
        elif align == "right":
            t.update(x=round(x1 - hw, 4), align=2)
        else:
            t["x"] = round((x0 + x1) / 2, 4)
        self.occupy(
            float(t["start"]),
            float(t["start"]) + float(t["dur"]),
            f"文字「{t['content'][:8]}」",
            (t["x"] - hw, t["y"] - hh, t["x"] + hw, t["y"] + hh),
        )
        return t

    def check_layout(self):
        """同一时刻出现的元素, 版面区域不能重叠 (底卡、字幕带之外)。"""
        bs = self.boxes
        seen = set()
        for i in range(len(bs)):
            for j in range(i + 1, len(bs)):
                a, b = bs[i], bs[j]
                if a[0] < b[1] - 0.05 and b[0] < a[1] - 0.05:
                    A, B = a[3], b[3]
                    if A[0] < B[2] and B[0] < A[2] and A[1] < B[3] and B[1] < A[3]:
                        key = (a[2], b[2])
                        if key not in seen:
                            seen.add(key)
                            self.notes.append(
                                f"⚠️ 版面重叠: {a[2]} 和 {b[2]} ({max(a[0], b[0]):.1f}s)"
                            )

    def card_scale(self, info: dict) -> float:
        """插卡在 media 区域里尽量放大 (contain: 宽或高贴满区域)。"""
        x0, y0, x1, y1 = self.region("media")
        cw, ch = float(self.fmt["canvas"]["width"]), float(self.fmt["canvas"]["height"])
        iw, ih = float(info.get("width") or 16), float(info.get("height") or 9)
        fit = min(cw / iw, ch / ih)  # 剪映 scale 1 = 素材适应画布
        return round(min((x1 - x0) * cw / 2 / (iw * fit), (y1 - y0) * ch / 2 / (ih * fit)), 4)

    # -- 元素 --
    def card(self, el: dict, beat_t: Tuple[float, float], shot: dict):
        src = el["asset"]
        info = next(
            (
                a
                for a in self.analysis.get("assets", [])
                if os.path.basename(a["path"])
                == os.path.basename((self.sc.get("sources") or {}).get(src, src))
            ),
            {},
        )
        s, e = resolve_time(el.get("time"), self.tr, beat_t[0], beat_t)
        s, e = s + float(el.get("delay", 0.3)), min(e, beat_t[1])
        orient = info.get("orientation", "landscape")
        sc = self.fmt["card"].get(f"scale_{orient}", 0.78)
        mode = el.get("mode", self.insert_mode)
        ov = {
            "source": src,
            "start": round(s, 3),
            "dur": round(e - s, 3),
            "pos": list(self.fmt["card"]["pos"]),
            "scale": [sc, round(sc * 1.05, 3)],
            "motion": "float_in",
            "shadow": True,
        }
        if mode == "circle" and self.circle:
            ov["shadow"] = {
                "alpha": 30,
                "blur": 16,
                "distance": 6,
                "angle": 90,
            }  # 底卡上只要一点点投影
            self.page(
                s - 0.15,
                e,
                shot,
                f"插卡页「{el.get('label', src)}」: 底卡 + 图片铺满安全区 + 圆形讲解人",
            )
            sc = self.card_scale(info)
            x0, y0, x1, y1 = self.region("media")
            ov.update(
                pos=[round((x0 + x1) / 2, 4), round((y0 + y1) / 2, 4)],
                scale=[round(sc / 1.03, 4), sc],
            )  # 缓推到贴满
            shot["overlays"].append(ov)
            cw, ch = float(self.fmt["canvas"]["width"]), float(self.fmt["canvas"]["height"])
            iw, ih = float(info.get("width") or 16), float(info.get("height") or 9)
            f = min(cw / iw, ch / ih) * sc
            hw, hh = iw * f / cw, ih * f / ch
            self.occupy(
                s,
                e,
                f"插卡「{el.get('label', src)}」",
                (ov["pos"][0] - hw, ov["pos"][1] - hh, ov["pos"][0] + hw, ov["pos"][1] + hh),
            )
            if el.get("label"):
                shot.setdefault("texts", []).append(
                    self.place(
                        self.card_role(
                            "card_label",
                            content=el["label"],
                            start=round(s + 0.25, 3),
                            dur=round(e - s - 0.4, 3),
                            track="CardLabel",
                        ),
                        "label",
                    )
                )
            sfx = (self.style.get("sfx") or {}).get("card")
            if sfx and el.get("sfx", True):
                shot.setdefault("sfx", []).append(
                    {
                        "name": sfx,
                        "at": round(s, 3),
                        "volume": self.style["sfx"].get("volume", 0.35),
                    }
                )
            return
        if mode == "cover":
            ov.update(fit="cover", scale=[1.0, 1.06], motion="push")
        elif mode == "pip":
            self.add_camera(
                "pip", s - 0.2, e - s + 0.2, f"插卡「{el.get('label', src)}」: 真人缩成小窗"
            )
        elif mode == "overlay" and self.faces.get("card_zones"):
            z = self.faces["card_zones"][0]["y_range"]
            ov["pos"] = [0, round(sum(z) / 2, 3)]
        shot.setdefault("overlays", []).append(ov)
        if el.get("label"):
            shot.setdefault("texts", []).append(
                self._on_bright(
                    self.role(
                        "label",
                        content=el["label"],
                        start=round(s + 0.25, 3),
                        dur=round(e - s - 0.5, 3),
                        track="CardLabel",
                    )
                )
            )
        sfx = (self.style.get("sfx") or {}).get("card")
        if sfx and el.get("sfx", True):
            shot.setdefault("sfx", []).append(
                {"name": sfx, "at": round(s, 3), "volume": self.style["sfx"].get("volume", 0.35)}
            )

    def _half_size(self, t: dict) -> Tuple[float, float]:
        """文字块的半宽 / 半高 (剪映 transform 单位), 按真实字体量。"""
        from utils.text_metrics import block_size

        cw, ch = float(self.fmt["canvas"]["width"]), float(self.fmt["canvas"]["height"])
        w, h = block_size(
            t["content"], t.get("font"), float(t.get("size", 8)) * float(t.get("scale", 1)), ch
        )
        return w / cw, h / ch

    def text(self, el: dict, kind: str, beat_t: Tuple[float, float], shot: dict, track: str):
        s, e = resolve_time(el.get("time"), self.tr, beat_t[0], beat_t)
        lims = self.scene.get("elements", {}).get(kind, {})
        if el.get("time") is None:
            s += float(lims.get("delay", 0.3))  # 标题卡钩子: 0.2s 内出大字
            if lims.get("max_dur"):
                e = min(e, s + lims["max_dur"])
        if (
            lims.get("min_dur") and e - s < lims["min_dur"]
        ):  # 停留太短看不完 (剪辑的语法: ≥ max(2s, 字数 × 0.4s))
            e = s + lims["min_dur"]
        over = {"slot": lims["slot"]} if lims.get("slot") else {}
        t = self.role(
            kind, content=el["text"], start=round(s, 3), dur=round(e - s, 3), track=track, **over
        )
        hw = hh = None
        if lims.get("align") == "left" and "x" in t:  # 标签卡: 左边贴安全区, 不是以槽位为中心
            hw, hh = self._half_size(t)
            t.update(x=round(float(self.fmt["safe"]["left"]) + 0.03 + hw, 4), align=0)
        shot.setdefault("texts", []).append(self._on_bright(t))
        if el.get("sub"):
            sub_role = "hero_sub" if kind == "hero" else ("cta_sub" if kind == "cta" else "label")
            st = self.role(
                sub_role,
                content=el["sub"],
                start=round(s + 0.3, 3),
                dur=round(e - s - 0.3, 3),
                track=track + "Sub",
            )
            if (
                lims.get("slot") and "y" in t
            ):  # 场景指定了位置: 副标题紧贴主标题下方, 不和它叠在一起
                if hh is None:
                    hw, hh = self._half_size(t)
                if kind == "label":  # 标签卡副标题 (如另一种语言) 用小字
                    st["size"] = round(float(st.get("size", 8)) * 0.72, 2)
                shw, shh = self._half_size(st)
                st["y"] = round(float(t["y"]) - hh - shh - 0.02, 4)
                if lims.get("align") == "left":
                    st.update(x=round(float(self.fmt["safe"]["left"]) + 0.03 + shw, 4), align=0)
                else:
                    st["x"] = t.get("x", 0)
            shot["texts"].append(st)
        return s, e

    def end_card(self, el: dict, beat_t: Tuple[float, float], shot: dict):
        """
        结束卡 (记忆点): 底卡 + 系列标签 + 大标题 + 词卡 + 互动引导 + 圆形讲解人 + 风格的结尾特效。
        el: {tag, title, words: [...], sub, last: 秒}
        """
        last = float(el.get("last", 6.0))
        s, e = max(beat_t[0], beat_t[1] - last), beat_t[1]
        if self.circle:
            self.page(s, e, shot, "结束卡: 底卡 + 系列标签 + 标题 + 词卡 + 圆形讲解人")
        else:
            shot.setdefault("overlays", []).append(
                {
                    "source": self.style.get("card_background", "paper:#F5F1E8"),
                    "start": round(s, 3),
                    "dur": round(e - s, 3),
                    "fit": "cover",
                    "scale": [1, 1],
                    "motion": "fade",
                    "track": "CardBG",
                }
            )
        texts = shot.setdefault("texts", [])
        if el.get("tag"):
            texts.append(
                self.place(
                    self.card_role(
                        "end_tag",
                        content=el["tag"],
                        start=round(s + 0.2, 3),
                        dur=round(e - s - 0.2, 3),
                        track="EndTag",
                    ),
                    "end_tag",
                    max_lines=1,
                )
            )
        if el.get("title"):
            texts.append(
                self.place(
                    self.card_role(
                        "end_title",
                        content=el["title"],
                        start=round(s + 0.35, 3),
                        dur=round(e - s - 0.35, 3),
                        track="EndTitle",
                    ),
                    "end_title",
                )
            )
        words = list(el.get("words") or [])
        if words:  # 词卡区按行均分, 每行两个词, 逐行出现
            rows = [words[i : i + 2] for i in range(0, len(words), 2)]
            x0, y0, x1, y1 = self.region("end_words")
            rh = (y1 - y0) / len(rows)
            for r_i, row in enumerate(rows):
                box = (x0, y1 - rh * (r_i + 1), x1, y1 - rh * r_i)
                texts.append(
                    self.place(
                        self.card_role(
                            "end_word",
                            content="  ·  ".join(row),
                            start=round(s + 0.6 + 0.25 * r_i, 3),
                            dur=round(e - s - 0.6 - 0.25 * r_i, 3),
                            track=f"EndWords{r_i}",
                        ),
                        box,
                        max_lines=1,
                    )
                )
        if el.get("sub"):
            texts.append(
                self.place(
                    self.card_role(
                        "end_sub",
                        content=el["sub"],
                        start=round(s + 1.0, 3),
                        dur=round(e - s - 1.0, 3),
                        track="EndSub",
                    ),
                    "label",
                )
            )
        eff = self.style.get("ending_effect")
        if eff:
            d = max(float(eff.get("min_dur", 3.0)), e - s)
            shot.setdefault("effects", []).append(
                {"name": eff["name"], "start": round(e - d, 3), "dur": round(d, 3)}
            )

    def vocab(self, el: dict, beat_t: Tuple[float, float], shot: dict):
        """词卡: 每个词按词级时间戳卡点, 上 / 中 / 下三行 (如 拼音 / 汉字 / 英文), 放在下巴下方。"""
        items = el["items"]
        times = []
        cursor = beat_t[0]
        for it in items:
            hit = self.tr.find(it.get("word") or it["main"], cursor)
            if not hit:
                raise ScriptError(f"词卡「{it['main']}」在转写里找不到")
            times.append(hit)
            cursor = hit[1]
        # 三行的间距按字高算 (真实字体): 上下行中心离主行 = 两行半高之和 + 间隙
        from utils.text_metrics import block_size

        ch = float(self.fmt["canvas"]["height"])

        def half(role: str, txt: str) -> float:
            r = self.style["roles"].get(role, {})
            return block_size(txt, r.get("font"), float(r.get("size", 9)), ch)[1] / ch

        gap = 12 / (ch / 2)
        h_main = max(half("vocab", it["main"]) for it in items)
        up = (
            h_main
            + max([half("vocab_top", it["top"]) for it in items if it.get("top")] or [0])
            + gap
        )
        down = (
            h_main
            + max([half("vocab_bottom", it["bottom"]) for it in items if it.get("bottom")] or [0])
            + gap
        )
        chin = self.faces.get("chin_y")
        main_y = self.fmt["slots"]["lower_zone"]["y"]
        if chin is not None:
            main_y = min(main_y, chin - h_main - gap)
        bottom_safe = self.fmt["safe"]["bottom"]
        if main_y - down < bottom_safe - 0.03:
            main_y = bottom_safe - 0.03 + down
        for i, (it, (s, _e)) in enumerate(zip(items, times)):
            st = max(beat_t[0], s - 0.12)
            en = (
                (times[i + 1][0] - 0.12 - 0.01) if i + 1 < len(times) else beat_t[1]
            )  # 相邻词卡留 10ms, 避免精度误差重叠
            d = round(en - st, 3)
            if it.get("top"):
                shot.setdefault("texts", []).append(
                    self.role(
                        "vocab_top",
                        content=it["top"],
                        start=round(st, 3),
                        dur=d,
                        x=0,
                        y=round(main_y + up, 3),
                        track="VocabTop",
                    )
                )
            shot.setdefault("texts", []).append(
                self.role(
                    "vocab",
                    content=it["main"],
                    start=round(st, 3),
                    dur=d,
                    y=round(main_y, 3),
                    track="Vocab",
                )
            )
            if it.get("bottom"):
                shot["texts"].append(
                    self.role(
                        "vocab_bottom",
                        content=it["bottom"],
                        start=round(st, 3),
                        dur=d,
                        x=0,
                        y=round(main_y - down, 3),
                        track="VocabBottom",
                    )
                )
        if self.scene.get("elements", {}).get("vocab", {}).get("hide_subtitles", True):
            self.no_sub.append((round(times[0][0] - 0.15, 3), round(beat_t[1], 3)))

    # -- 混剪模式 (宣传片 / MV / 卡点): 多段素材 + 音乐, 镜头由 beat_snap 按拍或按歌词行规划 --
    def prepare_montage(self):
        """
        剧本 montage: {music, beats?, clips, lyrics?, cut_on?, start?, duration?, density?}
        段落 time 用成片时间 (秒), 或歌词片段 (按 lyrics 定位); 段落的 cut_every_beats / clips 覆盖场景默认。
        """
        from beat_detect import detect_beats, load_beats
        from beat_snap import BeatPlanError, _grid, _resolve_start, load_lyrics, plan_beat_montage

        m = self.montage_cfg
        music = self._path(m["music"]) if m.get("music") else None
        try:
            if m.get("beats") is not None:
                bsrc = m["beats"]
                beats = load_beats(self._path(bsrc) if isinstance(bsrc, str) else bsrc)
            elif music:
                beats = detect_beats(music)
            else:
                raise ScriptError("montage 需要 music (音乐文件) 或 beats (拍点)")
            start = _resolve_start(m.get("start", 0.0), _grid(beats))
            ly = m.get("lyrics")
            lyrics = load_lyrics(self._path(ly) if isinstance(ly, str) else ly)
        except BeatPlanError as e:
            raise ScriptError(str(e)) from e
        self.music_start = float(start)
        self.lyrics = [
            {**ln, "start": round(ln["start"] - start, 3), "end": round(ln["end"] - start, 3)}
            for ln in lyrics
            if ln["end"] > start
        ]
        if self.lyrics:  # 段落可以用歌词片段定位 (成片时间)
            self.tr = Transcript(
                [
                    {"start": max(0.0, ln["start"]), "end": ln["end"], "text": ln["text"]}
                    for ln in self.lyrics
                ]
            )
        clips = m.get("clips")
        if isinstance(clips, str):
            clips = self._path(clips)
        else:
            clips = [
                {**c, "path": self._path(c["path"])} if isinstance(c, dict) else self._path(c)
                for c in clips or []
            ]
        sections, prev = [], 0.0
        for b in self.sc.get("beats") or []:
            bt = resolve_time(b.get("time"), self.tr, prev)
            if bt is None:
                raise ScriptError(f"段落 {b.get('id')} 需要 time (成片秒数或歌词片段)")
            prev = bt[1]
            n = b.get("cut_every_beats", self.scene_beat(b).get("cut_every_beats"))
            sections.append(
                {
                    "start": start + bt[0],
                    "end": start + bt[1],
                    "cut_every_beats": n,
                    "equal": bool(b.get("equal_length", self.scene_beat(b).get("equal_length"))),
                    "clips": [self._path(c) for c in b.get("clips") or []],
                    "name": b.get("id"),
                }
            )
        duration = m.get("duration") or (sections[-1]["end"] - start if sections else None)
        vol = (
            (self.sc.get("bgm") or {}).get("volume")
            if isinstance(self.sc.get("bgm"), dict)
            else None
        )
        try:
            self.plan = plan_beat_montage(
                clips,
                beats,
                density=m.get("density", "标准"),
                start=start,
                duration=duration,
                fps=float(self.fmt.get("fps", 30)),
                cut_on=m.get("cut_on"),
                lyrics=lyrics,
                scene=self.scene,
                sections=sections,
                music_path=music,
                music_volume=vol,
            )
        except BeatPlanError as e:
            raise ScriptError(str(e)) from e
        # 段落时间对齐到实际切点 (段落开头被吸到了拍点 / 歌词边界上)
        firsts = {}
        for sh in self.plan["shots"]:
            firsts.setdefault(sh.get("section"), float(sh["target_start"]))
        ids = [b.get("id") for b in self.sc.get("beats") or []]
        starts = [firsts.get(i) for i in ids]
        total = float(self.plan["total_duration"])
        self.section_times = {}
        for k, i in enumerate(ids):
            if starts[k] is None:
                continue
            nxt = next((x for x in starts[k + 1 :] if x is not None), total)
            self.section_times[i] = (round(starts[k], 3), round(nxt, 3))
        st = self.plan["stats"]
        msg = (
            f"混剪: {st['shots']} 个镜头, 平均 {st['avg_shot']:.2f}s, "
            f"{'按歌词行切' if self.plan['cut_on'] == 'lyric' else '按拍切'}"
        )
        if self.plan["cut_on"] == "lyric":
            msg += f" (歌词边界切点 {st['lyric_cuts']} 个)"
        self.notes.append(msg)

    def plan_cut_times(self) -> List[float]:
        """混剪计划的切点 (成片时间, 不含 0)。"""
        return [float(sh["target_start"]) for sh in (self.plan or {}).get("shots", [])[1:]]

    def montage_transitions(
        self, beats: List[dict], times: List[Tuple[float, float]]
    ) -> List[Optional[dict]]:
        """
        每个切点的转场 (None = 硬切)。场景 transitions: default / opening / ending / chapter;
        场景段落 transition_out / transition_in 指定段落边界用哪种, 剧本段落 transition 可以覆盖 (null = 硬切)。
        """
        tcfg = self.scene.get("transitions") or {}
        cuts = self.plan_cut_times()
        out: List[Optional[dict]] = [tcfg.get("default")] * len(cuts)

        def at(t: float, spec):
            if not cuts:
                return
            k = min(range(len(cuts)), key=lambda j: abs(cuts[j] - t))
            if abs(cuts[k] - t) > 0.4:  # 段落边界吸到拍点后最多差半拍
                return
            if isinstance(spec, str):
                spec = tcfg.get(spec, {"name": spec, "duration": 0.5})
            out[k] = spec

        for i, (b, bt) in enumerate(zip(beats, times)):
            sb_ = self.scene_beat(b)
            if sb_.get("transition_out") and i + 1 < len(times):
                at(bt[1], sb_["transition_out"])
            if sb_.get("transition_in") and i > 0:
                at(bt[0], sb_["transition_in"])
            if "transition" in b and i > 0:
                at(bt[0], b["transition"])
        hard = sum(1 for t in out if not t)
        if cuts:
            ratio = hard / len(cuts)
            need = tcfg.get("hard_cut_min")
            if need is not None and ratio < float(need) - 1e-6:
                self.notes.append(
                    f"⚠️ 硬切只占 {ratio:.0%} (场景要求 ≥ {float(need):.0%}): 转场只留在开头 / 结尾 / 章节"
                )
        return out

    def check_hook(self, shots: List[dict]):
        """标题卡钩子 (横屏宣传片 / MV / 教程): 0.3s 内出大字, 停 3–4s 不切。"""
        hk = self.scene.get("hook") or {}
        if hk.get("type") != "title_card" or not shots:
            return
        first = shots[0]
        starts = [
            float(t["start"])
            for t in first.get("texts") or []
            if t.get("role") in ("hero", "title")
        ]
        if not starts:
            self.notes.append("⚠️ 标题卡钩子: 第一段没有 hero / title 大字")
            return
        if min(starts) > float(hk.get("text_by_s", 0.3)) + 1e-6:
            self.notes.append(
                f"⚠️ 标题卡钩子: 大字 {min(starts):.2f}s 才出现 (应在 {hk.get('text_by_s', 0.3)}s 内)"
            )
        lo, hi = (list(hk.get("hold_s") or [3.0, 4.0]) * 2)[:2]
        cuts = self.plan_cut_times()
        if cuts:
            hold = cuts[0]
            if hold < float(lo) - 0.05 or hold > float(hi) + 0.6:
                self.notes.append(f"⚠️ 标题卡停 {hold:.1f}s (建议 {lo:g}–{hi:g}s 不切)")

    def compile(self) -> dict:
        sc = self.sc
        shots = []
        beats = sc.get("beats") or []
        if self.montage_cfg:
            self.prepare_montage()
        prev_end = 0.0
        video_end = self.keep_ranges()[-1][1]
        times: List[Tuple[float, float]] = []
        for i, b in enumerate(beats):
            bt = self.section_times.get(b.get("id")) or resolve_time(
                b.get("time"), self.tr, prev_end
            )
            if bt is None:
                raise ScriptError(f"段落 {b.get('id')} 需要 time (时间或台词)")
            if i == len(beats) - 1:  # 最后一段延到成片结尾 (结尾特效、互动卡要停到最后)
                bt = (bt[0], max(bt[1], video_end))
            times.append(bt)
            shot = {
                "id": b.get("id", f"S{i + 1:02d}"),
                "time": [round(bt[0], 3), round(bt[1], 3)],
                "purpose": b.get("purpose", ""),
                "line": b.get("line", ""),
                "note": b.get("note", ""),
            }
            if b.get("ui_insert"):  # 进出产品 UI: 快速推近 / 拉远 (MV 只在这里推拉)
                ui = self.scene.get("camera", {}).get("ui_insert") or {}
                d = float(ui.get("dur", 0.4))
                amt = float(ui.get("amount", 0.15))
                self.add_camera(
                    ui.get("in", "push_in"), bt[0], d, "切入产品 UI: 快速推近", ui=True, amount=amt
                )
                if bt[1] - bt[0] > 2 * d:
                    self.add_camera(
                        ui.get("out", "pull_out"),
                        bt[1] - d,
                        d,
                        "切出产品 UI: 快速拉远",
                        ui=True,
                        amount=amt,
                    )
            for el in b.get("elements") or []:
                el = self.localize(el)
                kind = el["kind"]
                if kind == "card":
                    self.card(el, bt, shot)
                elif kind == "vocab":
                    self.vocab(el, bt, shot)
                elif kind == "hero" and self.circle:
                    s_, e_ = resolve_time(el.get("time"), self.tr, bt[0], bt)
                    self.page(s_, e_, shot, "点题页: 底卡 + 大字 + 圆形讲解人")
                    shot.setdefault("texts", []).append(
                        self.place(
                            self.role(
                                "hero",
                                content=el["text"],
                                start=round(s_ + 0.2, 3),
                                dur=round(e_ - s_ - 0.2, 3),
                                track="Hero",
                            ),
                            "media",
                        )
                    )
                    if el.get("sub"):
                        shot["texts"].append(
                            self.place(
                                self.card_role(
                                    "card_label",
                                    content=el["sub"],
                                    start=round(s_ + 0.4, 3),
                                    dur=round(e_ - s_ - 0.4, 3),
                                    track="CardLabel",
                                ),
                                "label",
                            )
                        )
                elif kind == "end_card":
                    self.end_card(el, bt, shot)
                elif kind in ("title", "hero", "keyword", "label", "cta"):
                    track = {
                        "title": "Title",
                        "hero": "Hero",
                        "keyword": "CardLabel",
                        "label": "CardLabel",
                        "cta": "CTA",
                    }[kind]
                    if kind in ("hero", "cta") and el.get("pip", self.insert_mode == "pip"):
                        s, e = resolve_time(el.get("time"), self.tr, bt[0], bt)
                        if kind == "cta" and el.get("last"):
                            s = max(bt[0], bt[1] - float(el["last"]))
                            el = {**el, "time": [s + 0.2, bt[1]]}
                        self.add_camera(
                            "pip",
                            s,
                            e - s if kind == "hero" else bt[1] - s,
                            f"{'点题' if kind == 'hero' else '结尾互动'}: 真人缩成小窗",
                        )
                    self.text(el, kind, bt, shot, track)
                elif kind == "effect":
                    eff = (
                        self.style.get("ending_effect")
                        if el.get("name", "ending") == "ending"
                        else {"name": el["name"]}
                    )
                    if eff:
                        d = max(float(eff.get("min_dur", 3.0)), float(el.get("dur", 0)))
                        shot.setdefault("effects", []).append(
                            {"name": eff["name"], "start": round(bt[1] - d, 3), "dur": round(d, 3)}
                        )
                elif kind == "sfx":
                    name = (self.style.get("sfx") or {}).get(
                        el.get("name", "accent"), el.get("name")
                    )
                    if name:
                        s, _e = resolve_time(el.get("time"), self.tr, bt[0], bt)
                        shot.setdefault("sfx", []).append(
                            {
                                "name": name,
                                "at": round(s, 3),
                                "volume": (self.style.get("sfx") or {}).get("volume", 0.35),
                            }
                        )
                else:
                    raise ScriptError(f"未知元素类型: {kind}")
            cam = b.get("camera")
            if cam:
                cams = cam if isinstance(cam, list) else [cam]
                for c in cams:
                    c = {"move": c} if isinstance(c, str) else c
                    at = resolve_time(c.get("time"), self.tr, bt[0], (bt[0] + 0.2, bt[0] + 3))[0]
                    dur = float(c.get("dur", 0.9 if c["move"] == "punch_in" else 3.0))
                    if not self._overlaps_pip(at, at + dur):
                        self.add_camera(
                            c["move"],
                            at,
                            dur,
                            c.get("note", f"{shot['purpose']}"),
                            ui=bool(b.get("ui_insert")),
                        )
            shots.append(shot)
            prev_end = bt[1]
        if self.plan is not None:
            for a in self.plan.get("accents") or []:  # 强拍重音 (场景允许 punch_in 时才有)
                self.add_camera(a["move"], float(a["at"]), float(a["dur"]), "强拍重音")
            self.transitions = self.montage_transitions(beats, times)
        self.check_hook(shots)
        keep = self.keep_ranges()
        # 剪点遮盖: 没有转场时, 剪点后用「假景别」(放大后拉回) 遮跳剪
        if (
            not self.style.get("cut_transition")
            and self.scene.get("camera", {}).get("cut_cover") == "pull_out"
        ):
            for a, _b in keep[1:]:
                if not self._overlaps_pip(a, a + 1.5):
                    self.camera.append(
                        {
                            "at": a,
                            "dur": 1.5,
                            "move": "pull_out",
                            "amount": 0.25,
                            "note": "剪点后由 125% 拉回, 假景别遮跳剪 (差 ≥ 1.25x; 1.03–1.15x 的小跳最难看)",
                        }
                    )
        self.camera.sort(key=lambda c: c["at"])
        sb = self._storyboard(shots, keep)
        self.check_layout()
        return sb

    def _subtitles(self) -> Optional[dict]:
        cfg = self.sc.get("subtitles") or {}
        sub_cfg = self.scene.get("subtitles", {})
        default = sub_cfg.get("default")
        mode = cfg.get("mode") or ("labels" if default == "labels" else None)
        if mode in ("none", "labels") or default is False and not cfg:
            return None  # labels: 宣传片不逐句出字幕, 卖点用标签卡 (label 元素)
        from storyboard_builder import split_transcript

        lang = cfg.get("lang", self.lang or "zh")
        lay = self.fmt.get("subtitle_layout") or {}
        max_chars = int(
            cfg.get("max_chars")
            or (lay.get("wrap_chars_en", 26) * 1.6 if lang != "zh" and lay else 0)
            or sub_cfg.get("max_chars_en" if lang != "zh" else "max_chars_zh", 15)
        )
        total = self.keep_ranges()[-1][1]
        if self.lyrics and (default == "lyrics" or mode == "lyrics"):  # MV: 歌词一句一条
            table = (self.sc.get("translations") or {}).get(self.lang or "", {})
            lines = [
                {
                    "start": max(0.0, ln["start"]),
                    "end": min(total, ln["end"]),
                    "text": table.get(ln["text"], ln["text"]),
                }
                for ln in self.lyrics
                if ln["text"] and ln["start"] < total
            ]
        else:
            lines = split_transcript(
                self.tr.segments, max_chars, float(sub_cfg.get("max_dur", 3.2)), 0.28
            )
        fixes = cfg.get("fixes") or {}
        out = []
        for ln in lines:
            if any(a - 0.05 <= ln["start"] < b for a, b in self.no_sub):
                continue
            nxt_block = min([a for a, _ in self.no_sub if a > ln["start"]] + [1e9])
            end = min(ln["end"], nxt_block - 0.1)
            text = ln["text"]
            for k, v in fixes.items():
                text = text.replace(k, v)
            out.append({"start": round(ln["start"], 3), "end": round(end, 3), "text": text})
        slot = self.fmt["slots"]["subtitle"]
        prim = {
            **self.role("subtitle"),
            "track": "SubMain",
            "y": slot["y"],
            "x": slot["x"],
            "max_line_width": slot["max_line_width"],
            "size": max(
                self.fmt["subtitle"]["min_size"], self.style["roles"]["subtitle"].get("size", 9.0)
            ),
            "wrap_chars": self.fmt["subtitle"]["wrap_chars_en"] if lang != "zh" else 0,
        }
        prim.pop("role", None)
        if lang == "zh" and self.style["roles"]["subtitle"].get("font_zh"):
            prim["font"] = self.style["roles"]["subtitle"]["font_zh"]
        lay = self.fmt.get("subtitle_layout")
        if lay and cfg.get("anchor", "bottom") == "bottom":
            prim.update(
                anchor_bottom=lay["anchor_bottom"],
                px_per_size=self.fmt.get("px_per_size", 6.0),
                size=max(prim["size"], lay["size"]),
            )
            if lang != "zh":
                prim["wrap_chars"] = lay["wrap_chars_en"]
            reg = (self.circle_spec.get("regions") or {}).get("subtitle")
            if reg:  # 字幕带: 水平居中于字幕区, 按真实字宽折行, 最多 max_lines 行 (装配器里折)
                cw = float(self.fmt["canvas"]["width"])
                prim.update(
                    x=round((reg[0] + reg[2]) / 2, 4),
                    box_width_px=round((reg[2] - reg[0]) * cw / 2),
                    max_lines=lay.get("max_lines", 2),
                    wrap_chars=0,
                )
        y_fmt = (sub_cfg.get("y_by_format") or {}).get(self.fmt["id"])
        if (
            cfg.get("y") is None and y_fmt is not None
        ):  # 场景按画幅给的字幕位置 (如教程横屏底部深色条 -0.85)
            prim["y"] = y_fmt
            prim.pop("anchor_bottom", None)
        if cfg.get("y") is not None:
            prim["y"] = cfg["y"]
            prim.pop("anchor_bottom", None)
        if sub_cfg.get("size_scale"):  # MV 歌词用小号字
            prim["size"] = round(
                max(self.fmt["subtitle"]["min_size"], prim["size"] * float(sub_cfg["size_scale"])),
                2,
            )
        bg = cfg.get("background", sub_cfg.get("background"))
        if bg:  # 深色底条: 不再加描边
            prim["background"] = dict(bg)
            prim["border"] = False
        return {"lines": out, "lead": float(sub_cfg.get("lead", 0.15)), "primary": prim}

    def _storyboard(self, shots: List[dict], keep: List[List[float]]) -> dict:
        sc = self.sc
        trans = self.style.get("cut_transition")
        if "default" in (
            self.scene.get("transitions") or {}
        ):  # 场景规定了剪点转场 (宣传片 / MV: 硬切为主)
            trans = self.scene["transitions"]["default"]
        if self.transitions is not None:
            trans = self.transitions
        main = {
            "source": "main",
            "keep": keep,
            "transitions": trans,
            "audio": {"loudness_lufs": self.scene.get("audio", {}).get("loudness_lufs", -14)},
            "camera": self.camera,
        }
        if self.plan is not None:  # 混剪: 没有主视频, 镜头见顶层 montage (beat_snap 计划, 成片时间)
            main.update(source=None, montage=True)
        if self.speed:
            main["speed"] = self.speed
            self.notes.append(
                "加速段写在 main.speed (每段对应 keep 里的一个区间); 装配器不支持时按原速播放"
            )
        for w in self.waits_done:
            a, b = w["range"]
            how = (
                f"加速 {w['speed']}x"
                if w["mode"] == "speedup"
                else f"跳剪 (剪掉 {w['removed']:.1f}s)"
            )
            self.notes.append(f"等待段 {a:.1f}–{b:.1f}s ({b - a:.1f}s): {how}")
        if self.style.get("grade"):
            main["grade"] = self.style["grade"]
        cont = self.scene.get("continuity") or {}
        if cont.get("grade") == "unified" and not main.get("grade"):
            self.notes.append(
                "场景要求全片统一调色, 风格没有默认 grade: 在剧本里给所有镜头同一个滤镜 / LUT"
            )
        v = self.analysis.get("video") or {}
        canvas = self.fmt["canvas"]
        src_portrait = v.get("orientation") == "portrait"
        fmt_portrait = canvas["height"] > canvas["width"]
        if v and src_portrait != fmt_portrait:
            # 素材方向和画幅不一致 (如竖屏素材出横屏版): 两侧 / 上下会露边, 全程用画面自身的模糊垫底
            main["background"] = {"type": "blur", "blur": 1.0}
            self.notes.append(
                f"素材是{'竖' if src_portrait else '横'}屏, 画幅是{self.fmt['name']}: 全程用模糊背景填充"
            )
        elif self.pip_ranges and self.style.get("pip_background"):
            main["background"] = self.style["pip_background"]
        if (
            sc.get("bgm") is False or self.plan is not None
        ):  # 明确不要配乐; 混剪的音乐在 montage.music 里
            bgm = {}
        else:
            bgm = dict(sc.get("bgm") or self.style.get("bgm") or {})
            if bgm and "volume" not in (sc.get("bgm") or {}):
                bgm["volume"] = self.scene.get("audio", {}).get(
                    "bgm_volume", bgm.get("volume", 0.2)
                )
            if not bgm and self.scene.get("audio", {}).get("bgm") == "default":
                self.notes.append(
                    f"场景「{self.scene['name']}」默认加 BGM, 但风格没有默认配乐: 在剧本里写 bgm"
                )
        sb = {
            "version": 2,
            "compiled_from": {
                "script": sc.get("title"),
                "scene": self.scene["id"],
                "style": self.style["id"],
                "format": self.fmt["id"],
                "user_prefs": self.user_prefs,
            },
            "project": {
                "name": sc["name"],
                "canvas": self.fmt["canvas"],
                "scene": self.scene["name"],
                "style": self.style["id"],
                "platform": sc.get("platform", self.fmt["platforms"][0]),
                "allow_vip": sc.get("allow_vip", True),
            },
            "sources": sc.get("sources") or {},
            "main": main,
            "shots": shots,
            "audio": {"bgm": bgm} if bgm.get("id") or bgm.get("path") or bgm.get("query") else {},
            "notes": self.notes,
        }
        if self.lang:
            sb["compiled_from"]["lang"] = self.lang
        if self.plan is not None:
            sb["montage"] = self.plan
            sb["sources"] = sb["sources"] or {}
        review = self.review_hints(shots)
        if review:
            sb["review"] = review
        subs = self._subtitles()
        if subs:
            sb["subtitles"] = subs
        return sb

    def review_hints(self, shots: List[dict]) -> dict:
        """给审片器的场景提示: 钩子类型、色调检查范围 (按段落)、有意保留的静止段 (标题卡 / 结尾卡)。"""
        out = {}
        hk = self.scene.get("hook") or {}
        if hk.get("type"):
            out["hook"] = {"type": hk["type"], "hold_s": hk.get("hold_s")}
        cont = self.scene.get("continuity") or {}
        if cont.get("mode"):
            out["continuity"] = cont["mode"]
            if cont["mode"] == "per_section":
                out["sections"] = [sh["time"] for sh in shots]
        holds = []
        for i, sh in enumerate(shots):
            roles = {t.get("role") for t in sh.get("texts") or []}
            if (i == 0 and roles & {"hero", "title"}) or (
                i == len(shots) - 1
                and any(
                    t.get("track", "").startswith("End") or t.get("role") == "cta"
                    for t in sh.get("texts") or []
                )
            ):
                holds.append({"time": sh["time"], "why": "标题卡" if i == 0 else "结尾卡 (CTA)"})
        if holds and hk.get("type") == "title_card":
            out["intentional_holds"] = holds
        return out


def compile_script(
    path: str,
    out: Optional[str] = None,
    fmt: Optional[str] = None,
    style: Optional[str] = None,
    lang: Optional[str] = None,
) -> str:
    with open(path, "r", encoding="utf-8") as f:
        script = json.load(f)
    tags = [x for x in (fmt, style, lang) if x]
    if tags:  # 换画幅 / 风格 / 语言时, 草稿名加后缀, 不覆盖原来的
        script["name"] = f"{script['name']}_{'_'.join(tags)}"
    sb = Compiler(script, os.path.dirname(os.path.abspath(path)), fmt, style, lang).compile()
    if not out:  # 只换文件名里的 script, 不动目录名
        stem = os.path.splitext(path)[0]
        out = (
            os.path.join(
                os.path.dirname(stem), os.path.basename(stem).replace("script", "storyboard")
            )
            + (f"_{'_'.join(tags)}" if tags else "")
            + ".json"
        )
    if os.path.abspath(out) == os.path.abspath(path):
        out = os.path.splitext(path)[0] + ".storyboard.json"
    if os.path.exists(out):  # 不覆盖手写 / 手改过的分镜表 (没有 compiled_from 标记的)
        try:
            with open(out, "r", encoding="utf-8") as f:
                old = json.load(f)
        except (OSError, ValueError):
            old = {}
        if not old.get("compiled_from"):
            raise ScriptError(
                f"{out} 已存在且不是编译器生成的 (可能是手写的分镜表), 不覆盖; 用 -o 指定别的文件名"
            )
    with open(out, "w", encoding="utf-8") as f:
        json.dump(sb, f, ensure_ascii=False, indent=1)
    n_cuts = len(sb["montage"]["shots"]) - 1 if sb.get("montage") else len(sb["main"]["keep"]) - 1
    print(
        f"✅ 分镜表 {out}: {len(sb['shots'])} 个镜头, 运镜 {len(sb['main']['camera'])} 处, "
        f"字幕 {len((sb.get('subtitles') or {}).get('lines', []))} 条, 剪点 {n_cuts} 处 "
        f"({sb['compiled_from']['scene']} / {sb['compiled_from']['style']} / {sb['compiled_from']['format']})"
    )
    for n in sb["notes"]:
        print(f"   - {n}")
    return out


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="剧本 + 规范 -> 分镜表")
    ap.add_argument("script")
    ap.add_argument("-o", "--output")
    ap.add_argument("--format", help="换画幅: vertical / horizontal / portrait_3x4 / square")
    ap.add_argument("--style", help="换风格: guofeng / tech / fresh / variety / business")
    ap.add_argument(
        "--lang", help="多语言版本: 只换文字 (元素 i18n / 剧本 translations), 时间线不变, 如 en"
    )
    ap.add_argument("--build", action="store_true", help="编译后直接装配 (storyboard_builder.py)")
    ap.add_argument("--preview", action="store_true", help="装配后在剪映里截图 (macOS)")
    ap.add_argument(
        "--no-user-prefs", action="store_true", help="不套用个人偏好 (style_overrides.json)"
    )
    args = ap.parse_args(argv)
    if args.no_user_prefs:
        os.environ["JY_USER_PREFS"] = "off"
    try:
        out = compile_script(args.script, args.output, args.format, args.style, args.lang)
    except ScriptError as e:
        print(f"❌ 剧本有误: {e}", file=sys.stderr)
        return 2
    if args.build:
        from storyboard_builder import build

        res = build(out, review=True, preview=args.preview)
        return 0 if res.get("review_exit", 0) == 0 else 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
