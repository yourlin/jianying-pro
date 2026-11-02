"""
音效设计: 把时间线事件 (转场、文字出现、强调词、卡点、结尾 ...) 按规则表映射成音效铺设计划,
限制密度防止吵, 并可写入草稿音频轨。音效来自 data/sfx_library.csv (build_sfx_library.py 生成) 和本地音效目录。

规则表 (事件 -> 音效分类、音量、偏移): 例如转场配 whoosh 并提前 0.1s (嗖声的能量峰落在切点上),
强调词配 hit / ding, 文字出现配 pop, 卡点配轻 click; 见 DEFAULT_RULES, 可用 --rules 覆盖。
方法论: references/methodology/transitions_fx.md (少即是多, 只跟可见事件同步, 音效 -18 ~ -12 dB)。

用法:
    python scripts/sfx_designer.py rules                                 # 查看规则表
    python scripts/sfx_designer.py plan --events events.json -o sfx_plan.json
    python scripts/sfx_designer.py plan --draft 草稿名 [--root 草稿目录]    # 从草稿读转场 / 文字 / 结尾
    python scripts/sfx_designer.py plan --analysis analysis.json --keywords 重点,注意 --offset 0
    python scripts/sfx_designer.py plan --beats beats.json --draft 草稿名 --write   # 生成并写入草稿
    python scripts/sfx_designer.py apply --plan sfx_plan.json --draft 草稿名 [--track SFX]

事件文件: [{"type": "transition", "time": 3.2}, {"type": "emphasis", "time": 5.0, "word": "免费"}, ...]
或 {"events": [...], "duration": 30}。可选字段: category (覆盖分类)、sfx_id (指定音效)、priority、
volume_db、offset、duration (typing 等持续型事件的时长)。
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
for _p in (_SCRIPTS, os.path.join(_SCRIPTS, "vendor")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from build_sfx_library import LIBRARY, load_library  # noqa: E402
from utils.cli_protocol import emit_result, make_result  # noqa: E402

# 事件 -> 规则。categories 按偏好排序; offset 秒 (负数 = 提前); align: start = 音效起点对齐事件,
# end = 音效终点对齐事件 (riser / 结尾); priority 越大越重要 (密度冲突时保留);
# max_dur: 音效超过此长度会被截断 (末尾淡出); quiet: 人声段内跳过 (卡点这类装饰性音效)。
DEFAULT_RULES: Dict[str, Optional[dict]] = {
    "hook": {
        "categories": ["hit", "boom"],
        "volume_db": -12,
        "offset": 0.0,
        "priority": 5,
        "max_dur": 1.5,
    },
    "emphasis": {
        "categories": ["hit", "ding"],
        "volume_db": -14,
        "offset": -0.03,
        "priority": 4,
        "max_dur": 1.2,
    },
    "transition": {
        "categories": ["whoosh"],
        "volume_db": -15,
        "offset": -0.1,
        "priority": 3,
        "max_dur": 1.5,
    },
    "title": {
        "categories": ["hit", "pop"],
        "volume_db": -15,
        "offset": 0.0,
        "priority": 3,
        "max_dur": 1.2,
    },
    "reveal": {
        "categories": ["riser"],
        "volume_db": -16,
        "offset": 0.0,
        "align": "end",
        "priority": 3,
        "max_dur": 4.0,
    },
    "ending": {
        "categories": ["ding", "applause"],
        "volume_db": -14,
        "offset": -0.3,
        "align": "end",
        "priority": 3,
        "max_dur": 2.0,
    },
    "text_in": {
        "categories": ["pop", "click"],
        "volume_db": -18,
        "offset": 0.0,
        "priority": 2,
        "max_dur": 0.8,
    },
    "point": {
        "categories": ["ding", "pop"],
        "volume_db": -16,
        "offset": 0.0,
        "priority": 2,
        "max_dur": 1.0,
    },
    "zoom": {
        "categories": ["whoosh"],
        "volume_db": -18,
        "offset": -0.05,
        "priority": 2,
        "max_dur": 0.8,
    },
    "photo": {
        "categories": ["camera"],
        "volume_db": -15,
        "offset": 0.0,
        "priority": 3,
        "max_dur": 1.2,
    },
    "typing": {
        "categories": ["typing"],
        "volume_db": -20,
        "offset": 0.0,
        "priority": 2,
        "max_dur": 6.0,
        "fit_event": True,
    },
    "money": {
        "categories": ["cash", "ding"],
        "volume_db": -16,
        "offset": 0.0,
        "priority": 2,
        "max_dur": 1.5,
    },
    "error": {
        "categories": ["error"],
        "volume_db": -16,
        "offset": 0.0,
        "priority": 3,
        "max_dur": 1.0,
    },
    "glitch": {
        "categories": ["glitch"],
        "volume_db": -18,
        "offset": -0.05,
        "priority": 2,
        "max_dur": 1.0,
    },
    "page": {
        "categories": ["page", "whoosh"],
        "volume_db": -16,
        "offset": -0.05,
        "priority": 2,
        "max_dur": 1.2,
    },
    "beat": {
        "categories": ["click", "hit"],
        "volume_db": -22,
        "offset": 0.0,
        "priority": 1,
        "max_dur": 0.5,
        "quiet": True,
    },
    "cut": None,  # 普通硬切默认不配音效 (口播跳切太密); 需要时用 --rules '{"cut": {...}}' 打开
}

# 事件类型别名 (中文 / 其他 agent 的事件名)
EVENT_ALIASES = {
    "转场": "transition",
    "transitions": "transition",
    "文字": "text_in",
    "text": "text_in",
    "字幕": "text_in",
    "标题": "title",
    "强调": "emphasis",
    "强调词": "emphasis",
    "金句": "emphasis",
    "keyword": "emphasis",
    "卡点": "beat",
    "downbeat": "beat",
    "鼓点": "beat",
    "结尾": "ending",
    "end": "ending",
    "outro": "ending",
    "开场": "hook",
    "钩子": "hook",
    "揭晓": "reveal",
    "拍照": "photo",
    "定格": "photo",
    "打字": "typing",
    "要点": "point",
    "缩放": "zoom",
    "punch_in": "zoom",
    "错误": "error",
    "翻页": "page",
    "价格": "money",
    "切点": "cut",
    "scene": "cut",
}

DEFAULT_MIN_GAP = 0.6  # 任意两个音效起点最少间隔 (秒)
DEFAULT_SAME_GAP = 1.5  # 同一分类音效最少间隔
DEFAULT_MAX_PER_WINDOW = 4  # 每 10 秒最多几个音效
WINDOW = 10.0
CANDIDATE_POOL = 5  # 每个分类轮换使用的候选数量 (避免同一个音效反复出现)


def db_to_gain(db: float) -> float:
    return 10 ** (float(db) / 20)


def normalize_type(t: str) -> str:
    t = str(t or "").strip()
    return EVENT_ALIASES.get(t, EVENT_ALIASES.get(t.lower(), t.lower()))


def merge_rules(overrides: Optional[dict] = None) -> Dict[str, Optional[dict]]:
    """规则表 = 默认规则 + 覆盖 (同名事件整体替换字段; 值为 null 表示关闭该事件)。"""
    rules = {k: (dict(v) if v else None) for k, v in DEFAULT_RULES.items()}
    for k, v in (overrides or {}).items():
        k = normalize_type(k)
        if v is None:
            rules[k] = None
        else:
            base = rules.get(k) or {
                "categories": [],
                "volume_db": -16,
                "offset": 0.0,
                "priority": 2,
                "max_dur": 1.5,
            }
            rules[k] = {**base, **v}
    return rules


# ----------------------------------------------------------------- 事件来源


def load_events(src: Union[str, dict, list]) -> Tuple[List[dict], Optional[float]]:
    """读事件文件 / 对象 -> (事件列表, 时间线总时长或 None)。"""
    if isinstance(src, str):
        with open(src, "r", encoding="utf-8") as f:
            src = json.load(f)
    duration = None
    if isinstance(src, dict):
        duration = src.get("duration")
        src = src.get("events") or []
    out = []
    for e in src or []:
        if "time" not in e and "start" in e:
            e = {**e, "time": e["start"]}
        if "time" in e and e.get("type"):
            out.append({**e, "time": float(e["time"]), "type": normalize_type(e["type"])})
    return out, (float(duration) if duration else None)


def events_from_beats(
    src: Union[str, dict, list], strong_only: bool = True, offset: float = 0.0
) -> List[dict]:
    """
    节拍 -> beat 事件。接受 [秒, ...]、[{"time"/"t"}, ...]、或 {"downbeats": [...], "beats": [...]}
    (strong_only 时优先用 downbeats / strong / accents 强拍)。
    """
    if isinstance(src, str):
        with open(src, "r", encoding="utf-8") as f:
            src = json.load(f)
    if isinstance(src, dict):
        keys = (
            ("downbeats", "strong_beats", "strong", "accents", "beats")
            if strong_only
            else ("beats",)
        )
        src = next((src[k] for k in keys if src.get(k)), [])
    out = []
    for b in src or []:
        t = (
            b
            if isinstance(b, (int, float))
            else (b.get("time", b.get("t")) if isinstance(b, dict) else None)
        )
        if t is not None:
            out.append({"type": "beat", "time": float(t) + offset})
    return out


def events_from_analysis(
    analysis: Union[str, dict],
    keywords: Sequence[str] = (),
    offset: float = 0.0,
    scenes_as_cuts: bool = True,
) -> List[dict]:
    """analysis.json -> 事件: 转写里出现 keywords 的词 -> emphasis; 镜头切换 (scenes) -> cut。"""
    if isinstance(analysis, str):
        with open(analysis, "r", encoding="utf-8") as f:
            analysis = json.load(f)
    out = []
    kws = [k for k in (keywords or []) if k]
    for seg in analysis.get("transcript") or []:
        if not kws:
            break
        words = seg.get("words") or []
        hit_words = set()
        for w in words:
            text = str(w.get("w", "")).strip()
            for k in kws:
                if k in text and k not in hit_words:
                    out.append({"type": "emphasis", "time": float(w["s"]) + offset, "word": k})
                    hit_words.add(k)
        # 没有词级时间戳 (或关键词跨了多个词): 按字符位置在句子内插值
        text = seg.get("text") or ""
        for k in kws:
            if k in hit_words or k not in text:
                continue
            s, e = float(seg["start"]), float(seg["end"])
            pos = text.index(k) / max(1, len(text))
            out.append(
                {"type": "emphasis", "time": round(s + (e - s) * pos, 3) + offset, "word": k}
            )
    if scenes_as_cuts:
        out += [{"type": "cut", "time": float(t) + offset} for t in analysis.get("scenes") or []]
    return out


def _is_subtitle_track(name: str, segs: list, total: float) -> bool:
    n = str(name).lower()
    if any(k in n for k in ("字幕", "subtitle", "caption", "srt", "歌词", "lyric")):
        return True
    cover = sum((s.target_timerange.duration for s in segs)) / 1e6
    return len(segs) >= 8 and total > 0 and cover / total > 0.4


def events_from_draft(project_or_script) -> Tuple[List[dict], float]:
    """
    从草稿推事件: 主视频轨上带转场的切点 -> transition, 其他切点 -> cut; 非字幕文字片段出现 -> text_in
    (开头 0.5s 内的第一条 -> title); 时间线结尾 -> ending。返回 (事件, 时间线总时长秒)。
    """
    from core.mix_ops import iter_tracks

    script = getattr(project_or_script, "script", project_or_script)
    tracks = list(iter_tracks(script))
    total = 0.0
    for _n, _k, segs in tracks:
        for s in segs:
            total = max(total, (s.target_timerange.start + s.target_timerange.duration) / 1e6)
    imported = getattr(script, "imported_materials", {}) or {}
    trans_ids = {m.get("id") for m in imported.get("transitions", []) or []}

    def has_transition(seg) -> bool:
        if getattr(seg, "transition", None) is not None:
            return True
        raw = getattr(seg, "raw_data", None)
        return bool(raw and trans_ids.intersection(raw.get("extra_material_refs") or []))

    events: List[dict] = []
    videos = [(n, segs) for n, k, segs in tracks if k == "video" and segs]
    if videos:
        _name, main = videos[0]  # 第一条视频轨即主轨
        main = sorted(main, key=lambda s: s.target_timerange.start)
        for prev, cur in zip(main, main[1:]):
            t = cur.target_timerange.start / 1e6
            events.append(
                {"type": "transition" if has_transition(prev) else "cut", "time": round(t, 3)}
            )
    first_title = True
    for name, kind, segs in tracks:
        if kind != "text" or not segs or _is_subtitle_track(name, segs, total):
            continue
        for s in sorted(segs, key=lambda x: x.target_timerange.start):
            t = s.target_timerange.start / 1e6
            typ = "title" if first_title and t < 0.5 else "text_in"
            first_title = False
            events.append({"type": typ, "time": round(t, 3), "track": name})
    if total > 2:
        events.append({"type": "ending", "time": round(total, 3)})
    return events, total


# ----------------------------------------------------------------- 选音效 + 密度控制


def _window_ok(times: List[float], t: float, n_max: int, window: float) -> bool:
    ts = sorted(times + [t])
    for i, a in enumerate(ts):
        if sum(1 for b in ts[i:] if b < a + window) > n_max:
            return False
    return True


def _in_ranges(t: float, ranges: Iterable[Tuple[float, float]]) -> bool:
    return any(s <= t <= e for s, e in ranges or [])


class SfxPicker:
    """按分类挑音效: 时长合适优先、本地优先, 在前几个候选里轮换, 避免同一个音效反复出现。"""

    def __init__(self, library: List[dict], allow_vip: bool = False):
        self.by_cat: Dict[str, List[dict]] = {}
        self.by_id = {}
        for r in library:
            self.by_id[str(r.get("id"))] = r
            if str(r.get("available")) != "1" and not allow_vip:
                continue
            if r.get("is_vip") == "1" and not allow_vip:
                continue
            self.by_cat.setdefault(r.get("category", ""), []).append(r)
        self.used: Dict[str, int] = {}

    def pick(
        self,
        categories: Sequence[str],
        max_dur: Optional[float],
        min_dur: float = 0.0,
        want_dur: Optional[float] = None,
    ) -> Optional[dict]:
        for cat in categories:
            rows = self.by_cat.get(cat) or []
            if not rows:
                continue

            def key(r):
                d = r.get("duration_s")
                d = d if isinstance(d, (int, float)) else 1.0
                fits = (max_dur is None or d <= max_dur) and d >= min_dur
                target = want_dur if want_dur else (max_dur or 1.0) * 0.6
                return (not fits, r.get("source") != "local", abs(d - target))

            pool, names = [], set()
            for r in sorted(rows, key=key):  # 同名音效 (剪映里常有重复上架) 只算一个
                if r.get("name") not in names:
                    names.add(r.get("name"))
                    pool.append(r)
                if len(pool) >= CANDIDATE_POOL:
                    break
            # 在候选里挑用得最少的 (同样少时按排序先后), 让同类音效轮换
            choice = min(
                enumerate(pool), key=lambda x: (self.used.get(str(x[1].get("id")), 0), x[0])
            )[1]
            self.used[str(choice.get("id"))] = self.used.get(str(choice.get("id")), 0) + 1
            return choice
        return None


def design(
    events: Iterable[dict],
    library: Optional[List[dict]] = None,
    rules: Optional[dict] = None,
    min_gap: float = DEFAULT_MIN_GAP,
    same_gap: float = DEFAULT_SAME_GAP,
    max_per_window: int = DEFAULT_MAX_PER_WINDOW,
    window: float = WINDOW,
    duration: Optional[float] = None,
    voice_ranges: Optional[Iterable] = None,
    allow_vip: bool = False,
    library_path: str = LIBRARY,
    local_dirs: Sequence[str] = (),
) -> Dict[str, Any]:
    """
    生成音效铺设计划。
    events: [{"type", "time", ...}] (时间线秒); library 为空时读 data/sfx_library.csv (+ local_dirs)。
    密度控制: 任意两个音效起点间隔 >= min_gap, 同分类间隔 >= same_gap, 每 window 秒最多 max_per_window 个;
    冲突时保留优先级高的事件。voice_ranges: 人声区间, quiet 规则 (卡点) 在人声段内跳过。
    返回 {"items": [...], "dropped": [...], "stats": {...}}; items 按时间排序, 可直接给 apply_sfx_plan。
    """
    if rules is None or not set(DEFAULT_RULES) <= set(rules):  # 部分覆盖 -> 与默认规则合并
        rules = merge_rules(rules)
    lib = library if library is not None else load_library(library_path, local_dirs)
    picker = SfxPicker(lib, allow_vip=allow_vip)
    voice = list(voice_ranges or [])

    evs = []
    for i, e in enumerate(events or []):
        typ = normalize_type(e.get("type"))
        rule = rules.get(typ)
        evs.append((i, {**e, "type": typ}, rule))
    dropped = [{"event": e, "reason": "no_rule"} for _i, e, r in evs if not r]
    cand = [(i, e, r) for i, e, r in evs if r]
    # 重要的先占位; 同优先级按时间先后
    cand.sort(
        key=lambda x: (-int(x[1].get("priority", x[2].get("priority", 2))), x[1]["time"], x[0])
    )

    items: List[dict] = []
    for _i, e, rule in cand:
        t = float(e["time"])
        if t < 0 or (duration and t > duration + 0.05):
            dropped.append({"event": e, "reason": "out_of_timeline"})
            continue
        if rule.get("quiet") and _in_ranges(t, voice):
            dropped.append({"event": e, "reason": "during_voice"})
            continue
        cats = [e["category"]] if e.get("category") else list(rule.get("categories") or [])
        max_dur = rule.get("max_dur")
        want = float(e["duration"]) if rule.get("fit_event") and e.get("duration") else None
        if want and max_dur:
            want = min(want, max_dur)
        sfx = picker.by_id.get(str(e["sfx_id"])) if e.get("sfx_id") else None
        if e.get("sfx_id") and sfx is None:
            dropped.append({"event": e, "reason": f"sfx_id_not_found:{e['sfx_id']}"})
            continue
        if sfx is None:
            sfx = picker.pick(cats, max_dur, float(rule.get("min_dur", 0)), want)
        if sfx is None:
            dropped.append({"event": e, "reason": f"no_sfx_in:{'/'.join(cats)}"})
            continue
        src_dur = sfx.get("duration_s") if isinstance(sfx.get("duration_s"), (int, float)) else None
        dur = src_dur or (max_dur or 1.0)
        trimmed = False
        limit = want or max_dur
        if limit and dur > limit:
            dur, trimmed = float(limit), True
        offset = float(e.get("offset", rule.get("offset", 0.0)))
        start = t + offset - (dur if rule.get("align") == "end" else 0.0)
        start = max(0.0, start)
        if duration and start + dur > duration:
            dur = max(0.05, duration - start)
            trimmed = True
        cat = sfx.get("category", cats[0] if cats else "")
        starts = [x["start"] for x in items]
        if any(abs(start - s) < min_gap for s in starts):
            dropped.append({"event": e, "reason": "too_close"})
            continue
        if any(x["category"] == cat and abs(start - x["start"]) < same_gap for x in items):
            dropped.append({"event": e, "reason": "same_category_too_close"})
            continue
        if not _window_ok(starts, start, max_per_window, window):
            dropped.append({"event": e, "reason": "density_limit"})
            continue
        vol_db = float(e.get("volume_db", rule.get("volume_db", -16)))
        items.append(
            {
                "time": round(t, 3),
                "start": round(start, 3),
                "duration": round(dur, 3),
                "event_type": e["type"],
                "category": cat,
                "sfx_id": str(sfx.get("id")),
                "name": sfx.get("name"),
                "source": sfx.get("source"),
                "path": sfx.get("path") or "",
                "volume_db": vol_db,
                "volume": round(db_to_gain(vol_db), 4),
                "offset": offset,
                "trimmed": trimmed,
                **({"word": e["word"]} if e.get("word") else {}),
            }
        )
    items.sort(key=lambda x: x["start"])
    span = duration or (max((x["start"] + x["duration"] for x in items), default=0.0))
    stats = {
        "events": len(evs),
        "placed": len(items),
        "dropped": len(dropped),
        "per_minute": round(len(items) / span * 60, 2) if span else 0.0,
        "by_category": {
            c: sum(1 for x in items if x["category"] == c)
            for c in sorted({x["category"] for x in items})
        },
    }
    return {
        "items": items,
        "dropped": dropped,
        "stats": stats,
        "settings": {
            "min_gap": min_gap,
            "same_gap": same_gap,
            "max_per_window": max_per_window,
            "window": window,
            "duration": duration,
        },
    }


# ----------------------------------------------------------------- CLI


def _open_project(name: str, root: Optional[str]):
    from core.mix_ops import MixOpsMixin
    from jy_wrapper import JyProject

    class _Project(JyProject, MixOpsMixin):
        pass

    return _Project(name, drafts_root=root, overwrite=False)


def _apply(project, plan: dict, track: str) -> dict:
    if hasattr(project, "apply_sfx_plan"):
        res = project.apply_sfx_plan(plan, track_name=track)
    else:  # 主线已注册时 JyProject 自带; 否则临时组合
        from core.mix_ops import MixOpsMixin

        res = MixOpsMixin.apply_sfx_plan(project, plan, track_name=track)
    project.save()
    return res


def _print_plan(plan: dict) -> None:
    for it in plan["items"]:
        print(
            f"  {it['start']:7.2f}s  {it['event_type']:<10} {it['category']:<8} {it['volume_db']:>5.0f}dB  "
            f"{it['name']}{' (截断)' if it['trimmed'] else ''}"
        )
    st = plan["stats"]
    print(
        f"✅ 事件 {st['events']} 个 -> 铺音效 {st['placed']} 个 (每分钟 {st['per_minute']}), "
        f"丢弃 {st['dropped']} 个"
    )
    reasons: Dict[str, int] = {}
    for d in plan["dropped"]:
        reasons[d["reason"]] = reasons.get(d["reason"], 0) + 1
    if reasons:
        print("   丢弃原因: " + ", ".join(f"{k} x{v}" for k, v in sorted(reasons.items())))


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="音效设计: 时间线事件 -> 音效铺设计划 (可写入草稿)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("rules", help="打印规则表")
    r.add_argument("--json", action="store_true")

    p = sub.add_parser("plan", help="生成音效铺设计划")
    p.add_argument("--events", action="append", default=[], help="事件 JSON 文件, 可重复")
    p.add_argument("--draft", help="从草稿读事件 (转场 / 文字 / 结尾); 配合 --write 写回")
    p.add_argument("--root", default=None, help="草稿根目录")
    p.add_argument("--analysis", help="analysis.json: 强调词 (--keywords) 与镜头切换")
    p.add_argument("--keywords", default="", help="强调词, 逗号分隔")
    p.add_argument(
        "--offset", type=float, default=0.0, help="analysis / beats 时间 -> 时间线时间偏移 (秒)"
    )
    p.add_argument("--beats", help="节拍 JSON (beat_detect.py 输出或时间列表)")
    p.add_argument("--all-beats", action="store_true", help="用全部节拍而不只是强拍")
    p.add_argument("--duration", type=float, default=None, help="时间线总时长 (秒)")
    p.add_argument("--library", default=LIBRARY)
    p.add_argument("--sfx-dir", action="append", default=[], help="附加本地音效目录, 可重复")
    p.add_argument("--rules", help="覆盖规则: JSON 字符串或文件")
    p.add_argument("--min-gap", type=float, default=DEFAULT_MIN_GAP)
    p.add_argument("--same-gap", type=float, default=DEFAULT_SAME_GAP)
    p.add_argument("--max-per-10s", type=int, default=DEFAULT_MAX_PER_WINDOW)
    p.add_argument(
        "--voice-from-analysis",
        action="store_true",
        help="人声段内不放卡点音效 (用 analysis 的转写)",
    )
    p.add_argument(
        "--allow-vip", action="store_true", help="允许会员音效 (只在剪映内可用, skill 不下载)"
    )
    p.add_argument("-o", "--output", help="计划输出 JSON")
    p.add_argument("--write", action="store_true", help="生成后写入 --draft 草稿")
    p.add_argument("--track", default="SFX")
    p.add_argument("--json", action="store_true")

    a = sub.add_parser("apply", help="把计划写入草稿")
    a.add_argument("--plan", required=True)
    a.add_argument("--draft", required=True)
    a.add_argument("--root", default=None)
    a.add_argument("--track", default="SFX")
    a.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "rules":
        rules = merge_rules()
        if args.json:
            emit_result(
                make_result(True, "ok", "", {"rules": rules, "aliases": EVENT_ALIASES}), True
            )
        else:
            for k, v in rules.items():
                print(
                    f"  {k:<11} "
                    + (
                        f"{'/'.join(v['categories']):<14} {v['volume_db']:>4}dB  "
                        f"偏移 {v['offset']:+.2f}s  优先级 {v['priority']}"
                        + ("  (终点对齐)" if v.get("align") == "end" else "")
                        + ("  (人声段跳过)" if v.get("quiet") else "")
                        if v
                        else "(关闭)"
                    )
                )
        return 0

    try:
        if args.cmd == "apply":
            with open(args.plan, "r", encoding="utf-8") as f:
                plan = json.load(f)
            res = _apply(_open_project(args.draft, args.root), plan, args.track)
            ok = not res["failed"] or bool(res["added"])
            emit_result(make_result(ok, "ok" if ok else "apply_failed", "", res), args.json)
            if not args.json:
                print(
                    f"✅ 写入 {len(res['added'])} 个音效到轨道 {res['track']}, 失败 {len(res['failed'])} 个"
                )
            return 0 if ok else 1

        events: List[dict] = []
        duration = args.duration
        project = None
        for path in args.events:
            evs, dur = load_events(path)
            events += evs
            duration = duration or dur
        if args.draft:
            project = _open_project(args.draft, args.root)
            evs, dur = events_from_draft(project)
            events += evs
            duration = duration or dur
        voice = None
        if args.analysis:
            kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
            events += events_from_analysis(args.analysis, kws, args.offset)
            if args.voice_from_analysis:
                from core.mix_ops import voice_ranges_from_analysis

                voice = voice_ranges_from_analysis(args.analysis, "auto", args.offset)
        if args.beats:
            events += events_from_beats(
                args.beats, strong_only=not args.all_beats, offset=args.offset
            )
        if not events:
            raise ValueError("没有事件: 请给 --events / --draft / --analysis --keywords / --beats")
        overrides = None
        if args.rules:
            overrides = (
                json.load(open(args.rules, encoding="utf-8"))
                if os.path.exists(args.rules)
                else json.loads(args.rules)
            )
        plan = design(
            events,
            rules=merge_rules(overrides),
            min_gap=args.min_gap,
            same_gap=args.same_gap,
            max_per_window=args.max_per_10s,
            duration=duration,
            voice_ranges=voice,
            allow_vip=args.allow_vip,
            library_path=args.library,
            local_dirs=args.sfx_dir,
        )
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(plan, f, ensure_ascii=False, indent=2)
        applied = None
        if args.write:
            if project is None:
                raise ValueError("--write 需要 --draft")
            applied = _apply(project, plan, args.track)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
        emit_result(make_result(False, "sfx_failed", str(e)), args.json)
        if not args.json:
            print(f"❌ {e}")
        return 1

    if args.json:
        emit_result(
            make_result(True, "ok", "", {**plan, **({"applied": applied} if applied else {})}), True
        )
    else:
        _print_plan(plan)
        if args.output:
            print(f"   计划已写入 {args.output}")
        if applied:
            print(
                f"   已写入草稿: {len(applied['added'])} 个音效 (轨道 {applied['track']}), "
                f"失败 {len(applied['failed'])} 个"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
