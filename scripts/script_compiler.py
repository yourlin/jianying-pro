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

FORMAT_ALIASES = {"9:16": "vertical", "16:9": "horizontal", "3:4": "portrait_3x4", "1:1": "square",
                  "竖屏": "vertical", "横屏": "horizontal", "小红书": "portrait_3x4", "方形": "square"}
STYLE_ALIASES = {"国风": "guofeng", "国风古韵": "guofeng", "科技": "tech", "清新": "fresh", "清新文艺": "fresh",
                 "综艺": "variety", "综艺活泼": "variety", "商务": "business", "商务简约": "business"}
SCENE_ALIASES = {"口播": "talking_head", "口播知识": "talking_head", "vlog": "vlog", "Vlog": "vlog", "教程": "tutorial",
                 "教程录屏": "tutorial", "带货": "product", "带货种草": "product", "解说": "commentary",
                 "影视解说": "commentary", "卡点": "beat_montage", "卡点混剪": "beat_montage"}


class ScriptError(ValueError):
    pass


def load_profile(kind: str, key: str) -> dict:
    """kind: formats / styles / scenes。"""
    alias = {"formats": FORMAT_ALIASES, "styles": STYLE_ALIASES, "scenes": SCENE_ALIASES}[kind]
    key = alias.get(key, key)
    path = os.path.join(SKILL, "references", kind, f"{key}.json")
    if not os.path.exists(path):
        options = sorted(os.path.splitext(f)[0] for f in os.listdir(os.path.dirname(path)) if f.endswith(".json"))
        raise ScriptError(f"没有这个{ {'formats': '画幅', 'styles': '风格', 'scenes': '场景'}[kind] }: {key} (可选: {'、'.join(options)})")
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


def resolve_time(spec, tr: Transcript, after: float = 0.0, default: Optional[Tuple[float, float]] = None):
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

class Compiler:
    def __init__(self, script: dict, base_dir: str, fmt: Optional[str] = None, style: Optional[str] = None):
        self.sc = script
        self.base = base_dir
        self.fmt = load_profile("formats", fmt or script.get("format", "vertical"))
        self.style = load_profile("styles", style or script.get("style", "guofeng"))
        self.scene = load_profile("scenes", script.get("scene", "talking_head"))
        self.analysis = self._load_analysis()
        self.tr = Transcript(self.analysis.get("transcript") or [])
        self.faces = self.analysis.get("faces") or {}
        self.notes: List[str] = []
        self.camera: List[dict] = []
        self.pip_ranges: List[Tuple[float, float]] = []
        self.no_sub: List[Tuple[float, float]] = list(map(tuple, (script.get("subtitles") or {}).get("exclude", [])))
        self.insert_mode = script.get("insert_mode") or self.scene.get("inserts", {}).get("default", "pip")

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
        out = {"role": name, **{k: v for k, v in r.items() if not k.startswith("_") and k != "font_zh"}}
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
        if not self.style.get("flower_on_bright_background", True) and t.get("role") in ("label", "keyword"):
            t.pop("flower", None)
            t["border"] = True
        return t

    # -- 主轨 --
    def keep_ranges(self) -> List[List[float]]:
        """按节奏剪气口: 句子之间的停顿 >= 阈值时剪掉中间部分 (两侧各留 keep_breath 秒); 片尾留 0.35s。"""
        pacing = self.sc.get("pacing", "natural")
        p = self.scene.get("pacing", {})
        th = {"compact": p.get("compact", 0.45), "natural": p.get("natural", 0.8)}.get(pacing)
        dur = (self.analysis.get("video") or {}).get("duration") or self.tr.end
        end = min(dur, self.tr.end + 0.35) if self.tr.words else dur
        if not th or not self.tr.segments:
            return [[0.0, round(end, 3)]]
        breath = float(p.get("keep_breath", 0.25))
        ranges, cur = [], 0.0
        segs = self.tr.segments
        for a, b in zip(segs, segs[1:]):
            gap_s, gap_e = a["end"], b["start"]
            if gap_e - gap_s >= th:
                cut_s, cut_e = round(gap_s + breath, 3), round(gap_e - breath, 3)
                if cut_e - cut_s >= 0.2:
                    ranges.append([round(cur, 3), cut_s])
                    cur = cut_e
        ranges.append([round(cur, 3), round(end, 3)])
        return ranges

    def add_camera(self, move: str, at: float, dur: float, note: str = "", **kw):
        allowed = self.scene.get("camera", {}).get("allowed")
        if allowed and move not in allowed:
            self.notes.append(f"运镜 {move} 不在场景「{self.scene['name']}」的白名单里, 已跳过")
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
        self.camera.append({"at": round(at, 3), "dur": round(dur, 3), "move": move, "note": note, **kw})

    def _overlaps_pip(self, s: float, e: float) -> bool:
        return any(s < b and e > a for a, b in self.pip_ranges)

    # -- 元素 --
    def card(self, el: dict, beat_t: Tuple[float, float], shot: dict):
        src = el["asset"]
        info = next((a for a in self.analysis.get("assets", []) if os.path.basename(a["path"]) ==
                     os.path.basename((self.sc.get("sources") or {}).get(src, src))), {})
        s, e = resolve_time(el.get("time"), self.tr, beat_t[0], beat_t)
        s, e = s + float(el.get("delay", 0.3)), min(e, beat_t[1])
        orient = info.get("orientation", "landscape")
        sc = self.fmt["card"].get(f"scale_{orient}", 0.78)
        mode = el.get("mode", self.insert_mode)
        ov = {"source": src, "start": round(s, 3), "dur": round(e - s, 3), "pos": list(self.fmt["card"]["pos"]),
              "scale": [sc, round(sc * 1.05, 3)], "motion": "float_in", "shadow": True}
        if mode == "cover":
            ov.update(fit="cover", scale=[1.0, 1.06], motion="push")
        elif mode == "pip":
            self.add_camera("pip", s - 0.2, e - s + 0.2, f"插卡「{el.get('label', src)}」: 真人缩成小窗")
        elif mode == "overlay" and self.faces.get("card_zones"):
            z = self.faces["card_zones"][0]["y_range"]
            ov["pos"] = [0, round(sum(z) / 2, 3)]
        shot.setdefault("overlays", []).append(ov)
        if el.get("label"):
            shot.setdefault("texts", []).append(self._on_bright(self.role(
                "label", content=el["label"], start=round(s + 0.25, 3), dur=round(e - s - 0.5, 3), track="CardLabel")))
        sfx = (self.style.get("sfx") or {}).get("card")
        if sfx and el.get("sfx", True):
            shot.setdefault("sfx", []).append({"name": sfx, "at": round(s, 3), "volume": self.style["sfx"].get("volume", 0.35)})

    def text(self, el: dict, kind: str, beat_t: Tuple[float, float], shot: dict, track: str):
        s, e = resolve_time(el.get("time"), self.tr, beat_t[0], beat_t)
        if el.get("time") is None:
            s += 0.3
            lim = self.scene.get("elements", {}).get(kind, {}).get("max_dur")
            if lim:
                e = min(e, s + lim)
        t = self.role(kind, content=el["text"], start=round(s, 3), dur=round(e - s, 3), track=track)
        shot.setdefault("texts", []).append(self._on_bright(t))
        if el.get("sub"):
            sub_role = "hero_sub" if kind == "hero" else ("cta_sub" if kind == "cta" else "label")
            shot["texts"].append(self.role(sub_role, content=el["sub"], start=round(s + 0.3, 3),
                                           dur=round(e - s - 0.3, 3), track=track + "Sub"))
        return s, e

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
        chin = self.faces.get("chin_y")
        main_y = self.fmt["slots"]["lower_zone"]["y"]
        if chin is not None:
            main_y = min(main_y, chin - 0.11)
        bottom_safe = self.fmt["safe"]["bottom"]
        if main_y - 0.10 < bottom_safe - 0.03:
            main_y = bottom_safe - 0.03 + 0.10
        for i, (it, (s, _e)) in enumerate(zip(items, times)):
            st = max(beat_t[0], s - 0.12)
            en = (times[i + 1][0] - 0.12 - 0.01) if i + 1 < len(times) else beat_t[1]  # 相邻词卡留 10ms, 避免精度误差重叠
            d = round(en - st, 3)
            if it.get("top"):
                shot.setdefault("texts", []).append(self.role("vocab_top", content=it["top"], start=round(st, 3), dur=d,
                                                              x=0, y=round(main_y + 0.10, 3), track="VocabTop"))
            shot.setdefault("texts", []).append(self.role("vocab", content=it["main"], start=round(st, 3), dur=d,
                                                          y=round(main_y, 3), track="Vocab"))
            if it.get("bottom"):
                shot["texts"].append(self.role("vocab_bottom", content=it["bottom"], start=round(st, 3), dur=d,
                                               x=0, y=round(main_y - 0.10, 3), track="VocabBottom"))
        if self.scene.get("elements", {}).get("vocab", {}).get("hide_subtitles", True):
            self.no_sub.append((round(times[0][0] - 0.15, 3), round(beat_t[1], 3)))

    def compile(self) -> dict:
        sc = self.sc
        shots = []
        beats = sc.get("beats") or []
        prev_end = 0.0
        video_end = self.keep_ranges()[-1][1]
        for i, b in enumerate(beats):
            bt = resolve_time(b.get("time"), self.tr, prev_end)
            if bt is None:
                raise ScriptError(f"段落 {b.get('id')} 需要 time (时间或台词)")
            if i == len(beats) - 1:  # 最后一段延到成片结尾 (结尾特效、互动卡要停到最后)
                bt = (bt[0], max(bt[1], video_end))
            shot = {"id": b.get("id", f"S{i + 1:02d}"), "time": [round(bt[0], 3), round(bt[1], 3)],
                    "purpose": b.get("purpose", ""), "line": b.get("line", ""), "note": b.get("note", "")}
            for el in b.get("elements") or []:
                kind = el["kind"]
                if kind == "card":
                    self.card(el, bt, shot)
                elif kind == "vocab":
                    self.vocab(el, bt, shot)
                elif kind in ("title", "hero", "keyword", "label", "cta"):
                    track = {"title": "Title", "hero": "Hero", "keyword": "CardLabel", "label": "CardLabel",
                             "cta": "CTA"}[kind]
                    if kind in ("hero", "cta") and el.get("pip", self.insert_mode == "pip"):
                        s, e = resolve_time(el.get("time"), self.tr, bt[0], bt)
                        if kind == "cta" and el.get("last"):
                            s = max(bt[0], bt[1] - float(el["last"]))
                            el = {**el, "time": [s + 0.2, bt[1]]}
                        self.add_camera("pip", s, e - s if kind == "hero" else bt[1] - s,
                                        f"{'点题' if kind == 'hero' else '结尾互动'}: 真人缩成小窗")
                    self.text(el, kind, bt, shot, track)
                elif kind == "effect":
                    eff = self.style.get("ending_effect") if el.get("name", "ending") == "ending" else {"name": el["name"]}
                    if eff:
                        d = max(float(eff.get("min_dur", 3.0)), float(el.get("dur", 0)))
                        shot.setdefault("effects", []).append({"name": eff["name"], "start": round(bt[1] - d, 3),
                                                               "dur": round(d, 3)})
                elif kind == "sfx":
                    name = (self.style.get("sfx") or {}).get(el.get("name", "accent"), el.get("name"))
                    if name:
                        s, _e = resolve_time(el.get("time"), self.tr, bt[0], bt)
                        shot.setdefault("sfx", []).append({"name": name, "at": round(s, 3),
                                                           "volume": (self.style.get("sfx") or {}).get("volume", 0.35)})
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
                        self.add_camera(c["move"], at, dur, c.get("note", f"{shot['purpose']}"))
            shots.append(shot)
            prev_end = bt[1]
        keep = self.keep_ranges()
        # 剪点遮盖: 没有转场时, 剪点后用「假景别」(放大后拉回) 遮跳剪
        if not self.style.get("cut_transition") and self.scene.get("camera", {}).get("cut_cover") == "pull_out":
            for a, _b in keep[1:]:
                if not self._overlaps_pip(a, a + 1.5):
                    self.camera.append({"at": a, "dur": 1.5, "move": "pull_out", "amount": 0.12,
                                        "note": "剪点后由 112% 拉回, 假景别遮跳剪"})
        self.camera.sort(key=lambda c: c["at"])
        return self._storyboard(shots, keep)

    def _subtitles(self) -> Optional[dict]:
        cfg = self.sc.get("subtitles") or {}
        if cfg.get("mode") == "none" or self.scene.get("subtitles", {}).get("default") is False and not cfg:
            return None
        from storyboard_builder import split_transcript

        lang = cfg.get("lang", "zh")
        sub_cfg = self.scene.get("subtitles", {})
        max_chars = int(cfg.get("max_chars") or sub_cfg.get("max_chars_en" if lang != "zh" else "max_chars_zh", 15))
        lines = split_transcript(self.tr.segments, max_chars, float(sub_cfg.get("max_dur", 3.2)), 0.28)
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
        prim = {**self.role("subtitle"), "track": "SubMain", "y": slot["y"], "x": slot["x"],
                "max_line_width": slot["max_line_width"], "size": max(self.fmt["subtitle"]["min_size"],
                                                                      self.style["roles"]["subtitle"].get("size", 9.0)),
                "wrap_chars": self.fmt["subtitle"]["wrap_chars_en"] if lang != "zh" else 0}
        prim.pop("role", None)
        if lang == "zh" and self.style["roles"]["subtitle"].get("font_zh"):
            prim["font"] = self.style["roles"]["subtitle"]["font_zh"]
        if cfg.get("y") is not None:
            prim["y"] = cfg["y"]
        return {"lines": out, "lead": float(sub_cfg.get("lead", 0.15)), "primary": prim}

    def _storyboard(self, shots: List[dict], keep: List[List[float]]) -> dict:
        sc = self.sc
        main = {"source": "main", "keep": keep, "transitions": self.style.get("cut_transition"),
                "audio": {"loudness_lufs": self.scene.get("audio", {}).get("loudness_lufs", -14)},
                "camera": self.camera}
        if self.style.get("grade"):
            main["grade"] = self.style["grade"]
        v = self.analysis.get("video") or {}
        canvas = self.fmt["canvas"]
        src_portrait = v.get("orientation") == "portrait"
        fmt_portrait = canvas["height"] > canvas["width"]
        if v and src_portrait != fmt_portrait:
            # 素材方向和画幅不一致 (如竖屏素材出横屏版): 两侧 / 上下会露边, 全程用画面自身的模糊垫底
            main["background"] = {"type": "blur", "blur": 1.0}
            self.notes.append(f"素材是{'竖' if src_portrait else '横'}屏, 画幅是{self.fmt['name']}: 全程用模糊背景填充")
        elif self.pip_ranges and self.style.get("pip_background"):
            main["background"] = self.style["pip_background"]
        bgm = dict(sc.get("bgm") or self.style.get("bgm") or {})
        if bgm and "volume" not in (sc.get("bgm") or {}):
            bgm["volume"] = self.scene.get("audio", {}).get("bgm_volume", bgm.get("volume", 0.2))
        sb = {
            "version": 2,
            "compiled_from": {"script": sc.get("title"), "scene": self.scene["id"], "style": self.style["id"],
                              "format": self.fmt["id"]},
            "project": {"name": sc["name"], "canvas": self.fmt["canvas"], "scene": self.scene["name"],
                        "style": self.style["id"], "platform": sc.get("platform", self.fmt["platforms"][0]),
                        "allow_vip": sc.get("allow_vip", True)},
            "sources": sc.get("sources") or {},
            "main": main,
            "shots": shots,
            "audio": {"bgm": bgm} if bgm.get("id") or bgm.get("path") or bgm.get("query") else {},
            "notes": self.notes,
        }
        subs = self._subtitles()
        if subs:
            sb["subtitles"] = subs
        return sb


def compile_script(path: str, out: Optional[str] = None, fmt: Optional[str] = None,
                   style: Optional[str] = None) -> str:
    with open(path, "r", encoding="utf-8") as f:
        script = json.load(f)
    if fmt or style:  # 换画幅 / 风格时, 草稿名加后缀, 不覆盖原来的
        suffix = "_".join(x for x in (fmt, style) if x)
        script["name"] = f"{script['name']}_{suffix}"
    sb = Compiler(script, os.path.dirname(os.path.abspath(path)), fmt, style).compile()
    out = out or os.path.splitext(path)[0].replace("script", "storyboard") + (
        f"_{'_'.join(x for x in (fmt, style) if x)}" if fmt or style else "") + ".json"
    if os.path.abspath(out) == os.path.abspath(path):
        out = os.path.splitext(path)[0] + ".storyboard.json"
    if os.path.exists(out):  # 不覆盖手写 / 手改过的分镜表 (没有 compiled_from 标记的)
        try:
            with open(out, "r", encoding="utf-8") as f:
                old = json.load(f)
        except (OSError, ValueError):
            old = {}
        if not old.get("compiled_from"):
            raise ScriptError(f"{out} 已存在且不是编译器生成的 (可能是手写的分镜表), 不覆盖; 用 -o 指定别的文件名")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(sb, f, ensure_ascii=False, indent=1)
    print(f"✅ 分镜表 {out}: {len(sb['shots'])} 个镜头, 运镜 {len(sb['main']['camera'])} 处, "
          f"字幕 {len((sb.get('subtitles') or {}).get('lines', []))} 条, 剪点 {len(sb['main']['keep']) - 1} 处 "
          f"({sb['compiled_from']['scene']} / {sb['compiled_from']['style']} / {sb['compiled_from']['format']})")
    for n in sb["notes"]:
        print(f"   - {n}")
    return out


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="剧本 + 规范 -> 分镜表")
    ap.add_argument("script")
    ap.add_argument("-o", "--output")
    ap.add_argument("--format", help="换画幅: vertical / horizontal / portrait_3x4 / square")
    ap.add_argument("--style", help="换风格: guofeng / tech / fresh / variety / business")
    ap.add_argument("--build", action="store_true", help="编译后直接装配 (storyboard_builder.py)")
    ap.add_argument("--preview", action="store_true", help="装配后在剪映里截图 (macOS)")
    args = ap.parse_args(argv)
    try:
        out = compile_script(args.script, args.output, args.format, args.style)
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
