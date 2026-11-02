"""最佳 take Mixin: 按 best_take.select_takes 的结果把保留的 take 放进轨道。"""

from typing import Any, Dict, List, Optional


class BestTakeOpsMixin:
    """
    JyProject 的最佳 take Mixin (需与 MediaOpsMixin 一起使用, 依赖 add_clip)。
    """

    def add_best_takes(
        self,
        selection: Dict[str, Any],
        track_name: str = "VideoTrack",
        punch_in: Optional[float] = None,
        use_plan: bool = True,
        pad: float = 0.08,
    ) -> List[Any]:
        """
        selection: best_take.select_takes() 的结果。
        use_plan:  单源时按精剪计划 (plan.keep) 放片段 (同时去掉了口癖 / 气口);
                   多源或 use_plan=False 时按 sequence 逐句拼接, 每句前后各留 pad 秒。
        punch_in:  奇数段缩放到该倍数 (假景别遮跳剪), 1.0–4.0。
        """
        if punch_in is not None and not (1.0 <= float(punch_in) <= 4.0):
            raise ValueError(f"punch_in 应在 1.0–4.0 之间: {punch_in}")
        plan = selection.get("plan")
        if use_plan and plan and plan.get("keep"):
            from core.speech_cut_ops import SpeechCutOpsMixin

            media = plan.get("source") or selection["sources"][0]
            return SpeechCutOpsMixin.add_speech_cleanup(
                self, media, plan, track_name=track_name, punch_in=punch_in
            )
        segments = []
        for idx, item in enumerate(selection.get("sequence") or []):
            start = max(0.0, float(item["start"]) - pad)
            dur = float(item["end"]) + pad - start
            seg = self.add_clip(
                item["source"],
                source_start=int(round(start * 1_000_000)),
                duration=int(round(dur * 1_000_000)),
                track_name=track_name,
            )
            if seg is None:
                print(f"⚠️ 第 {idx + 1} 句添加失败: {item['text']}")
                continue
            if punch_in and idx % 2 == 1 and getattr(seg, "clip_settings", None) is not None:
                seg.clip_settings.scale_x = float(punch_in)
                seg.clip_settings.scale_y = float(punch_in)
            segments.append(seg)
        return segments
