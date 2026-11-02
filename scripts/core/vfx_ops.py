import os
import time
from typing import Union, Optional
import pyJianYingDraft as draft
from utils.formatters import safe_tim, tim
from utils.frame_align import (
    TRANSITION_WARN_RATIO,
    frame_us,
    project_fps,
    snap_range,
    snap_us,
    transition_duration,
)

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
        start_us, dur_us = snap_range(safe_tim(start_time), safe_tim(duration), project_fps(self))
        self.script.add_effect(eff_type, draft.Timerange(start_us, dur_us), track_name=track_name)
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
        tr = video_segment.transition
        # 记下 skill 请求的时长; tr.duration 改成按剪映规则规整后的值
        tr.requested_duration = int(tr.duration)
        self._normalize_transition(video_segment)
        self._warn_transition_handles(video_segment)
        return tr

    def _segment_track(self, segment):
        for track in self.script.tracks.values():
            if segment in getattr(track, "segments", []):
                return track
        return None

    @staticmethod
    def _next_segment(track, segment, fps=30):
        """同轨上紧接在 segment 后面的片段 (起点在它终点 1 帧以内), 没有则 None。"""
        end = segment.target_timerange.start + segment.target_timerange.duration
        tol = frame_us(fps)
        best = None
        for s in getattr(track, "segments", []) if track is not None else []:
            st = s.target_timerange.start
            if s is not segment and st > segment.target_timerange.start and abs(st - end) <= tol:
                if best is None or st < best.target_timerange.start:
                    best = s
        return best

    def _normalize_transition(self, segment):
        """
        转场时长按剪映的规则预先规整, skill 记录的时长和剪映显示的一致:
        重叠转场 = 2 * floor(min(d, 相邻较短片段 / 2) / 2 / 帧长) 帧; 非重叠转场只对齐到整帧。
        被截短超过 TRANSITION_WARN_RATIO 时记一条警告 (进装配报告)。返回 (请求时长, 规整后时长) 微秒。
        """
        tr = segment.transition
        if tr is None:
            return None
        fps = project_fps(self)
        req = int(getattr(tr, "requested_duration", tr.duration))
        if tr.is_overlap:
            nxt = self._next_segment(self._segment_track(segment), segment, fps)
            seg_d = snap_us(segment.target_timerange.duration, fps)
            nxt_d = snap_us(nxt.target_timerange.duration, fps) if nxt is not None else None
            new = transition_duration(req, seg_d, nxt_d, fps)
        else:
            new = snap_us(req, fps)
        tr.duration = new
        if getattr(self, "_transition_notes", None) is None:
            self._transition_notes = {}
        if getattr(self, "warnings", None) is None:
            self.warnings = []
        notes, warns = self._transition_notes, self.warnings
        old = notes.pop(segment.segment_id, None)
        if old in warns:
            warns.remove(old)
        if req > 0 and 1 - new / req > TRANSITION_WARN_RATIO:
            start_s = segment.target_timerange.start / 1e6
            why = "片段太短放不下" if new == 0 else "超过相邻较短片段的一半 (剪映会截短)"
            msg = (f"转场「{tr.name}」@{start_s:.2f}s: 请求 {req / 1000:.0f}ms, {why}, "
                   f"按剪映规则规整为 {new / 1000:.1f}ms")  # fmt: skip
            notes[segment.segment_id] = msg
            warns.append(msg)
            if msg != old:
                print(f"⚠️ {msg}")
        return req, new

    def _renormalize_track_transitions(self, track_name):
        """轨道上新加了片段: 已挂的转场按新的相邻片段重新规整 (转场常在后一段加入前就挂上)。"""
        track = self.script.tracks.get(track_name) if track_name else None
        for seg in getattr(track, "segments", None) or []:
            if getattr(seg, "transition", None) is not None:
                self._normalize_transition(seg)

    def transition_report(self):
        """所有转场的请求时长 / 规整后时长 (秒), 供装配报告。"""
        out = []
        for name, track in self.script.tracks.items():
            for seg in getattr(track, "segments", []):
                tr = getattr(seg, "transition", None)
                if tr is not None:
                    req = int(getattr(tr, "requested_duration", tr.duration))
                    out.append({"track": name, "at": round(seg.target_timerange.start / 1e6, 3),
                                "name": tr.name, "requested": round(req / 1e6, 4),
                                "duration": round(tr.duration / 1e6, 4)})  # fmt: skip
        return out

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
