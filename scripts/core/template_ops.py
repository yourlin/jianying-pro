from typing import List, Optional, Sequence, Union

import pyJianYingDraft as draft
from utils.formatters import safe_tim
from utils.panel_assets import PanelAssetUnavailable, find_panel_asset
from utils.template_presets import find_preset, harvest_presets, instantiate_preset, template_texts


class TemplateTextSegment(draft.TextSegment):
    """套用了字幕模板/文字模板的文字片段: 素材指向 materials.text_templates 中克隆出的模板。"""

    def __init__(self, text: str, timerange: draft.Timerange, instance: dict,
                 clip_settings: Optional[draft.ClipSettings] = None):
        super().__init__(text, timerange, clip_settings=clip_settings)
        self.material_id = instance["material_id"]
        self.extra_material_refs = list(instance["extra_material_refs"])
        self.template_materials = instance["materials"]


def _clip_from_json(clip: Optional[dict]) -> draft.ClipSettings:
    clip = clip or {}
    t, s, f = clip.get("transform", {}), clip.get("scale", {}), clip.get("flip", {})
    return draft.ClipSettings(
        alpha=clip.get("alpha", 1.0), rotation=clip.get("rotation", 0.0),
        flip_horizontal=f.get("horizontal", False), flip_vertical=f.get("vertical", False),
        scale_x=s.get("x", 1.0), scale_y=s.get("y", 1.0),
        transform_x=t.get("x", 0.0), transform_y=t.get("y", 0.0),
    )


class TemplateOpsMixin:
    """
    JyProject 的字幕模板 / 文字模板 Mixin (剪映 11.x)。
    模板需在剪映里套用过一次 (任一草稿), 由 sync_jy_templates.py 或首次使用时自动采集为预设。
    """

    def _load_template_preset(self, template: str, kind: str):
        preset = find_preset(template)
        if preset is None:
            harvest_presets(verbose=False)
            preset = find_preset(template)
        csv = "panel_subtitle_templates.csv" if kind == "subtitle_template" else "panel_text_templates.csv"
        row = find_panel_asset(csv, str(template))
        if preset is None:
            name = row["name"] if row else template
            raise PanelAssetUnavailable(
                f"模板「{name}」还没有预设: 请在剪映里任选一个草稿, 给一段文字套用一次该模板 "
                f"(稍等剪映自动保存), 再重试。"
                + ("" if row else " (面板索引中也没找到该名称, 请先运行 sync_jy_panel.py)")
            )
        is_vip = (row or {}).get("is_vip") == "1"
        if is_vip and not getattr(self, "allow_vip", True):
            raise PanelAssetUnavailable(f"⛔ 模板「{preset['name']}」为 VIP 素材, 已按 allow_vip=False 拒绝")
        return preset

    def _add_template_text(self, kind: str, text: Union[str, Sequence[Optional[str]]], template: str,
                           start_time: Union[str, int, None], duration: Union[str, int],
                           track_name: str, clip_settings: Optional[draft.ClipSettings]):
        preset = self._load_template_preset(template, kind)
        if start_time is None:
            start_time = self.get_track_duration(track_name)
        self._ensure_track(draft.TrackType.text, track_name)
        start_us, dur_us = safe_tim(start_time), safe_tim(duration)
        inst = instantiate_preset(preset, text, dur_us)
        label = text if isinstance(text, str) else " ".join(t for t in text if t)
        seg = TemplateTextSegment(
            label, draft.Timerange(start_us, dur_us), inst,
            clip_settings=clip_settings or _clip_from_json(inst["clip"]),
        )
        self.script.add_segment(seg, track_name)
        return seg

    def add_subtitle_template(self, text: str, template: str, start_time: Union[str, int] = None,
                              duration: Union[str, int] = "3s", track_name: str = "Subtitles",
                              clip_settings: Optional[draft.ClipSettings] = None):
        """添加一条套用字幕模板的字幕。template 为模板名称或 resource_id; 位置默认沿用剪映里的套用效果。"""
        return self._add_template_text("subtitle_template", text, template, start_time, duration,
                                       track_name, clip_settings)

    def add_text_template(self, text: Union[str, Sequence[Optional[str]]], template: str,
                          start_time: Union[str, int] = None, duration: Union[str, int] = "3s",
                          track_name: str = "Subtitles", clip_settings: Optional[draft.ClipSettings] = None):
        """
        添加一段套用文字模板的文字。模板里有多段文字时 (如「2025再见」: 5、2、0、2、GOOD BYE),
        text 传列表按顺序替换各段, None 保留模板原文字 (各段默认内容见 template_texts); 传字符串则每段都替换为它。
        """
        return self._add_template_text("text_template", text, template, start_time, duration,
                                       track_name, clip_settings)

    def template_texts(self, template: str, kind: str = "text_template") -> List[str]:
        """模板里各段文字的默认内容, 用来决定 add_text_template 的 text 列表怎么填。"""
        return template_texts(self._load_template_preset(template, kind))
