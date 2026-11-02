#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从用户手剪的剪映草稿学习剪辑风格 (Style Profile)

只读扫描草稿目录, 用 utils.draft_store.read_draft_content 读每个草稿最新的明文内容
(剪映的 template.json 或 skill 副本; 加密且无明文的跳过), 按画幅和时长自动分成三类:

  - short_vertical  短视频竖屏 (高 > 宽)
  - horizontal_short 横屏短片 (横屏, 时长 < 180s, 或字幕很少)
  - long_tutorial   长教程 (横屏, 时长 >= 180s 且每分钟字幕 >= 2 条)

统计镜头时长 / 切点密度、字幕样式、花字和文字模板、转场、特效 / 贴纸 / 滤镜、变速、关键帧运镜、
音频 (BGM / 人声 / TTS / 音效 的音量和淡入淡出)、画中画、蒙版、片头片尾结构。
每一项都带样本数 n 和来源草稿 drafts, 方便核对。

单位 (与 layout_preview.py / pyJianYingDraft 一致):
  - 时间: 秒 (草稿里是微秒)
  - 字号: 剪映 TextStyle.size x 片段 clip.scale.x (与 formats/*.json 的 subtitle.size、styles 的 roles.*.size 同单位)
  - 位置: 剪映 transform (中心 0, 右 / 上为正, 半个画布为 1)
  - 音量: 线性增益 (1.0 = 0 dB), 同时给 dB

用法:
  python style_profile.py                       # 扫描默认草稿目录, 写 ~/.config/jianying-pro/learned/
  python style_profile.py --root DIR --out-dir DIR
  python style_profile.py --exclude 某草稿 --category 某草稿=long_tutorial --json

对草稿目录只读, 不写入任何草稿文件。
"""

import argparse
import datetime
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.draft_store import SKILL_CONTENT, EncryptedDraftError, read_draft_content  # noqa: E402

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFS_DIR = os.path.join(SKILL_ROOT, "references")
DEFAULT_ROOT = os.path.expanduser("~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft")
DEFAULT_OUT = os.path.expanduser("~/.config/jianying-pro/learned")

CATEGORIES = {
    "short_vertical": "短视频竖屏",
    "horizontal_short": "横屏短片",
    "long_tutorial": "长教程",
}
# skill 生成 / 测试用草稿 (名字前缀); 另外有 draft_info.skill.json 的也排除
DEFAULT_EXCLUDE_PREFIXES = (
    "Diagnostic_Test",
    "ZZ 字幕标定",
    "RoughCut_",
)
LONG_MIN_S = 180.0
LONG_MIN_SUBS_PER_MIN = 2.0
OPEN_S = 3.0
END_S = 5.0
US = 1_000_000.0


# ---------------------------------------------------------------- 基础工具


def _sec(us) -> float:
    return float(us or 0) / US


def _tr(seg: dict) -> Tuple[float, float]:
    """片段在时间线上的 (起点, 时长), 秒。精简格式会省略 start=0。"""
    tr = seg.get("target_timerange") or {}
    return _sec(tr.get("start")), _sec(tr.get("duration"))


def _db(v: float) -> Optional[float]:
    return round(20 * math.log10(v), 1) if v and v > 0 else None


def _hex(color) -> Optional[str]:
    if not color or len(color) < 3:
        return None
    return "#" + "".join(f"{int(round(max(0.0, min(1.0, float(c))) * 255)):02X}" for c in color[:3])


def _pct(vals: List[float], q: float) -> float:
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * q
    lo = int(math.floor(k))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _r(v, nd=3):
    return round(v, nd) if isinstance(v, float) else v


def _seg_scale(seg: dict) -> float:
    return float(((seg.get("clip") or {}).get("scale") or {}).get("x", 1.0))


def _seg_pos(seg: dict) -> Tuple[float, float]:
    t = (seg.get("clip") or {}).get("transform") or {}
    return float(t.get("x", 0.0)), float(t.get("y", 0.0))


def _kf_points(seg: dict, prop: str) -> List[Tuple[float, float]]:
    for kf in seg.get("common_keyframes") or []:
        if kf.get("property_type") == prop:
            pts = [
                (_sec(k.get("time_offset")), float(k["values"][0]))
                for k in kf.get("keyframe_list") or []
                if k.get("values")
            ]
            return sorted(pts)
    return []


def _text_len(text: str) -> int:
    """每条字数: 去掉空白和标点后的字符数 (中文按字, 英文按字母)。"""
    return len(re.sub(r"[\s\.,，。!！?？:：;；、\"'“”‘’()（）\-—·…]", "", text or ""))


def _font_from_path(path: str) -> Optional[str]:
    if not path:
        return None
    if "SystemFont" in path or os.path.basename(path).startswith("zh-hans"):
        return "系统默认"
    return os.path.splitext(os.path.basename(path))[0]


def _font_by_id(rid: str) -> Optional[str]:
    if not rid:
        return None
    try:
        from utils.text_metrics import font_name_by_id

        return font_name_by_id(str(rid))
    except Exception:  # 字体索引缺失时不影响统计
        return None


def resolve_font(mat: dict, style: dict) -> str:
    """文字素材的字体名: 素材记录的名字 > 资源 id 查字体索引 > 字体文件名 > 系统默认。"""
    for k in ("font_name", "font_title"):
        v = mat.get(k)
        if v and v != "none":
            return v
    for f in mat.get("fonts") or []:
        v = f.get("title") or f.get("name")
        if v:
            return v
    font = style.get("font") or {}
    name = _font_by_id(font.get("id") or mat.get("font_resource_id") or mat.get("font_id"))
    if name:
        return name
    return _font_from_path(font.get("path") or mat.get("font_path") or "") or "系统默认"


# ---------------------------------------------------------------- 统计容器


class Bag:
    """收集 (值, 来源草稿) 样本, 最后汇总成带 n / drafts 的统计。key 用 . 分层。"""

    def __init__(self):
        self.nums: Dict[str, List[Tuple[float, str]]] = defaultdict(list)
        self.cats: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        self.flags: Dict[str, List[Tuple[bool, str]]] = defaultdict(list)
        self.rates: Dict[str, Dict[str, float]] = defaultdict(dict)

    def num(self, key: str, value, draft: str):
        if value is not None and not (isinstance(value, float) and math.isnan(value)):
            self.nums[key].append((float(value), draft))

    def cat(self, key: str, value, draft: str):
        if value not in (None, ""):
            self.cats[key].append((str(value), draft))

    def flag(self, key: str, value: bool, draft: str):
        self.flags[key].append((bool(value), draft))

    def rate(self, key: str, draft: str, value: float):
        """每个草稿一个值 (如每分钟切点数); 汇总时 n = 草稿数。"""
        self.rates[key][draft] = float(value)

    def summary(self) -> dict:
        out: dict = {}
        for k, v in self.nums.items():
            _put(out, k, num_stat(v))
        for k, v in self.cats.items():
            _put(out, k, cat_stat(v))
        for k, v in self.flags.items():
            _put(out, k, flag_stat(v))
        for k, v in self.rates.items():
            _put(out, k, rate_stat(v))
        return out


def _put(d: dict, key: str, val):
    parts = key.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = val


def _drafts(entries) -> List[str]:
    return sorted({d for _, d in entries})


def num_stat(entries: List[Tuple[float, str]]) -> dict:
    """数值统计。draft_median = 各草稿中位数的中位数, 不让样本特别多的草稿主导。"""
    vals = [v for v, _ in entries]
    per = defaultdict(list)
    for v, d in entries:
        per[d].append(v)
    dmed = {d: _pct(vs, 0.5) for d, vs in per.items()}
    return {
        "n": len(vals),
        "drafts": _drafts(entries),
        "median": _r(_pct(vals, 0.5)),
        "p10": _r(_pct(vals, 0.1)),
        "p25": _r(_pct(vals, 0.25)),
        "p75": _r(_pct(vals, 0.75)),
        "p90": _r(_pct(vals, 0.9)),
        "mean": _r(sum(vals) / len(vals)),
        "min": _r(min(vals)),
        "max": _r(max(vals)),
        "draft_median": _r(_pct(list(dmed.values()), 0.5)),
        "per_draft_median": {d: _r(v) for d, v in sorted(dmed.items())},
    }


def cat_stat(entries: List[Tuple[str, str]], top: int = 10) -> dict:
    c = Counter(v for v, _ in entries)
    per = defaultdict(set)
    for v, d in entries:
        per[v].add(d)
    n = len(entries)
    return {
        "n": n,
        "drafts": _drafts(entries),
        "distinct": len(c),
        "top": [
            {"value": v, "count": k, "share": round(k / n, 3), "drafts": sorted(per[v])}
            for v, k in c.most_common(top)
        ],
    }


def flag_stat(entries: List[Tuple[bool, str]]) -> dict:
    yes = sum(1 for v, _ in entries if v)
    return {
        "n": len(entries),
        "drafts": _drafts(entries),
        "yes": yes,
        "share": round(yes / len(entries), 3) if entries else 0.0,
    }


def rate_stat(per_draft: Dict[str, float]) -> dict:
    st = num_stat([(v, d) for d, v in per_draft.items()])
    st.pop("per_draft_median", None)
    st["per_draft"] = {d: _r(v) for d, v in sorted(per_draft.items())}
    return st


# ---------------------------------------------------------------- 草稿解析


def index_materials(content: dict) -> Dict[str, Tuple[str, dict]]:
    """素材 id -> (素材类别, 素材)。同一 id 只记第一次出现。"""
    idx: Dict[str, Tuple[str, dict]] = {}
    for kind, items in (content.get("materials") or {}).items():
        if not isinstance(items, list):
            continue
        for m in items:
            if isinstance(m, dict) and m.get("id") and m["id"] not in idx:
                idx[m["id"]] = (kind, m)
    return idx


def _refs(seg: dict, idx: dict, kind: str) -> List[dict]:
    """片段 extra_material_refs 里属于某类素材的 (去重)。"""
    out, seen = [], set()
    for r in seg.get("extra_material_refs") or []:
        if r in seen or r not in idx:
            continue
        seen.add(r)
        if idx[r][0] == kind:
            out.append(idx[r][1])
    return out


def _speed(seg: dict, idx: dict) -> float:
    if seg.get("speed") is not None:
        return float(seg["speed"])
    for sp in _refs(seg, idx, "speeds"):
        if sp.get("speed") is not None:
            return float(sp["speed"])
    return 1.0


def _animations(seg: dict, idx: dict) -> List[dict]:
    out = []
    for m in _refs(seg, idx, "material_animations"):
        out.extend(a for a in m.get("animations") or [] if a.get("name"))
    return out


def provenance(content: dict, source: str) -> dict:
    """
    推断草稿是剪映里手剪的还是脚本生成的。
    pyJianYingDraft 一类脚本生成的素材 id 是 32 位小写十六进制 (无连字符), 剪映原生是大写 UUID;
    轨道名是英文 (VideoTrack / VoiceOver / BGM / Subtitles) 也是脚本生成的特征。
    """
    ids = [m["id"] for _, m in index_materials(content).values()]
    hexr = sum(1 for i in ids if re.fullmatch(r"[0-9a-f]{32}", i)) / len(ids) if ids else 0.0
    names = [t.get("name") or "" for t in content.get("tracks") or []]
    named = [n for n in names if n]
    app = ((content.get("last_modified_platform") or {}).get("app_version")) or ""
    likely = hexr >= 0.15 or (source == "draft" and bool(named))
    return {
        "script_id_ratio": round(hexr, 3),
        "named_tracks": named,
        "app_version": app,
        "likely_script_generated": likely,
        "note": (
            "素材 id / 轨道名像脚本生成, 之后可能在剪映里改过; 数值更接近当时脚本的设定"
            if likely
            else "剪映原生素材 id, 应为手剪"
        ),
    }


def _tracks(content: dict, kind: str) -> List[dict]:
    return [t for t in content.get("tracks") or [] if t.get("type") == kind and t.get("segments")]


def _text_info(seg: dict, idx: dict) -> dict:
    """解析文字片段: 普通文字 / 字幕 / 文字模板。"""
    kind, mat = idx.get(seg.get("material_id"), (None, {}))
    info = {"kind": kind, "template": None, "mat": mat}
    if kind == "text_templates":
        info["template"] = mat
        tmat = {}
        for r in mat.get("text_info_resources") or []:
            if r.get("text_material_id") in idx:
                tmat = idx[r["text_material_id"]][1]
                break
        mat = tmat
        info["mat"] = mat
        fonts = [
            _font_from_path(r.get("path", ""))
            for r in mat.get("resources") or info["template"].get("resources") or []
            if r.get("panel") == "fonts"
        ]
        info["template_font"] = fonts[0] if fonts else None
    try:
        cont = json.loads(mat.get("content") or "{}")
    except (TypeError, ValueError):
        cont = {}
    styles = cont.get("styles") or [{}]
    info["text"] = cont.get("text") or ""
    info["style"] = styles[0] if styles else {}
    info["mtype"] = mat.get("type")
    return info


def classify_text_track(track: dict, idx: dict) -> str:
    """文字轨道角色: subtitle (字幕) / overlay (标题 / 关键词 / 标注)。"""
    name = (track.get("name") or "").lower()
    if re.search(r"subtitle|字幕|caption", name):
        return "subtitle"
    if re.search(r"chapter|title|标题|名牌|倒计时|keyword|关键词|label", name):
        return "overlay"
    segs = track.get("segments") or []
    infos = [_text_info(s, idx) for s in segs]
    sub = sum(
        1
        for i in infos
        if i["mtype"] == "subtitle"
        or (
            i["template"]
            and (i["template"].get("text_template_resource_type") == "subtitle_template")
        )
    )
    if sub >= max(1, len(segs) / 2):
        return "subtitle"
    durs = [_tr(s)[1] for s in segs]
    if len(segs) >= 8 and _pct(durs, 0.5) <= 4.5:
        return "subtitle"
    return "overlay"


def classify_audio(track: dict, mat: dict, dur: float) -> str:
    """音频角色: tts (剪映朗读) / voice (导入的人声或配音) / bgm / sfx / original (视频原声)。"""
    t = mat.get("type") or ""
    tn = (track.get("name") or "").lower()
    if t == "text_to_audio":
        return "tts"
    if t == "sound":
        return "sfx"
    if t == "video_original_sound":
        return "original"
    if t == "music":
        return "bgm"
    if t == "record":
        return "voice"
    if re.search(r"bgm|music|音乐|配乐", tn):
        return "bgm"
    if re.search(r"sfx|音效", tn):
        return "sfx"
    if re.search(r"voice|tts|配音|旁白|口播|人声|录音|narrat", tn):
        return "voice"
    if (mat.get("name") or "").startswith("tts_"):
        return "voice"
    if dur <= 4.0:
        return "sfx"
    if dur >= 20.0:
        return "bgm"
    return "voice"


def categorize(content: dict) -> str:
    """按画幅和时长分类; 横屏长片还要求字幕密度 (区分长教程和横屏长 reel)。"""
    cv = content.get("canvas_config") or {}
    w, h = float(cv.get("width") or 1920), float(cv.get("height") or 1080)
    dur = _sec(content.get("duration"))
    if h > w:
        return "short_vertical"
    if dur >= LONG_MIN_S:
        idx = index_materials(content)
        subs = sum(
            len(t["segments"])
            for t in _tracks(content, "text")
            if classify_text_track(t, idx) == "subtitle"
        )
        if subs / (dur / 60.0) >= LONG_MIN_SUBS_PER_MIN:
            return "long_tutorial"
    return "horizontal_short"


# ---------------------------------------------------------------- 单个草稿 -> 样本


def _video_stats(content: dict, idx: dict, name: str, bag: Bag, ctx: dict):
    vtracks = _tracks(content, "video")
    dur = ctx["duration"]
    minutes = max(dur / 60.0, 1e-6)
    if not vtracks:
        return
    main, others = vtracks[0], vtracks[1:]
    msegs = sorted(main["segments"], key=lambda s: _tr(s)[0])
    ctx["main_cuts"] = [_tr(s)[0] for s in msegs[1:]]
    photos = 0
    for s in msegs:
        st, d = _tr(s)
        bag.num("pacing.shot_duration_s", d, name)
        kind, m = idx.get(s.get("material_id"), (None, {}))
        photos += m.get("type") == "photo"
        sc = _seg_scale(s)
        bag.flag("pacing.main_scaled", abs(sc - 1.0) > 0.02, name)
        if abs(sc - 1.0) > 0.02:
            bag.num("pacing.main_scale", sc, name)
        for cv in _refs(s, idx, "canvases"):
            bag.cat("pip.canvas_background", cv.get("type"), name)
    bag.rate("pacing.cuts_per_min", name, max(0, len(msegs) - 1) / minutes)
    bag.rate("pacing.main_shots", name, len(msegs))
    bag.rate("pacing.photo_share", name, photos / len(msegs))
    bag.rate("structure.duration_s", name, dur)
    bounds = set()
    for t in vtracks:
        for s in t["segments"]:
            st, d = _tr(s)
            bounds.update((round(st, 2), round(st + d, 2)))
    inner = sorted(b for b in bounds if 0.05 < b < dur - 0.05)
    bag.rate("pacing.visual_changes_per_min", name, len(inner) / minutes)
    # 平均画面变化间隔: 竖屏常把底图放主轨、内容放叠加轨, 只看主轨会高估镜头时长
    bag.rate("pacing.visual_change_interval_s", name, dur / (len(inner) + 1))
    moves = 0
    for ti, t in enumerate(vtracks):
        for s in t["segments"]:
            st, d = _tr(s)
            # 转场
            for tr in _refs(s, idx, "transitions"):
                bag.cat("transitions.name", tr.get("name"), name)
                bag.num("transitions.duration_s", _sec(tr.get("duration")), name)
                ctx["transitions"] += 1
            # 入场 / 出场动画
            for a in _animations(s, idx):
                bag.cat(f"video_animation.{a.get('type', 'in')}", a.get("name"), name)
                bag.num(f"video_animation.{a.get('type', 'in')}_s", _sec(a.get("duration")), name)
            # 变速
            sp = _speed(s, idx)
            bag.flag("speed.changed", abs(sp - 1.0) > 0.005, name)
            if abs(sp - 1.0) > 0.005:
                bag.num("speed.value", sp, name)
            if any(x.get("curve_speed") for x in _refs(s, idx, "speeds")):
                bag.flag("speed.curve", True, name)
            # 关键帧运镜
            sc = _kf_points(s, "KFTypeScaleX")
            px, py = _kf_points(s, "KFTypePositionX"), _kf_points(s, "KFTypePositionY")
            moved = False
            if len(sc) >= 2:
                lo, hi = min(v for _, v in sc), max(v for _, v in sc)
                if lo > 0 and hi / lo > 1.01:
                    moved = True
                    ratio = sc[-1][1] / sc[0][1] if sc[0][1] else 1.0
                    bag.cat("keyframes.move", "push_in" if ratio >= 1 else "pull_out", name)
                    bag.num("keyframes.zoom_ratio", max(ratio, 1 / ratio if ratio else 1), name)
                    span = (sc[-1][0] - sc[0][0]) / max(sp, 1e-6)
                    bag.num("keyframes.zoom_dur_s", span, name)
            for pts in (px, py):
                if len(pts) >= 2 and max(v for _, v in pts) - min(v for _, v in pts) > 0.02:
                    moved = True
                    bag.cat("keyframes.move", "pan", name)
                    break
            moves += moved
            # 蒙版 / 抠像
            for mk in _refs(s, idx, "common_mask") + _refs(s, idx, "masks"):
                bag.cat("pip.mask", mk.get("name") or mk.get("resource_type"), name)
            if _refs(s, idx, "chromas"):
                bag.cat("pip.chroma", "色度抠图", name)
            if ti > 0:
                sx = _seg_scale(s)
                x, y = _seg_pos(s)
                full = sx >= 0.95 and abs(x) < 0.05 and abs(y) < 0.05
                kind_ = "full_overlay" if full else ("pip" if sx <= 0.6 else "inset_or_zoomed")
                bag.cat("pip.overlay_kind", kind_, name)
                if kind_ == "inset_or_zoomed":
                    bag.num("pip.inset_scale", sx, name)
                if kind_ == "pip":
                    bag.num("pip.scale", sx, name)
                    bag.num("pip.x", x, name)
                    bag.num("pip.y", y, name)
                    bag.num("pip.duration_s", d, name)
    bag.rate("keyframes.moves_per_min", name, moves / minutes)
    bag.rate("pip.overlay_tracks", name, len(others))
    cuts = max(1, len(msegs) - 1)
    bag.rate("transitions.per_cut", name, ctx["transitions"] / cuts)
    ctx["first_shot"] = _tr(msegs[0])[1]
    ctx["last_shot"] = _tr(msegs[-1])[1]


def _text_stats(content: dict, idx: dict, name: str, bag: Bag, ctx: dict):
    dur = ctx["duration"]
    minutes = max(dur / 60.0, 1e-6)
    n_sub = n_over = n_flower = n_tpl = 0
    for t in _tracks(content, "text"):
        role = classify_text_track(t, idx)
        for s in t["segments"]:
            st, d = _tr(s)
            info = _text_info(s, idx)
            mat, style = info["mat"], info["style"]
            pre = "subtitle" if role == "subtitle" else "overlay"
            if role == "subtitle":
                n_sub += 1
                ctx["sub_starts"].append(st)
            else:
                n_over += 1
                ctx["overlay"].append((st, st + d, info["text"]))
            x, y = _seg_pos(s)
            sc = _seg_scale(s)
            bag.num(f"{pre}.transform_y", y, name)
            bag.num(f"{pre}.duration_s", d, name)
            n = _text_len(info["text"])
            if n:
                bag.num(f"{pre}.chars", n, name)
                if role == "subtitle" and d > 0:
                    bag.num("subtitle.chars_per_s", n / d, name)
                bag.num(f"{pre}.lines", info["text"].count("\n") + 1, name)
            if role == "overlay":
                bag.num("overlay.transform_x", x, name)
            # 花字 / 气泡
            for e in _refs(s, idx, "effects"):
                if e.get("type") == "text_effect":
                    bag.cat(f"{pre}.flower", e.get("name") or f"id:{e.get('resource_id')}", name)
                    n_flower += 1
                elif e.get("type") == "text_shape":
                    bag.cat(f"{pre}.bubble", e.get("name") or f"id:{e.get('resource_id')}", name)
            bag.flag(
                f"{pre}.uses_flower",
                any(e.get("type") == "text_effect" for e in _refs(s, idx, "effects")),
                name,
            )
            for a in _animations(s, idx):
                bag.cat(f"{pre}.anim_{a.get('type', 'in')}", a.get("name"), name)
            if info["template"] is not None:
                n_tpl += 1
                tp = info["template"]
                bag.cat(
                    f"{pre}.template",
                    f"{tp.get('name')} ({tp.get('text_template_resource_type') or 'text_template'})",
                    name,
                )
                bag.num(f"{pre}.template_scale", sc, name)
                bag.flag(f"{pre}.uses_template", True, name)
                if info.get("template_font"):
                    bag.cat(f"{pre}.font", info["template_font"], name)
                continue
            bag.flag(f"{pre}.uses_template", False, name)
            size = style.get("size", mat.get("font_size"))
            if size is not None:
                bag.num(f"{pre}.size", float(size) * sc, name)
            bag.cat(f"{pre}.font", resolve_font(mat, style), name)
            fill = ((style.get("fill") or {}).get("content") or {}).get("solid", {}).get("color")
            bag.cat(f"{pre}.color", _hex(fill) or "#FFFFFF", name)
            strokes = [k for k in style.get("strokes") or [] if float(k.get("width", 0)) > 0.01]
            bag.flag(f"{pre}.stroke", bool(strokes), name)
            if strokes:
                bag.num(f"{pre}.stroke_width", float(strokes[0]["width"]), name)
                col = ((strokes[0].get("content") or {}).get("solid") or {}).get("color")
                bag.cat(f"{pre}.stroke_color", _hex(col), name)
            bg_on = (
                int(mat.get("background_style") or 0) > 0
                and float(mat.get("background_alpha", 1.0) or 0) > 0
            )
            bag.flag(f"{pre}.background", bg_on, name)
            if bg_on:
                bag.cat(f"{pre}.background_color", mat.get("background_color"), name)
                bag.num(f"{pre}.background_alpha", float(mat.get("background_alpha", 1.0)), name)
    bag.rate("subtitle.per_min", name, n_sub / minutes)
    bag.rate("overlay.per_min", name, n_over / minutes)
    bag.rate("text.flower_per_min", name, n_flower / minutes)
    bag.rate("text.templates_per_min", name, n_tpl / minutes)
    ctx["n_sub"] = n_sub


def _audio_stats(content: dict, idx: dict, name: str, bag: Bag, ctx: dict):
    dur = ctx["duration"]
    minutes = max(dur / 60.0, 1e-6)
    counts: Counter = Counter()
    cover: Dict[str, float] = defaultdict(float)
    cuts = ctx.get("main_cuts") or []
    for t in _tracks(content, "audio"):
        for s in t["segments"]:
            st, d = _tr(s)
            kind, mat = idx.get(s.get("material_id"), (None, {}))
            role = classify_audio(t, mat, d)
            counts[role] += 1
            cover[role] += d
            vol = float(s.get("volume", 1.0))
            kf = _kf_points(s, "KFTypeVolume")
            if kf:
                lo, hi = min(v for _, v in kf) * vol, max(v for _, v in kf) * vol
                bag.flag(f"audio.{role}.volume_keyframed", True, name)
                bag.num(f"audio.{role}.duck_low", lo, name)
                bag.num(f"audio.{role}.duck_high", hi, name)
                eff = lo
            else:
                bag.flag(f"audio.{role}.volume_keyframed", False, name)
                eff = vol
            bag.num(f"audio.{role}.volume", eff, name)
            if eff > 0:
                bag.num(f"audio.{role}.volume_db", _db(eff), name)
            bag.num(f"audio.{role}.duration_s", d, name)
            sp = _speed(s, idx)
            if role in ("tts", "voice"):
                bag.num(f"audio.{role}.speed", sp, name)
            if role == "tts":
                bag.cat(
                    "audio.tts.voice_name", mat.get("tone_type") or mat.get("tone_speaker"), name
                )
            if role in ("sfx", "bgm"):
                bag.cat(f"audio.{role}.name", (mat.get("name") or "")[:40], name)
            if role == "sfx" and cuts:
                near = min(abs(st - c) for c in cuts) <= 0.5
                bag.flag("audio.sfx.on_cut", near, name)
            for f in _refs(s, idx, "audio_fades"):
                fi, fo = _sec(f.get("fade_in_duration")), _sec(f.get("fade_out_duration"))
                bag.num(f"audio.{role}.fade_in_s", fi, name)
                bag.num(f"audio.{role}.fade_out_s", fo, name)
            if not _refs(s, idx, "audio_fades"):
                bag.num(f"audio.{role}.fade_in_s", 0.0, name)
                bag.num(f"audio.{role}.fade_out_s", 0.0, name)
            for ln in _refs(s, idx, "loudnesses"):
                if ln.get("enable") or ln.get("target_loudness"):
                    bag.num("audio.loudness_target_lufs", float(ln.get("target_loudness", 0)), name)
            for ae in _refs(s, idx, "audio_effects"):
                bag.cat("audio.voice_effect", ae.get("name"), name)
            if role in ("tts", "voice"):
                ctx["voice_starts"].append(st)
            if role == "bgm":
                ctx["bgm"].append((st, d, s, idx))
    # 视频原声
    for t in _tracks(content, "video")[:1]:
        for s in t["segments"]:
            kind, mat = idx.get(s.get("material_id"), (None, {}))
            if mat.get("type") == "video":
                v = float(s.get("volume", 1.0))
                bag.flag("audio.clip_audio_muted", v <= 0.001, name)
                if v > 0.001:
                    bag.num("audio.clip_audio_volume", v, name)
    for role in ("bgm", "sfx", "tts", "voice", "original"):
        bag.rate(f"audio.{role}.count", name, counts[role])
        bag.rate(f"audio.{role}.coverage", name, min(1.0, cover[role] / dur) if dur else 0.0)
    bag.rate("audio.sfx.per_min", name, counts["sfx"] / minutes)


def _fx_stats(content: dict, idx: dict, name: str, bag: Bag, ctx: dict):
    dur = ctx["duration"]
    minutes = max(dur / 60.0, 1e-6)
    n_eff = n_stk = n_filter = n_adjust = 0
    for t in content.get("tracks") or []:
        typ = t.get("type")
        for s in t.get("segments") or []:
            st, d = _tr(s)
            kind, mat = idx.get(s.get("material_id"), (None, {}))
            label = mat.get("name") or f"id:{mat.get('resource_id') or mat.get('effect_id')}"
            pos = "opening" if st < OPEN_S else ("ending" if st + d > dur - END_S else "middle")
            if typ == "effect":
                n_eff += 1
                bag.cat("effects.name", label, name)
                bag.num("effects.duration_s", d, name)
                bag.cat("effects.position", pos, name)
                ctx["effects"].append(pos)
            elif typ == "sticker":
                n_stk += 1
                bag.cat("stickers.name", label, name)
                bag.num("stickers.duration_s", d, name)
                bag.cat("stickers.position", pos, name)
                bag.flag("stickers.keyframed", bool(s.get("common_keyframes")), name)
                for a in _animations(s, idx):
                    bag.cat(f"stickers.anim_{a.get('type', 'in')}", a.get("name"), name)
                ctx["stickers"].append(pos)
            elif typ == "filter":
                n_filter += 1
                bag.cat("grade.filter", label, name)
            elif typ == "adjust":
                n_adjust += 1
                bag.cat("grade.adjust", label, name)
            if typ == "video":
                for e in _refs(s, idx, "effects"):
                    if e.get("type") == "filter":
                        n_filter += 1
                        bag.cat("grade.filter", e.get("name"), name)
                    elif e.get("type") in ("adjust", "hsl", "color_curves"):
                        n_adjust += 1
                        bag.cat("grade.adjust", e.get("type"), name)
                for e in _refs(s, idx, "video_effects"):
                    n_eff += 1
                    bag.cat("effects.name", e.get("name"), name)
                    bag.cat("effects.position", "clip", name)
    bag.rate("effects.per_min", name, n_eff / minutes)
    bag.rate("effects.count", name, n_eff)
    bag.rate("stickers.per_min", name, n_stk / minutes)
    bag.rate("grade.filter_count", name, n_filter)
    bag.rate("grade.adjust_count", name, n_adjust)


def _structure_stats(content: dict, name: str, bag: Bag, ctx: dict):
    dur = ctx["duration"]
    over = ctx["overlay"]
    bag.flag("structure.title_in_first_3s", any(st < OPEN_S for st, _, _ in over), name)
    if over:
        bag.num("structure.first_overlay_start_s", min(st for st, _, _ in over), name)
    if ctx["sub_starts"]:
        bag.num("structure.first_subtitle_start_s", min(ctx["sub_starts"]), name)
    if ctx["voice_starts"]:
        bag.num("structure.first_voice_start_s", min(ctx["voice_starts"]), name)
    bag.flag("structure.effect_at_opening", "opening" in ctx["effects"], name)
    bag.flag("structure.sticker_at_opening", "opening" in ctx["stickers"], name)
    bag.flag("structure.overlay_in_last_5s", any(en > dur - END_S for _, en, _ in over), name)
    bag.flag("structure.effect_at_ending", "ending" in ctx["effects"], name)
    if ctx.get("first_shot") is not None:
        bag.num("structure.first_shot_s", ctx["first_shot"], name)
        bag.num("structure.last_shot_s", ctx["last_shot"], name)
    if ctx["bgm"]:
        st0 = min(b[0] for b in ctx["bgm"])
        bag.num("structure.bgm_start_s", st0, name)
        first = min(ctx["bgm"], key=lambda b: b[0])
        last = max(ctx["bgm"], key=lambda b: b[0] + b[1])
        fi = sum(_sec(f.get("fade_in_duration")) for f in _refs(first[2], first[3], "audio_fades"))
        fo = sum(_sec(f.get("fade_out_duration")) for f in _refs(last[2], last[3], "audio_fades"))
        # BGM 常被切成多段, 片头 / 片尾的淡入淡出只看第一段和最后一段
        bag.num("audio.bgm.first_fade_in_s", fi, name)
        bag.num("audio.bgm.last_fade_out_s", fo, name)
        bag.flag("structure.bgm_fades_out_at_end", fo > 0, name)
        bag.num("structure.bgm_end_gap_s", dur - (last[0] + last[1]), name)


def analyze_draft(content: dict, name: str, bag: Bag) -> dict:
    """把一个草稿的样本加进 bag, 返回草稿概况。"""
    idx = index_materials(content)
    cv = content.get("canvas_config") or {}
    ctx = {
        "duration": _sec(content.get("duration")),
        "transitions": 0,
        "sub_starts": [],
        "overlay": [],
        "voice_starts": [],
        "bgm": [],
        "effects": [],
        "stickers": [],
    }
    _video_stats(content, idx, name, bag, ctx)
    _text_stats(content, idx, name, bag, ctx)
    _audio_stats(content, idx, name, bag, ctx)
    _fx_stats(content, idx, name, bag, ctx)
    _structure_stats(content, name, bag, ctx)
    counts = Counter(t.get("type") for t in content.get("tracks") or [] if t.get("segments"))
    return {
        "canvas": f"{cv.get('width')}x{cv.get('height')}",
        "duration_s": round(ctx["duration"], 2),
        "tracks": dict(counts),
        "main_shots": len(ctx.get("main_cuts") or []) + 1 if _tracks(content, "video") else 0,
        "subtitles": ctx.get("n_sub", 0),
        "transitions": ctx["transitions"],
    }


# ---------------------------------------------------------------- 扫描与汇总


def exclusion_reason(
    draft_dir: str, content: Optional[dict], prefixes: Iterable[str] = DEFAULT_EXCLUDE_PREFIXES
) -> Optional[str]:
    """该草稿不算手剪样本的原因; 返回 None 表示保留。"""
    name = os.path.basename(draft_dir.rstrip("/\\"))
    if os.path.exists(os.path.join(draft_dir, SKILL_CONTENT)):
        return "skill 生成 (有 draft_info.skill.json)"
    for p in prefixes:
        if name.startswith(p):
            return f"skill 测试 / 生成草稿 (名字以「{p}」开头)"
    if content is None:
        return "无法读取"
    tracks = content.get("tracks") or []
    n_video = sum(len(t.get("segments") or []) for t in tracks if t.get("type") == "video")
    n_other = sum(
        len(t.get("segments") or []) for t in tracks if t.get("type") in ("text", "audio")
    )
    if n_video <= 1 and n_other == 0:
        return "纯录屏 / 只有 1 个视频片段"
    if n_video <= 2 and n_other == 0:
        return "素材片段 (无文字、无音频)"
    return None


def scan_drafts(
    root: str,
    exclude: Iterable[str] = (),
    prefixes: Iterable[str] = DEFAULT_EXCLUDE_PREFIXES,
) -> Tuple[List[dict], List[dict]]:
    """只读扫描草稿目录, 返回 (保留的草稿, 排除的草稿)。"""
    kept, excluded = [], []
    exclude = set(exclude)
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if name.startswith(".") or not os.path.isdir(path):
            continue
        if name in exclude:
            excluded.append({"name": name, "reason": "命令行 --exclude"})
            continue
        try:
            content, source = read_draft_content(path)
        except EncryptedDraftError:
            excluded.append({"name": name, "reason": "已加密且没有明文 template.json / skill 副本"})
            continue
        except (OSError, ValueError) as e:
            excluded.append({"name": name, "reason": f"读取失败: {e}"})
            continue
        why = exclusion_reason(path, content, prefixes)
        if why:
            excluded.append({"name": name, "reason": why})
            continue
        kept.append({"name": name, "path": path, "source": source, "content": content})
    return kept, excluded


def build_profile(drafts: List[dict], overrides: Optional[Dict[str, str]] = None) -> dict:
    """
    drafts: [{"name", "content", "source"?, "path"?}]; overrides: 草稿名 -> 类别。
    返回 profile: {"categories": {类别: {"drafts", "metrics"}}, "drafts": [...]}。
    """
    overrides = overrides or {}
    bags: Dict[str, Bag] = defaultdict(Bag)
    members: Dict[str, List[str]] = defaultdict(list)
    infos = []
    for d in drafts:
        content, name = d["content"], d["name"]
        cat = overrides.get(name) or categorize(content)
        prov = provenance(content, d.get("source", ""))
        info = analyze_draft(content, name, bags[cat])
        info.update(
            {
                "name": name,
                "category": cat,
                "category_zh": CATEGORIES.get(cat, cat),
                "source": d.get("source", ""),
                "path": d.get("path", ""),
                "provenance": prov,
            }
        )
        infos.append(info)
        members[cat].append(name)
    cats = {}
    for cat in CATEGORIES:
        if cat not in bags:
            continue
        gen = [
            i["name"]
            for i in infos
            if i["category"] == cat and i["provenance"]["likely_script_generated"]
        ]
        cats[cat] = {
            "name_zh": CATEGORIES[cat],
            "drafts": members[cat],
            "likely_script_generated": gen,
            "metrics": bags[cat].summary(),
        }
    return {"categories": cats, "drafts": infos}


# ---------------------------------------------------------------- 与 skill 默认值对比

# 类别 -> 对比用的 references 文件 (格式 / 场景 / 风格)。用户的内容是 AI / 科技类, 风格取 tech。
REF_TARGETS = {
    "short_vertical": {"format": "vertical", "scene": "talking_head", "style": "tech"},
    "horizontal_short": {"format": "horizontal", "scene": "drama", "style": None},
    "long_tutorial": {"format": "horizontal", "scene": "tutorial", "style": "tech"},
}


def load_refs(refs_dir: str = REFS_DIR) -> dict:
    out = {}
    for sub in ("formats", "scenes", "styles"):
        d = os.path.join(refs_dir, sub)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.endswith(".json"):
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    out[f"{sub}/{fn}"] = json.load(f)
    return out


def _get(d, path: str):
    for p in path.split("."):
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


def strength(stat: dict, gen_share: float) -> str:
    """证据强度: 看样本数、草稿数, 来源多为脚本生成时降一级。"""
    n = stat.get("n", 0)
    nd = len(stat.get("drafts") or [])
    lv = 2 if (nd >= 3 and n >= 20) else (1 if (nd >= 2 or n >= 10) else 0)
    if gen_share >= 0.5:
        lv = max(0, lv - 1)
    return ["弱", "中", "强"][lv]


# (说明, 用户指标路径, 取哪个统计量, 引用文件角色, 字段, 比较方式, 容差)
COMPARE_SPECS = [
    ("时长 (对上限)", "structure.duration_s", "median", "scene", "duration.max", "max", None),
    ("时长 (对理想值)", "structure.duration_s", "median", "scene", "duration.ideal", "scalar", 0.3),
    (
        "镜头时长 (主轨)",
        "pacing.shot_duration_s",
        "median",
        "scene",
        "pacing.avg_shot",
        "range",
        None,
    ),
    (
        "画面变化间隔 (所有视频轨)",
        "pacing.visual_change_interval_s",
        "median",
        "scene",
        "pacing.avg_shot",
        "range",
        None,
    ),
    (
        "关键帧运镜频率 (次/分钟)",
        "keyframes.moves_per_min",
        "median",
        "scene",
        "camera.per_minute",
        "range",
        None,
    ),
    ("字幕字号", "subtitle.size", "median", "format", "subtitle.size", "scalar", 0.15),
    (
        "字幕字号 (排版网格)",
        "subtitle.size",
        "median",
        "format",
        "subtitle_layout.size",
        "scalar",
        0.15,
    ),
    (
        "字幕位置 transform_y",
        "subtitle.transform_y",
        "median",
        "format",
        "slots.subtitle.y",
        "abs",
        0.08,
    ),
    (
        "字幕每条字数 (P90 对上限)",
        "subtitle.chars",
        "p90",
        "scene",
        "subtitles.max_chars_zh",
        "max",
        None,
    ),
    (
        "字幕每条时长 (P90 对上限)",
        "subtitle.duration_s",
        "p90",
        "scene",
        "subtitles.max_dur",
        "max",
        None,
    ),
    ("字幕字号 (风格)", "subtitle.size", "median", "style", "roles.subtitle.size", "scalar", 0.15),
    ("字幕字体", "subtitle.font", "top", "style", "roles.subtitle.font", "name", None),
    ("字幕描边", "subtitle.stroke", "share", "style", "roles.subtitle.border", "bool", None),
    ("标题 / 关键词字号", "overlay.size", "median", "style", "roles.title.size", "scalar", 0.2),
    ("标题入场动画", "overlay.anim_in", "top", "style", "roles.title.anim_in", "name", None),
    ("转场类型", "transitions.name", "top", "style", "cut_transition.name", "name", None),
    (
        "转场时长",
        "transitions.duration_s",
        "median",
        "style",
        "cut_transition.duration",
        "scalar",
        0.25,
    ),
    ("BGM 音量", "audio.bgm.volume", "median", "scene", "audio.bgm_volume", "scalar", 0.25),
    ("BGM 音量 (风格)", "audio.bgm.volume", "median", "style", "bgm.volume", "scalar", 0.25),
    ("BGM 片头淡入", "audio.bgm.first_fade_in_s", "median", "style", "bgm.fade_in", "scalar", 0.3),
    ("BGM 片尾淡出", "audio.bgm.last_fade_out_s", "median", "style", "bgm.fade_out", "scalar", 0.3),
    (
        "人声 / 对白音量",
        "audio.voice.volume|audio.tts.volume",
        "median",
        "scene",
        "audio.dialogue_volume",
        "scalar",
        0.15,
    ),
    ("音效个数 (每条片子)", "audio.sfx.count", "p90", "scene", "audio.sfx_max", "max", None),
    ("音效音量", "audio.sfx.volume", "median", "style", "sfx.volume", "scalar", 0.3),
    ("特效个数 (每条片子)", "effects.count", "p90", "scene", "effects.max", "max", None),
    ("调色滤镜", "grade.filter_count", "median", "style", "grade.filter.name", "presence", None),
    (
        "运镜幅度 (推拉倍数-1)",
        "keyframes.zoom_ratio",
        "median",
        "style",
        "camera.amount",
        "zoom",
        0.3,
    ),
    ("画中画缩放", "pip.scale", "median", "format", "pip.scale", "scalar", 0.25),
]


def _stat_value(stat: dict, field: str):
    if field == "median" and stat.get("draft_median") is not None:
        return stat["draft_median"]
    if field == "top":
        return stat["top"][0]["value"] if stat.get("top") else None
    return stat.get(field)


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.3g}" if abs(v) < 10 else f"{v:.1f}"
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    return str(v)


def _judge(kind: str, user, default, stat: dict, tol) -> Tuple[bool, Any]:
    """返回 (是否算差异, 建议值)。"""
    if user is None or default is None:
        return False, None
    if kind == "scalar":
        if not isinstance(default, (int, float)) or default == 0:
            return False, None
        return abs(user - default) / abs(default) > tol, round(user, 2)
    if kind == "abs":
        return abs(user - default) > tol, round(user, 2)
    if kind == "zoom":
        amt = user - 1.0
        return abs(amt - default) / max(abs(default), 1e-6) > tol, round(amt, 2)
    if kind == "range":
        lo, hi = default[0], default[-1]
        sug = [round(stat.get("p25", user), 1), round(stat.get("p75", user), 1)]
        if sug[0] == sug[1]:
            sug = [round(stat.get("p10", user), 1), round(stat.get("p90", user), 1)]
        if sug[0] == sug[1]:
            sug = [sug[0], round(sug[0] + 1, 1)]
        return not (lo <= user <= hi), sug
    if kind == "max":
        sug = int(math.ceil(user)) if isinstance(default, int) else round(user, 1)
        return user > default * 1.15, sug
    if kind == "name":
        top = stat["top"][0] if stat.get("top") else {}
        return user != default and top.get("share", 0) >= 0.4, user
    if kind == "bool":
        share = stat.get("share", 0)
        return (bool(default) and share < 0.5) or (not default and share > 0.5), share >= 0.5
    if kind == "presence":
        return bool(default) and user == 0, "可选 / 默认不加 (用户从不用滤镜)"
    return False, None


def compare_with_defaults(profile: dict, refs: Optional[dict] = None) -> List[dict]:
    """拿学到的统计对比 references 下的默认值, 只给建议, 不改文件。"""
    refs = refs if refs is not None else load_refs()
    gen = {d["name"] for d in profile["drafts"] if d["provenance"]["likely_script_generated"]}
    out = []
    for cat, c in profile["categories"].items():
        tgt = REF_TARGETS.get(cat, {})
        for label, mpath, field, role, rfield, kind, tol in COMPARE_SPECS:
            ref_id = tgt.get(role)
            if not ref_id:
                continue
            fname = f"{role}s/{ref_id}.json"
            ref = refs.get(fname)
            default = _get(ref, rfield) if ref else None
            stat = None
            for p in mpath.split("|"):
                stat = _get(c["metrics"], p)
                if stat:
                    break
            if not stat or not stat.get("n"):
                continue
            user = _stat_value(stat, field)
            diff, sug = _judge(kind, user, default, stat, tol)
            if not diff:
                continue
            note = ""
            safe_bottom = _get(refs.get(f"formats/{tgt.get('format')}.json"), "safe.bottom")
            if rfield == "slots.subtitle.y" and safe_bottom is not None and user < safe_bottom:
                note = (
                    f"用户位置低于该格式安全区下沿 safe.bottom={safe_bottom}, "
                    "可能被平台底部文案遮挡; 建议先核对成片再决定是否改"
                )
            drafts = stat.get("drafts") or []
            gshare = sum(1 for d in drafts if d in gen) / len(drafts) if drafts else 0.0
            out.append(
                {
                    "category": cat,
                    "item": label,
                    "user": user,
                    "user_measure": field,
                    "user_stat": {
                        k: stat.get(k)
                        for k in ("draft_median", "median", "p10", "p90", "share")
                        if k in stat
                    },
                    "default": default,
                    "file": f"references/{fname}",
                    "field": rfield,
                    "suggested": sug,
                    "n": stat.get("n"),
                    "n_drafts": len(drafts),
                    "drafts": drafts,
                    "script_generated_share": round(gshare, 2),
                    "strength": strength(stat, gshare),
                    "note": note,
                }
            )
    return out


# ---------------------------------------------------------------- 中文摘要


def _n(stat: Optional[dict], unit: str = "", pct: bool = False) -> str:
    if not stat or not stat.get("n"):
        return "无样本"
    if "top" in stat:
        tops = " / ".join(f"{t['value']} {t['share']:.0%}" for t in stat["top"][:4])
        return f"{tops} (n={stat['n']}, {len(stat['drafts'])} 个草稿)"
    if "share" in stat:
        return f"{stat['share']:.0%} (n={stat['n']}, {len(stat['drafts'])} 个草稿)"
    f = (lambda v: f"{v:.0%}") if pct else (lambda v: f"{_fmt(v)}{unit}")
    return (
        f"中位 {f(stat['median'])} (P10 {f(stat['p10'])} / P90 {f(stat['p90'])}), "
        f"n={stat['n']}, {len(stat['drafts'])} 个草稿"
    )


# (标题, [(说明, 指标路径, 单位)])
MD_SECTIONS = [
    (
        "节奏",
        [
            ("片长", "structure.duration_s", "s"),
            ("镜头时长 (主轨)", "pacing.shot_duration_s", "s"),
            ("每分钟切点 (主轨)", "pacing.cuts_per_min", ""),
            ("每分钟画面变化 (所有视频轨)", "pacing.visual_changes_per_min", ""),
            ("平均画面变化间隔 (所有视频轨)", "pacing.visual_change_interval_s", "s"),
            ("主轨图片占比", "pacing.photo_share", "%"),
        ],
    ),
    (
        "字幕",
        [
            ("字号 (size x scale)", "subtitle.size", ""),
            ("位置 transform_y", "subtitle.transform_y", ""),
            ("字体", "subtitle.font", ""),
            ("颜色", "subtitle.color", ""),
            ("描边", "subtitle.stroke", ""),
            ("描边宽度", "subtitle.stroke_width", ""),
            ("背景", "subtitle.background", ""),
            ("每条字数", "subtitle.chars", ""),
            ("每条时长", "subtitle.duration_s", "s"),
            ("每秒字数", "subtitle.chars_per_s", ""),
            ("每分钟条数", "subtitle.per_min", ""),
            ("用字幕模板", "subtitle.uses_template", ""),
            ("字幕模板", "subtitle.template", ""),
            ("用花字", "subtitle.uses_flower", ""),
            ("花字", "subtitle.flower", ""),
            ("入场动画", "subtitle.anim_in", ""),
        ],
    ),
    (
        "标题 / 关键词文字",
        [
            ("字号", "overlay.size", ""),
            ("位置 transform_y", "overlay.transform_y", ""),
            ("字体", "overlay.font", ""),
            ("颜色", "overlay.color", ""),
            ("描边", "overlay.stroke", ""),
            ("每条时长", "overlay.duration_s", "s"),
            ("每条字数", "overlay.chars", ""),
            ("每分钟条数", "overlay.per_min", ""),
            ("用文字模板", "overlay.uses_template", ""),
            ("文字模板", "overlay.template", ""),
            ("模板缩放", "overlay.template_scale", ""),
            ("用花字", "overlay.uses_flower", ""),
            ("花字", "overlay.flower", ""),
            ("气泡 / 形状", "overlay.bubble", ""),
            ("入场动画", "overlay.anim_in", ""),
            ("出场动画", "overlay.anim_out", ""),
            ("每分钟花字 (含字幕)", "text.flower_per_min", ""),
        ],
    ),
    (
        "转场",
        [
            ("类型", "transitions.name", ""),
            ("时长", "transitions.duration_s", "s"),
            ("每个切点用转场的比例", "transitions.per_cut", "%"),
        ],
    ),
    (
        "特效 / 贴纸 / 调色",
        [
            ("特效", "effects.name", ""),
            ("特效位置", "effects.position", ""),
            ("每条片子特效数", "effects.count", ""),
            ("贴纸", "stickers.name", ""),
            ("每分钟贴纸", "stickers.per_min", ""),
            ("贴纸位置", "stickers.position", ""),
            ("滤镜数", "grade.filter_count", ""),
            ("调节数", "grade.adjust_count", ""),
            ("画面入场动画", "video_animation.in", ""),
            ("画面出场动画", "video_animation.out", ""),
        ],
    ),
    (
        "变速与关键帧",
        [
            ("视频片段变速比例", "speed.changed", ""),
            ("变速倍数", "speed.value", "x"),
            ("关键帧运镜类型", "keyframes.move", ""),
            ("推拉倍数", "keyframes.zoom_ratio", "x"),
            ("推拉时长", "keyframes.zoom_dur_s", "s"),
            ("每分钟运镜", "keyframes.moves_per_min", ""),
            ("主轨静态缩放比例", "pacing.main_scaled", ""),
        ],
    ),
    (
        "音频",
        [
            ("BGM 音量", "audio.bgm.volume", ""),
            ("BGM 音量 dB", "audio.bgm.volume_db", " dB"),
            ("BGM 关键帧压低 (低 / 高)", "audio.bgm.duck_low", ""),
            ("BGM 关键帧高位", "audio.bgm.duck_high", ""),
            ("BGM 片头淡入 (第一段)", "audio.bgm.first_fade_in_s", "s"),
            ("BGM 片尾淡出 (最后一段)", "audio.bgm.last_fade_out_s", "s"),
            ("BGM 每段淡入", "audio.bgm.fade_in_s", "s"),
            ("BGM 每段淡出", "audio.bgm.fade_out_s", "s"),
            ("BGM 覆盖率", "audio.bgm.coverage", "%"),
            ("BGM 曲目", "audio.bgm.name", ""),
            ("剪映 TTS 音量", "audio.tts.volume", ""),
            ("剪映 TTS 语速", "audio.tts.speed", "x"),
            ("TTS 音色", "audio.tts.voice_name", ""),
            ("导入配音音量", "audio.voice.volume", ""),
            ("视频原声音量", "audio.original.volume", ""),
            ("主轨视频静音", "audio.clip_audio_muted", ""),
            ("音效个数", "audio.sfx.count", ""),
            ("每分钟音效", "audio.sfx.per_min", ""),
            ("音效音量", "audio.sfx.volume", ""),
            ("音效落在切点 ±0.5s", "audio.sfx.on_cut", ""),
            ("音效", "audio.sfx.name", ""),
            ("变声", "audio.voice_effect", ""),
            ("响度统一目标", "audio.loudness_target_lufs", " LUFS"),
        ],
    ),
    (
        "画中画 / 蒙版",
        [
            ("叠加轨数", "pip.overlay_tracks", ""),
            ("叠加类型", "pip.overlay_kind", ""),
            ("小窗缩放 (scale ≤ 0.6)", "pip.scale", ""),
            ("缩小 / 放大的叠加层缩放", "pip.inset_scale", ""),
            ("小窗 x", "pip.x", ""),
            ("小窗 y", "pip.y", ""),
            ("蒙版", "pip.mask", ""),
            ("抠像", "pip.chroma", ""),
            ("画布背景", "pip.canvas_background", ""),
        ],
    ),
    (
        "片头片尾",
        [
            ("前 3 秒有标题", "structure.title_in_first_3s", ""),
            ("第一条标题出现", "structure.first_overlay_start_s", "s"),
            ("第一条字幕出现", "structure.first_subtitle_start_s", "s"),
            ("第一句人声出现", "structure.first_voice_start_s", "s"),
            ("首镜时长", "structure.first_shot_s", "s"),
            ("尾镜时长", "structure.last_shot_s", "s"),
            ("开头有特效", "structure.effect_at_opening", ""),
            ("开头有贴纸", "structure.sticker_at_opening", ""),
            ("最后 5 秒有标题 / CTA 文字", "structure.overlay_in_last_5s", ""),
            ("BGM 结尾淡出", "structure.bgm_fades_out_at_end", ""),
        ],
    ),
]


def render_markdown(profile: dict, diffs: List[dict], excluded: List[dict]) -> str:
    L = ["# 剪辑风格画像 (从手剪草稿学习)", ""]
    L.append(f"生成时间: {profile.get('generated_at', '')}  ")
    L.append(f"草稿目录: `{profile.get('root', '')}` (只读)  ")
    L.append(
        "单位: 时间秒; 字号 = 剪映 size x 片段缩放; 位置 = 剪映 transform (中心 0, 上为正); "
        "音量线性 (1.0 = 0 dB)。每项都写了样本数 n 和草稿数。"
    )
    L += [
        "",
        "## 样本",
        "",
        "| 草稿 | 类别 | 画布 | 时长 | 来源 | 推断 |",
        "|---|---|---|---|---|---|",
    ]
    for d in profile["drafts"]:
        p = d["provenance"]
        tag = (
            f"疑似脚本生成 (id {p['script_id_ratio']:.0%})"
            if p["likely_script_generated"]
            else "手剪"
        )
        L.append(
            f"| {d['name']} | {d['category_zh']} | {d['canvas']} | {d['duration_s']:.0f}s | "
            f"{d['source']} | {tag} |"
        )
    if excluded:
        L += ["", "排除: " + "; ".join(f"{e['name']} ({e['reason']})" for e in excluded)]
    for cat, c in profile["categories"].items():
        L += ["", f"## {c['name_zh']} ({len(c['drafts'])} 个草稿)", ""]
        L.append("草稿: " + "、".join(c["drafts"]))
        if c["likely_script_generated"]:
            L.append("")
            L.append(
                "注意: "
                + "、".join(c["likely_script_generated"])
                + " 疑似脚本生成后再在剪映里修改, 数值可能反映脚本设定而不是手剪习惯。"
            )
        m = c["metrics"]
        for title, rows in MD_SECTIONS:
            lines = []
            for label, path, unit in rows:
                st = _get(m, path)
                if st and st.get("n"):
                    lines.append(f"- {label}: {_n(st, '' if unit == '%' else unit, unit == '%')}")
            if lines:
                L += ["", f"### {title}", ""] + lines
    L += ["", "## 与 skill 默认值的差异", ""]
    L.append(
        "只是建议, 没有改 references 下的文件。证据强度: 强 = ≥3 个草稿且 ≥20 个样本; "
        "中 = ≥2 个草稿或 ≥10 个样本; 弱 = 其余; 来源一半以上疑似脚本生成时降一级。"
    )
    if not diffs:
        L += ["", "没有发现明显差异。"]
    for cat in CATEGORIES:
        ds = [d for d in diffs if d["category"] == cat]
        if not ds:
            continue
        L += ["", f"### {CATEGORIES[cat]}", ""]
        for d in sorted(ds, key=lambda x: ["强", "中", "弱"].index(x["strength"])):
            us = d["user_stat"]
            extra = ""
            if "p10" in us:
                how = {"median": "各草稿中位数的中位数", "p90": "全部样本 P90"}.get(
                    d.get("user_measure"), d.get("user_measure")
                )
                extra = (
                    f" ({how}; 全部样本中位 {_fmt(us.get('median'))}, "
                    f"P10 {_fmt(us['p10'])} / P90 {_fmt(us['p90'])})"
                )
            elif "share" in us:
                extra = f" (占比 {us['share']:.0%})"
            gen = (
                f"; 其中 {d['script_generated_share']:.0%} 的草稿疑似脚本生成"
                if d["script_generated_share"]
                else ""
            )
            L.append(
                f"- **{d['item']}** [{d['strength']}]: 用户 {_fmt(d['user'])}{extra}; "
                f"skill 默认 {_fmt(d['default'])}。建议改 `{d['file']}` 的 `{d['field']}` → "
                f"{_fmt(d['suggested'])}。证据: n={d['n']}, {d['n_drafts']} 个草稿{gen}。"
                + (f" 注: {d['note']}。" if d.get("note") else "")
            )
    L += [
        "",
        "## 数据局限",
        "",
        "- 剪映 11.x 精简格式 (is_minimal_draft) 会省略默认值: 没写 volume / speed / scale 按 1, "
        "没写淡入淡出按 0。",
        "- 很多字幕和标题用了字幕模板 / 文字模板, 模板内部字号和颜色由模板决定, 只统计了模板名和缩放。",
        "- 字体常常只记录了文件路径 (系统默认字体), 读不到面板里的字体名。",
        "- 滤镜 / 调节如果做在剪映的「调节」面板全局层, 只有出现在轨道上的才统计得到。",
        "- 疑似脚本生成的草稿 (素材 id 为 32 位十六进制、英文轨道名) 代表的是当时脚本的参数, 不完全是手剪习惯。",
    ]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- CLI


def run(
    root: str,
    out_dir: Optional[str],
    exclude: Iterable[str] = (),
    overrides: Optional[Dict[str, str]] = None,
    refs_dir: str = REFS_DIR,
) -> dict:
    """扫描 -> 汇总 -> 对比; out_dir 不为 None 时写 style_profile.json / .md。"""
    kept, excluded = scan_drafts(root, exclude)
    profile = build_profile(kept, overrides)
    profile = {
        "version": 1,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "root": root,
        "units": {
            "time": "s",
            "size": "剪映 TextStyle.size x clip.scale.x",
            "position": "剪映 transform: 中心 0, 右 / 上为正, 半个画布为 1",
            "volume": "线性增益 (1.0 = 0 dB); volume_db 为 dB",
        },
        "excluded": excluded,
        **profile,
    }
    diffs = compare_with_defaults(profile, load_refs(refs_dir))
    profile["diffs_vs_skill_defaults"] = diffs
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "style_profile.json"), "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=1)
        with open(os.path.join(out_dir, "style_profile.md"), "w", encoding="utf-8") as f:
            f.write(render_markdown(profile, diffs, excluded))
    return profile


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从手剪草稿学习剪辑风格 (只读草稿目录)")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="剪映草稿目录")
    ap.add_argument("--out-dir", default=DEFAULT_OUT, help="输出目录 (个人数据, 放仓库外)")
    ap.add_argument("--exclude", action="append", default=[], help="额外排除的草稿名, 可重复")
    ap.add_argument(
        "--category",
        action="append",
        default=[],
        help="手动指定类别: 草稿名=short_vertical|horizontal_short|long_tutorial",
    )
    ap.add_argument("--no-write", action="store_true", help="只打印, 不写文件")
    ap.add_argument("--json", action="store_true", help="打印完整 JSON")
    args = ap.parse_args(argv)
    overrides = {}
    for item in args.category:
        name, _, cat = item.rpartition("=")
        if cat not in CATEGORIES or not name:
            ap.error(f"--category 格式应为 草稿名=类别: {item}")
        overrides[name] = cat
    if not os.path.isdir(args.root):
        print(f"找不到草稿目录: {args.root}", file=sys.stderr)
        return 2
    prof = run(args.root, None if args.no_write else args.out_dir, args.exclude, overrides)
    if args.json:
        print(json.dumps(prof, ensure_ascii=False, indent=1))
    else:
        for cat, c in prof["categories"].items():
            print(f"{c['name_zh']}: {len(c['drafts'])} 个草稿 ({'、'.join(c['drafts'])})")
        print(
            f"排除 {len(prof['excluded'])} 个; 与默认值的差异 {len(prof['diffs_vs_skill_defaults'])} 条"
        )
        if not args.no_write:
            print(f"已写入 {os.path.join(args.out_dir, 'style_profile.json')} 和 style_profile.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
