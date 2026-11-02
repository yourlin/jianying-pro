#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""草稿修改学习 (Edit Diff): 对比 skill 生成的草稿和用户在剪映里改过的版本, 归纳用户的修正规律。

输入 (都是明文草稿 JSON, 只读):
  - 修改前: skill 保存时写的 draft_info.skill.json, 或 .backup 里剪映保存的明文历史版本 (*.load.bak)
  - 修改后: 剪映在关闭草稿 / 回到首页时写的明文 template.json (根目录或 Timelines/<id>/)

对齐方式: 按片段做语义对齐 (素材路径 / 文字内容 / 特效名 + 轨道类型 + 时间重叠), 不按数组下标、不依赖 id。
过滤的噪声 (剪映打开草稿时自动做的, 不算用户修改):
  - id 被改写、补齐默认字段 (canvases / vocal_separations / loudnesses 等默认附属素材)
  - 时间对齐到整帧 (误差 ≤ 1 帧, 主轨吸附后会逐段累积)
  - 重叠转场时长取偶数帧、超过相邻片段一半时被截短
  - 素材路径改成相对路径 / 占位符、素材时长按帧取整

报告的修改: 切点 (入点 / 出点)、片段增删 / 换序 / 换轨、时长、文字 (内容 / 字号 / 位置 / 字体 / 颜色 / 样式)、
转场 / 特效 / 滤镜 / 动画的增删和更换、音量 / 淡入淡出、关键帧、画布。

用法:
  python edit_diff.py diff 修改前.json 修改后.json [--json]
  python edit_diff.py draft "草稿名" [--json]                      # skill 副本 + 明文备份 → template.json
  python edit_diff.py learn "草稿A" "草稿B" ... --out-dir ~/.config/jianying-pro/learned
退出码: 0 = 完成, 2 = 输入错误。对剪映草稿目录只读, learn 只写 --out-dir。
"""

import argparse
import difflib
import glob
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.cli_protocol import emit_result, make_result  # noqa: E402

US = 1_000_000
DRAFT_ROOTS = [
    os.path.expanduser("~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"),
    os.path.expanduser(
        "~/Library/Containers/com.lemon.lvpro/Data/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"
    ),
]
DEFAULT_OUT_DIR = os.path.expanduser("~/.config/jianying-pro/learned")
SKILL_CONTENT = "draft_info.skill.json"

# extra_material_refs 里剪映自动补齐的默认附属素材: 只有偏离默认值时才算修改
ATTACH_KINDS = {
    "transitions": "transition",
    "material_animations": "animation",
    "filters": "filter",
    "effects": "effect",
    "video_effects": "effect",
    "audio_fades": "audio_fade",
    "speeds": "speed",
    "canvases": "canvas",
    "masks": "mask",
    "loudnesses": "loudness",
    "audio_effects": "audio_effect",
}
CATEGORY_ZH = {
    "cut": "切点",
    "structure": "片段增删 / 换序",
    "duration": "时长",
    "text": "文字",
    "fx": "转场 / 特效 / 滤镜 / 动画",
    "audio": "音量 / 音频",
    "keyframe": "关键帧",
    "transform": "画面位置 / 缩放",
    "canvas": "画布 / 画幅",
}


class InputError(Exception):
    """草稿 JSON 读不了, CLI 退出码 2。"""


# ---------------- 读取 ----------------
def load_json(path: str) -> dict:
    """读取明文草稿 JSON; 已加密或没有 tracks 时抛 InputError。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise InputError(f"不是可读的明文草稿 JSON: {path} ({type(e).__name__})") from e
    if not isinstance(data, dict) or data.get("tracks") is None:
        raise InputError(f"JSON 里没有 tracks, 不是草稿内容: {path}")
    return data


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _basename(p: Optional[str]) -> str:
    """素材路径只比文件名: 剪映会把绝对路径改成相对路径或 ##_draftpath_placeholder_..##。"""
    return os.path.basename(str(p or "").replace("\\", "/"))


def _hex(color) -> Optional[str]:
    if isinstance(color, str):
        return color.upper() or None
    if isinstance(color, (list, tuple)) and len(color) >= 3:
        return "#" + "".join(f"{int(round(_num(c) * 255)):02X}" for c in color[:3])
    return None


def _font_key(font: dict, m: dict) -> str:
    """字体只比文件名 / id; 剪映给没设字体的文字补上的系统默认字体 (SystemFont/*) 视为默认 ("")。"""
    path = str(font.get("path") or m.get("font_path") or "")
    if "SystemFont" in path.replace("\\", "/").split("/"):
        return ""
    return _basename(path) or str(font.get("id") or "")


def _text_info(m: dict) -> dict:
    try:
        content = json.loads(m.get("content") or "{}")
    except (TypeError, ValueError):
        content = {}
    st = (content.get("styles") or [{}])[0] if isinstance(content, dict) else {}
    font = st.get("font") or {}
    fill = ((st.get("fill") or {}).get("content") or {}).get("solid") or {}
    strokes = [s for s in st.get("strokes") or [] if _num(s.get("width")) > 0.001]
    stroke = None
    if strokes:
        sc = ((strokes[0].get("content") or {}).get("solid") or {}).get("color")
        stroke = {"color": _hex(sc), "width": round(_num(strokes[0].get("width")), 3)}
    return {
        "text": content.get("text", "") if isinstance(content, dict) else "",
        "size": round(_num(st.get("size"), m.get("font_size") or 0), 2),
        "font": _font_key(font, m),
        "color": _hex(fill.get("color")) or _hex(m.get("text_color")),
        "bold": bool(st.get("bold", False)),
        "italic": bool(st.get("italic", False)),
        "underline": bool(st.get("underline", False)),
        "stroke": stroke,
        "alignment": m.get("alignment", 1),
        "line_spacing": round(_num(m.get("line_spacing"), 0.02), 3),
        "letter_spacing": round(_num(m.get("letter_spacing")), 3),
    }


def _attachment(kind: str, m: dict) -> Optional[dict]:
    """附属素材的语义摘要; 默认值 (剪映自动补的) 返回 None。"""
    k = ATTACH_KINDS.get(kind)
    if k == "transition":
        return {"name": m.get("name"), "effect_id": m.get("effect_id"), "duration": int(_num(m.get("duration"))),
                "is_overlap": bool(m.get("is_overlap", True))}  # fmt: skip
    if k == "animation":
        return {"items": [{"name": a.get("name"), "type": a.get("type"), "id": a.get("id"),
                           "duration": int(_num(a.get("duration")))} for a in m.get("animations") or []]}  # fmt: skip
    if k in ("filter", "effect", "audio_effect"):
        return {"name": m.get("name"), "effect_id": m.get("effect_id") or m.get("resource_id"),
                "value": round(_num(m.get("value"), 1.0), 3)}  # fmt: skip
    if k == "audio_fade":
        return {
            "in": int(_num(m.get("fade_in_duration"))),
            "out": int(_num(m.get("fade_out_duration"))),
        }
    if k == "canvas":
        t, color = m.get("type") or "canvas_color", (m.get("color") or "").upper()
        if t == "canvas_color" and color in ("", "#000000", "#00000000", "#FF000000"):
            return None
        return {
            "type": t,
            "color": color,
            "blur": round(_num(m.get("blur")), 3),
            "image": _basename(m.get("image")),
        }
    if k == "mask":
        return {"name": m.get("name"), "type": m.get("resource_type") or m.get("type"),
                "config": {kk: round(_num(v), 3) for kk, v in (m.get("config") or {}).items()
                           if isinstance(v, (int, float))}}  # fmt: skip
    if k == "loudness":
        return {"target": m.get("target_loudness")} if m.get("enable") else None
    return None  # speed 用片段上的 speed 字段


def _keyframes(seg: dict) -> Dict[str, List[Tuple[int, float]]]:
    out = {}
    for kf in seg.get("common_keyframes") or []:
        pts = []
        for k in kf.get("keyframe_list") or []:
            vals = k.get("values") or [None]
            if vals[0] is not None:
                pts.append((int(_num(k.get("time_offset"))), round(_num(vals[0]), 4)))
        if pts:
            out[str(kf.get("property_type"))] = sorted(pts)
    return out


def normalize(content: dict) -> dict:
    """把草稿 JSON 归一成和 id / 默认字段无关的结构: 画布 + 按轨道类型分组的片段语义特征。"""
    mats: Dict[str, Tuple[str, dict]] = {}
    for kind, lst in (content.get("materials") or {}).items():
        if isinstance(lst, list):
            for m in lst:
                if isinstance(m, dict) and m.get("id"):
                    mats[m["id"]] = (kind, m)
    canvas = content.get("canvas_config") or {}
    fps = _num(content.get("fps"), 30) or 30.0
    segs = []
    type_ord: Counter = Counter()
    for ti, track in enumerate(content.get("tracks") or []):
        ttype = track.get("type") or "video"
        ordinal = type_ord[ttype]
        type_ord[ttype] += 1
        for si, s in enumerate(track.get("segments") or []):
            tt = s.get("target_timerange") or {}
            st = s.get("source_timerange") or None
            kind, m = mats.get(s.get("material_id"), ("", {}))
            clip = s.get("clip") or {}
            feat: Dict[str, Any] = {
                "id": s.get("id"),
                "track_type": ttype,
                "track_ord": ordinal,
                "track_name": track.get("name") or "",
                "index": si,
                "start": int(_num(tt.get("start"))),
                "duration": int(_num(tt.get("duration"))),
                "src_start": int(_num(st.get("start"))) if st else None,
                "src_duration": int(_num(st.get("duration"))) if st else None,
                "speed": round(_num(s.get("speed"), 1.0), 4),
                "volume": round(_num(s.get("volume"), 1.0), 4),
                "visible": bool(s.get("visible", True)),
                "kind": kind,
                "material": _basename(m.get("path"))
                or m.get("name")
                or m.get("material_name")
                or "",
                "material_name": m.get("name") or m.get("material_name") or "",
                "clip": {
                    "alpha": round(_num(clip.get("alpha"), 1.0), 3),
                    "rotation": round(_num(clip.get("rotation")), 2),
                    "scale_x": round(_num((clip.get("scale") or {}).get("x"), 1.0), 3),
                    "scale_y": round(_num((clip.get("scale") or {}).get("y"), 1.0), 3),
                    "x": round(_num((clip.get("transform") or {}).get("x")), 3),
                    "y": round(_num((clip.get("transform") or {}).get("y")), 3),
                    "flip_h": bool((clip.get("flip") or {}).get("horizontal", False)),
                    "flip_v": bool((clip.get("flip") or {}).get("vertical", False)),
                },
                "keyframes": _keyframes(s),
                "attach": {},
            }
            if kind == "texts" or ttype == "text":
                feat["text"] = _text_info(m)
                feat["material"] = feat["text"]["text"]
            elif kind in ATTACH_KINDS:  # 特效 / 滤镜轨道上的片段, 本体就是特效
                info = _attachment(kind, m) or {}
                feat["material"] = info.get("name") or feat["material"]
                feat["fx"] = info
            for ref in s.get("extra_material_refs") or []:
                rk, rm = mats.get(ref, ("", {}))
                info = _attachment(rk, rm) if rk else None
                if info is not None:
                    feat["attach"][ATTACH_KINDS[rk]] = info
            segs.append(feat)
    return {
        "canvas": {"width": int(_num(canvas.get("width"))), "height": int(_num(canvas.get("height"))),
                   "ratio": canvas.get("ratio") or "original"},  # fmt: skip
        "fps": fps,
        "duration": int(_num(content.get("duration"))),
        "segments": segs,
    }


# ---------------- 对齐 ----------------
def _iou(a0, a1, b0, b1) -> float:
    inter = max(0, min(a1, b1) - max(a0, b0))
    union = max(a1, b1) - min(a0, b0)
    return inter / union if union > 0 else (1.0 if a0 == b0 else 0.0)


def _match_score(a: dict, b: dict) -> float:
    """两个片段是同一个片段的可能性。素材相同 + 时间重叠 + 同一条轨道; 不同类型轨道不配对。"""
    if a["track_type"] != b["track_type"]:
        return 0.0
    t_iou = _iou(a["start"], a["start"] + a["duration"], b["start"], b["start"] + b["duration"])
    if a["material"] and a["material"] == b["material"]:
        mat = 1.0
    elif a.get("text") is not None and b.get("text") is not None:
        mat = difflib.SequenceMatcher(None, a["text"]["text"], b["text"]["text"]).ratio()
    else:
        mat = 0.0
    if mat < 0.5 and t_iou < 0.3:
        return 0.0
    score = 3.0 * mat + 2.0 * t_iou
    if a["src_start"] is not None and b["src_start"] is not None and mat >= 1.0:
        score += _iou(a["src_start"], a["src_start"] + a["src_duration"],
                      b["src_start"], b["src_start"] + b["src_duration"])  # fmt: skip
    if a["track_ord"] == b["track_ord"]:
        score += 0.5
    if a.get("id") and a["id"] == b.get("id"):
        score += 0.5  # id 没被改写时是额外证据, 但不是必要条件
    return score


def align(
    sa: List[dict], sb: List[dict], min_score: float = 2.0
) -> Tuple[List[Tuple[int, int]], List[int], List[int]]:
    """贪心最大权匹配, 返回 (配对 [(i, j)], 只在修改前的 i, 只在修改后的 j)。"""
    cands = []
    for i, a in enumerate(sa):
        for j, b in enumerate(sb):
            s = _match_score(a, b)
            if s >= min_score:
                cands.append((s, -abs(a["start"] - b["start"]), i, j))
    cands.sort(reverse=True)
    used_a, used_b, pairs = set(), set(), []
    for _s, _d, i, j in cands:
        if i not in used_a and j not in used_b:
            used_a.add(i)
            used_b.add(j)
            pairs.append((i, j))
    pairs.sort()
    return (
        pairs,
        [i for i in range(len(sa)) if i not in used_a],
        [j for j in range(len(sb)) if j not in used_b],
    )


def _lis_keep(seq: List[int]) -> set:
    """最长递增子序列的下标集合; 不在里面的就是被换序的片段。"""
    if not seq:
        return set()
    tails, prev = [], [-1] * len(seq)
    for k, v in enumerate(seq):
        lo, hi = 0, len(tails)
        while lo < hi:
            mid = (lo + hi) // 2
            if seq[tails[mid]] < v:
                lo = mid + 1
            else:
                hi = mid
        if lo > 0:
            prev[k] = tails[lo - 1]
        if lo == len(tails):
            tails.append(k)
        else:
            tails[lo] = k
    k, keep = tails[-1], set()
    while k != -1:
        keep.add(k)
        k = prev[k]
    return keep


# ---------------- 对比 ----------------
def _label(s: dict) -> str:
    name = s["material"] if s.get("text") is None else "「" + s["text"]["text"][:16] + "」"
    return (
        f"{s['track_type']}#{s['track_ord']} {name or s['material_name']} @{s['start'] / US:.2f}s"
    )


def _ref(s: dict) -> dict:
    return {"track_type": s["track_type"], "track_ord": s["track_ord"], "track_name": s["track_name"],
            "start_s": round(s["start"] / US, 3), "duration_s": round(s["duration"] / US, 3),
            "material": s["material"] if s.get("text") is None else s["text"]["text"], "label": _label(s)}  # fmt: skip


def _expected_transition(
    old_us: int, seg_dur: int, next_dur: Optional[int], frame_us: float
) -> int:
    """剪映对重叠转场的规整 (实测推测): 时长不超过相邻两段较短者的一半, 再取偶数帧 (两侧各一半, 向下取整)。"""
    limit = min(seg_dur, next_dur) / 2 if next_dur else seg_dur / 2
    half_frames = int(min(old_us, limit) / 2 / frame_us + 1e-6)
    return int(round(2 * half_frames * frame_us))


class _Differ:
    def __init__(self, na: dict, nb: dict, tol_ms: Optional[float] = None):
        self.na, self.nb = na, nb
        self.frame_us = US / (nb.get("fps") or na.get("fps") or 30)
        self.tol = max((tol_ms or 0) * 1000, self.frame_us + 1000)  # 默认 1 帧 + 1ms
        self.changes: List[dict] = []
        self.noise: Counter = Counter()
        self.max_drift = 0
        self.auto_transitions: List[dict] = []
        self.snap_examples: List[str] = []

    def add(self, category, kind, seg, field, before, after, delta_ms=None, **extra):
        c = {"category": category, "kind": kind, "segment": _ref(seg) if seg else None, "field": field,
             "before": before, "after": after}  # fmt: skip
        if delta_ms is not None:
            c["delta_ms"] = round(delta_ms, 1)
        c.update(extra)
        self.changes.append(c)

    def time_changed(self, va, vb, what: str = "", tol: Optional[float] = None) -> bool:
        if va is None or vb is None:
            return va != vb
        d = abs(vb - va)
        if d == 0:
            return False
        if d <= max(self.tol, tol or 0):
            self.noise["frame_snap"] += 1
            self.max_drift = max(self.max_drift, d)
            if what and len(self.snap_examples) < 6:
                self.snap_examples.append(f"{what} {va} → {vb}us")
            return False
        return True

    def run(self) -> dict:
        na, nb = self.na, self.nb
        if (
            na["canvas"]["width"] != nb["canvas"]["width"]
            or na["canvas"]["height"] != nb["canvas"]["height"]
        ):
            self.add("canvas", "resize", None, "canvas", f"{na['canvas']['width']}x{na['canvas']['height']}",
                     f"{nb['canvas']['width']}x{nb['canvas']['height']}")  # fmt: skip
        if na["canvas"]["ratio"] != nb["canvas"]["ratio"]:
            self.add(
                "canvas",
                "ratio",
                None,
                "canvas.ratio",
                na["canvas"]["ratio"],
                nb["canvas"]["ratio"],
            )
        if na["fps"] != nb["fps"]:
            self.add("canvas", "fps", None, "fps", na["fps"], nb["fps"])
        sa, sb = na["segments"], nb["segments"]
        pairs, only_a, only_b = align(sa, sb)
        for i in only_a:
            self.add("structure", "deleted", sa[i], "segment", _label(sa[i]), None)
        for j in only_b:
            self.add("structure", "added", sb[j], "segment", None, _label(sb[j]))
        # 换序: 每种轨道类型内, 按修改前的顺序看修改后的顺序
        by_type = defaultdict(list)
        for i, j in pairs:
            by_type[sa[i]["track_type"]].append((i, j))
        for _t, lst in by_type.items():
            lst.sort(key=lambda p: (sa[p[0]]["track_ord"], sa[p[0]]["start"]))
            order = [(sb[j]["track_ord"], sb[j]["start"]) for _i, j in lst]
            ranks = {v: r for r, v in enumerate(sorted(set(order)))}
            keep = _lis_keep([ranks[v] for v in order])
            for k, (i, j) in enumerate(lst):
                if k not in keep and sa[i]["track_ord"] == sb[j]["track_ord"]:
                    self.add(
                        "structure", "reordered", sa[i], "order", sa[i]["index"], sb[j]["index"]
                    )
        b_next = self._next_durations(sb)
        prev_end: Dict[Tuple[str, int], Tuple[int, int]] = {}
        for i, j in sorted(
            pairs, key=lambda p: (sa[p[0]]["track_type"], sa[p[0]]["track_ord"], sa[p[0]]["start"])
        ):
            a, b = sa[i], sb[j]
            if a.get("id") and b.get("id") and a["id"] != b["id"]:
                self.noise["id_rewritten"] += 1
            key = (a["track_type"], a["track_ord"])
            self._diff_timing(a, b, prev_end.get(key))
            prev_end[key] = (a["start"] + a["duration"], b["start"] + b["duration"])
            self._diff_props(a, b, b_next.get(j))
        cats = Counter(c["category"] for c in self.changes)
        return {
            "modified": bool(self.changes),
            "fps": nb["fps"],
            "tolerance_ms": round(self.tol / 1000, 1),
            "alignment": {"base_segments": len(sa), "edited_segments": len(sb), "matched": len(pairs),
                          "deleted": len(only_a), "added": len(only_b)},  # fmt: skip
            "changes": self.changes,
            "counts": dict(cats),
            "noise": {**dict(self.noise), "max_frame_drift_ms": round(self.max_drift / 1000, 1),
                      "auto_transitions": self.auto_transitions, "snap_examples": self.snap_examples},  # fmt: skip
        }

    @staticmethod
    def _next_durations(segs: List[dict]) -> Dict[int, Optional[int]]:
        by = defaultdict(list)
        for j, s in enumerate(segs):
            by[(s["track_type"], s["track_ord"])].append(j)
        out = {}
        for lst in by.values():
            lst.sort(key=lambda j: segs[j]["start"])
            for k, j in enumerate(lst):
                out[j] = segs[lst[k + 1]]["duration"] if k + 1 < len(lst) else None
        return out

    def _diff_timing(self, a, b, prev):
        if a["track_type"] != b["track_type"] or a["track_ord"] != b["track_ord"]:
            self.add("structure", "track_moved", a, "track", f"{a['track_type']}#{a['track_ord']}",
                     f"{b['track_type']}#{b['track_ord']}")  # fmt: skip
        has_src = a["src_start"] is not None and b["src_start"] is not None
        if has_src and self.time_changed(a["src_start"], b["src_start"]):
            d = b["src_start"] - a["src_start"]
            self.add("cut", "trim_in", a, "source_in", a["src_start"], b["src_start"], d / 1000)
        if has_src:
            ea, eb = a["src_start"] + a["src_duration"], b["src_start"] + b["src_duration"]
            # 出点 = 起点 + 时长, 两者各自按帧取整, 误差最多 2 帧
            if self.time_changed(ea, eb, tol=2 * self.frame_us + 1000):
                self.add("cut", "trim_out", a, "source_out", ea, eb, (eb - ea) / 1000)
        if self.time_changed(a["duration"], b["duration"], _label(a) + " 时长"):
            self.add("duration", "duration", a, "duration", a["duration"], b["duration"],
                     (b["duration"] - a["duration"]) / 1000)  # fmt: skip
        # 位置移动只看和前一个片段的间隔有没有变 (前面片段变长变短引起的整体顺移不重复报)
        gap_a = a["start"] - (prev[0] if prev else 0)
        gap_b = b["start"] - (prev[1] if prev else 0)
        if self.time_changed(gap_a, gap_b):
            kind = "text_timing" if a.get("text") is not None else "moved"
            self.add(
                "cut", kind, a, "start", a["start"], b["start"], (b["start"] - a["start"]) / 1000
            )
        elif a["start"] != b["start"]:
            self.time_changed(a["start"], b["start"], _label(a) + " 起点")

    def _diff_props(self, a, b, b_next_dur):
        if a["speed"] != b["speed"]:
            self.add("duration", "speed", a, "speed", a["speed"], b["speed"])
        if abs(a["volume"] - b["volume"]) > 1e-3:
            self.add("audio", "volume", a, "volume", a["volume"], b["volume"])
        if a["visible"] != b["visible"]:
            self.add("structure", "visibility", a, "visible", a["visible"], b["visible"])
        if a["material"] != b["material"] and a.get("text") is None:
            self.add("structure", "replaced", a, "material", a["material"], b["material"])
        for k, va in a["clip"].items():
            vb = b["clip"][k]
            if (isinstance(va, bool) and va != vb) or (
                not isinstance(va, bool) and abs(va - vb) > 2e-3
            ):
                cat = (
                    "text"
                    if a.get("text") is not None and k in ("x", "y", "scale_x", "scale_y")
                    else "transform"
                )
                self.add(cat, "position" if k in ("x", "y") else k, a, f"clip.{k}", va, vb)
        if a.get("text") is not None and b.get("text") is not None:
            for k, va in a["text"].items():
                vb = b["text"][k]
                if va == vb or (
                    isinstance(va, float) and isinstance(vb, float) and abs(va - vb) < 1e-3
                ):
                    continue
                kind = {"text": "content", "size": "size", "font": "font", "color": "color"}.get(
                    k, "style"
                )
                self.add("text", kind, a, f"text.{k}", va, vb)
        if a.get("fx") != b.get("fx") and (a.get("fx") or b.get("fx")):
            fa, fb = a.get("fx") or {}, b.get("fx") or {}
            if fa.get("name") != fb.get("name") or fa.get("effect_id") != fb.get("effect_id"):
                self.add("fx", "effect_replaced", a, "effect", fa.get("name"), fb.get("name"))
            elif abs(_num(fa.get("value")) - _num(fb.get("value"))) > 1e-3:
                self.add("fx", "effect_value", a, "effect.value", fa.get("value"), fb.get("value"))
        self._diff_attach(a, b, b_next_dur)
        self._diff_keyframes(a, b)

    def _diff_attach(self, a, b, b_next_dur):
        aa, ab = a["attach"], b["attach"]
        for k in sorted(set(aa) | set(ab)):
            va, vb = aa.get(k), ab.get(k)
            cat = "audio" if k in ("audio_fade", "loudness", "audio_effect") else (
                "canvas" if k == "canvas" else "fx")  # fmt: skip
            if va is None:
                self.add(cat, f"{k}_added", a, k, None, vb)
                continue
            if vb is None:
                self.add(cat, f"{k}_removed", a, k, va, None)
                continue
            if va == vb:
                continue
            if k == "transition":
                self._diff_transition(a, b, va, vb, b_next_dur)
            elif k == "animation":
                self._diff_animation(a, va, vb)
            elif k == "audio_fade":
                for side in ("in", "out"):
                    if self.time_changed(va[side], vb[side]):
                        self.add("audio", f"fade_{side}", a, f"audio_fade.{side}", va[side], vb[side],
                                 (vb[side] - va[side]) / 1000)  # fmt: skip
            elif va.get("name") != vb.get("name") or va.get("effect_id") != vb.get("effect_id"):
                self.add(cat, f"{k}_replaced", a, k, va.get("name") or va, vb.get("name") or vb)
            else:
                self.add(cat, f"{k}_changed", a, k, va, vb)

    def _diff_transition(self, a, b, va, vb, b_next_dur):
        if va["name"] != vb["name"] or va["effect_id"] != vb["effect_id"]:
            self.add("fx", "transition_replaced", a, "transition", va["name"], vb["name"])
        da, db = va["duration"], vb["duration"]
        if da == db:
            return
        exp = _expected_transition(da, b["duration"], b_next_dur, self.frame_us)
        if db <= da and abs(db - exp) <= self.frame_us / 2 + 1000:
            self.noise["transition_auto"] += 1
            if abs(db - da) > self.tol:
                self.auto_transitions.append({"segment": _label(a), "name": va["name"], "before_ms": da / 1000,
                                              "after_ms": round(db / 1000, 1)})  # fmt: skip
            return
        if self.time_changed(da, db):
            self.add(
                "fx", "transition_duration", a, "transition.duration", da, db, (db - da) / 1000
            )

    def _diff_animation(self, a, va, vb):
        ia = {x["type"]: x for x in va["items"]}
        ib = {x["type"]: x for x in vb["items"]}
        for t in sorted(set(ia) | set(ib)):
            xa, xb = ia.get(t), ib.get(t)
            if xa is None:
                self.add("fx", "animation_added", a, f"animation.{t}", None, xb["name"])
            elif xb is None:
                self.add("fx", "animation_removed", a, f"animation.{t}", xa["name"], None)
            elif xa["name"] != xb["name"] or xa["id"] != xb["id"]:
                self.add("fx", "animation_replaced", a, f"animation.{t}", xa["name"], xb["name"])
            elif self.time_changed(xa["duration"], xb["duration"]):
                self.add("fx", "animation_duration", a, f"animation.{t}.duration", xa["duration"],
                         xb["duration"], (xb["duration"] - xa["duration"]) / 1000)  # fmt: skip

    def _diff_keyframes(self, a, b):
        ka, kb = a["keyframes"], b["keyframes"]
        for p in sorted(set(ka) | set(kb)):
            pa, pb = ka.get(p), kb.get(p)
            if pa is None:
                self.add("keyframe", "keyframe_added", a, p, None, pb)
            elif pb is None:
                self.add("keyframe", "keyframe_removed", a, p, pa, None)
            elif len(pa) != len(pb) or any(
                abs(x[0] - y[0]) > self.tol or abs(x[1] - y[1]) > 2e-3 for x, y in zip(pa, pb)
            ):
                self.add("keyframe", "keyframe_changed", a, p, pa, pb)


def _raw_noise(ra: dict, rb: dict) -> dict:
    """原始 JSON 层面的噪声统计: 自动补齐的默认附属素材、路径改写。"""

    def mats(r):
        out = {}
        for kind, lst in (r.get("materials") or {}).items():
            if isinstance(lst, list):
                for m in lst:
                    if isinstance(m, dict) and m.get("id"):
                        out[m["id"]] = (kind, m)
        return out

    ma, mb = mats(ra), mats(rb)
    auto = Counter(k for i, (k, _m) in mb.items() if i not in ma)
    a_paths = {m.get("path") for _k, m in ma.values() if m.get("path")}
    a_names = {_basename(p) for p in a_paths}
    paths = sum(
        1
        for _k, m in mb.values()
        if m.get("path") and m["path"] not in a_paths and _basename(m["path"]) in a_names
    )
    return {"auto_materials_added": dict(auto), "path_rewritten": paths}


def diff_drafts(base: dict, edited: dict, tol_ms: Optional[float] = None) -> dict:
    """对比两份草稿 JSON (dict), 返回结构化差异。base = 修改前 (skill 版), edited = 修改后 (剪映版)。"""
    res = _Differ(normalize(base), normalize(edited), tol_ms).run()
    res["noise"].update(_raw_noise(base, edited))
    return res


def diff_files(base_path: str, edited_path: str, tol_ms: Optional[float] = None) -> dict:
    res = diff_drafts(load_json(base_path), load_json(edited_path), tol_ms)
    res["base"], res["edited"] = base_path, edited_path
    return res


# ---------------- 草稿目录: 找修改前后的版本 (只读) ----------------
def find_draft(name_or_path: str) -> str:
    if os.path.isdir(name_or_path):
        return os.path.abspath(name_or_path)
    for root in DRAFT_ROOTS:
        cand = os.path.join(root, name_or_path)
        if os.path.isdir(cand):
            return cand
    raise InputError(f"找不到草稿: {name_or_path}")


def _is_plain_draft(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            if not f.read(64).lstrip().startswith(b"{"):
                return False
        load_json(path)
        return True
    except (OSError, InputError):
        return False


def _file_digest(path: str) -> str:
    import hashlib

    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _bak_time(path: str) -> float:
    m = re.match(r"(\d{14})_", os.path.basename(path))
    if m:
        return time.mktime(time.strptime(m.group(1), "%Y%m%d%H%M%S"))
    return os.path.getmtime(path)


def draft_versions(draft_dir: str) -> dict:
    """列出草稿的明文版本: skill 副本、.backup 里的明文历史版本、剪映最新的 template.json; 加密备份单列。"""
    from utils.draft_store import app_plain_copy

    versions, encrypted, seen = [], [], set()
    skill = os.path.join(draft_dir, SKILL_CONTENT)
    if os.path.exists(skill) and _is_plain_draft(skill):
        versions.append(
            {"label": "skill 副本", "role": "skill", "path": skill, "time": os.path.getmtime(skill)}
        )
    for p in sorted(glob.glob(os.path.join(draft_dir, ".backup", "*.bak"))
                    + glob.glob(os.path.join(draft_dir, ".backup", "*", "*.bak"))):  # fmt: skip
        if os.sep + "projectBackUp" + os.sep in p:
            continue  # 工程级元信息, 不是时间线内容
        rel = os.path.relpath(p, draft_dir)
        kind = os.path.basename(p).split(".")[-2] if p.count(".") >= 2 else "bak"
        digest = _file_digest(p)
        if digest in seen:
            continue  # 根目录和 Timelines/<id>/ 下常是同一份
        seen.add(digest)
        if _is_plain_draft(p):
            versions.append({"label": f"备份 {kind} {os.path.basename(p)[:14]}",
                             "role": "backup", "path": p, "rel": rel, "time": _bak_time(p)})  # fmt: skip
        else:
            encrypted.append(rel)
    final = app_plain_copy(draft_dir)
    if final:
        versions.append(
            {
                "label": "剪映 template.json",
                "role": "final",
                "path": final,
                "time": os.path.getmtime(final),
            }
        )
    return {"versions": versions, "encrypted_backups": encrypted}


def diff_draft(name_or_path: str, tol_ms: Optional[float] = None) -> dict:
    """一个草稿: skill 副本 → template.json 为主对比; 明文备份也和最终版、skill 副本分别比一次。"""
    d = find_draft(name_or_path)
    info = draft_versions(d)
    vs = info["versions"]
    skill = next((v for v in vs if v["role"] == "skill"), None)
    final = next((v for v in vs if v["role"] == "final"), None)
    base = skill or next((v for v in vs if v["role"] == "backup"), None)
    out = {"draft": os.path.basename(d), "path": d, "encrypted_backups": info["encrypted_backups"],
           "versions": [{k: v[k] for k in ("label", "role", "path")} | {"time": time.strftime(
               "%Y-%m-%d %H:%M:%S", time.localtime(v["time"]))} for v in vs], "comparisons": []}  # fmt: skip
    if not base or not final:
        out["error"] = "缺少修改前 (skill 副本 / 明文备份) 或修改后 (template.json) 的明文版本"
        out["modified"] = None
        return out
    pairs = [(base, final, "main")]
    for v in vs:
        if v["role"] == "backup":
            pairs.append((v, final, "backup→final"))
            if skill:
                pairs.append((skill, v, "skill→backup"))
    for va, vb, kind in pairs:
        r = diff_files(va["path"], vb["path"], tol_ms)
        r.update({"kind": kind, "base_label": va["label"], "edited_label": vb["label"]})
        out["comparisons"].append(r)
    out["modified"] = any(c["modified"] for c in out["comparisons"] if c["kind"] != "skill→backup")
    return out


# ---------------- 归纳: 用户修正规律 ----------------
# 每类修改对应 skill 里的代码 / 规范字段 (改进建议的落点)
TARGETS = {
    "trim_in": "scripts/script_compiler.py (按节奏剪气口 keep_breath) / references/scenes/*.json pacing.keep_breath / storyboard main.keep",
    "trim_out": "scripts/script_compiler.py (片尾留 0.35s, keep_breath) / references/scenes/*.json pacing.compact|natural",
    "moved": "scripts/beat_snap.py (snap tolerance) / scripts/storyboard_builder.py Builder._main",
    "duration": "references/scenes/*.json pacing.avg_shot / elements.*.min_dur|max_dur",
    "text_timing": "references/scenes/*.json subtitles.lead|max_dur / scripts/script_compiler.py 字幕排时",
    "deleted": "scripts/script_compiler.py (镜头 / 插卡生成) / references/scenes/*.json elements",
    "added": "scripts/script_compiler.py / references/scenes/*.json inserts|elements",
    "reordered": "scripts/storyboard_builder.py Builder._main (main.keep 顺序)",
    "size": "references/styles/*.json roles.<role>.size / references/formats/*.json subtitle.size|subtitle_layout.size",
    "position": "references/formats/*.json slots.<slot>.x|y / safe",
    "font": "references/styles/*.json roles.<role>.font",
    "color": "references/styles/*.json roles.<role>.color / ink / stroke_color",
    "content": "prompts/hook_writer.md / scripts/script_compiler.py 文案生成",
    "style": "references/styles/*.json roles.<role>.border|flower",
    "transition": "references/styles/*.json cut_transition|chapter_transition / scripts/storyboard_builder.py Builder._main",
    "animation": "references/styles/*.json roles.<role>.anim_in|anim_out / scripts/core/text_ops.py anim_in_duration",
    "filter": "references/styles/*.json grade.filter",
    "effect": "references/styles/*.json ending_effect / references/scenes/*.json effects",
    "volume": "references/styles/*.json bgm.volume|sfx.volume / references/scenes/*.json audio.bgm_volume",
    "audio_fade": "references/styles/*.json bgm.fade_in|fade_out",
    "keyframe": "scripts/storyboard_builder.py Builder._camera / references/styles/*.json camera.amount|punch",
    "canvas": "references/formats/*.json canvas.width|height",
    "transform": "references/formats/*.json pip|card / scripts/storyboard_builder.py",
}


def _target_key(c: dict) -> str:
    k = c["kind"]
    for p in ("transition", "animation", "filter", "effect", "audio_fade", "keyframe", "canvas"):
        if k.startswith(p):
            return p
    if k.startswith("fade_"):
        return "audio_fade"
    if c["category"] == "canvas":
        return "canvas"
    if c["category"] == "transform":
        return "transform"
    return k


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def learn_patterns(diffs: List[dict]) -> List[dict]:
    """把多份对比结果里的修改汇总成规律, 每条带次数 / 均值 / 证据 / 对应的 skill 代码。"""
    groups = defaultdict(list)
    for d in diffs:
        for c in d.get("changes", []):
            groups[_target_key(c)].append({**c, "draft": d.get("draft", "")})
    rules = []
    for key, cs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        deltas = [c["delta_ms"] for c in cs if c.get("delta_ms") is not None]
        drafts = sorted({c["draft"] for c in cs})
        if deltas:
            m = _mean(deltas)
            word = {"trim_in": ("入点推后", "入点提前"), "trim_out": ("出点推后", "出点提前")}.get(
                key, ("增加", "减少")
            )
            text = f"{key}: {len(cs)} 处, 平均{word[0] if m > 0 else word[1]} {abs(m):.0f}ms"
        elif key in ("size", "font", "color", "position", "volume", "style", "content"):
            pairs = Counter((json.dumps(c["before"], ensure_ascii=False), json.dumps(c["after"], ensure_ascii=False))
                            for c in cs)  # fmt: skip
            (b, a), n = pairs.most_common(1)[0]
            text = f"{key}: {len(cs)} 处, 最常见 {b} → {a} ({n} 次)"
        else:
            kinds = Counter(c["kind"] for c in cs)
            text = f"{key}: " + ", ".join(f"{k} ×{n}" for k, n in kinds.most_common())
        rules.append({"key": key, "count": len(cs), "drafts": drafts, "mean_delta_ms": round(_mean(deltas), 1)
                      if deltas else None, "rule": text, "target": TARGETS.get(key, ""),
                      "evidence": [{"draft": c["draft"], "segment": (c.get("segment") or {}).get("label"),
                                    "field": c["field"], "before": c["before"], "after": c["after"]}
                                   for c in cs[:8]]})  # fmt: skip
    return rules


def normalization_findings(results: List[dict]) -> List[dict]:
    """剪映自动规整 (不是用户修改) 的汇总: skill 写出的值和剪映实际显示的值不一致, 可在 skill 端预先对齐。"""
    snap, drift, trans, paths, autos, ex = 0, 0.0, [], 0, Counter(), []
    for r in results:
        for c in r["comparisons"]:
            if c["kind"] != "main":
                continue
            n = c["noise"]
            snap += n.get("frame_snap", 0)
            ex += [f"{r['draft']} {e}" for e in n.get("snap_examples", [])[:2]]
            drift = max(drift, n.get("max_frame_drift_ms", 0))
            trans += [{**t, "draft": r["draft"]} for t in n.get("auto_transitions", [])]
            paths += n.get("path_rewritten", 0)
            autos.update(n.get("auto_materials_added", {}))
    out = []
    if snap:
        out.append({"key": "frame_snap", "count": snap, "max_drift_ms": drift, "examples": ex,
                    "text": f"时间值被剪映对齐到整帧 {snap} 处, 主轨逐段吸附后最大偏移 {drift:.1f}ms",
                    "target": "scripts/storyboard_builder.py Builder._main (_sec 直接换算微秒, 未按帧取整) / "
                              "scripts/core/media_ops.py add_media_safe; 参考已有做法 scripts/core/motion_ops.py "
                              "_retime 和 scripts/beat_snap.py _to_frame"})  # fmt: skip
    if trans:
        out.append({"key": "transition_auto", "count": len(trans), "examples": trans[:8],
                    "text": f"转场时长被剪映截短 {len(trans)} 处 (重叠转场取偶数帧, 且不超过相邻较短片段的一半)",
                    "target": "scripts/core/vfx_ops.py add_transition_simple / references/styles/*.json "
                              "cut_transition.duration / scripts/storyboard_builder.py Builder._main"})  # fmt: skip
    if paths:
        out.append({"key": "path_rewritten", "count": paths, "text": f"素材绝对路径被剪映改成相对路径 {paths} 处 (无需处理)",
                    "target": ""})  # fmt: skip
    if autos:
        out.append({"key": "auto_materials", "count": sum(autos.values()),
                    "text": "剪映自动补齐默认附属素材: " + ", ".join(f"{k} ×{v}" for k, v in autos.most_common()),
                    "target": ""})  # fmt: skip
    return out


# ---------------- 中文摘要 ----------------
def _fmt(v) -> str:
    if isinstance(v, int) and abs(v) >= 1000:
        return f"{v / 1000:.1f}ms"
    return json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v


def _change_line(c: dict) -> str:
    seg = (c.get("segment") or {}).get("label") or "全局"
    zh = CATEGORY_ZH.get(c["category"], c["category"])
    d = f" (Δ {c['delta_ms']:+.0f}ms)" if c.get("delta_ms") is not None else ""
    return f"- [{zh}] {seg} `{c['field']}`: {_fmt(c['before'])} → {_fmt(c['after'])}{d}"


def suggestions(rules: List[dict], norms: List[dict]) -> List[dict]:
    out = []
    for r in rules:
        ev = "; ".join(f"{e['draft']} {e['segment'] or ''} {e['field']}: {_fmt(e['before'])} → {_fmt(e['after'])}"
                       for e in r["evidence"][:3])  # fmt: skip
        out.append({"source": "用户修改", "advice": f"按用户习惯调整默认值: {r['rule']}", "target": r["target"],
                    "evidence": ev})  # fmt: skip
    for n in norms:
        if not n.get("target"):
            continue
        if n["key"] == "frame_snap":
            advice = ("skill 写入时间线前先把片段起点 / 时长 / 截取起点对齐到整帧 (round(t*fps)/fps), "
                      "主轨按「上一段终点 = 下一段起点」累加帧数, 让 skill 副本和剪映显示一致, 后续对比和审片不再有 1 帧误差")  # fmt: skip
            ev = n["text"] + "; " + "; ".join(n.get("examples", [])[:4])
        else:
            advice = ("添加重叠转场时按剪映规则预先规整: 时长 = 2 × floor(min(d, 相邻较短片段/2) / 2 / 帧长) × 帧长, "
                      "并在超长时警告 (现在 skill 副本里记的时长比剪映实际短, 节奏计算会偏)")  # fmt: skip
            ev = n["text"] + "; " + "; ".join(
                f"{t['draft']} {t['segment']} {t['name']} {t['before_ms']:.0f}→{t['after_ms']:.1f}ms"
                for t in n.get("examples", [])[:4])  # fmt: skip
        out.append(
            {"source": "剪映自动规整", "advice": advice, "target": n["target"], "evidence": ev}
        )
    return out


def render_markdown(
    results: List[dict], rules: List[dict], norms: List[dict], sugg: List[dict]
) -> str:
    L = ["# 草稿修改学习 (edit_diff)", "",
         f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}。对比 skill 生成的版本和剪映里的最终版本 (template.json), "
         "已过滤剪映自动规整 (整帧对齐、id 改写、默认字段补齐、路径改写等)。", ""]  # fmt: skip
    L += ["## 各草稿结果", ""]
    for r in results:
        L.append(f"### {r['draft']}")
        L.append("")
        if r.get("error"):
            L += [f"- 无法对比: {r['error']}", ""]
            continue
        L.append("- 明文版本: " + "; ".join(f"{v['label']} ({v['time']})" for v in r["versions"]))
        if r["encrypted_backups"]:
            L.append(f"- 已加密无法读取的备份 {len(r['encrypted_backups'])} 份: "
                     + ", ".join(os.path.basename(p) for p in r["encrypted_backups"]))  # fmt: skip
        for c in r["comparisons"]:
            a = c["alignment"]
            head = f"- {c['base_label']} → {c['edited_label']} (片段 {a['base_segments']}→{a['edited_segments']}, " \
                   f"对齐 {a['matched']}): "  # fmt: skip
            if not c["modified"]:
                n = c["noise"]
                L.append(head + f"**无修改** (过滤噪声: 整帧对齐 {n.get('frame_snap', 0)} 处, 最大 "
                         f"{n.get('max_frame_drift_ms', 0)}ms; 转场自动规整 {n.get('transition_auto', 0)} 处)")  # fmt: skip
            else:
                L.append(head + f"**{len(c['changes'])} 处修改**")
                L += ["  " + _change_line(x) for x in c["changes"][:40]]
        L.append(f"- 结论: **{'有用户修改' if r['modified'] else '无修改'}**")
        L.append("")
    L += ["## 用户修正规律", ""]
    if rules:
        for r in rules:
            L.append(f"- {r['rule']} (草稿: {', '.join(r['drafts'])}) → 对应 `{r['target']}`")
    else:
        L.append("- 这些草稿里没有检测到用户的手动修改, 暂时没有可学习的修正规律。")
    L += ["", "## 剪映自动规整 (非用户修改)", ""]
    L += [f"- {n['text']}" for n in norms] or ["- 无"]
    L += ["", "## 对 skill 的改进建议", ""]
    if not sugg:
        L.append("- 暂无。")
    for i, s in enumerate(sugg, 1):
        L += [
            f"{i}. [{s['source']}] {s['advice']}",
            f"   - 落点: `{s['target']}`",
            f"   - 证据: {s['evidence']}",
        ]
    L.append("")
    return "\n".join(L)


def learn(drafts: List[str], out_dir: str, tol_ms: Optional[float] = None) -> dict:
    """对多个草稿做对比, 写 edit_diffs.json + edit_diffs.md 到 out_dir (不写草稿目录)。"""
    out_dir = os.path.abspath(os.path.expanduser(out_dir))
    for root in DRAFT_ROOTS:
        if os.path.commonpath([out_dir, os.path.abspath(root)]) == os.path.abspath(root):
            raise InputError(f"输出目录不能在剪映草稿目录里: {out_dir}")
    results = []
    for name in drafts:
        try:
            results.append(diff_draft(name, tol_ms))
        except InputError as e:
            results.append({"draft": name, "error": str(e), "modified": None, "comparisons": [], "versions": [],
                            "encrypted_backups": []})  # fmt: skip
    main_diffs = [
        {**c, "draft": r["draft"]} for r in results for c in r["comparisons"] if c["kind"] == "main"
    ]
    rules = learn_patterns(main_diffs)
    norms = normalization_findings(results)
    sugg = suggestions(rules, norms)
    payload = {"tool": "edit_diff", "version": 1, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "drafts": results, "patterns": rules, "normalizations": norms, "suggestions": sugg}  # fmt: skip
    os.makedirs(out_dir, exist_ok=True)
    jp, mp = os.path.join(out_dir, "edit_diffs.json"), os.path.join(out_dir, "edit_diffs.md")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    with open(mp, "w", encoding="utf-8") as f:
        f.write(render_markdown(results, rules, norms, sugg))
    return {"json": jp, "markdown": mp, "modified": {r["draft"]: r.get("modified") for r in results},
            "patterns": len(rules), "suggestions": len(sugg)}  # fmt: skip


# ---------------- CLI ----------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="对比 skill 生成的草稿和剪映里改过的版本, 学习用户的修改"
    )
    ap.add_argument("--tol-ms", type=float, default=None, help="时间容差 (毫秒), 默认 1 帧 + 1ms")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("diff", help="对比两份草稿 JSON 文件")
    p.add_argument("base", help="修改前 (如 draft_info.skill.json)")
    p.add_argument("edited", help="修改后 (如 template.json)")
    p = sub.add_parser("draft", help="对比一个草稿的 skill 副本 / 明文备份 和 template.json")
    p.add_argument("name", help="草稿名或草稿目录")
    p = sub.add_parser("learn", help="多个草稿汇总成修正规律, 写 edit_diffs.json / edit_diffs.md")
    p.add_argument("names", nargs="+", help="草稿名或草稿目录")
    p.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help=f"输出目录 (默认 {DEFAULT_OUT_DIR})")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "diff":
            data = diff_files(args.base, args.edited, args.tol_ms)
            text = "\n".join(_change_line(c) for c in data["changes"]) or "无修改"
        elif args.cmd == "draft":
            data = diff_draft(args.name, args.tol_ms)
            text = render_markdown([data], [], [], [])
        else:
            data = learn(args.names, args.out_dir, args.tol_ms)
            text = f"已写入: {data['json']}\n        {data['markdown']}\n" + "\n".join(
                f"- {k}: {'有用户修改' if v else ('无修改' if v is False else '无法对比')}"
                for k, v in data["modified"].items())  # fmt: skip
    except InputError as e:
        emit_result(make_result(False, "invalid_input", str(e)), args.json)
        if not args.json:
            print(f"错误: {e}", file=sys.stderr)
        return 2
    if args.json:
        emit_result(make_result(True, "ok", "", data), True)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
