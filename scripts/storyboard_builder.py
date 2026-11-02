"""
通用装配器 (引导模式第 5 步): 按分镜表 (storyboard JSON) 确定性地生成剪映草稿。分镜表是唯一真相来源,
要改剪辑就改分镜表重跑, 不要再手写剪辑脚本。格式见 references/storyboard_schema.md。

    python storyboard_builder.py storyboard.json [--no-review] [--preview]

所有时间都用「源视频时间」(秒): 剪掉气口后, 装配器自动换算到成片时间, 所以分镜里可以直接抄转写的时间戳。
"""

import argparse
import json
import os
import subprocess
import sys
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "vendor"))

from utils.frame_align import (  # noqa: E402
    frames_to_us,
    project_fps,
    snap_range,
    snap_us,
    us_to_frames,
)

US = 1_000_000

# 后期运镜预设 (关键帧模拟): 名称 -> 说明。数值见 references/methodology/camera.md
CAMERA_MOVES = {
    "push_in": "缓推: 缩放 1 -> 1+amount (默认 0.10)",
    "pull_out": "缓拉: 缩放 1+amount -> 1",
    "punch_in": "强调推近: 0.15s 内推到 1+amount (默认 0.15) 并保持, 段末回到 1",
    "pan_left": "左摇: 放大 1+amount 后画面从右向左平移",
    "pan_right": "右摇: 放大 1+amount 后画面从左向右平移",
    "tilt_up": "上摇: 放大后画面向上平移",
    "tilt_down": "下摇: 放大后画面向下平移",
    "shake": "震动: 放大 1.06 后短促抖动 (默认 0.4s), 配合重音 / 音效",
    "pip": "真人小窗: 0.3s 内缩到 scale (默认 0.4) 并移到 pos (默认右下), 保持, 段末 0.3s 恢复全屏",
}


class StoryboardError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 时间换算: 源视频时间 -> 成片时间
# ---------------------------------------------------------------------------

class Timeline:
    """main.keep 里保留的源视频区间依次拼接成成片。speeds[k] 是第 k 段的倍速 (main.speed), 成片时长 (b-a)/speed。"""

    def __init__(self, keep: List[Tuple[float, float]], speeds: Optional[List[float]] = None):
        pairs = [(float(a), float(b)) for a, b in keep]
        sp = list(speeds) if speeds else [1.0] * len(pairs)
        kept = [(ab, float(s or 1.0)) for ab, s in zip(pairs, sp) if ab[1] > ab[0]]
        if not kept:
            raise StoryboardError("main.keep 为空")
        self.keep = [ab for ab, _s in kept]
        self.speeds = [s for _ab, s in kept]
        self.set_layout(None)

    def set_layout(self, ranges: Optional[List[Tuple[float, float]]]):
        """每段在成片里的 (起点, 时长) 秒; None 时按理想时长 (b-a)/speed 首尾相接。装配后换成实际写入的整帧位置。"""
        if ranges is None:
            ranges, t = [], 0.0
            for (a, b), s in zip(self.keep, self.speeds):
                ranges.append((t, (b - a) / s))
                t += (b - a) / s
        self.starts = [float(s) for s, _d in ranges]
        self.out_durs = [float(d) for _s, d in ranges]
        self.total = self.starts[-1] + self.out_durs[-1]

    def to_out(self, src: float) -> float:
        """源时间 -> 成片时间 (变速段按倍速换算)。落在被剪掉的区间里时, 吸附到下一个保留区间的开头。"""
        for (a, b), s, d in zip(self.keep, self.starts, self.out_durs):
            if src < a:
                return s
            if src <= b:
                return s + (src - a) * d / (b - a)
        return self.total

    def span(self, start: float, dur: float) -> Tuple[float, float]:
        """源区间 [start, start+dur] -> 成片 (开始, 时长); 跨剪点时时长按成片计算。"""
        s = self.to_out(start)
        e = self.to_out(start + dur)
        return s, max(0.0, min(e, self.total) - s)


def speed_keep(keep, speed) -> Tuple[List[Tuple[float, float]], List[float], List[dict]]:
    """main.speed [{range: [a, b], speed}] -> (keep, 每段倍速, 没对上 keep 的条目)。

    range 一般就是 keep 里的一段; 落在某段 keep 里面时把那段拆开 (前后按原速)。
    """
    out = [(float(a), float(b)) for a, b in keep]
    speeds = [1.0] * len(out)
    bad: List[dict] = []
    eps = 1e-3
    for item in speed or []:
        try:
            a, b = (float(v) for v in item["range"][:2])
            sp = float(item.get("speed", 1.0))
        except (KeyError, TypeError, ValueError):
            bad.append(item)
            continue
        if not (b > a and 0.1 <= sp <= 100):
            bad.append(item)
            continue
        for k, (ka, kb) in enumerate(out):
            if ka - eps <= a and b <= kb + eps and speeds[k] == 1.0:
                a, b = max(a, ka), min(b, kb)
                parts = [((ka, a), 1.0), ((a, b), sp), ((b, kb), 1.0)]
                parts = [p for p in parts if p[0][1] - p[0][0] > eps]
                out[k : k + 1] = [p[0] for p in parts]
                speeds[k : k + 1] = [p[1] for p in parts]
                break
        else:
            bad.append(item)
    return out, speeds, bad


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _rgb(color) -> Optional[Tuple[float, float, float]]:
    if color is None:
        return None
    if isinstance(color, str):
        c = color.lstrip("#")
        return tuple(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return tuple(float(v) for v in color[:3])


def text_background(spec):
    """分镜表的文字背景 -> pyJianYingDraft.TextBackground。

    spec: {"color": "#RRGGBB" 或 "#RRGGBBAA", "alpha": 0~1, "round_radius": 0~1, "style": 1|2,
           "height", "width", "horizontal_offset", "vertical_offset" (0~1, 和剪映面板一致)}; True 表示黑色 60%。
    """
    import pyJianYingDraft as draft

    spec = {"color": "#000000", "alpha": 0.6} if spec is True else dict(spec)
    color = str(spec.get("color") or "#000000")
    if not color.startswith("#"):
        color = "#" + color
    alpha = spec.get("alpha")
    if len(color) == 9:  # #RRGGBBAA: 透明度从颜色里取 (没单独写 alpha 时)
        if alpha is None:
            alpha = int(color[7:9], 16) / 255
        color = color[:7]
    kw = {k: float(spec[k]) for k in ("round_radius", "height", "width", "horizontal_offset", "vertical_offset")
          if spec.get(k) is not None}  # fmt: skip
    style = int(spec.get("style", 1))
    return draft.TextBackground(color=color, style=style if style in (1, 2) else 1,
                                alpha=float(1.0 if alpha is None else alpha), **kw)  # fmt: skip


def _sec(v) -> str:
    return f"{float(v):.3f}s"


def _us(v) -> int:
    """秒 -> 微秒 (未对齐; 对齐由 StoryboardBuilder._at / _range 按工程帧率做)。"""
    return int(round(float(v) * US))


def wrap_text(text: str, limit: int) -> str:
    """英文按词折行, 中文按字数折行, 最多 2 行。"""
    if not limit or len(text) <= limit or "\n" in text:
        return text
    if " " in text.strip():
        words, line, lines = text.split(), "", []
        for w in words:
            if line and len(line) + 1 + len(w) > limit:
                lines.append(line)
                line = w
            else:
                line = (line + " " + w).strip()
        lines.append(line)
    else:
        lines = [text[i:i + limit] for i in range(0, len(text), limit)]
    return "\n".join(lines) if len(lines) <= 2 else "\n".join([lines[0], " ".join(lines[1:]) if " " in text else "".join(lines[1:])])


def split_transcript(transcript: List[dict], max_chars: int = 15, max_dur: float = 3.2,
                     pause: float = 0.28) -> List[dict]:
    """
    转写 -> 字幕条: 词间停顿 >= pause 秒是强断点; 每条不超过 max_chars 个字 (英文按字符) 和 max_dur 秒。
    规则来自 references/methodology/subtitles.md。
    """
    lines = []
    for seg in transcript:
        words = seg.get("words") or []
        if not words:
            lines.append({"start": seg["start"], "end": seg["end"], "text": seg["text"].strip()})
            continue
        cur, cs, prev_e = "", None, None
        for w in words:
            token = w["w"]
            gap = (w["s"] - prev_e) if prev_e is not None else 0
            cand = (cur + token) if cur else token.lstrip()
            too_long = len(cand.strip()) > max_chars or (cs is not None and w["e"] - cs > max_dur)
            if cur and (gap >= pause or too_long):
                lines.append({"start": cs, "end": prev_e, "text": cur.strip()})
                cur, cs = token.lstrip(), w["s"]
            else:
                cur, cs = cand, (cs if cs is not None else w["s"])
            prev_e = w["e"]
        if cur.strip():
            lines.append({"start": cs, "end": prev_e, "text": cur.strip()})
    # 碎条 (少于 4 个字或短于 0.55s) 并到相邻一条里
    def short(ln):
        return len(ln["text"].replace(" ", "")) < 4 or ln["end"] - ln["start"] < 0.55

    def join(a, b):
        sep = " " if (" " in a["text"] + b["text"] or b["text"][:1].isascii()) else ""
        return {"start": a["start"], "end": b["end"], "text": a["text"] + sep + b["text"]}

    def fits(a, b):  # 合并后不能明显超出字数 / 时长上限
        return (len(a["text"]) + len(b["text"]) + 1 <= max_chars * 1.2 and b["end"] - a["start"] <= max_dur * 1.3
                and b["start"] - a["end"] < 1.0)

    merged: List[dict] = []
    pending: Optional[dict] = None  # 没法并到前一条的碎条, 并到下一条开头
    for ln in lines:
        ln = dict(ln)
        if pending:
            if fits(pending, ln):
                ln = join(pending, ln)
            else:
                merged.append(pending)
            pending = None
        if short(ln) and merged and fits(merged[-1], ln):
            merged[-1] = join(merged[-1], ln)
        elif short(ln):
            pending = ln
        else:
            merged.append(ln)
    if pending:
        merged.append(pending)
    return merged


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------

class StoryboardBuilder:
    def __init__(self, sb: dict, base_dir: str):
        self.sb = sb
        self.base = base_dir
        self.warnings: List[str] = []
        self.p = None
        self.tl: Optional[Timeline] = None
        self.main_segs: List = []
        self.report: Dict[str, list] = {"shots": []}
        self.fps = 30.0
        self.music_seg = None  # 混剪模式的音乐片段
        self._continuous_joins: set = set()  # 源视频里连续的剪点 (变速边界, 不是真的剪点)

    # -- 帧对齐: 写入草稿的时间都先对齐到工程帧率的整帧, 和剪映打开草稿时的吸附结果一致 --
    def _at(self, t: float) -> int:
        return snap_us(_us(t), self.fps)

    def _range(self, start: float, dur: float) -> Tuple[int, int]:
        """成片区间 (秒) -> 对齐到整帧的 (起点, 时长) 微秒: 起点 / 终点各自取最近的帧边界。"""
        return snap_range(_us(start), _us(dur), self.fps)

    # -- helpers --
    def color_card(self, spec: str) -> str:
        """"color:#RRGGBB" / "paper:#RRGGBB" -> 画布大小的纯色 / 宣纸纹理图片 (缓存), 用作插卡页的底。"""
        import hashlib

        from PIL import Image, ImageFilter

        kind, _, color = spec.partition(":")
        c = self.sb.get("project", {}).get("canvas") or {}
        w, h = int(c.get("width", 1080)), int(c.get("height", 1920))
        key = hashlib.md5(f"{spec}{w}x{h}".encode()).hexdigest()[:12]
        out = os.path.join(os.path.expanduser("~/.cache/jianying-pro/cards"), f"{kind}_{key}.png")
        if not os.path.exists(out):
            os.makedirs(os.path.dirname(out), exist_ok=True)
            rgb = tuple(int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
            img = Image.new("RGB", (w, h), rgb)
            if kind == "paper":  # 宣纸: 低对比的纤维噪点
                import random

                rnd = random.Random(7)
                noise = Image.new("L", (w // 4, h // 4))
                noise.putdata([rnd.randint(118, 138) for _ in range((w // 4) * (h // 4))])
                noise = noise.resize((w, h)).filter(ImageFilter.GaussianBlur(1.2))
                img = Image.blend(img, Image.merge("RGB", [noise] * 3), 0.06)
            img.save(out)
        return out

    def path(self, ref: str) -> str:
        if isinstance(ref, str) and ref.split(":", 1)[0] in ("color", "paper") and ":#" in ref:
            return self.color_card(ref)
        srcs = self.sb.get("sources") or {}
        p = srcs.get(ref, ref)
        p = p if os.path.isabs(p) else os.path.join(self.base, p)
        if not os.path.exists(p):
            raise StoryboardError(f"素材不存在: {ref} -> {p}")
        return p

    def warn(self, msg: str):
        self.warnings.append(msg)
        print(f"⚠️ {msg}")

    def _main_end_us(self) -> int:
        if not self.main_segs:
            return self._at(self.tl.total) if self.tl else 0
        return max(s.target_timerange.start + s.target_timerange.duration for s in self.main_segs)

    def main_seg_at(self, out_t: float):
        for seg in self.main_segs:
            s = seg.target_timerange.start / US
            if s <= out_t < s + seg.target_timerange.duration / US:
                return seg
        return self.main_segs[-1] if self.main_segs else None

    # -- steps --
    def build(self) -> dict:
        import pyJianYingDraft as draft
        from jy_wrapper import JyProject

        self.draft = draft
        proj = self.sb.get("project") or {}
        canvas = proj.get("canvas") or {}
        self.p = JyProject(proj["name"], width=int(canvas.get("width", 1080)), height=int(canvas.get("height", 1920)),
                           overwrite=True)
        self.p.allow_vip = bool(proj.get("allow_vip", True))
        self.fps = project_fps(self.p)
        self._main()
        self._camera()
        for shot in self.sb.get("shots") or []:
            self._shot(shot)
        self._subtitles()
        self._audio()
        for w in getattr(self.p, "warnings", []) or []:  # API 层的警告 (如蒙版资源未下载) 也进装配报告
            if w not in self.warnings:
                self.warnings.append(w)
        result = self.p.save()
        self.report["total"] = round(self._main_end_us() / US, 3)
        self.report["fps"] = self.fps
        trans = self.p.transition_report() if hasattr(self.p, "transition_report") else []
        if trans:
            self.report["transitions"] = trans
        self.report["warnings"] = self.warnings
        return {"draft": proj["name"], "result": result, "report": self.report}

    def _main(self):
        main = self.sb.get("main") or {}
        if main.get("montage"):
            pairs = self._main_montage(main)
        else:
            pairs = self._main_keep(main)
        # 剪点转场: transitions 可以是 {name, duration} (每个剪点都用) 或列表 (按剪点顺序, null 表示硬切)
        trans = main.get("transitions")
        cuts = len(pairs)
        plan = trans if isinstance(trans, list) else ([trans] * cuts if trans else [])
        if isinstance(trans, list) and len(trans) != cuts:
            self.warn(
                f"main.transitions 有 {len(trans)} 项, 剪点有 {cuts} 个: 多出的忽略, 缺的硬切"
            )
        for i, ((seg, nxt), t) in enumerate(zip(pairs, plan[:cuts])):
            if t and t.get("name") and not isinstance(trans, list) and i in self._continuous_joins:
                continue  # 变速段和前后原速段在源视频里是连着的, 不是剪点: 统一转场不加在这里
            if t and t.get("name"):
                if seg is None or nxt is None:  # 剪点两边有镜头没放进去 (素材缺失)
                    self.warn(f"转场「{t['name']}」所在剪点的镜头缺失, 跳过")
                    continue
                # 时长按剪映规则规整 (偶数帧, 不超过相邻较短片段的一半), 截短较多时警告进装配报告
                dur_us = _us(t.get("duration", 0.5))
                if not self.p.add_transition_simple(t["name"], seg, duration=dur_us):
                    self.warn(f"转场「{t['name']}」没找到")
        bg = main.get("background")  # 背景填充: 主视频缩小 (小窗) 时露出的区域, 不设就是黑色
        if bg:
            for seg in self.main_segs:
                self.p.set_background_fill(seg, bg.get("type", "blur"), blur=float(bg.get("blur", 0.75)),
                                           color=bg.get("color", "#00000000"))
        grade = main.get("grade") or {}
        for seg in self.main_segs:
            if grade.get("adjust"):
                self.p.set_adjust(seg, **grade["adjust"])
            if grade.get("filter"):
                f = grade["filter"]
                self.p.add_filter_simple(seg, f["name"], float(f.get("intensity", 80)))
            if grade.get("beauty"):
                self.p.set_beauty(seg, **grade["beauty"])
        audio = main.get("audio") or {}
        if main.get("montage"):  # 混剪的素材是静音的, 响度统一加在音乐上
            if self.music_seg is not None and audio.get("loudness_lufs") is not None:
                self.p.set_loudness_normalize(self.music_seg, float(audio["loudness_lufs"]))
            return
        for seg in self.main_segs:
            if audio.get("denoise"):
                self.p.set_audio_denoise(seg, float(audio.get("denoise_strength", 85)))
            if audio.get("vocal_beautify"):
                self.p.set_vocal_beautify(seg)
            if audio.get("loudness_lufs") is not None:
                self.p.set_loudness_normalize(seg, float(audio["loudness_lufs"]))

    def _main_keep(self, main: dict) -> List[Tuple]:
        """主视频按 keep 拼接; main.speed 的区间变速。返回剪点两侧的片段 [(前, 后)]。"""
        src = self.path(main.get("source") or "main")
        keep = main.get("keep")
        if not keep:
            from analyze_footage import probe

            keep = [[0, probe(src).get("duration") or 0]]
        keep, speeds, bad = speed_keep(keep, main.get("speed"))
        for item in bad:
            self.warn(f"main.speed {item} 没对上 main.keep 里的区间 (或倍速不在 0.1~100), 按原速")
        self.tl = Timeline(keep, speeds)
        self._continuous_joins = {i for i, ((_a, b), (c, _d)) in enumerate(zip(self.tl.keep, self.tl.keep[1:]))
                             if abs(b - c) < 1e-3}  # fmt: skip
        # 主轨按帧数累加: 每段起点 / 终点从累计的成片时间换算成帧, 相邻片段共用帧边界, 误差不随段数累积。
        # 变速段先按素材时长放, 再用 set_speed 改成 (b-a)/speed (motion_ops._retime 按帧取整);
        # 它的帧数原样计入累计 (extra), 后面原速段的帧边界仍按理想时间换算, 和 accumulate_ranges 一致。
        cum, extra, f0 = Fraction(0), 0, 0
        for (a, b), sp in zip(self.tl.keep, self.tl.speeds):
            s_us = frames_to_us(f0, self.fps)
            if sp == 1.0:
                cum += _us(b - a)
                f1 = max(f0, us_to_frames(cum, self.fps) + extra)
                seg = self.p.add_media_safe(
                    src, s_us, frames_to_us(f1, self.fps) - s_us, "Main", source_start=_us(a)
                )
            else:
                seg = self.p.add_media_safe(src, s_us, _us(b - a), "Main", source_start=_us(a))
                if seg is not None:
                    self.p.set_speed(seg, sp, ripple=False)
                    f1 = us_to_frames(
                        seg.target_timerange.start + seg.target_timerange.duration, self.fps
                    )
                    extra += f1 - f0
                    self.report.setdefault("speed", []).append({
                        "range": [a, b], "speed": sp, "out_start": round(s_us / US, 3),
                        "out_dur": round(seg.target_timerange.duration / US, 3)})  # fmt: skip
            if seg is None:
                raise StoryboardError(f"主视频导入失败: {src}")
            seg.volume = float(main.get("volume", 1.0))
            self.main_segs.append(seg)
            f0 = f1
        # 文字 / 插卡按实际写入的整帧位置换算成片时间
        self.tl.set_layout([(s.target_timerange.start / US, s.target_timerange.duration / US)
                            for s in self.main_segs])  # fmt: skip
        return list(zip(self.main_segs[:-1], self.main_segs[1:]))

    def _main_montage(self, main: dict) -> List[Tuple]:
        """混剪: 按顶层 montage (beat_snap 计划) 铺镜头 + 音乐, 时间线 0 到总时长。返回剪点两侧的片段。"""
        import copy

        plan = copy.deepcopy(self.sb.get("montage") or {})
        shots = plan.get("shots") or []
        if not shots:
            raise StoryboardError(
                "main.montage 为 true, 但分镜表没有 montage.shots (先用 script_compiler 编译)"
            )
        for sh in shots:  # 相对路径相对分镜表所在目录
            ac = sh["add_clip"]
            if not os.path.isabs(ac["media_path"]):
                ac["media_path"] = os.path.join(self.base, ac["media_path"])
        music = plan.get("music") or {}
        if music.get("path") and not os.path.isabs(music["path"]):
            music["path"] = os.path.join(self.base, music["path"])
        if music.get("path") and not os.path.exists(music["path"]):
            self.warn(f"混剪音乐不存在: {music['path']}")
        # 强拍重音已由编译器并进 main.camera, 这里不再加 punch_in, 免得叠两次
        res = self.p.apply_beat_plan(plan, track_name="Main", accents=False)
        segs = res.get("video") or []
        if not segs:
            raise StoryboardError("混剪镜头一个都没放进去 (素材都不存在?)")
        if res.get("missing"):
            self.warn(
                f"混剪有 {len(res['missing'])} 个镜头素材缺失: {sorted(set(res['missing']))[:3]}"
            )
        self.music_seg = res.get("music")
        self.main_segs = sorted(segs, key=lambda s: s.target_timerange.start)
        by_frame = {us_to_frames(s.target_timerange.start, self.fps): s for s in self.main_segs}
        by_shot = [
            by_frame.get(us_to_frames(int(sh["add_clip"]["target_start"]), self.fps))
            for sh in shots
        ]
        total = float(plan.get("total_duration") or ((main.get("keep") or [[0, 0]])[-1][1]))
        end = self._main_end_us() / US
        self.tl = Timeline([(0.0, max(total, end))])  # 段落 / 文字时间就是成片时间
        self.report["montage"] = {
            "shots": len(self.main_segs), "planned": len(shots), "cut_on": plan.get("cut_on"),
            "music": self.music_seg is not None,
        }  # fmt: skip
        return list(zip(by_shot[:-1], by_shot[1:]))

    def _camera(self):
        """后期运镜: 主视频上的关键帧缩放 / 位移。同一片段上的多个运镜合并成一组关键帧。"""
        moves = (self.sb.get("main") or {}).get("camera") or []
        points: Dict[Tuple[int, str], List[Tuple[int, float]]] = {}
        eases: Dict[Tuple[int, str], Optional[str]] = {}
        for mv in moves:
            kind = mv.get("move")
            if kind not in CAMERA_MOVES:
                self.warn(f"未知运镜「{kind}」(可选: {'、'.join(CAMERA_MOVES)})")
                continue
            s, d = self.tl.span(float(mv["at"]), float(mv.get("dur", 3)))
            seg = self.main_seg_at(s + 0.01)
            if seg is None or d <= 0:
                continue
            seg_s = seg.target_timerange.start / US
            seg_e = seg_s + seg.target_timerange.duration / US
            d = min(d, seg_e - s)
            rel0, rel1 = int((s - seg_s) * US), int((s - seg_s + d) * US)
            a = float(mv.get("amount", 0.15 if kind == "punch_in" else 0.10))
            key = id(seg)
            def add(prop, pts):
                points.setdefault((key, prop), []).extend(pts)
                eases[(key, prop)] = mv.get("ease", "缓动" if kind not in ("punch_in", "shake", "pip") else None)
            if kind == "push_in":
                add("scale", [(rel0, 1.0), (rel1, 1 + a)])
            elif kind == "pull_out":
                add("scale", [(rel0, 1 + a), (rel1, 1.0)])
            elif kind == "punch_in":
                hit = min(rel1, rel0 + int(0.15 * US))
                add("scale", [(rel0, 1.0), (hit, 1 + a), (max(hit, rel1 - 1), 1 + a), (rel1, 1.0)])
            elif kind in ("pan_left", "pan_right", "tilt_up", "tilt_down"):
                shift = a / 2  # 放大 a 后最多能平移 a/2 (半个画布宽 / 高为 1)
                sign = 1 if kind in ("pan_left", "tilt_down") else -1
                prop = "x" if kind.startswith("pan") else "y"
                add("scale", [(rel0, 1 + a), (rel1, 1 + a)])
                add(prop, [(rel0, sign * shift), (rel1, -sign * shift)])
            elif kind == "pip":
                sc = float(mv.get("scale", 0.4))
                px, py = (mv.get("pos") or [0.55, -0.45])[:2]
                ramp = min(int(0.3 * US), (rel1 - rel0) // 3)
                for prop, full, small in (("scale", 1.0, sc), ("x", 0.0, float(px)), ("y", 0.0, float(py))):
                    add(prop, [(rel0, full), (rel0 + ramp, small), (rel1 - ramp, small), (rel1, full)])
            elif kind == "shake":
                n = max(2, int(d / 0.08))
                amp = float(mv.get("amount", 0.02))
                add("scale", [(rel0, 1.06), (rel1, 1.06)])
                add("x", [(rel0 + int(i * (rel1 - rel0) / n), (amp if i % 2 else -amp) if i < n else 0.0)
                          for i in range(n + 1)])
            self.report.setdefault("camera", []).append({"move": kind, "out_start": round(s, 3), "dur": round(d, 3)})
        segs = {id(s): s for s in self.main_segs}
        for (key, prop), pts in points.items():
            pts = sorted({t: v for t, v in pts}.items())
            self.p.add_keyframes(segs[key], prop, pts, ease=eases.get((key, prop)))

    def _text(self, t: dict, default_track: str):
        draft = self.draft
        start, dur = self.tl.span(float(t["start"]), float(t["dur"]))
        floor = getattr(self, "_track_end", {}).get(t.get("track", default_track))
        if floor is not None and start < floor:  # 跨剪点换算后可能和同轨上一段重叠: 往后让
            dur -= floor - start
            start = floor
        if dur <= 0:  # 整段落在剪掉的部分里 (比如片尾被裁掉), 不出现
            return None
        if dur < 0.4:
            self.warn(f"文字「{t.get('content', '')[:10]}」在成片里只有 {dur:.2f}s (被剪点截断?)")
        track = t.get("track", default_track)
        clip = draft.ClipSettings(transform_x=float(t.get("x", 0)), transform_y=float(t.get("y", 0)),
                                  scale_x=float(t.get("scale", 1)), scale_y=float(t.get("scale", 1)))
        s_us, d_us = self._range(start, dur)
        tpl = t.get("template")
        if tpl:
            kind = tpl.get("kind", "text")
            content = t.get("content")
            add = self.p.add_subtitle_template if kind == "subtitle" else self.p.add_text_template
            return add(content if content is not None else [None], tpl["name"], s_us, d_us,
                       track_name=track, clip_settings=clip if ("x" in t or "y" in t) else None)
        style = draft.TextStyle(size=float(t.get("size", 8)), align=int(t.get("align", 1)),
                                max_line_width=float(t.get("max_line_width", 0.82)),
                                bold=bool(t.get("bold", False)),
                                color=_rgb(t.get("color")) or (1.0, 1.0, 1.0))
        kw = {"style": style, "clip_settings": clip}
        if t.get("font"):
            kw["font"] = t["font"]
        if t.get("flower"):
            kw["flower"] = t["flower"]
        if t.get("border"):
            b = t["border"] if isinstance(t["border"], dict) else {}
            kw["border"] = draft.TextBorder(color=_rgb(b.get("color", "#000000")), width=float(b.get("width", 40)),
                                            alpha=float(b.get("alpha", 1)))
        elif t.get("border") is False:  # 明确不要描边 (浅底卡片上的实心字; 不写时 add_text_simple 默认加黑描边)
            kw["border"] = None
        # 文字背景 (字幕条): {color, alpha, round_radius, style, height, width, ...}
        if t.get("background"):
            kw["background"] = text_background(t["background"])
        for k in ("anim_in", "anim_out", "anim_loop", "bubble"):
            if t.get(k):
                kw[k] = t[k]
        content = wrap_text(t["content"], int(t.get("wrap_chars", 0)))
        seg = self.p.add_text_simple(content, s_us, d_us, track_name=track, **kw)
        if not hasattr(self, "_track_end"):
            self._track_end = {}
        self._track_end[track] = round((s_us + d_us) / US + 0.001, 6)
        return seg

    def _overlay(self, o: dict):
        start, dur = self.tl.span(float(o["start"]), float(o["dur"]))
        if dur < 3:
            self.warn(f"插卡 {o.get('source')} 在成片里只有 {dur:.1f}s, 少于 3 秒观众看不清")
        x, y = (o.get("pos") or [0, 0.34])[:2]
        s0, s1 = (list(o.get("scale") or [0.8]) * 2)[:2]
        if o.get("fit") == "cover":  # 整屏切图: 铺满画布 (剪映 scale 1 = 适应画布, 按宽高比换算成铺满)
            from analyze_footage import probe

            info = probe(self.path(o["source"]))
            c = self.sb.get("project", {}).get("canvas") or {}
            cw, ch = float(c.get("width", 1080)), float(c.get("height", 1920))
            if info.get("width") and info.get("height"):
                ir, cr = info["width"] / info["height"], cw / ch
                cover = max(ir / cr, cr / ir)
                grow = s1 / s0 if s0 else 1.0
                s0, s1 = cover, cover * grow
                x, y = 0.0, 0.0
        kw = {}
        if o.get("source_in") is not None:  # 视频叠加层从源视频的这个时间开始播放 (如和主视频同步的讲解人小窗)
            kw["source_start"] = _us(o["source_in"])
        s_us, d_us = self._range(start, dur)
        seg = self.p.add_overlay(
            self.path(o["source"]), s_us, d_us, track_name=o.get("track", "Cards"),
            position=(float(x), float(y)), scale=float(s0), **kw,
        )  # fmt: skip
        if seg is None:
            self.warn(f"插卡导入失败: {o.get('source')}")
            return None
        if o.get("mute", True):
            seg.volume = 0.0
        d = seg.target_timerange.duration  # 对齐到整帧后的实际时长, 末尾关键帧不超出片段
        fade = int(min(0.45, dur / 4) * US)
        motion = o.get("motion", "float_in")
        if motion in ("float_in", "push") and s1 != s0:
            self.p.add_keyframes(seg, "scale", [(0, s0), (d, s1)], ease="缓动")
        if motion in ("float_in", "fade"):
            self.p.add_keyframes(seg, "alpha", [(0, 0.0), (fade, 1.0), (d - fade, 1.0), (d, 0.0)])
        if motion == "float_in":
            self.p.add_keyframes(seg, "y", [(0, float(y) - 0.05), (fade, float(y)), (d, float(y))], ease="缓出")
        if o.get("mask"):
            m = dict(o["mask"])
            self.p.add_mask_simple(seg, m.pop("name", "圆形"), **m)
            if o.get("mask_stroke"):
                st = dict(o["mask_stroke"])
                self.p.add_mask_stroke(seg, st.pop("style", "单层"), st.pop("color", "#FFFFFF"), **st)
        if o.get("shadow"):
            sh = o["shadow"] if isinstance(o["shadow"], dict) else {}
            self.p.add_video_shadow(seg, alpha=sh.get("alpha", 55), blur=sh.get("blur", 28),
                                    distance=sh.get("distance", 14), angle=sh.get("angle", 45))
        if o.get("label"):
            self._text({**o["label"], "start": o["label"].get("start", float(o["start"]) + 0.25),
                        "dur": o["label"].get("dur", float(o["dur"]) - 0.55)}, "CardLabel")
        return seg

    def _shot(self, shot: dict):
        t0, t1 = (shot.get("time") or [0, 0])[:2]
        out = self.tl.span(float(t0), float(t1) - float(t0))
        self.report["shots"].append({"id": shot.get("id"), "purpose": shot.get("purpose"),
                                     "out_start": round(out[0], 3), "out_dur": round(out[1], 3)})
        for o in shot.get("overlays") or []:
            self._overlay(o)
        for t in shot.get("texts") or []:
            self._text(t, t.get("track") or ("Title" if t.get("role") in ("title", "hero") else "Text"))
        for e in shot.get("effects") or []:
            s, d = self.tl.span(float(e["start"]), float(e["dur"]))
            if d < 3:
                self.warn(f"特效「{e['name']}」只有 {d:.1f}s, 氛围特效少于 3 秒观众看不出意图")
            s_us, d_us = self._range(s, d)
            track = e.get("track", "EffectTrack")
            if not self.p.add_effect_simple(e["name"], s_us, d_us, track_name=track):
                self.warn(f"特效「{e['name']}」没找到")
        for st in shot.get("stickers") or []:
            s, d = self.tl.span(float(st["start"]), float(st["dur"]))
            self.p.add_sticker(st["name"], *self._range(s, d), track_name=st.get("track", "Sticker"),
                               position=tuple(st.get("pos") or (0, 0)), scale=float(st.get("scale", 1)),
                               anim_in=st.get("anim_in"), anim_out=st.get("anim_out"), anim_loop=st.get("anim_loop"))
        for sfx in shot.get("sfx") or []:
            self._sfx(sfx)

    def _sfx(self, sfx: dict):
        path = sfx.get("path")
        path = self.path(path) if path else self.p.cloud_manager.download_asset(sfx["name"])
        if not path or not os.path.exists(path):
            self.warn(f"音效「{sfx.get('name') or sfx.get('path')}」下载失败")
            return
        at = self.tl.to_out(float(sfx["at"]))
        self.p.add_audio_safe(path, self._at(at), _us(sfx["dur"]) if sfx.get("dur") else None,
                              sfx.get("track", "SFX"), volume=float(sfx.get("volume", 0.5)))

    def _subtitles(self):
        sub = self.sb.get("subtitles")
        if not sub:
            return
        lines = sub.get("lines")
        if lines is None and sub.get("from_transcript"):
            path = self.path(sub["from_transcript"])
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            tr = data.get("transcript") if isinstance(data, dict) else data
            lines = split_transcript(tr or [], int(sub.get("max_chars", 15)), float(sub.get("max_dur", 3.2)))
        primary = {"track": "SubMain", "size": 9.5, "y": -0.44, "max_line_width": 0.70, "border": True,
                   **(sub.get("primary") or {})}
        secondary = {"track": "SubSecond", "size": 6.8, "y": -0.58, "max_line_width": 0.70, "border": True,
                     "wrap_chars": 26, **(sub.get("secondary") or {})}
        lead = float(sub.get("lead", 0.0))  # 字幕提前出现的秒数 (0.2~0.5 更跟手)
        n = 0
        for i, ln in enumerate(lines or []):
            s = max(0.0, float(ln["start"]) - lead)
            e = float(ln["end"])
            if i + 1 < len(lines):
                e = min(e, float(lines[i + 1]["start"]) - lead)  # 结尾不超过下一句开始
            for text, style in ((ln.get("text"), primary), (ln.get("text2"), secondary)):
                if text:
                    st = dict(style)
                    ch = float((self.sb.get("project", {}).get("canvas") or {}).get("height", 1920))
                    if st.get("box_width_px"):  # 字幕带: 按真实字宽折行, 超过 max_lines 行就缩字号
                        from utils.text_metrics import fit, LINE_HEIGHT, PX_PER_SIZE

                        text, size, w, _h = fit(text, st.get("font"), float(st.get("size", 9.5)), float(st["box_width_px"]),
                                                1e9, max_lines=int(st.get("max_lines", 2)), canvas_h=ch)
                        cw = float((self.sb.get("project", {}).get("canvas") or {}).get("width", 1080))
                        st.update(size=size, max_line_width=round(min(1.0, w / cw + 0.04), 3), wrap_chars=0,
                                  px_per_size=PX_PER_SIZE * LINE_HEIGHT / 1.35)
                    if st.get("anchor_bottom") is not None:  # 底边贴着安全区下沿, 按行数往上排
                        lines_n = wrap_text(text, int(st.get("wrap_chars", 0))).count("\n") + 1
                        line_h = float(st.get("size", 9.5)) * float(st.get("px_per_size", 6.0)) * 1.35 / (ch / 2)
                        st["y"] = round(float(st["anchor_bottom"]) + lines_n * line_h / 2, 4)
                    if self._text({**st, "content": text, "start": s, "dur": max(0.0, e - s)}, st["track"]):
                        n += 1
        self.report["subtitles"] = n

    def _audio(self):
        bgm = (self.sb.get("audio") or {}).get("bgm")
        if not bgm:
            return
        if self.music_seg is not None:  # 混剪已经按计划铺了音乐, 不再叠一条 BGM
            self.warn("混剪模式已有 montage.music, 忽略 audio.bgm")
            return
        path = bgm.get("path")
        path = self.path(path) if path else self.p.cloud_manager.download_asset(str(bgm.get("id") or bgm.get("query")))
        if not path or not os.path.exists(path):
            self.warn(f"BGM 下载失败 ({bgm.get('id') or bgm.get('query')}), 请在剪映里手动添加")
            return
        self.p.add_audio_safe(path, 0, self._main_end_us(), bgm.get("track", "BGM"),
                              volume=float(bgm.get("volume", 0.22)),
                              fade_in=_sec(bgm.get("fade_in", 1.2)), fade_out=_sec(bgm.get("fade_out", 2.5)))


def _guard_editor(name: str):
    """
    剪映编辑页开着时不重新生成: 关闭编辑器时剪映会把内存里的旧版本写回草稿, 刚生成的新版被覆盖
    (2026-10-23 V7 实测)。只在 macOS 上能检测; 检测超时则只提醒。
    """
    if sys.platform != "darwin" or os.environ.get("JY_SKIP_EDITOR_CHECK"):
        return
    try:
        import jy_preview

        if not jy_preview.jianying_running():
            return
        if jy_preview._center("currentProgress", timeout=20) is not None:
            raise SystemExit(f"❌ 剪映停在编辑页: 先回到首页 (关闭编辑器) 再生成「{name}」, 否则关闭时剪映会用旧版本覆盖它。"
                             f"确认编辑页打开的不是这个草稿时, 设置 JY_SKIP_EDITOR_CHECK=1 跳过")
    except SystemExit:
        raise
    except Exception as e:  # 辅助功能权限 / 超时: 不阻塞生成, 只提醒
        print(f"ℹ️ 没能确认剪映是否停在编辑页 ({e}); 如果编辑器开着这个草稿, 先关掉再生成")


def build(storyboard_path: str, review: bool = True, preview: bool = False) -> dict:
    with open(storyboard_path, "r", encoding="utf-8") as f:
        sb = json.load(f)
    _guard_editor(sb["project"]["name"])
    b = StoryboardBuilder(sb, os.path.dirname(os.path.abspath(storyboard_path)))
    res = b.build()
    name = sb["project"]["name"]
    report_path = os.path.splitext(storyboard_path)[0] + ".build.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(res["report"], f, ensure_ascii=False, indent=1)
    print(f"✅ 草稿「{name}」已生成, 成片 {res['report']['total']:.2f}s; 装配报告 {report_path}")
    if review:
        code = subprocess.call([sys.executable, os.path.join(HERE, "layout_preview.py"), "--draft", name, "--auto"])
        res["review_exit"] = code
        print("🔎 审片器" + ("通过" if code == 0 else "发现问题, 请看报告和对比图"))
    if preview:
        from jy_preview import preview as jy_capture

        times = [s["out_start"] + min(1.5, s["out_dur"] / 2) for s in res["report"]["shots"] if s["out_dur"] > 0]
        res["preview"] = jy_capture(name, times)
        for pth in res["preview"]:
            print(f"🖼️ {pth}")
    return res


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="按分镜表生成剪映草稿")
    ap.add_argument("storyboard")
    ap.add_argument("--no-review", action="store_true", help="不跑审片器")
    ap.add_argument("--preview", action="store_true", help="在剪映里截取每个镜头的画面 (macOS)")
    args = ap.parse_args(argv)
    try:
        res = build(args.storyboard, review=not args.no_review, preview=args.preview)
    except StoryboardError as e:
        print(f"❌ 分镜表有误: {e}", file=sys.stderr)
        return 2
    return 0 if res.get("review_exit", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(_main())
