"""
动态图形 Mixin (T12): 把 assets/motion_graphics 的参数化模板渲染成视频并放到时间线上。

渲染在 scripts/motion_graphics.py (playwright 逐帧截图 + ffmpeg)。默认输出 ProRes 4444 .mov (带透明),
导入时跳过 add_media_safe 的 H.264 转码 (转码会丢掉透明通道), 直接复制进草稿目录。
依赖 MediaOpsMixin (_stage_local_asset / _add_video_safe / add_media_safe) 和 JyProjectBase。
"""

import os
import time
from typing import Any, Optional, Union


class MotionGraphicsOpsMixin:
    """JyProject 的动态图形模板 Mixin。"""

    def add_motion_graphic(
        self,
        template: str,
        data: Any,
        start_time: Union[str, int, None] = None,
        duration: Union[str, int, None] = None,
        track_name: str = "MotionGraphics",
        background: str = "transparent",
        output_dir: Optional[str] = None,
        fmt: str = "mov",
        **opts,
    ):
        """
        渲染动效模板并加到时间线 (画幅默认跟草稿画布一致):
            p.add_motion_graphic("bar_chart", {"items": [{"label": "A", "value": 3}]}, "2s")
            p.add_motion_graphic("counter", {"to": 10000, "suffix": "+"}, "5s", background="#101820", fmt="mp4")
        template: bar_chart / line_chart / counter / callout / progress_bar / map_route / list_reveal
        duration: 片段时长 (默认 = 模板估算时长); 也决定动画总长。
        background: transparent (叠加用, 输出 .mov) 或颜色; fmt: mov / webm / mp4 (mp4 不透明)。
        opts: 透传给 motion_graphics.render_video (hold / theme / fps / aspect / size ...)。
        缺 playwright / ffmpeg 时抛 motion_graphics.MGDependencyError。返回片段。
        """
        import motion_graphics as mg
        from utils.formatters import safe_tim

        if fmt not in ("mov", "webm", "mp4"):
            raise mg.MGValidationError(f"fmt 只能是 mov / webm / mp4, 收到 {fmt}")
        if start_time is None:
            start_time = self.get_track_duration(track_name)
        if duration is not None and "duration" not in opts:
            opts["duration"] = safe_tim(duration) / 1e6
        if not any(k in opts for k in ("aspect", "size", "width", "height")):
            opts["width"], opts["height"] = int(self.script.width), int(self.script.height)
        out_dir = output_dir or os.path.join(self.draft_dir, "temp_assets")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, f"mg_{template}_{int(time.time() * 1000)}.{fmt}")
        info = mg.render_video(template, data, out, background=background, **opts)
        clip_dur = duration if duration is not None else int(info["duration"] * 1e6)
        if info["transparent"]:
            # 透明素材不能走 add_media_safe (会被规范化成 yuv420p H.264, 透明通道丢失)
            staged = self._stage_local_asset(info["output"])
            seg = self._add_video_safe(staged, start_time, clip_dur, track_name=track_name)
        else:
            seg = self.add_media_safe(info["output"], start_time, clip_dur, track_name=track_name)
        if seg is not None:
            seg._motion_graphic = info
        return seg
