"""
统一调色 Mixin (T13): 把草稿里的视频/图片片段按参考帧匹配到同一色调。

计算逻辑在 scripts/color_match.py (Lab 统计 + 近似调节模型 + LUT 生成), 这里只负责:
从片段取素材路径与源时间范围 -> 算建议 -> 用 set_adjust / set_lut 写回片段。
依赖 AdjustOpsMixin (set_adjust / set_lut) 和 JyProjectBase (script / draft_dir)。
"""

import os
from typing import Iterable, List, Optional, Union

import pyJianYingDraft as draft


def _segment_shot(segment) -> dict:
    """视频/图片片段 -> color_match 的镜头描述 (源素材路径 + 源时间范围, 秒)。"""
    mat = getattr(segment, "material_instance", None)
    path = getattr(mat, "path", None)
    if not path:
        raise ValueError("片段没有本地素材路径")
    shot = {"source": path, "start": None, "end": None}
    src = getattr(segment, "source_timerange", None)
    if getattr(mat, "material_type", "video") == "video" and src is not None:
        shot["start"] = src.start / 1e6
        shot["end"] = (src.start + src.duration) / 1e6
    return shot


class ColorMatchOpsMixin:
    """JyProject 的统一调色 Mixin。"""

    def _color_match_segments(self, segments=None, track_name: Optional[str] = None) -> list:
        if segments is not None:
            return [s for s in segments if isinstance(s, draft.VideoSegment)]
        out = []
        for name, track in self.script.tracks.items():
            if track_name is not None and name != track_name:
                continue
            if getattr(track, "track_type", None) != draft.TrackType.video:
                continue
            out += [s for s in track.segments if isinstance(s, draft.VideoSegment)]
        return out

    def match_color(
        self,
        reference: Union[str, dict, "draft.VideoSegment", None] = None,
        segments: Optional[Iterable] = None,
        track_name: Optional[str] = None,
        mode: str = "adjust",
        strength: float = 1.0,
        samples: int = 5,
        min_distance: float = 2.0,
        lut_dir: Optional[str] = None,
        lut_strength: float = 100,
        dry_run: bool = False,
    ) -> List[dict]:
        """
        统一调色: 把片段匹配到参考的色调。
            p.match_color("ref.jpg", track_name="Main")              # 面板参数 (色温/色调/亮度/...)
            p.match_color(segs[0], segments=segs[1:], mode="lut")    # 以第一段为参考, 每段生成 LUT
            p.match_color(None, track_name="Main", dry_run=True)     # 自动选参考, 只返回建议不写入
        reference: 图片/视频路径, "路径@开始-结束" (秒), {"source","start","end"}, 某个片段, 或 None (自动选最典型的一段)。
        segments: 要调的片段 (默认 track_name 轨道 / 所有视频轨道的全部片段)。
        mode: "adjust" 用 set_adjust 写面板参数; "lut" 生成 .cube 用 set_lut 挂上 (更精确, 会导入剪映 LUT 库)。
        strength: 建议强度 0~1。min_distance: 与参考差异小于它的片段不动。
        返回每段的建议 (同 color_match.match_shots 的 shots 项, 另加 segment_id / applied)。
        """
        import color_match as cm

        if mode not in ("adjust", "lut"):
            raise ValueError(f"mode 只能是 adjust / lut, 收到 {mode}")
        segs = self._color_match_segments(segments, track_name)
        if not segs:
            print("⚠️ 没有可调色的视频/图片片段")
            return []
        ref_index = None
        ref_shot = None
        if isinstance(reference, draft.VideoSegment):
            if reference in segs:
                ref_index = segs.index(reference)
            else:
                ref_shot = _segment_shot(reference)
        elif isinstance(reference, str):
            ref_shot = cm.parse_shot_spec(reference)
        elif isinstance(reference, dict):
            ref_shot = reference
        shots = [_segment_shot(s) for s in segs]
        if mode == "lut" and lut_dir is None:
            lut_dir = os.path.join(self.draft_dir, "color_match")
        res = cm.match_shots(
            shots,
            reference=ref_shot,
            ref_index=ref_index,
            samples=samples,
            strength=strength,
            lut_dir=lut_dir if mode == "lut" else None,
            min_distance=min_distance,
        )
        out = []
        for seg, item in zip(segs, res["shots"]):
            item["segment_id"] = seg.segment_id
            item["applied"] = None
            if not dry_run and not item["is_reference"]:
                if mode == "adjust" and item["adjust"]:
                    self.set_adjust(seg, **item["adjust"])
                    item["applied"] = "adjust"
                elif mode == "lut" and item.get("lut"):
                    self.set_lut(seg, item["lut"], strength=lut_strength)
                    item["applied"] = "lut"
            out.append(item)
        changed = sum(1 for i in out if i["applied"])
        print(f"🎨 统一调色: 参考 {res['reference']['label']}, {len(out)} 段中调整 {changed} 段")
        return out
