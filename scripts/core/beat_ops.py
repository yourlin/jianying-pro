"""
卡点混剪落地: 把 beat_snap.plan_beat_montage 的计划写进草稿 (视频轨 + BGM + 强拍 punch_in)。

    class P(JyProject, BeatOpsMixin): ...
    p.apply_beat_plan(plan)

依赖 JyProject 的 add_media_safe / _stage_local_asset / _ensure_track / add_keyframes。
"""

import os
from typing import Dict, List, Optional

import pyJianYingDraft as draft
from pyJianYingDraft import trange

US = 1_000_000


class BeatOpsMixin:
    """JyProject 的节拍混剪 Mixin。"""

    def add_music_from(
        self,
        music_path: str,
        source_start_us: int = 0,
        duration_us: Optional[int] = None,
        target_start_us: int = 0,
        track_name: str = "BGM",
        volume: float = 0.9,
        fade_out_us: int = 0,
    ):
        """从音乐的 source_start_us 处开始截取 duration_us, 放在时间轴 target_start_us (add_audio_safe 只能从头截)。"""
        if not music_path or not os.path.exists(music_path):
            print(f"❌ Music Missing: {music_path}")
            return None
        original = os.path.basename(music_path)
        staged = self._stage_local_asset(music_path)
        names = getattr(self, "_display_names", None)
        if names is None:
            names = self._display_names = {}
        names[os.path.abspath(staged)] = original
        self._ensure_track(draft.TrackType.audio, track_name)
        mat = draft.AudioMaterial(staged, original)
        avail = max(0, int(mat.duration) - int(source_start_us))
        dur = min(int(duration_us), avail) if duration_us else avail
        if dur <= 0:
            print(f"❌ 音乐起点超出时长: {source_start_us / US:.2f}s")
            return None
        seg = draft.AudioSegment(
            mat,
            trange(int(target_start_us), dur),
            source_timerange=trange(int(source_start_us), dur),
            volume=float(volume),
        )
        if fade_out_us:
            seg.add_fade(0, int(min(fade_out_us, dur // 2)))
        self.script.add_segment(seg, track_name)
        return seg

    def apply_beat_plan(
        self,
        plan: Dict,
        track_name: Optional[str] = None,
        music_track: str = "BGM",
        mute_footage: Optional[bool] = None,
        accents: bool = True,
        punch_amount: float = 0.12,
        music_fade_out: float = 0.5,
    ) -> Dict:
        """
        按计划铺镜头: 每个 shot 用 add_clip 参数 (微秒) 放到视频轨, 素材默认静音;
        plan.music.path 存在时从 music.source_start 截取放到 BGM 轨; accents 里的镜头加 punch_in 关键帧。
        返回 {"video": [segment...], "music": segment|None, "missing": [path...]}
        """
        mute = plan.get("mute_footage", True) if mute_footage is None else bool(mute_footage)
        segs: List = []
        missing: List[str] = []
        accent_shots = {a["shot"]: a for a in plan.get("accents") or []} if accents else {}
        for sh in plan.get("shots") or []:
            ac = dict(sh["add_clip"])
            tn = track_name or ac.get("track_name") or "VideoTrack"
            seg = self.add_media_safe(
                ac["media_path"],
                int(ac["target_start"]),
                int(ac["duration"]),
                tn,
                source_start=int(ac["source_start"]),
            )
            if seg is None:
                missing.append(ac["media_path"])
                continue
            if mute and hasattr(seg, "volume"):
                seg.volume = 0.0
            a = accent_shots.get(sh["index"])
            if a is not None:
                d = seg.target_timerange.duration
                hit = min(d, int(0.15 * US))
                k = 1.0 + float(punch_amount)
                self.add_keyframes(
                    seg, "scale", [(0, 1.0), (hit, k), (max(hit, d - 1), k), (d, 1.0)]
                )
            segs.append(seg)
        music = plan.get("music") or {}
        mseg = None
        if music.get("path"):
            mseg = self.add_music_from(
                music["path"],
                int(music.get("source_start_us", 0)),
                int(music.get("duration_us") or 0) or None,
                0,
                music_track,
                float(music.get("volume", 0.9)),
                int(music_fade_out * US),
            )
        if missing:
            print(f"⚠️ {len(missing)} 个镜头素材没放进去: {sorted(set(missing))[:5]}")
        return {"video": segs, "music": mseg, "missing": missing}
