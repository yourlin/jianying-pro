"""口播精剪 Mixin: 按 speech_cleanup.plan_cleanup 的 keep 区间把源视频切段放进轨道。"""

from typing import Any, Dict, List, Optional, Union


class SpeechCutOpsMixin:
    """
    JyProject 的口播精剪 Mixin (需与 MediaOpsMixin / TrackOpsMixin 一起使用)。
    """

    def add_speech_cleanup(
        self,
        media_path: str,
        plan_or_transcript: Union[Dict[str, Any], List[dict]],
        track_name: str = "VideoTrack",
        punch_in: Optional[float] = None,
        target_start: Union[str, int, None] = None,
        **plan_kwargs,
    ) -> List[Any]:
        """
        按精剪计划把 media_path 的保留区间依次接到 track_name 上, 返回片段列表。

        plan_or_transcript: plan_cleanup() 的结果 (含 keep), 或 analysis.json / 转写 (此时现场规划,
                            plan_kwargs 透传给 plan_cleanup)。
        punch_in:           跳剪遮挡 (假景别): 奇数段缩放到该倍数, 如 1.25 (见 edit_grammar.md,
                            1.03–1.15x 的小跳最难看, 不建议)。
        target_start:       第一段放在时间线的位置 (默认接在轨道末尾)。
        """
        plan = plan_or_transcript
        if not (isinstance(plan, dict) and "keep" in plan):
            from speech_cleanup import plan_cleanup

            plan = plan_cleanup(plan_or_transcript, **plan_kwargs)
        if punch_in is not None and not (1.0 <= float(punch_in) <= 4.0):
            raise ValueError(f"punch_in 应在 1.0–4.0 之间: {punch_in}")

        segments = []
        cursor = None
        if target_start is not None:
            from utils.formatters import safe_tim

            cursor = safe_tim(target_start)
        for idx, k in enumerate(plan["keep"]):
            src_us = int(round(k["start"] * 1_000_000))
            dur_us = int(round((k["end"] - k["start"]) * 1_000_000))
            if dur_us <= 0:
                continue
            seg = self.add_clip(
                media_path,
                source_start=src_us,
                duration=dur_us,
                target_start=cursor,
                track_name=track_name,
            )
            if seg is None:
                print(f"⚠️ 第 {idx + 1} 段添加失败: {k['start']:.2f}s–{k['end']:.2f}s")
                continue
            if punch_in and idx % 2 == 1 and getattr(seg, "clip_settings", None) is not None:
                seg.clip_settings.scale_x = float(punch_in)
                seg.clip_settings.scale_y = float(punch_in)
            segments.append(seg)
            if cursor is not None:
                cursor = seg.target_timerange.start + seg.target_timerange.duration
        return segments
