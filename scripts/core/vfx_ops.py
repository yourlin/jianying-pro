import os
import time
from typing import Union, Optional
import pyJianYingDraft as draft
from utils.formatters import safe_tim, tim

class VfxOpsMixin:
    """
    JyProject 的特效与转场 Mixin。
    """
    def add_effect_simple(self, effect_name: str, start_time: Union[str, int] = None, duration: Union[str, int] = "3s", track_name: str = "EffectTrack"):
        if start_time is None:
            start_time = self.get_track_duration(track_name)
        self._ensure_track(draft.TrackType.effect, track_name)
        
        eff_type = self._resolve_enum(draft.VideoSceneEffectType, effect_name)
        if not eff_type: return None
        
        # 走 script.add_effect 以同步注册 video_effects 素材 (直接 add_segment 会漏注册)
        self.script.add_effect(
            eff_type, draft.Timerange(safe_tim(start_time), safe_tim(duration)), track_name=track_name
        )
        return self.script.tracks[track_name].segments[-1]

    def add_transition_simple(
        self,
        transition_name: str,
        video_segment: Optional[draft.VideoSegment] = None,
        duration: Union[str, int] = "1s",
        track_name: Optional[str] = None,
    ):
        # 兼容调用：如果未直接给 segment，则尝试从指定轨道获取最后一个视频片段
        if video_segment is None and track_name:
            track = self.script.tracks.get(track_name)
            if not track or not getattr(track, "segments", None):
                return None
            video_segment = track.segments[-1]

        if video_segment is None:
            return None

        trans_type = self._resolve_enum(draft.TransitionType, transition_name)
        if not trans_type: return None
        
        video_segment.add_transition(trans_type, duration=duration)
        self._warn_transition_handles(video_segment)
        return video_segment.transition

    def _warn_transition_handles(self, segment):
        """重叠转场需要后一段在入点之前有余量画面, 否则剪映在时间线上显示斜纹 (定格)。"""
        tr = segment.transition
        if not tr or not tr.is_overlap:
            return
        for track in self.script.tracks.values():
            segs = getattr(track, "segments", [])
            if segment in segs:
                i = segs.index(segment)
                nxt = segs[i + 1] if i + 1 < len(segs) else None
                if nxt is not None and getattr(nxt, "source_timerange", None) is not None \
                        and nxt.source_timerange.start < tr.duration / 2:
                    print(f"ℹ️ 转场「{tr.name}」: 后一段从素材开头开始，缺少转场余量，剪映中会显示斜纹。"
                          "可用 source_start 让后一段从素材稍后位置开始。")
                return

    def add_web_asset_safe(self, html_path: str, start_time: Union[str, int] = None, duration: Union[str, int] = "5s", 
                           track_name: str = "WebVfxTrack", output_dir: Optional[str] = None):
        from web_recorder import record_web_animation
        
        if start_time is None:
            start_time = self.get_track_duration(track_name)
        if output_dir is None:
            output_dir = os.path.join(self.root, self.name, "temp_assets")
        os.makedirs(output_dir, exist_ok=True)
        
        video_output = os.path.join(output_dir, f"web_vfx_{int(time.time())}.webm")
        if record_web_animation(html_path, video_output, max_duration=safe_tim(duration)/1e6 + 5):
            return self.add_media_safe(video_output, start_time, duration, track_name=track_name)
        return None
