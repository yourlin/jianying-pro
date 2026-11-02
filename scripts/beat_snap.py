"""
卡点吸附 + 节拍混剪规划 (卡点混剪第 2 步): 切点落到鼓点上, 强拍放好镜头, 高能段切得快。

    # 已有切点 (如 analyze_footage 的 scenes / 手工剪点) 吸附到拍点
    python beat_snap.py snap --beats song.beats.json --cuts "1.02,2.4,3.97" [--tolerance 0.15] [--fps 30]
    python beat_snap.py snap --audio song.mp3 --cuts cuts.json

    # 按节拍生成混剪计划 (镜头时长序列), 可选直接生成剪映草稿
    python beat_snap.py plan --audio song.mp3 --clips ./clips_dir [--density 标准] [--start climax]
                             [--duration 20] [-o plan.json] [--build 草稿名] [--drafts-root DIR]

clips 可以是目录 (里面的视频 / 图片) 或 JSON: [{path, duration?, score?, highlight?, source_start?}]
  score: 越高越重要 (任意量纲, 比如 shot_quality.py 的 summary.score), 强拍 / 高潮段优先放高分镜头
  highlight: 镜头里最精彩的时刻 (秒, 如 shot_quality 的 summary.best_t), 截取时让它落在镜头前段

切速 (每几拍切一次) 以 references/scenes/beat_montage.json 的 pacing.cut_every_beats 为基准:
  舒缓 4 / 标准 2 / 快闪 1; 高能段 (energy_sections.level = high) 切速加倍, 低能段减半。
  --scene 换场景 (promo / music_video) 时用该场景的切速表和镜头长短。

按歌词行切 (MV): --cut-on lyric --lyrics lyrics.json (或场景 pacing.cut_on = "lyric")
  切点只落在歌词行边界上, 太短的行并进上一镜头, 太长的在拍点上补切, 高潮段允许长镜头。
"""

import argparse
import json
import math
import os
import sys
from typing import Dict, List, Optional, Sequence, Union

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from beat_detect import BeatDetectError, detect_beats, load_beats  # noqa: E402

US = 1_000_000
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}
DENSITY_ALIASES = {
    "舒缓": "舒缓",
    "slow": "舒缓",
    "标准": "标准",
    "normal": "标准",
    "standard": "标准",
    "快闪": "快闪",
    "fast": "快闪",
}
SAFETY_S = 0.05  # 截取素材时离片尾留的余量 (ffprobe 与剪映读到的时长有细微差异)


class BeatPlanError(ValueError):
    pass


def load_scene(scene: Union[str, dict, None] = None) -> dict:
    """场景规范: 默认 beat_montage; 也可以传场景 id (promo / music_video ...) 或已读好的 dict。"""
    if isinstance(scene, dict):
        return scene
    path = os.path.join(SKILL, "references", "scenes", f"{scene or 'beat_montage'}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _grid(beats) -> dict:
    """beats 可以是拍点列表 / detect_beats 结果 / beats.json 路径。"""
    res = load_beats(beats)
    return {
        "beats": sorted(float(b) for b in res.get("beats") or []),
        "downbeats": sorted(float(b) for b in res.get("downbeats") or []),
        "energy_sections": res.get("energy_sections") or [],
        "climax": res.get("climax"),
        "tempo": res.get("tempo"),
        "duration": res.get("duration"),
        "audio": res.get("audio"),
    }


def _nearest(sorted_xs: Sequence[float], t: float) -> Optional[float]:
    import bisect

    if not sorted_xs:
        return None
    i = bisect.bisect_left(sorted_xs, t)
    cands = [sorted_xs[j] for j in (i - 1, i) if 0 <= j < len(sorted_xs)]
    return min(cands, key=lambda x: abs(x - t))


def _to_frame(t: float, fps: Optional[float]) -> float:
    return round(round(t * fps) / fps, 6) if fps else round(t, 6)


# ---------------------------------------------------------------------------
# 切点吸附
# ---------------------------------------------------------------------------


def snap_cuts(
    cut_times: Sequence[float],
    beats,
    tolerance: float = 0.15,
    downbeat_bias: float = 0.04,
    min_gap: float = 0.2,
    fps: Optional[float] = None,
    use_onsets: bool = False,
    details: bool = False,
) -> Union[List[float], List[dict]]:
    """
    把切点吸附到最近的拍点 (距离 <= tolerance 秒才吸, 否则保持原位)。

    beats: 拍点列表, 或 detect_beats 结果 / beats.json 路径 (此时也会用 downbeats、onsets)
    downbeat_bias: 强拍比普通拍「近」这么多秒也优先选强拍
    min_gap: 吸附后相邻切点至少隔这么久; 两个切点抢同一拍时, 后一个保持原位
    fps: 给了就把结果对齐到帧 (剪映按帧切)
    use_onsets: 拍点之外也允许吸到起音点 (鼓点 / 重音, 比拍点密)
    details=True 返回 [{time, original, delta, kind: downbeat/beat/onset/None}]
    """
    res = load_beats(beats)
    bs = sorted(float(b) for b in res.get("beats") or [])
    ds = sorted(float(b) for b in res.get("downbeats") or [])
    os_ = sorted(float(b) for b in res.get("onsets") or []) if use_onsets else []
    out = []
    last = -math.inf
    for c in [float(x) for x in cut_times]:
        options = []
        for kind, xs, bias in (
            ("downbeat", ds, downbeat_bias),
            ("beat", bs, 0.0),
            ("onset", os_, -0.02),
        ):
            n = _nearest(xs, c)
            if n is not None and abs(n - c) <= tolerance:
                options.append((abs(n - c) - bias, n, kind))
        options.sort()
        chosen, kind = c, None
        for _, n, k in options:
            if n - last >= min_gap:
                chosen, kind = n, k
                break
        t = _to_frame(chosen, fps)
        out.append({"time": t, "original": round(c, 6), "delta": round(t - c, 6), "kind": kind})
        last = t
    return out if details else [o["time"] for o in out]


# ---------------------------------------------------------------------------
# 混剪规划
# ---------------------------------------------------------------------------


def _probe_duration(path: str) -> Optional[float]:
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return math.inf
    try:
        from analyze_footage import probe
    except Exception:  # pragma: no cover - analyze_footage 总在同目录
        return None
    d = (probe(path) or {}).get("duration")
    return float(d) if d else None


def load_clips(clips) -> List[dict]:
    """clips: 目录 / JSON 路径 / 列表 [{path, duration?, score?, highlight?, source_start?}] 或路径列表。"""
    if isinstance(clips, str):
        if os.path.isdir(clips):
            names = sorted(
                f
                for f in os.listdir(clips)
                if os.path.splitext(f)[1].lower() in IMAGE_EXTS | VIDEO_EXTS
            )
            clips = [os.path.join(clips, f) for f in names]
        else:
            with open(clips, "r", encoding="utf-8") as f:
                data = json.load(f)
            clips = data.get("clips", data) if isinstance(data, dict) else data
    out = []
    for i, c in enumerate(clips or []):
        c = {"path": c} if isinstance(c, str) else dict(c)
        if not c.get("path"):
            raise BeatPlanError(f"第 {i + 1} 个素材缺少 path")
        c["path"] = os.path.abspath(c["path"])
        if c.get("duration") is None:
            c["duration"] = _probe_duration(c["path"])
        if c["duration"] is None:
            raise BeatPlanError(
                f"读不到素材时长 (需要 ffprobe 或在 clips 里写 duration): {c['path']}"
            )
        c["duration"] = float(c["duration"])
        c["source_start"] = float(c.get("source_start") or 0.0)
        c["score"] = float(c["score"]) if c.get("score") is not None else None
        c["is_image"] = os.path.splitext(c["path"])[1].lower() in IMAGE_EXTS or math.isinf(
            c["duration"]
        )
        c["id"] = i
        out.append(c)
    if not out:
        raise BeatPlanError("没有素材")
    return out


def _cut_every(density, scene: Optional[dict] = None) -> int:
    if isinstance(density, (int, float)) and not isinstance(density, bool):
        return max(1, int(density))
    key = DENSITY_ALIASES.get(str(density))
    if key is None:
        if str(density).isdigit():
            return max(1, int(density))
        raise BeatPlanError(f"未知切点密度: {density} (可选 舒缓 / 标准 / 快闪 或每几拍的整数)")
    table = (((scene or load_scene()).get("pacing") or {}).get("cut_every_beats")) or {
        "舒缓": 4,
        "标准": 2,
        "快闪": 1,
    }
    return int(table.get(key, 2))


def _section_at(sections: List[dict], t: float) -> dict:
    for s in sections:
        if s["start"] - 1e-6 <= t < s["end"] - 1e-6:
            return s
    return sections[-1] if sections else {"level": "mid", "energy": 0.5, "is_climax": False}


# ---------------------------------------------------------------------------
# 按歌词行切 (cut_on = "lyric") / 段落切速覆盖
# ---------------------------------------------------------------------------

CUT_ON = ("beat", "lyric")


def load_lyrics(lyrics) -> List[dict]:
    """
    歌词行 (音乐时间, 秒): [{start, end, text}] / [[start, end, text]] / JSON 路径
    (列表, 或 {lyrics | lines | transcript | segments: [...]}, 比如 analyze_footage 对歌曲的转写)。
    """
    if lyrics is None:
        return []
    if isinstance(lyrics, str):
        with open(lyrics, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            keys = ("lyrics", "lines", "transcript", "segments")
            data = next((data[k] for k in keys if data.get(k)), [])
        lyrics = data
    out = []
    for i, ln in enumerate(lyrics or []):
        try:
            if isinstance(ln, (list, tuple)):
                s, e = float(ln[0]), float(ln[1])
                text = str(ln[2]) if len(ln) > 2 else ""
            else:
                s = float(ln["start"])
                e = float(ln.get("end", s))
                text = str(ln.get("text", ""))
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise BeatPlanError(
                f"第 {i + 1} 行歌词格式不对 (需要 start / end / text): {ln}"
            ) from exc
        out.append({"start": s, "end": max(s, e), "text": text})
    out.sort(key=lambda x: x["start"])
    return out


def lyric_boundaries(lines: List[dict], gap: float = 1.0) -> List[float]:
    """可下刀的歌词边界: 每行开头; 行尾离下一行开头 >= gap 秒 (间奏 / 换气长停) 时, 行尾也算。"""
    out = []
    for i, ln in enumerate(lines):
        out.append(ln["start"])
        nxt = lines[i + 1]["start"] if i + 1 < len(lines) else None
        if ln["end"] > ln["start"] and (nxt is None or nxt - ln["end"] >= gap):
            out.append(ln["end"])
    return sorted(set(round(x, 6) for x in out))


def _in_climax(g: dict, t: float) -> bool:
    cl = g.get("climax")
    if cl and float(cl["start"]) - 1e-6 <= t < float(cl["end"]):
        return True
    return bool(g["energy_sections"]) and bool(
        _section_at(g["energy_sections"], t).get("is_climax")
    )


def _lyric_cuts(lines, g: dict, start: float, end: float, cfg: dict, min_shot: float):
    """
    按歌词行切: 切点只放在歌词边界上; 比 min_shot 短的行并进上一个镜头;
    超过 max_shot 的镜头在中间的拍点上补切 (高潮段用 climax_min_shot / climax_max_shot, 允许长镜头)。
    返回 [(时间, 类型)], 类型: start / lyric / split / end。
    """
    lo = float(cfg.get("min_shot", min_shot))
    hi = float(cfg.get("max_shot", 4.0))
    c_lo = float(cfg.get("climax_min_shot", lo))
    c_hi = float(cfg.get("climax_max_shot", hi))
    cuts = [(start, "start")]
    for b in lyric_boundaries(lines, float(cfg.get("gap_s", 1.0))):
        if not (start + 1e-3 < b < end - 1e-3):
            continue
        need = c_lo if _in_climax(g, b) else lo
        if b - cuts[-1][0] >= need - 1e-6:
            cuts.append((b, "lyric"))
    if len(cuts) > 1 and end - cuts[-1][0] < lo - 1e-6:
        cuts.pop()
    cuts.append((end, "end"))
    out = [cuts[0]]
    for (a, _ka), (b, kind) in zip(cuts[:-1], cuts[1:]):
        cap = c_hi if _in_climax(g, (a + b) / 2) else hi  # 镜头大半在高潮段: 允许长镜头
        n = math.ceil((b - a) / cap - 1e-6) if cap > 0 else 1
        for j in range(1, n):
            t = a + (b - a) * j / n
            inner = [x for x in g["beats"] if out[-1][0] + lo <= x <= b - lo]
            snap = _nearest(inner, t)
            out.append((snap if snap is not None else t, "split"))
        out.append((b, kind))
    return out


def _norm_sections(sections, g: dict, start: float, end: float, cut_on: str, lyric_b) -> List[dict]:
    """
    段落切速覆盖 (音乐时间): [{start, end, cut_every_beats?, clips?, name?}]。
    cut_every_beats: 每几拍切一次 (覆盖能量自动切速); 0 = 整段一个镜头 (标题卡 / 片尾卡)。
    equal: 段内镜头等长 (同样的帧数, 宣传片蒙太奇段)。
    段落开头是强制切点: 按拍切时吸到半拍以内的拍点, 按歌词切时吸到 1s 以内的歌词边界。
    """
    out = []
    bs = g["beats"]
    diffs = sorted(b - a for a, b in zip(bs, bs[1:]))
    half = diffs[len(diffs) // 2] / 2 if diffs else 0.25
    for sec in sections or []:
        s0, s1 = float(sec["start"]), float(sec["end"])
        if s1 <= start or s0 >= end:
            continue
        pool = lyric_b if cut_on == "lyric" else bs
        tol = 1.0 if cut_on == "lyric" else half
        n = _nearest(pool, s0)
        if n is not None and abs(n - s0) <= tol and s0 > start + 1e-3:
            s0 = n
        anchor = next((k for k, b in enumerate(bs) if b >= s0 - 1e-3), len(bs))
        clips = [os.path.abspath(c) for c in sec.get("clips") or []]
        out.append(
            {
                "start": max(start, s0),
                "end": min(end, s1),
                "cut_every_beats": sec.get("cut_every_beats"),
                "equal": bool(sec.get("equal")),
                "clips": clips,
                "name": sec.get("name"),
                "_anchor": anchor,
            }
        )
    out.sort(key=lambda x: x["start"])
    for a, b in zip(out, out[1:]):  # 吸附后首尾相接
        a["end"] = b["start"]
    return out


def _beat_interval(beats: Sequence[float]) -> Optional[float]:
    diffs = sorted(b - a for a, b in zip(beats, beats[1:]))
    return diffs[len(diffs) // 2] if diffs else None


def _override_at(secs: List[dict], t: float) -> Optional[dict]:
    for s in secs:
        if s["start"] - 1e-6 <= t < s["end"] - 1e-6:
            return s
    return None


def _resolve_start(start, g: dict) -> float:
    if isinstance(start, str):
        if start == "climax":
            cl = g.get("climax")
            if not cl:
                return 0.0
            t = float(cl["start"])
            near = _nearest(g["downbeats"], t)
            return near if near is not None and abs(near - t) < 2.5 else t
        start = float(start)
    return float(start or 0.0)


def plan_beat_montage(
    clips,
    beats,
    density="标准",
    start: Union[float, str] = 0.0,
    duration: Optional[float] = None,
    fps: float = 30.0,
    min_shot: Optional[float] = None,
    max_shot: Optional[float] = None,
    energy_speedup: Optional[bool] = None,
    accent_max: Optional[int] = None,
    music_path: Optional[str] = None,
    music_volume: Optional[float] = None,
    track_name: str = "VideoTrack",
    cut_on: Optional[str] = None,
    lyrics=None,
    scene: Union[str, dict, None] = None,
    sections: Optional[List[dict]] = None,
) -> Dict:
    """
    按节拍 (或歌词行) 生成镜头时长序列 (成片时间轴从 0 开始, 音乐从 start 秒开始播)。

    clips: 见 load_clips; beats: 拍点列表 / detect_beats 结果 / beats.json 路径
    density: 舒缓 / 标准 / 快闪 / 每几拍切一次的整数
    start: 音乐起点 (秒), 或 "climax" 从高潮段的强拍开始 (短视频常用)
    duration: 成片时长; 默认 min(音乐剩余, 素材总时长)
    energy_speedup: 高能段切速加倍、低能段减半 (默认取场景 pacing.energy_speedup, 没写为 True)
    cut_on: "beat" 按拍切 / "lyric" 按歌词行切 (默认取场景 pacing.cut_on, 没写为 beat)
    lyrics: cut_on="lyric" 时必填, 见 load_lyrics (音乐时间)
    scene: 场景 id / dict, 决定镜头长短、切速表、重音上限、音量 (默认 beat_montage)
    sections: 段落切速覆盖 (音乐时间), 见 _norm_sections; 段落开头是强制切点,
              段落的 clips 只从这几个素材里挑
    返回 {music, fps, cut_on, shots[{..., cut_kind, add_clip}], cuts, accents, total_duration, stats}
    """
    scene = load_scene(scene)
    pacing = scene.get("pacing") or {}
    cut_on = cut_on or pacing.get("cut_on") or "beat"
    if cut_on not in CUT_ON:
        raise BeatPlanError(f"未知 cut_on: {cut_on} (可选 beat / lyric)")
    if energy_speedup is None:
        energy_speedup = bool(pacing.get("energy_speedup", True))
    avg_shot = pacing.get("avg_shot") or [0.3, 1.0]
    lyric_cfg = pacing.get("lyric") or {}
    if min_shot is None:
        min_shot = lyric_cfg.get("min_shot", avg_shot[0]) if cut_on == "lyric" else avg_shot[0]
    min_shot = float(min_shot)
    allowed = (scene.get("camera") or {}).get("allowed")
    if accent_max is None:
        accent_max = (scene.get("effects") or {}).get("max", 4)
        if allowed is not None and "punch_in" not in allowed:
            accent_max = 0  # 场景不允许强调推近 (如 MV 只在进出产品 UI 时推拉)
    accent_max = int(accent_max)
    min_gap = float((scene.get("camera") or {}).get("min_gap_s", 0.5))
    vol = float(
        music_volume
        if music_volume is not None
        else (scene.get("audio") or {}).get("bgm_volume", 0.9)
    )

    g = _grid(beats)
    cl = load_clips(clips)
    base = _cut_every(density, scene)
    lines = load_lyrics(lyrics)
    if cut_on == "lyric" and not lines:
        raise BeatPlanError("cut_on=lyric 需要歌词行时间 (lyrics: [{start, end, text}])")
    start = _resolve_start(start, g)
    music_end = (
        float(g["duration"]) if g.get("duration") else (g["beats"][-1] if g["beats"] else 0.0)
    )
    footage = sum(c["duration"] - c["source_start"] for c in cl)
    if duration is None:
        duration = min(music_end - start, footage) if music_end > start else footage
    duration = float(duration)
    if duration <= 0:
        raise BeatPlanError("成片时长为 0 (检查 start / duration / 音乐时长)")
    end = start + duration

    # 1) 切点: 从 start 起按拍走, 每段按能量决定每几拍切一次, 并对齐到小节网格
    #    (cut_on = lyric 时改为按歌词行切, 见 _lyric_cuts; sections 覆盖段落切速)
    user_sections = sections
    beats_l = g["beats"]
    sections = g["energy_sections"]
    lyric_b = lyric_boundaries(lines, float(lyric_cfg.get("gap_s", 1.0))) if lines else []
    secs = _norm_sections(user_sections, g, start, end, cut_on, lyric_b)
    down = set(round(d, 3) for d in g["downbeats"])
    bidx_down = [i for i, b in enumerate(beats_l) if round(b, 3) in down]
    kinds: Dict[float, str] = {}
    if cut_on == "lyric":
        lc = _lyric_cuts(lines, g, start, end, lyric_cfg, min_shot)
        holds = [s_ for s_ in secs if s_.get("cut_every_beats") == 0]  # 标题卡 / 片尾卡: 段内不切
        lc = [
            (t, k)
            for t, k in lc
            if k in ("start", "end")
            or not any(h["start"] + 1e-3 < t < h["end"] - 1e-3 for h in holds)
        ]
        cuts = [t for t, _k in lc[:-1]]
        kinds.update({round(t, 6): k for t, k in lc[:-1]})
    else:
        cuts = [start]
        if len(beats_l) >= 2:
            i = next((k for k, b in enumerate(beats_l) if b > start + 1e-3), len(beats_l))
            while i < len(beats_l) and beats_l[i] < end - 1e-3:
                ov = _override_at(secs, beats_l[i])
                if ov is not None and ov.get("cut_every_beats") is not None:
                    n = int(ov["cut_every_beats"])
                    if n <= 0:  # 整段一个镜头: 跳到段落结尾
                        i = next(
                            (k for k in range(i, len(beats_l)) if beats_l[k] >= ov["end"] - 1e-3),
                            len(beats_l),
                        )
                        continue
                    pos = i - ov["_anchor"]  # 段内等长: 从段落开头那一拍起数
                else:
                    sec = _section_at(sections, beats_l[i])
                    n = base
                    if energy_speedup:
                        n = (
                            max(1, base // 2)
                            if sec["level"] == "high"
                            else (min(8, base * 2) if sec["level"] == "low" else base)
                        )
                    # 小节网格: 以最近的强拍为 0 计位置, 落在 n 的倍数上才切
                    prev_down = max((k for k in bidx_down if k <= i), default=None)
                    pos = i - prev_down if prev_down is not None else 0
                if n > 1 and pos % n:
                    i += n - pos % n
                    continue
                if beats_l[i] - cuts[-1] >= min_shot - 1e-6:
                    cuts.append(beats_l[i])
                i += n
        else:  # 没有拍点: 均匀切 (每 1s)
            t = start + 1.0
            while t < end - 1e-3:
                cuts.append(t)
                t += 1.0
        kinds.update({round(t, 6): "beat" for t in cuts[1:]})
        kinds[round(start, 6)] = "start"
        if end - cuts[-1] < min_shot and len(cuts) > 1:
            cuts.pop()
    # 段落开头 = 强制切点 (离它不到 min_shot 的普通切点让位)
    forced = {round(s_["start"], 6) for s_ in secs if s_["start"] > start + 1e-3}
    if forced:
        merged: List[float] = []
        for c in sorted(set(round(x, 6) for x in cuts) | forced):
            if merged and c - merged[-1] < min_shot - 1e-6:
                if c in forced and merged[-1] not in forced and len(merged) > 1:
                    merged[-1] = c
                continue
            merged.append(c)
        if len(merged) > 1 and end - merged[-1] < min_shot - 1e-6 and merged[-1] not in forced:
            merged.pop()
        cuts = merged
        for c in forced:
            kinds[c] = "section"
    # 等长段 (宣传片蒙太奇): 段内每 n 拍一刀, 每个镜头同样的帧数 (不随拍点的亚帧误差抖动)
    bi = _beat_interval(beats_l)
    for s_ in secs:
        n = s_.get("cut_every_beats")
        if cut_on != "beat" or not s_.get("equal") or not n or n <= 0 or not bi:
            continue
        lf = max(1, int(round(n * bi * fps)))
        f0 = int(round((s_["start"] - start) * fps))
        cuts = [c for c in cuts if not (s_["start"] + 1e-3 < c < s_["end"] - 1e-3)]
        k = 1
        while (f0 + k * lf) / fps + start < s_["end"] - 0.5 * lf / fps:
            t = round(start + (f0 + k * lf) / fps, 6)
            cuts.append(t)
            kinds[t] = "beat"
            k += 1
        cuts.sort()
    cuts.append(end)

    # 2) 镜头不能比素材长: 超长的在中间拍点上再切开
    longest = max(
        min(c["duration"] - c["source_start"] - SAFETY_S, max_shot or math.inf) for c in cl
    )
    if longest <= 0:
        raise BeatPlanError("素材都太短")
    cap = min(max_shot or math.inf, longest)
    refined = [cuts[0]]
    for a, b in zip(cuts[:-1], cuts[1:]):
        while b - refined[-1] > cap + 1e-6:
            inner = [x for x in beats_l if refined[-1] + min_shot <= x <= refined[-1] + cap]
            nxt = inner[-1] if inner else refined[-1] + cap
            refined.append(nxt)
            kinds.setdefault(round(nxt, 6), "split")
        refined.append(b)
    cuts = refined

    # 3) 对齐到帧, 生成槽位
    frames = [int(round((t - start) * fps)) for t in cuts]
    frames = sorted(set(frames))
    kind_by_frame = {int(round((t - start) * fps)): k for t, k in kinds.items()}
    slots = []
    for k, (fa, fb) in enumerate(zip(frames[:-1], frames[1:])):
        t0 = start + fa / fps
        sec = _section_at(sections, t0 + 1e-3)
        on_down = any(abs(t0 - d) < 0.5 / fps + 1e-3 for d in g["downbeats"])
        imp = (
            float(sec.get("energy", 0.5))
            + (0.5 if on_down else 0.0)
            + (0.3 if sec.get("is_climax") else 0.0)
        )
        if k == 0:
            imp += 2.0  # 开头第一个镜头 = 钩子, 永远最先挑 (放最高分镜头)
        slots.append(
            {
                "index": k,
                "fa": fa,
                "fb": fb,
                "music_time": round(t0, 3),
                "on_downbeat": on_down,
                "level": sec.get("level", "mid"),
                "energy": round(float(sec.get("energy", 0.5)), 3),
                "is_climax": bool(sec.get("is_climax")),
                "importance": round(imp, 3),
                "cut_kind": "start" if k == 0 else kind_by_frame.get(fa, "split"),
                "section": _override_at(secs, start + (fa + fb) / 2 / fps),  # 镜头中点所在段落
            }
        )

    # 4) 分配素材: 重要的槽位先挑; 优先没用过的、分高的; 相邻不重复
    scores = [c["score"] for c in cl if c["score"] is not None]
    lo, hi = (min(scores), max(scores)) if scores else (0.0, 1.0)
    for c in cl:
        c["_norm"] = 0.5 if c["score"] is None or hi <= lo else (c["score"] - lo) / (hi - lo)
        c["_uses"] = 0
        c["_cursor"] = c["source_start"]
    assigned: Dict[int, dict] = {}
    reserved = {p for x in secs if x.get("cut_every_beats") == 0 for p in x["clips"]}
    reserved -= {p for x in secs if x.get("cut_every_beats") != 0 for p in x["clips"]}
    for s in sorted(slots, key=lambda s: (-s["importance"], s["index"])):
        need = (s["fb"] - s["fa"]) / fps
        neigh = {assigned[j]["id"] for j in (s["index"] - 1, s["index"] + 1) if j in assigned}
        fits = [c for c in cl if c["duration"] - c["source_start"] - SAFETY_S >= need - 1e-6]
        own = (s["section"] or {}).get("clips")
        if own:  # 段落指定了素材 (如标题卡底图、片尾卡): 只从这几个里挑
            fits = (
                [c for c in fits if c["path"] in own] or [c for c in cl if c["path"] in own] or fits
            )
        elif reserved:  # 没指定素材的段落: 不用整段不切的段落 (标题卡 / 片尾卡) 专用的素材
            fits = [c for c in fits if c["path"] not in reserved] or fits
        pool = [c for c in fits if c["id"] not in neigh] or fits or cl
        best = min(pool, key=lambda c: (c["_uses"], -c["_norm"], c["id"]))
        best["_uses"] += 1
        assigned[s["index"]] = best

    # 5) 截取位置: 有 highlight 时让它落在镜头前 30%; 重复使用的素材顺着往后取
    shots = []
    for s in slots:
        c = assigned[s["index"]]
        dur = (s["fb"] - s["fa"]) / fps
        if c["is_image"]:
            src = 0.0
        else:
            limit = c["duration"] - SAFETY_S - dur
            if c.get("highlight") is not None and not c.get("_hl_used"):
                src = float(c["highlight"]) - 0.3 * dur
                c["_hl_used"] = True
            else:
                src = c["_cursor"]
                if src > limit + 1e-6:
                    src = c["source_start"]
            src = max(c["source_start"], min(src, limit))
            c["_cursor"] = src + dur
        tgt_us = int(round(s["fa"] * US / fps))
        dur_us = int(round(s["fb"] * US / fps)) - tgt_us
        src_us = int(round(src * US))
        shots.append(
            {
                "index": s["index"],
                "path": c["path"],
                "clip_id": c["id"],
                "source_start": round(src, 3),
                "duration": round(dur, 4),
                "target_start": round(s["fa"] / fps, 4),
                "music_time": s["music_time"],
                "on_downbeat": s["on_downbeat"],
                "level": s["level"],
                "energy": s["energy"],
                "is_climax": s["is_climax"],
                "importance": s["importance"],
                "score": c["score"],
                "accent": None,
                "cut_kind": s["cut_kind"],
                "section": (s["section"] or {}).get("name"),
                "add_clip": {
                    "media_path": c["path"],
                    "source_start": src_us,
                    "duration": dur_us,
                    "target_start": tgt_us,
                    "track_name": track_name,
                },
            }
        )

    # 6) 强拍重音: 高能 / 高潮段的强拍镜头加 punch_in, 不超过 accent_max 个, 间隔 >= min_gap
    accents = []
    cands = sorted(
        (
            sh
            for sh in shots
            if sh["on_downbeat"] and sh["index"] > 0 and (sh["level"] == "high" or sh["is_climax"])
        ),
        key=lambda sh: (-sh["importance"], sh["index"]),
    )
    for sh in cands:
        if len(accents) >= accent_max:
            break
        if all(abs(sh["target_start"] - a["at"]) >= max(min_gap, 2.0) for a in accents):
            sh["accent"] = "punch_in"
            accents.append(
                {
                    "at": sh["target_start"],
                    "shot": sh["index"],
                    "move": "punch_in",
                    "dur": min(sh["duration"], 0.9),
                }
            )
    accents.sort(key=lambda a: a["at"])

    total_us = int(round(frames[-1] * US / fps))
    beats_in = [b for b in beats_l if start - 1e-6 <= b <= end + 1e-6]
    errs = (
        [min(abs(start + sh["target_start"] - b) for b in beats_in) for sh in shots]
        if beats_in
        else []
    )
    return {
        "scene": scene.get("id", "beat_montage"),
        "cut_on": cut_on,
        "music": {
            "path": music_path or g.get("audio"),
            "source_start": round(start, 3),
            "duration": round(total_us / US, 4),
            "source_start_us": int(round(start * US)),
            "duration_us": total_us,
            "volume": vol,
            "tempo": g.get("tempo"),
        },
        "fps": fps,
        "density": density,
        "cut_every_beats": base,
        "total_duration": round(total_us / US, 4),
        "shots": shots,
        "cuts": [round(start + f / fps, 4) for f in frames],
        "accents": accents,
        "mute_footage": bool((scene.get("audio") or {}).get("mute_footage", True)),
        "stats": {
            "shots": len(shots),
            "clips_used": len({sh["clip_id"] for sh in shots}),
            "clips_total": len(cl),
            "avg_shot": round(total_us / US / max(1, len(shots)), 3),
            "max_cut_error_s": round(max(errs), 4) if errs else None,
            "max_cut_error_frames": round(max(errs) * fps, 2) if errs else None,
            "lyric_cuts": sum(1 for sh in shots if sh["cut_kind"] == "lyric"),
        },
    }


def build_draft(
    plan: Dict,
    project_name: str,
    drafts_root: Optional[str] = None,
    width: int = 1080,
    height: int = 1920,
    accents: bool = True,
    overwrite: bool = True,
) -> Dict:
    """把 plan 写成剪映草稿 (视频轨 + BGM, 素材静音, 强拍 punch_in)。返回 save() 的结果。"""
    # 先导入 jy_wrapper: 它会把 vendor/pyJianYingDraft 加到 sys.path, core.beat_ops 才能导入
    from jy_wrapper import JyProject  # noqa: I001
    from core.beat_ops import BeatOpsMixin

    class _Project(JyProject, BeatOpsMixin):
        pass

    p = _Project(
        project_name, width=width, height=height, drafts_root=drafts_root, overwrite=overwrite
    )
    p.apply_beat_plan(plan, accents=accents)
    return p.save()


def _parse_cuts(s: str) -> List[float]:
    if os.path.exists(s):
        with open(s, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):  # analysis.json 的 scenes 或 {"cuts": [...]}
            data = data.get("cuts") or data.get("scenes") or []
        return [float(x["time"] if isinstance(x, dict) else x) for x in data]
    return [float(x) for x in s.replace("，", ",").split(",") if x.strip()]


def _get_beats(args) -> dict:
    if args.beats:
        return load_beats(args.beats)
    if args.audio:
        return detect_beats(args.audio, backend=args.backend)
    raise BeatPlanError("需要 --beats beats.json 或 --audio 音乐文件")


def _main(argv=None) -> int:
    from utils.cli_protocol import emit_result, make_result

    ap = argparse.ArgumentParser(description="卡点吸附 / 节拍混剪规划")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("snap", "plan"):
        sp = sub.add_parser(name)
        sp.add_argument("--beats", help="beat_detect.py 输出的 beats.json")
        sp.add_argument("--audio", help="音乐文件 (没有 --beats 时现场检测)")
        sp.add_argument("--backend", default="auto", choices=["auto", "librosa", "numpy"])
        sp.add_argument("--fps", type=float, default=30.0)
        sp.add_argument("-o", "--output", help="输出 JSON")
        sp.add_argument("--json", action="store_true", help="按 CLI 协议输出 JSON 到 stdout")
    sp = sub.choices["snap"]
    sp.add_argument(
        "--cuts",
        required=True,
        help='切点: "1.2,3.4" 或 JSON 文件 (列表 / {"cuts"} / analysis.json)',
    )
    sp.add_argument("--tolerance", type=float, default=0.15, help="最大吸附距离 (秒)")
    sp.add_argument("--onsets", action="store_true", help="也吸附到起音点")
    sp = sub.choices["plan"]
    sp.add_argument("--clips", required=True, help="素材目录或 clips.json")
    sp.add_argument("--density", default="标准", help="舒缓 / 标准 / 快闪 或每几拍切一次")
    sp.add_argument("--start", default="0", help='音乐起点秒数, 或 "climax"')
    sp.add_argument("--duration", type=float, help="成片时长 (秒)")
    sp.add_argument("--no-speedup", action="store_true", help="不按段落能量调切速")
    sp.add_argument(
        "--cut-on", choices=list(CUT_ON), help="按拍切 (beat) 或按歌词行切 (lyric, 需要 --lyrics)"
    )
    sp.add_argument("--lyrics", help="歌词行时间 JSON: [{start, end, text}] 或带 transcript 的转写")
    sp.add_argument(
        "--scene", help="场景 id (默认 beat_montage; MV 用 music_video, 宣传片用 promo)"
    )
    sp.add_argument("--build", metavar="NAME", help="直接生成剪映草稿")
    sp.add_argument("--drafts-root", help="草稿目录 (默认剪映草稿目录)")
    sp.add_argument("--size", default="1080x1920", help="草稿画布 宽x高")
    args = ap.parse_args(argv)

    try:
        g = _get_beats(args)
        if args.cmd == "snap":
            res = {
                "cuts": snap_cuts(
                    _parse_cuts(args.cuts),
                    g,
                    args.tolerance,
                    fps=args.fps,
                    use_onsets=args.onsets,
                    details=True,
                ),
                "tempo": g.get("tempo"),
            }
            moved = sum(1 for c in res["cuts"] if c["kind"])
            msg = f"🎯 {moved}/{len(res['cuts'])} 个切点吸到了拍点"
        else:
            start = args.start if args.start == "climax" else float(args.start)
            res = plan_beat_montage(
                args.clips,
                g,
                args.density,
                start,
                args.duration,
                fps=args.fps,
                energy_speedup=False if args.no_speedup else None,
                music_path=args.audio or g.get("audio"),
                cut_on=args.cut_on,
                lyrics=args.lyrics,
                scene=args.scene,
            )
            st = res["stats"]
            msg = (
                f"🎬 {st['shots']} 个镜头, {res['total_duration']:.2f}s, 平均 {st['avg_shot']:.2f}s, "
                f"用到 {st['clips_used']}/{st['clips_total']} 个素材, 重音 {len(res['accents'])} 处"
            )
            if args.build:
                w, h = (int(x) for x in args.size.lower().split("x"))
                saved = build_draft(res, args.build, args.drafts_root, w, h)
                res["draft"] = saved.get("draft_path")
                msg += f"\n✅ 草稿: {res['draft']}"
    except (BeatPlanError, BeatDetectError, OSError, ValueError) as e:
        if args.json:
            emit_result(make_result(False, "beat_snap_failed", str(e)), True)
        else:
            print(f"❌ {e}")
        return 1
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    if args.json:
        emit_result(make_result(True, "ok", "", {"output": args.output, **res}), True)
        return 0
    print(msg)
    if args.output:
        print(f"✅ {args.output}")
    elif args.cmd == "snap":
        print(", ".join(f"{c['original']:.3f}->{c['time']:.3f}" for c in res["cuts"]))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
