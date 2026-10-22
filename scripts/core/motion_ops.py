"""
变速与动效 (剪映 11.x): 常规变速 / 声音变调、曲线变速、关键帧缓动曲线、文字发光。
预设数据来自面板缓存 (panel_curve_speeds / panel_keyframe_curves / panel_text_glows.csv), 字段对照
references/jy11_samples/motion_ref.json。
"""

import json
import math
import uuid
from typing import Optional

import pyJianYingDraft as draft
from core.picture_ops import _hex_color, _pct
from utils.panel_assets import _local_md5_index, find_panel_asset

TEXT_GLOW_FLAG = 64  # 文字素材 check_flag: 发光


def _curve_mean_speed(points) -> float:
    """曲线变速的平均速度 (横轴为素材进度, 片段时长 = 素材时长 / 平均速度)。剪映 11.7 实测沿用此速度, 只把时长对齐到整帧。"""
    total = 0.0
    for a, b in zip(points, points[1:]):
        (x1, y1), (x2, y2) = (a["x"], max(a["y"], 1e-3)), (b["x"], max(b["y"], 1e-3))
        length = x2 - x1
        total += length / y1 if abs(y2 - y1) < 1e-9 else length / (y2 - y1) * math.log(y2 / y1)
    return 1 / total if total > 0 else 1.0


class MotionOpsMixin:
    """JyProject 的变速 / 动效 Mixin。"""

    def _retime(self, segment, new_duration: int, ripple: bool = True):
        """改片段时长; ripple=True 时同一轨道后面的片段跟着前移/后移 (与剪映一致)。"""
        fps = getattr(self.script, "fps", 30) or 30  # 剪映会把变速后的时长对齐到整帧
        new_duration = round(round(new_duration * fps / 1_000_000) * 1_000_000 / fps)
        old = segment.target_timerange.duration
        delta = int(new_duration) - old
        segment.target_timerange.duration = int(new_duration)
        if ripple and delta:
            for track in self.script.tracks.values():
                if segment in track.segments:
                    for other in track.segments:
                        if other is not segment and other.target_timerange.start >= segment.target_timerange.start + old:
                            other.target_timerange.start += delta
        self.script.duration = max((s.end for t in self.script.tracks.values() for s in t.segments), default=0)

    def set_speed(self, segment, speed: float = 2.0, change_pitch: bool = False, ripple: bool = True):
        """常规变速: speed 倍速 (片段时长随之改变), change_pitch=True 为「声音变调」。"""
        if segment is None or not isinstance(segment, (draft.VideoSegment, draft.AudioSegment)):
            print("⚠️ 变速只能用于视频/音频片段")
            return None
        if not 0.1 <= float(speed) <= 100:
            raise ValueError(f"变速倍数取值 0.1~100, 收到 {speed}")
        segment.speed.speed = float(speed)
        segment.speed.curve = None
        segment.change_pitch = bool(change_pitch)
        self._retime(segment, round(segment.source_timerange.duration / float(speed)), ripple)
        return segment

    def set_curve_speed(self, segment, preset: str = "蒙太奇", ripple: bool = True):
        """曲线变速 (「变速 → 曲线变速」): 蒙太奇/英雄时刻/子弹时间/跳接/闪进/闪出/自定。"""
        if segment is None or not isinstance(segment, draft.VideoSegment):
            print("⚠️ 曲线变速只能用于视频片段")
            return None
        row = find_panel_asset("panel_curve_speeds.csv", str(preset))
        if row is None:
            raise ValueError(f"未知曲线变速预设: {preset} (先运行 jy_wrapper.py sync --only panel)")
        points = json.loads(row.get("params") or "[]")
        mean = _curve_mean_speed(points)
        segment.speed.speed = mean
        segment.speed.curve = {"name": row["name"], "id": row["resource_id"], "speed_points": points}
        self._retime(segment, round(segment.source_timerange.duration / mean), ripple)
        return segment

    def set_keyframe_ease(self, segment, prop: str, preset: str = "缓动"):
        """
        关键帧缓动曲线 (剪映「关键帧曲线」): 对片段某个属性的所有相邻关键帧套用预设。
        preset: 线性/缓入/缓出/缓动/二次方缓入/二次方缓出/二次方缓动/三次方缓入/三次方缓出/三次方缓动/回弹离开。
        prop 同 add_keyframes (x/y/scale/rotation/alpha/位置x/缩放/...)。
        """
        from core.track_ops import KEYFRAME_ALIASES

        row = find_panel_asset("panel_keyframe_curves.csv", str(preset))
        if row is None:
            raise ValueError(f"未知关键帧曲线: {preset} (先运行 jy_wrapper.py sync --only panel)")
        graph = [p["point"] for p in json.loads(row.get("params") or "[]")]
        if len(graph) != 4:
            raise ValueError(f"关键帧曲线「{row['name']}」数据异常")
        (c1x, c1y), (c2x, c2y) = (graph[1]["x"], graph[1]["y"]), (graph[2]["x"], graph[2]["y"])
        key = KEYFRAME_ALIASES.get(str(prop).lower(), str(prop))
        # 统一缩放在底层存成 scale_x / scale_y 两组关键帧
        names = ("scale_x", "scale_y") if key == "uniform_scale" else (key,)
        props = {getattr(draft.KeyframeProperty, n, None) for n in names}
        lists = [k for k in getattr(segment, "common_keyframes", []) if k.keyframe_property in props]
        if not lists or any(len(k.keyframes) < 2 for k in lists):
            raise ValueError(f"片段的「{prop}」至少需要 2 个关键帧, 先调用 add_keyframes")
        linear = row["name"] == "线性"
        for a, b in ((a, b) for kl in lists for a, b in zip(kl.keyframes, kl.keyframes[1:])):
            dt, dv = b.time_offset - a.time_offset, b.values[0] - a.values[0]
            a.curve_type = b.curve_type = "Line" if linear else "FreeCurveInOut"
            a.right_control = (0.0, 0.0) if linear else (round(c1x * dt), c1y * dv)  # x 为微秒
            b.left_control = (0.0, 0.0) if linear else (round((c2x - 1) * dt), (c2y - 1) * dv)
            b.graph_id = "" if linear else str(uuid.uuid4()).upper()
        return segment

    def add_text_glow(self, segment, style: str = "轮廓光", color="#ffffff", strength: float = 50,
                      range: float = 40, angle: float = 0):  # noqa: A002 - 与面板名称一致
        """
        文字发光 (文字面板「发光」): style 轮廓光/外发光(VIP)/多色发光/DeepGlow (以剪映面板实际提供的为准);
        strength 强度 / range 范围为面板数值 0~100, angle 垂直角度 (度)。
        """
        if segment is None or not isinstance(segment, draft.TextSegment):
            print("⚠️ 发光只能用于文字片段")
            return None
        row = find_panel_asset("panel_text_glows.csv", str(style))
        if row is None:
            raise ValueError(f"未知文字发光样式: {style} (先运行 jy_wrapper.py sync --only panel)")
        if row.get("is_vip") == "1" and not getattr(self, "allow_vip", True):
            print(f"⛔ 发光「{row['name']}」为 VIP 素材，已按 allow_vip=False 跳过")
            return None
        segment.check_flag_bits = (getattr(segment, "check_flag_bits", 0) or 0) | TEXT_GLOW_FLAG
        return self._set_segment_raw(segment, "text_glow", "effects", {
            "type": "bloom", "panel_id": "tool_text_glow", "name": row["name"], "resource_id": row["resource_id"],
            "effect_id": row["resource_id"], "path": _local_md5_index().get(row.get("md5", ""), ""),
            "source_platform": 1, "value": 0, "apply_target_type": 0, "sub_type": "none", "platform": "all",
            "visible": True, "bloom_params": {
                "color": _hex_color(color), "strength": _pct(strength, "strength"), "range": _pct(range, "range"),
                "dir_x": 0.5, "dir_y": 0.5 + 0.5 * math.tan(math.radians(float(angle))),  # 30° -> 0.789 (实测)
            },
        })
