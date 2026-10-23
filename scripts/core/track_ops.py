from typing import Iterable, Optional, Tuple, Union

import pyJianYingDraft as draft
from utils.formatters import safe_tim
from utils.panel_assets import PanelAssetUnavailable, is_vip_member, resolve_panel_member

# 关键帧属性的友好别名
KEYFRAME_ALIASES = {
    "x": "position_x", "position_x": "position_x", "位置x": "position_x",
    "y": "position_y", "position_y": "position_y", "位置y": "position_y",
    "scale": "uniform_scale", "uniform_scale": "uniform_scale", "缩放": "uniform_scale",
    "scale_x": "scale_x", "scale_y": "scale_y",
    "rotation": "rotation", "旋转": "rotation",
    "alpha": "alpha", "opacity": "alpha", "不透明度": "alpha",
    "brightness": "brightness", "亮度": "brightness",
    "contrast": "contrast", "对比度": "contrast",
    "saturation": "saturation", "饱和度": "saturation",
    "volume": "volume", "音量": "volume",
}

# 画中画位置预设 (transform 单位: 半个画布宽/高)
PIP_POSITIONS = {
    "center": (0.0, 0.0),
    "top_left": (-0.6, 0.6), "top_right": (0.6, 0.6),
    "bottom_left": (-0.6, -0.6), "bottom_right": (0.6, -0.6),
    "left": (-0.5, 0.0), "right": (0.5, 0.0),
}


class TrackOpsMixin:
    """
    JyProject 的多轨道增强 Mixin：画中画、分屏、视频动画、滤镜、蒙版、关键帧、背景填充。
    所有特效名称均支持内置枚举 + 剪映 11.x 面板索引 (含 VIP 素材)。
    """

    def _ensure_overlay_track(self, track_name: str):
        """创建位于现有视频轨之上的新视频轨 (画中画层)。"""
        if track_name in self.script.tracks:
            return
        n_video = sum(
            1 for t in self.script.tracks.values() if t.track_type == draft.TrackType.video
        )
        self.script.add_track(draft.TrackType.video, track_name, relative_index=n_video)

    def add_overlay(
        self,
        media_path: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = None,
        track_name: str = "Overlay",
        *,
        position: Union[str, Tuple[float, float]] = "center",
        scale: float = 0.4,
        alpha: float = 1.0,
        rotation: float = 0.0,
        source_start: Union[str, int] = 0,
    ):
        """
        画中画：把素材放到主视频之上的独立轨道。
        position: 预设名 (center/top_left/top_right/bottom_left/bottom_right/left/right)
                  或 (x, y)，单位为半个画布宽/高，右/上为正。
        """
        self._ensure_overlay_track(track_name)
        seg = self.add_media_safe(media_path, start_time, duration, track_name, source_start=source_start)
        if not isinstance(seg, draft.VideoSegment):
            return seg
        x, y = PIP_POSITIONS.get(position, (0.0, 0.0)) if isinstance(position, str) else position
        cs = seg.clip_settings
        cs.transform_x, cs.transform_y = float(x), float(y)
        cs.scale_x = cs.scale_y = float(scale)
        cs.alpha, cs.rotation = float(alpha), float(rotation)
        return seg

    def add_split_screen(
        self,
        media_paths: Iterable[str],
        start_time: Union[str, int] = 0,
        duration: Union[str, int] = None,
        layout: str = "horizontal",
        track_prefix: str = "Split",
    ):
        """分屏：2~4 个素材并排 (horizontal / vertical / grid)。返回片段列表。"""
        paths = list(media_paths)
        n = len(paths)
        if not 2 <= n <= 4:
            raise ValueError("add_split_screen 需要 2~4 个素材")
        if layout == "grid" or n > 3:
            cells = [(-0.5, 0.5), (0.5, 0.5), (-0.5, -0.5), (0.5, -0.5)][:n]
            scale = 0.5
        elif layout == "vertical":
            step = 2.0 / n
            cells = [(0.0, 1 - step / 2 - i * step) for i in range(n)]
            scale = 1.0 / n
        else:
            step = 2.0 / n
            cells = [(-1 + step / 2 + i * step, 0.0) for i in range(n)]
            scale = 1.0 / n
        segs = []
        for i, (path, pos) in enumerate(zip(paths, cells)):
            segs.append(
                self.add_overlay(
                    path, start_time, duration, f"{track_prefix}_{i + 1}", position=pos, scale=scale
                )
            )
        return segs

    def add_sticker(
        self,
        sticker_name: str,
        start_time: Union[str, int] = 0,
        duration: Union[str, int] = "3s",
        track_name: str = "Sticker",
        *,
        position: Union[str, Tuple[float, float]] = "center",
        scale: float = 1.0,
        rotation: float = 0.0,
        anim_in: Optional[str] = None,
        anim_out: Optional[str] = None,
        anim_loop: Optional[str] = None,
    ):
        """
        添加贴纸 (剪映 11.x 面板贴纸, 名称或 resource_id; 先运行 sync_jy_panel.py 刷新索引)。
        anim_in / anim_out / anim_loop: 贴纸动画名称 (见 panel_sticker_animations.csv), 也可之后用 add_sticker_animation。
        """
        from utils.panel_assets import find_panel_asset

        row = find_panel_asset("panel_stickers.csv", str(sticker_name))
        if not row:
            print(f"⚠️ Sticker not found: {sticker_name}")
            return None
        if row.get("is_vip") == "1" and not getattr(self, "allow_vip", True):
            print(f"⛔ '{row['name']}' 为 VIP 贴纸，已按 allow_vip=False 跳过")
            return None
        if track_name not in self.script.tracks:
            self.script.add_track(draft.TrackType.sticker, track_name)
        x, y = PIP_POSITIONS.get(position, (0.0, 0.0)) if isinstance(position, str) else position
        cs = draft.ClipSettings(transform_x=float(x), transform_y=float(y),
                                scale_x=float(scale), scale_y=float(scale), rotation=float(rotation))
        seg = draft.StickerSegment(row["resource_id"], draft.Timerange(safe_tim(start_time), safe_tim(duration)),
                                   clip_settings=cs)
        self.script.add_segment(seg, track_name)
        for kind, name in (("in", anim_in), ("out", anim_out), ("loop", anim_loop)):
            if name:
                self.add_sticker_animation(seg, name, kind)
        return seg

    def add_sticker_animation(self, segment, name: str, kind: str = "in", duration: Union[str, int, None] = None):
        """
        贴纸动画 (贴纸面板「动画」): kind = in 入场 / out 出场 / loop 循环; duration 默认取动画自身时长。
        循环动画的 duration 是一个周期 (剪映会重复播放)。同一种 kind 重复设置时替换。
        """
        import json as _json

        from utils.panel_assets import _local_md5_index, load_panel_rows

        sub = {"in": "intro", "out": "outro", "loop": "loop"}.get(kind)
        if sub is None:
            raise ValueError(f"kind 只能是 in / out / loop, 收到 {kind}")
        rows = [r for r in load_panel_rows("panel_sticker_animations.csv") if r["sub_type"] == sub]
        row = next((r for r in rows if name in (r["name"], r["resource_id"])), None)
        if row is None:
            raise ValueError(f"未知贴纸{kind}动画: {name} (可选: {'、'.join(sorted({r['name'] for r in rows}))})")
        seg_dur = segment.target_timerange.duration
        dur = min(seg_dur, safe_tim(duration) if duration else round(float(row.get("duration_s") or 0.5) * 1e6))
        anims = getattr(segment, "_sticker_anims", None)
        if anims is None:
            anims = segment._sticker_anims = {}
        anims[kind] = {
            "type": kind, "name": row["name"], "resource_id": row["resource_id"],
            "third_resource_id": _json.loads(row.get("params") or "{}").get("third_resource_id", row["resource_id"]),
            "path": _local_md5_index().get(row.get("md5", ""), ""), "material_type": "sticker",
            "start": seg_dur - dur if kind == "out" else 0, "duration": dur, "source_platform": 1,
            "platform": "all", "panel": "", "category_id": "", "category_name": "", "id": "",
        }
        return self._set_segment_raw(segment, "sticker_animation", "material_animations", {
            "type": "sticker_animation", "multi_language_current": "none",
            "animations": [anims[k] for k in ("in", "out", "loop") if k in anims],
        })

    def add_video_animation(
        self,
        segment,
        animation_name: str,
        kind: str = "intro",
        duration: Union[str, int] = None,
    ):
        """为视频/图片片段添加入场 (intro) / 出场 (outro) / 组合 (group) 动画。"""
        enum_cls = {"intro": draft.IntroType, "outro": draft.OutroType, "group": draft.GroupAnimationType}.get(kind)
        if enum_cls is None:
            raise ValueError("kind 必须是 intro / outro / group")
        anim = self._resolve_enum(enum_cls, animation_name)
        if not anim or segment is None:
            print(f"⚠️ Video animation not found: {animation_name} ({kind})")
            return None
        segment.add_animation(anim, duration=safe_tim(duration) if duration is not None else None)
        return segment

    def add_filter_simple(self, segment, filter_name: str, intensity: float = 100.0):
        """为视频片段添加滤镜 (intensity 0~100)。"""
        flt = self._resolve_enum(draft.FilterType, filter_name)
        if not flt or segment is None:
            print(f"⚠️ Filter not found: {filter_name}")
            return None
        segment.add_filter(flt, intensity)
        return segment

    def add_segment_effect(self, segment, effect_name: str, params: Optional[list] = None):
        """为单个视频片段添加画面特效 (只作用于该片段，区别于 add_effect_simple 的特效轨)。"""
        eff = self._resolve_enum(draft.VideoSceneEffectType, effect_name)
        if not eff or segment is None:
            print(f"⚠️ Effect not found: {effect_name}")
            return None
        segment.add_effect(eff, params)
        return segment

    def add_character_effect(self, segment, effect_name: str, params: Optional[list] = None):
        """为视频片段添加人物特效 (需画面中有可识别的人物, 剪映自动跟踪人脸/人体)。"""
        eff = self._resolve_enum(draft.VideoCharacterEffectType, effect_name)
        if not eff or segment is None:
            print(f"⚠️ Character effect not found: {effect_name}")
            return None
        segment.add_effect(eff, params)
        return segment

    def add_mask_simple(self, segment, mask_name: str = "圆形", **kwargs):
        """
        为视频片段添加蒙版。
        - 基础: 线性/镜面/圆形/矩形/爱心/星形 (本地已下载 11.x 版本时优先使用, 否则用内置旧版)
        - 剪映 11.x 面板新形状: 圆角瓶盖/饼干/八边形/六边形/爆炸/... (需在剪映蒙版面板中点过一次以下载资源)
        kwargs 透传: center_x, center_y, size, rotation, feather, invert, rect_width, round_corner
        """
        # 优先使用剪映 11.x 面板版本 (新 ID + 本地资源): 实测 11.6 中旧版「爱心」不生效, 新版正常
        mask, unavailable = None, None
        try:
            mask = resolve_panel_member(draft.MaskType, str(mask_name), draft)
        except PanelAssetUnavailable as e:
            unavailable = e
        if mask is not None and is_vip_member(mask) and not getattr(self, "allow_vip", True):
            print(f"⛔ '{mask_name}' 为 VIP 蒙版，已按 allow_vip=False 跳过")
            return None
        if mask is None:
            builtin = draft.MaskType.__members__.get(str(mask_name))
            if builtin is not None:
                mask = builtin
                # 2026-10-23 实测: 剪映 11.7 不渲染旧版蒙版 (圆形 / 爱心都是), 片段会以原形状显示
                msg = (f"旧版「{mask_name}」蒙版在剪映 11.x 中不生效 (新版资源没下载到本地)。请在剪映的蒙版面板中"
                       f"点一次「{mask_name}」下载资源, 然后重新生成草稿。")
                print(f"⚠️ {msg}")
                self.warnings = getattr(self, "warnings", []) + [msg]
            elif unavailable is not None:
                print(f"⚠️ {unavailable}")
                return None
            else:
                mask = self._resolve_enum(draft.MaskType, mask_name)
        if not mask or segment is None:
            print(f"⚠️ Mask not found: {mask_name}")
            return None
        segment.add_mask(mask, **kwargs)
        return segment

    @staticmethod
    def _keyframe_base(segment) -> Tuple[int, float]:
        """
        剪映的关键帧时间是「素材时间」: 从素材开头算, 而不是从片段在时间线上的开头算 (11.7 实测:
        从素材 27.55s 处截取的片段, time_offset=1.85s 的关键帧根本不出现)。add_keyframes 接收片段内的相对时间,
        这里返回换算用的 (截取起点, 速度): 素材时间 = 截取起点 + 相对时间 x 速度。
        """
        src = getattr(segment, "source_timerange", None)
        base = int(src.start) if src is not None else 0
        spd = float(getattr(getattr(segment, "speed", None), "speed", 1.0) or 1.0)
        return base, spd

    def add_keyframes(self, segment, prop: str, points: Iterable[Tuple[Union[str, int], float]],
                      curve_type: str = "Line", ease: Optional[str] = None):
        """
        批量添加关键帧。prop 支持 x/y/scale/rotation/alpha/brightness/contrast/saturation/volume
        (或中文: 位置x/缩放/旋转/不透明度/亮度/...)；points: [(时间偏移, 值), ...]
        时间偏移是相对片段开头的时间 (成片时间), 写入草稿时自动换算成剪映要求的素材时间。
        例: project.add_keyframes(seg, "scale", [("0s", 1.0), ("2s", 1.3)], ease="三次方缓动")
        """
        key = KEYFRAME_ALIASES.get(str(prop).lower(), str(prop))
        if isinstance(segment, draft.AudioSegment):
            # 音频片段只支持音量关键帧 (底层签名不同)
            if key != "volume":
                raise ValueError(f"Audio segments only support volume keyframes, got: {prop}")
            for offset, value in points:
                segment.add_keyframe(safe_tim(offset), float(value))
            return segment
        kf_prop = getattr(draft.KeyframeProperty, key, None)
        if kf_prop is None or segment is None:
            raise ValueError(f"Unsupported keyframe property: {prop}")
        base, spd = self._keyframe_base(segment)
        for offset, value in points:
            segment.add_keyframe(kf_prop, base + int(safe_tim(offset) * spd), float(value), curve_type=curve_type)
        if ease:  # 剪映「关键帧曲线」预设, 如 "缓入" / "回弹离开"
            self.set_keyframe_ease(segment, prop, ease)
        return segment

    def add_ken_burns(self, segment, zoom_from: float = 1.0, zoom_to: float = 1.15,
                      pan: Tuple[float, float] = (0.0, 0.0)):
        """推拉摇移 (Ken Burns)：在片段时长内平滑缩放 + 平移。"""
        if segment is None:
            return None
        dur = segment.target_timerange.duration
        self.add_keyframes(segment, "scale", [(0, zoom_from), (dur, zoom_to)])
        if pan != (0.0, 0.0):
            self.add_keyframes(segment, "x", [(0, 0.0), (dur, pan[0])])
            self.add_keyframes(segment, "y", [(0, 0.0), (dur, pan[1])])
        return segment

    def set_background_fill(self, segment, fill_type: str = "blur", blur: float = 0.0625,
                            color: str = "#00000000"):
        """背景填充 (仅对主视频轨生效)：blur 模糊 / color 纯色。"""
        if segment is None:
            return None
        segment.add_background_filling(fill_type, blur=blur, color=color)
        return segment
