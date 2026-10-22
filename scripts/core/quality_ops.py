"""
AI 画质与智能调色 (剪映 11.x, 多为 VIP): 一键画质提升、超清画质、视频降噪、AI补帧、补分辨率、视频去频闪、
运动模糊、智能打光、色彩校正、色彩克隆。

只写开关和参数, 不带剪映算好的结果文件; 剪映 11.7 实测打开草稿时会自动处理 (状态栏「N 个任务处理中」)。
字段来自 references/jy11_samples/ai_quality_ref.json。
"""

import copy
import json
import os
from typing import Optional

from core.adjust_ops import ADJUST_RES, _adjust_resource
from utils.panel_assets import PanelAssetUnavailable, _local_md5_index, find_panel_asset

SMART_RELIGHT_FLAG = 0x20000  # 视频素材 check_flag: 启用智能打光

# 智能打光面板分类 -> 草稿里的 category_id
RELIGHT_CATEGORIES = {"基础面光": "basics", "氛围彩光": "ambient", "创意光效": "creative", "预设": "preset"}


def _hex_rgba(r, g, b, a=1.0) -> str:
    """剪映写法: 各通道 int(v*255) 截断, 如 alpha 0.5 -> 7F。"""
    return "#" + "".join(f"{int(float(v) * 255 + 1e-6):02X}" for v in (r, g, b, a))


def build_relight(row: dict, package_dir: str) -> dict:
    """由智能打光资源包里的 relit_default.json 生成草稿素材 (字段对应关系由剪映 11.7 草稿逐项核对)。"""
    with open(os.path.join(package_dir, "relit_default.json"), "r", encoding="utf-8") as f:
        preset = json.load(f)
    lp = preset.get("effects_adjust_light_params") or {}
    ar, ag, ab, aa = (list(lp.get("albedo_Color") or [1, 1, 1, 1]) + [1, 1, 1, 1])[:4]
    lights = []
    for L in preset.get("effects_adjust_lights") or []:
        L = list(L) + [0] * (16 - len(L))
        lights.append({
            "light_source_type": 1 if L[0] else 2, "action_object": int(L[1]) if L[1] else 3,
            "color": _hex_rgba(L[2], L[3], L[4], L[5]), "alpha": L[5], "x": L[6], "y": L[7], "z": L[8],
            "intensity": L[9], "specular_light_intensity": L[10], "reflectance": L[11], "radius": L[12],
            "color_changed_speed": L[13], "intensity_changed_speed": L[14], "xy_changed_speed": L[15],
        })
    cats = [c for c in (row.get("categories") or "").split("|") if c in RELIGHT_CATEGORIES]
    return {
        "type": "smart_relight", "source_platform": 1, "resource_id": row["resource_id"],
        "resource_name": row["name"], "resource_path": package_dir, "preset_id": "",
        "category_id": RELIGHT_CATEGORIES[cats[0]] if cats else "basics", "is_from_intelligent_quality": False,
        "light_mode": preset.get("effects_adjust_lightMode", 1), "glow_intensity": lp.get("glow_Intensity", 0),
        "albedo_color": _hex_rgba(ar, ag, ab, aa), "albedo_alpha": aa,
        "albedo_intensity": lp.get("albedo_Intensity", 0.8), "albedo_rate": lp.get("albedo_Rate", 1),
        "light_sources": lights,
    }


# 色彩克隆官方预设的参考 ID (面板上的 4 个)
COLOR_CLONE_PRESETS = {"汐蓝": "official:7688654939660373282", "透亮": "official:7688654960623521059",
                       "食色": "official:7688656360942095631", "秋枫": "official:7688656591809219875"}


class QualityOpsMixin:
    """JyProject 的 AI 画质 / 智能调色 Mixin。依赖 PictureOpsMixin 的 _own_video_material / _set_segment_raw。"""

    def _set_algorithm(self, segment, alg_type: str, configs: Optional[dict] = None, intelligent: bool = False,
                       remove: bool = False):
        """在视频素材的 video_algorithm.algorithms 中增 / 删一种算法 (多种算法可叠加)。"""
        if segment is None or not self._require_video(segment, "画质处理"):
            return None
        mat = self._own_video_material(segment)
        va = mat.extra_json.setdefault("video_algorithm", {
            "algorithms": [], "path": "", "gameplay_configs": [],
            "time_range": {"start": 0, "duration": mat.duration},
        })
        va["algorithms"] = [a for a in va["algorithms"] if a.get("type") != alg_type]
        if not remove:
            alg = {"type": alg_type, "aigc_output_config": {}, "node_output": {}}
            if intelligent:
                alg["is_from_intelligent_quality"] = True
            va["algorithms"].append(alg)
            va.update(copy.deepcopy(configs or {}))
        if not va["algorithms"]:
            mat.extra_json.pop("video_algorithm", None)
        return segment

    def set_one_click_quality(self, segment, ultra_hd: bool = False):
        """一键画质提升 (= 画质增强 + 超清 + 补帧 60fps); ultra_hd=True 为「超清画质」。"""
        qe = {"from": "multi_track", **({"level": 1} if ultra_hd else {})}
        cfg = {"quality_enhance": qe, "super_resolution": {"mode": 2, "from": "multi_track"},
               "smart_complement_frame": {"sub_type": 1, "fps": 60}}
        for t in ("QualityEnhance", "SuperResolution", "SmartComplementFrame"):
            self._set_algorithm(segment, t, cfg, intelligent=True)
        return segment

    def set_ultra_hd(self, segment):
        """超清画质 (VIP)。"""
        return self.set_one_click_quality(segment, ultra_hd=True)

    def set_denoise(self, segment, enable: bool = True):
        """视频降噪 (VIP)。"""
        return self._set_algorithm(segment, "NoiseReduction", remove=not enable)

    def set_frame_interpolation(self, segment, fps: int = 50, enable: bool = True):
        """AI 补帧 (VIP), fps 为目标帧率 (11.7 默认档 50)。"""
        return self._set_algorithm(segment, "SmartComplementFrame", {"smart_complement_frame": {"fps": int(fps)}},
                                   remove=not enable)

    def set_super_resolution(self, segment, mode: int = 1, enable: bool = True):
        """补分辨率 (VIP), mode=1 为默认档。"""
        return self._set_algorithm(segment, "SuperResolution",
                                   {"super_resolution": {"mode": int(mode), "from": "multi_track"}}, remove=not enable)

    def set_deflicker(self, segment, enable: bool = True):
        """视频去频闪 (VIP)。"""
        return self._set_algorithm(segment, "Deflicker", remove=not enable)

    def set_motion_blur(self, segment, blur: float = 1.0, enable: bool = True):
        """运动模糊, blur 为剪映存储值 (默认 1.0 = 面板默认)。"""
        return self._set_algorithm(segment, "MotionBlur", {"motion_blur_config": {"blur": float(blur)}},
                                   remove=not enable)

    def set_smart_relight(self, segment, preset: str = "氛围暖光"):
        """
        智能打光 (VIP)。preset 为面板预设名或 resource_id (基础面光/氛围彩光/创意光效共 18 个, 见 panel_smart_relights.csv)。
        参数取自剪映下载的预设资源包; 未下载时需在剪映智能打光面板点一次该预设。需要 check_flag 加 0x20000。
        """
        if segment is None or not self._require_video(segment, "智能打光"):
            return None
        row = find_panel_asset("panel_smart_relights.csv", str(preset))
        if row is None:
            raise ValueError(f"未知智能打光预设: {preset} (先运行 jy_wrapper.py sync --only panel)")
        pkg = _local_md5_index().get(row.get("md5", ""), "")
        if not pkg or not os.path.exists(os.path.join(pkg, "relit_default.json")):
            raise PanelAssetUnavailable(f"智能打光「{row['name']}」本地未下载: 请在剪映的智能打光面板点一次该预设后重试")
        mat = self._own_video_material(segment)
        mat.check_flag_bits |= SMART_RELIGHT_FLAG
        return self._set_segment_raw(segment, "smart_relight", "smart_relights", build_relight(row, pkg))

    def _flag(self, segment, name: str, on: bool):
        flags = dict(getattr(segment, "extra_flags", None) or {})
        flags[name] = on
        segment.extra_flags = flags

    def set_color_correct(self, segment, strength: float = 80):
        """色彩校正 (VIP, 「调整 → 基础 → 色彩校正」), strength 0~100; 剪映打开草稿时自动分析。"""
        if segment is None:
            return None
        self._flag(segment, "enable_color_correct_adjust", True)
        return self._set_segment_raw(segment, "color_correct", "effects", {
            "type": "color_correct", "value": float(strength) / 100, "effect_id": ADJUST_RES[0],
            "sub_type": "none", **_adjust_resource()})

    def set_color_clone(self, segment, preset: str = "汐蓝", strength: float = 80):
        """色彩克隆 (VIP, 「调整 → 基础 → 色彩克隆」), 官方预设 + 强度 0~100; 剪映打开草稿时自动提取色彩特征。"""
        if segment is None:
            return None
        if preset not in COLOR_CLONE_PRESETS:
            raise ValueError(f"暂不支持的色彩克隆预设: {preset} (可选: {'、'.join(COLOR_CLONE_PRESETS)})")
        self._flag(segment, "enable_color_match_adjust", True)
        return self._set_segment_raw(segment, "color_match", "effects", {
            "type": "color_match", "value": float(strength) / 100, "effect_id": ADJUST_RES[0], "sub_type": "none",
            "color_match_info": {"reference_name": preset, "reference_id": COLOR_CLONE_PRESETS[preset]},
            **_adjust_resource()})
