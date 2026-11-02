"""
混音: 自动 ducking (人声段压低 BGM) 与音效铺设计划写入草稿。

ducking 用音量关键帧实现 (KFTypeVolume, 写在片段的 common_keyframes 里):
人声开始前 attack 秒开始下压, 人声段内保持 depth_db, 人声结束后 release 秒内恢复;
两段人声间隔太短 (< attack + release + hold) 时不恢复, 避免 BGM 一抽一抽 ("pumping")。

人声区间的来源:
  - analysis.json (scripts/analyze_footage.py): transcript 的句子区间, 或 silences 的补集
  - 时间线上的人声 / TTS / 配音片段 (按轨道名识别, 或显式传入片段 / 轨道名)
  - 直接传入 [(start_s, end_s), ...]

关键帧时间: 剪映的关键帧 time_offset 是「素材时间」(截取起点 + 片段内相对时间 x 速度), 见
TrackOpsMixin._keyframe_base 的实测说明; 本模块写音量关键帧时同样换算。关键帧的值是绝对音量
(1.0 = 原始音量), 以片段当前 volume 为基准乘以衰减系数。

命令行 (对已有草稿做 ducking):
    python scripts/core/mix_ops.py --draft 草稿名 [--root 草稿目录] [--bgm-track BGM]
        [--voice-tracks 配音,TTS] [--analysis analysis.json --source transcript --offset 0]
        [--depth -10 --attack 0.15 --release 0.3] [--dry-run] [--json]
"""

import json
import os
import sys
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union

if __name__ == "__main__":  # 直接运行时补齐 scripts/ 与 vendor/ 路径
    _SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for _p in (_SCRIPTS, os.path.join(_SCRIPTS, "vendor")):
        if _p not in sys.path:
            sys.path.insert(0, _p)

import pyJianYingDraft as draft  # noqa: E402
from pyJianYingDraft.keyframe import KeyframeList, KeyframeProperty  # noqa: E402

US = 1_000_000
VOLUME_KF = KeyframeProperty.volume.value  # "KFTypeVolume"

# 按轨道名识别人声 / BGM 轨道 (小写包含匹配)
VOICE_TRACK_HINTS = (
    "voice",
    "vocal",
    "tts",
    "narration",
    "dub",
    "配音",
    "人声",
    "旁白",
    "口播",
    "解说",
    "录音",
    "朗读",
)
BGM_TRACK_HINTS = ("bgm", "music", "音乐", "配乐", "背景")

Range = Tuple[float, float]


# ----------------------------------------------------------------- 纯函数 (秒)


def db_to_gain(db: float) -> float:
    """dB -> 线性音量系数: -10 dB -> 0.316。"""
    return 10 ** (float(db) / 20)


def _as_range(r) -> Optional[Range]:
    if isinstance(r, dict):
        s, e = r.get("start"), r.get("end")
        if e is None and r.get("duration") is not None and s is not None:
            e = float(s) + float(r["duration"])
    else:
        s, e = r[0], r[1]
    if s is None or e is None:
        return None
    s, e = float(s), float(e)
    return (s, e) if e > s else None


def merge_ranges(ranges: Iterable, gap: float = 0.0) -> List[Range]:
    """排序并合并区间: 相邻区间间隔 <= gap 秒时合并。接受 (s, e) 或 {"start", "end"}。"""
    items = sorted(x for x in (_as_range(r) for r in ranges or []) if x)
    out: List[List[float]] = []
    for s, e in items:
        if out and s - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(round(s, 6), round(e, 6)) for s, e in out]


def _load_analysis(analysis: Union[str, dict]) -> dict:
    if isinstance(analysis, str):
        with open(analysis, "r", encoding="utf-8") as f:
            return json.load(f)
    return analysis or {}


def voice_ranges_from_analysis(
    analysis: Union[str, dict], source: str = "auto", offset: float = 0.0, min_len: float = 0.15
) -> List[Range]:
    """
    从 analysis.json 取人声区间 (秒, 已加 offset 换算到时间线)。
    source: "transcript" 用转写句子区间; "silences" 用静音段的补集 (需要 video.duration);
            "auto" 有转写用转写, 否则用静音补集。
    analysis.json 的时间是素材时间; 素材在时间线上从 T 秒开始放、且从素材 S 秒截取时, offset = T - S。
    """
    a = _load_analysis(analysis)
    transcript = a.get("transcript") or []
    ranges: List[Range] = []
    if source in ("transcript", "auto") and transcript:
        ranges = [
            (float(s["start"]), float(s["end"])) for s in transcript if "start" in s and "end" in s
        ]
    elif source in ("silences", "auto"):
        dur = float((a.get("video") or {}).get("duration") or 0)
        if dur <= 0:
            if source == "silences":
                raise ValueError("analysis.json 缺少 video.duration, 无法由静音段推出人声区间")
            return []
        if (a.get("video") or {}).get("has_audio") is False:
            return []
        cur = 0.0
        for s in sorted(a.get("silences") or [], key=lambda x: x["start"]):
            if s["start"] > cur:
                ranges.append((cur, float(s["start"])))
            cur = max(cur, float(s["end"]))
        if cur < dur:
            ranges.append((cur, dur))
    elif source not in ("transcript", "silences", "auto"):
        raise ValueError(f"未知人声来源: {source} (可选 transcript / silences / auto)")
    ranges = [(s + offset, e + offset) for s, e in ranges if e - s >= min_len]
    return merge_ranges(ranges)


def duck_envelope(
    seg_start: float,
    seg_end: float,
    voice_ranges: Iterable,
    depth_db: float = -10,
    attack: float = 0.15,
    release: float = 0.3,
    hold: float = 0.3,
    base_volume: float = 1.0,
) -> List[Tuple[float, float]]:
    """
    计算一段 BGM 在 [seg_start, seg_end] (时间线秒) 内的 ducking 包络, 返回 [(片段内相对秒, 音量), ...]。
    没有人声与该片段重叠时返回 []。
    """
    if seg_end <= seg_start:
        return []
    attack, release, hold = max(0.0, attack), max(0.0, release), max(0.0, hold)
    ducked = base_volume * db_to_gain(depth_db)
    rel = [
        (s, e)
        for s, e in merge_ranges(voice_ranges)
        if e > seg_start - release and s < seg_end + attack
    ]
    # 间隔不够一次完整的 恢复+下压 (再留 hold) 就保持压低
    rel = merge_ranges(rel, gap=attack + release + hold)
    if not rel:
        return []
    raw: List[Tuple[float, float]] = []
    for s, e in rel:
        raw += [(s - attack, base_volume), (s, ducked), (e, ducked), (e + release, base_volume)]

    def value_at(t: float) -> float:
        if t <= raw[0][0]:
            return raw[0][1]
        for (t0, v0), (t1, v1) in zip(raw, raw[1:]):
            if t0 <= t <= t1:
                return v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
        return raw[-1][1]

    pts = [(seg_start, value_at(seg_start))] + [p for p in raw if seg_start < p[0] < seg_end]
    pts.append((seg_end, value_at(seg_end)))
    # 去掉与前后都相同的冗余点 (平台段中间的点), 保留拐点
    out: List[Tuple[float, float]] = []
    for i, (t, v) in enumerate(pts):
        if 0 < i < len(pts) - 1 and abs(pts[i - 1][1] - v) < 1e-9 and abs(pts[i + 1][1] - v) < 1e-9:
            continue
        if out and abs(out[-1][0] - t) < 1e-6:
            out[-1] = (t, v)
            continue
        out.append((t, v))
    if all(abs(v - base_volume) < 1e-9 for _, v in out):
        return []
    return [(round(t - seg_start, 6), round(v, 6)) for t, v in out]


# ----------------------------------------------------------------- 草稿访问 (原生 / 导入片段)


def iter_tracks(script) -> Iterator[Tuple[str, str, list]]:
    """遍历草稿所有轨道 (导入的 + 新建的), 产出 (轨道名, 类型名 video/audio/text/..., 片段列表)。"""
    for t in getattr(script, "imported_tracks", []) or []:
        yield t.name, t.track_type.name, list(getattr(t, "segments", []) or [])
    for name, t in (getattr(script, "tracks", {}) or {}).items():
        yield name, t.track_type.name, list(t.segments)


def seg_range(segment) -> Range:
    """片段在时间线上的 (开始秒, 结束秒)。"""
    tr = segment.target_timerange
    return tr.start / US, (tr.start + tr.duration) / US


def _seg_raw(segment) -> Optional[dict]:
    return getattr(segment, "raw_data", None)


def _seg_volume(segment) -> float:
    raw = _seg_raw(segment)
    if raw is not None:
        return float(raw.get("volume", 1.0))
    return float(getattr(segment, "volume", 1.0))


def _seg_speed(segment) -> float:
    raw = _seg_raw(segment)
    if raw is not None:
        return float(raw.get("speed", 1.0) or 1.0)
    return float(getattr(getattr(segment, "speed", None), "speed", 1.0) or 1.0)


def _seg_source_start(segment) -> int:
    src = getattr(segment, "source_timerange", None)
    return int(src.start) if src is not None else 0


def _is_media_segment(segment) -> bool:
    if isinstance(segment, (draft.AudioSegment, draft.VideoSegment)):
        return True
    raw = _seg_raw(segment)
    return raw is not None and getattr(segment, "source_timerange", None) is not None


def get_volume_keyframes(segment) -> List[Tuple[int, float]]:
    """读出片段的音量关键帧 [(素材时间 us, 音量), ...] (测试 / 检查用)。"""
    raw = _seg_raw(segment)
    if raw is not None:
        lists = [
            kl for kl in raw.get("common_keyframes") or [] if kl.get("property_type") == VOLUME_KF
        ]
        return [
            (int(k["time_offset"]), float(k["values"][0]))
            for kl in lists
            for k in kl["keyframe_list"]
        ]
    out = []
    for kl in getattr(segment, "common_keyframes", []) or []:
        if kl.keyframe_property == KeyframeProperty.volume:
            out += [(k.time_offset, k.values[0]) for k in kl.keyframes]
    return out


def write_volume_keyframes(segment, points: Sequence[Tuple[float, float]]) -> int:
    """
    用 points [(片段内相对秒, 音量), ...] 替换片段已有的音量关键帧; points 为空时只清除。
    相对时间换算成剪映的素材时间: 截取起点 + 相对时间 x 速度。返回写入的关键帧数。
    """
    base, spd = _seg_source_start(segment), _seg_speed(segment)
    kl = KeyframeList(KeyframeProperty.volume)
    for t, v in points:
        kl.add_keyframe(base + int(round(float(t) * US * spd)), float(v))
    raw = _seg_raw(segment)
    if raw is not None:
        lists = [
            x for x in raw.get("common_keyframes") or [] if x.get("property_type") != VOLUME_KF
        ]
        if points:
            lists.append(kl.export_json())
        raw["common_keyframes"] = lists
    else:
        kept = [
            x for x in segment.common_keyframes if x.keyframe_property != KeyframeProperty.volume
        ]
        segment.common_keyframes = kept + ([kl] if points else [])
    return len(points)


# ----------------------------------------------------------------- Mixin


class MixOpsMixin:
    """JyProject 的混音 Mixin: 自动 ducking、铺音效。依赖 MediaOpsMixin.add_audio_safe / cloud_manager。"""

    def _tracks_by_hint(self, hints: Sequence[str], kinds=("audio",)) -> List[Tuple[str, list]]:
        out = []
        for name, kind, segs in iter_tracks(self.script):
            if kind in kinds and any(h in str(name).lower() for h in hints):
                out.append((name, segs))
        return out

    def _segments_of_tracks(self, track_names: Iterable[str]) -> List:
        wanted = set(track_names)
        found = {n for n, _k, _s in iter_tracks(self.script) if n in wanted}
        missing = wanted - found
        if missing:
            raise KeyError(f"草稿里没有这些轨道: {', '.join(sorted(missing))}")
        return [s for n, _k, segs in iter_tracks(self.script) if n in wanted for s in segs]

    def voice_ranges_from_tracks(
        self, track_names: Optional[Iterable[str]] = None, exclude: Iterable[str] = ()
    ) -> List[Range]:
        """
        时间线上人声 / TTS 片段的区间 (秒)。track_names 为空时按轨道名识别
        (含 voice / tts / 配音 / 人声 / 旁白 / 口播 ... 的音频轨)。
        """
        if track_names:
            segs = self._segments_of_tracks(track_names)
        else:
            ex = set(exclude)
            segs = [
                s for n, ss in self._tracks_by_hint(VOICE_TRACK_HINTS) if n not in ex for s in ss
            ]
        return merge_ranges(
            seg_range(s) for s in segs if _is_media_segment(s) and _seg_volume(s) > 0
        )

    def duck_bgm(
        self,
        bgm_segment,
        voice_ranges: Optional[Iterable] = None,
        voice_segments: Optional[Iterable] = None,
        depth_db: float = -10,
        attack: float = 0.15,
        release: float = 0.3,
        hold: float = 0.3,
        analysis: Union[str, dict, None] = None,
        analysis_source: str = "auto",
        analysis_offset: float = 0.0,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        在人声段把 BGM 片段压低 depth_db (默认 -10 dB, 口播建议 -8 ~ -12), 用音量关键帧实现。
        人声区间 = voice_ranges [(s, e) 秒] ∪ voice_segments (片段对象) ∪ analysis.json 推出的区间。
        attack / release: 下压 / 恢复的过渡时间 (秒); hold: 两段人声间隔小于 attack+release+hold 时保持压低。
        已有的音量关键帧会被替换 (可重复调用)。dry_run=True 只计算不写入。
        返回 {"points": [(相对秒, 音量)], "voice_ranges": [...], "keyframes": n}。
        """
        if bgm_segment is None or not _is_media_segment(bgm_segment):
            raise ValueError("duck_bgm 需要一个音频 / 视频片段")
        ranges = list(voice_ranges or [])
        ranges += [seg_range(s) for s in voice_segments or [] if s is not bgm_segment]
        if analysis is not None:
            ranges += voice_ranges_from_analysis(analysis, analysis_source, analysis_offset)
        ranges = merge_ranges(ranges)
        start, end = seg_range(bgm_segment)
        pts = duck_envelope(
            start, end, ranges, depth_db, attack, release, hold, _seg_volume(bgm_segment)
        )
        n = 0 if dry_run else write_volume_keyframes(bgm_segment, pts)
        return {
            "segment": [round(start, 3), round(end, 3)],
            "points": pts,
            "keyframes": n,
            "voice_ranges": [r for r in ranges if r[1] > start and r[0] < end],
        }

    def auto_duck(
        self,
        bgm_track: Optional[str] = None,
        voice_tracks: Optional[Iterable[str]] = None,
        analysis: Union[str, dict, None] = None,
        analysis_source: str = "auto",
        analysis_offset: float = 0.0,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        对整条 BGM 轨做 ducking。bgm_track 为空时按轨道名识别 (bgm / music / 音乐 / 配乐 ...);
        人声来自 voice_tracks (为空则按轨道名识别) 和 analysis.json。kwargs 透传给 duck_bgm。
        """
        if bgm_track:
            bgm_tracks = [(bgm_track, self._segments_of_tracks([bgm_track]))]
        else:
            bgm_tracks = self._tracks_by_hint(BGM_TRACK_HINTS)
        if not bgm_tracks:
            raise KeyError("没有找到 BGM 轨道 (轨道名含 BGM / music / 音乐), 请用 bgm_track 指定")
        names = [n for n, _ in bgm_tracks]
        ranges = self.voice_ranges_from_tracks(voice_tracks, exclude=names)
        if analysis is not None:
            ranges += voice_ranges_from_analysis(analysis, analysis_source, analysis_offset)
        ranges = merge_ranges(ranges)
        if not ranges:
            raise ValueError(
                "没有找到人声区间: 请指定 voice_tracks、传入 analysis.json, 或把配音轨命名为 配音 / TTS"
            )
        results = []
        for name, segs in bgm_tracks:
            for seg in segs:
                if _is_media_segment(seg):
                    r = self.duck_bgm(seg, voice_ranges=ranges, **kwargs)
                    r["track"] = name
                    results.append(r)
        return {
            "bgm_tracks": names,
            "voice_ranges": ranges,
            "segments": results,
            "keyframes": sum(r["keyframes"] for r in results),
        }

    def _free_track_name(self, name: str) -> str:
        """新建轨道名不能与导入的轨道重名。"""
        imported = {t.name for t in getattr(self.script, "imported_tracks", []) or []}
        if name not in imported:
            return name
        i = 2
        while f"{name}_{i}" in imported:
            i += 1
        return f"{name}_{i}"

    def apply_sfx_plan(self, plan: Union[dict, list], track_name: str = "SFX") -> Dict[str, Any]:
        """
        把 sfx_designer 生成的铺设计划写入草稿音频轨 (重叠时自动放到 SFX_1, SFX_2 ...)。
        计划项: {"start", "duration", "volume", "source": "local"|"jianying_cloud", "path", "sfx_id", "name"}。
        云端音效经 cloud_manager 按 id 下载到本地缓存 (需要联网; 会员音效不下载)。
        """
        items = plan.get("items", []) if isinstance(plan, dict) else list(plan)
        track_name = self._free_track_name(track_name)
        added, failed = [], []
        for it in items:
            path = it.get("path") or ""
            if it.get("source") != "local" or not path:
                try:
                    path = self.cloud_manager.download_asset(str(it.get("sfx_id"))) or ""
                except Exception as e:  # 网络 / 解析错误
                    print(f"⚠️ 音效下载失败 {it.get('name')}: {e}")
                    path = ""
            if not path or not os.path.exists(path):
                failed.append({**it, "reason": "音效文件不可用 (下载失败或本地文件不存在)"})
                continue
            dur = float(it.get("duration") or 0)
            kwargs = {"volume": float(it.get("volume", 1.0))}
            if it.get("trimmed"):
                kwargs["fade_out"] = int(min(0.08, dur / 4) * US)
            seg = self.add_audio_safe(
                path,
                start_time=int(round(float(it["start"]) * US)),
                duration=int(round(dur * US)) if dur > 0 else None,
                track_name=track_name,
                **kwargs,
            )
            if seg is None:
                failed.append({**it, "reason": "无法读取音效文件"})
                continue
            added.append({"name": it.get("name"), "start": it["start"], "path": path})
        return {"added": added, "failed": failed, "track": track_name}


# ----------------------------------------------------------------- CLI


def _parse_list(s: Optional[str]) -> Optional[List[str]]:
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


def _main(argv=None) -> int:
    import argparse

    from utils.cli_protocol import emit_result, make_result

    ap = argparse.ArgumentParser(description="自动 ducking: 人声段用音量关键帧压低 BGM")
    ap.add_argument("--draft", required=True, help="草稿名")
    ap.add_argument("--root", default=None, help="草稿根目录 (默认剪映草稿目录)")
    ap.add_argument("--bgm-track", default=None, help="BGM 轨道名 (默认按名称识别 BGM / 音乐)")
    ap.add_argument(
        "--voice-tracks",
        default=None,
        help="人声轨道名, 逗号分隔 (默认按名称识别 配音 / TTS / 人声)",
    )
    ap.add_argument("--analysis", default=None, help="analyze_footage.py 生成的 analysis.json")
    ap.add_argument("--source", default="auto", choices=["auto", "transcript", "silences"])
    ap.add_argument(
        "--offset", type=float, default=0.0, help="analysis 时间 -> 时间线时间的偏移 (秒)"
    )
    ap.add_argument("--depth", type=float, default=-10, help="压低多少 dB (默认 -10)")
    ap.add_argument("--attack", type=float, default=0.15)
    ap.add_argument("--release", type=float, default=0.3)
    ap.add_argument("--hold", type=float, default=0.3)
    ap.add_argument("--dry-run", action="store_true", help="只计算包络, 不保存草稿")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    from jy_wrapper import JyProject

    class _Project(JyProject, MixOpsMixin):
        pass

    try:
        p = _Project(args.draft, drafts_root=args.root, overwrite=False)
        res = p.auto_duck(
            args.bgm_track,
            _parse_list(args.voice_tracks),
            analysis=args.analysis,
            analysis_source=args.source,
            analysis_offset=args.offset,
            depth_db=args.depth,
            attack=args.attack,
            release=args.release,
            hold=args.hold,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            p.save()
    except (KeyError, ValueError, OSError) as e:
        emit_result(make_result(False, "duck_failed", str(e)), args.json)
        if not args.json:
            print(f"❌ {e}")
        return 1
    if args.json:
        emit_result(make_result(True, "ok", "", res), True)
    else:
        print(
            f"✅ BGM 轨 {', '.join(res['bgm_tracks'])}: {len(res['voice_ranges'])} 段人声, "
            f"写入 {res['keyframes']} 个音量关键帧{' (dry-run 未保存)' if args.dry_run else ''}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
