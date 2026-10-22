"""
画面类设置 (剪映 11.x「画面」面板): 混合模式、蒙版描边 / 蒙版阴影、画面描边 / 阴影、变形、色度抠图。

字段来自剪映 11.7 写出的真实草稿 (references/jy11_samples/)。滑块参数统一用**剪映面板上显示的数值**
(不透明度、粗细、模糊等为 0-100, 角度为度), 内部按剪映的存储方式换算 (多数为 数值/100)。
"""

import copy
import json
import os
import uuid
from typing import Optional, Tuple

import pyJianYingDraft as draft
from utils.panel_assets import _local_md5_index, find_jianying_app_resource, load_panel_rows

# 混合模式英文别名 -> 剪映名称
BLEND_ALIASES = {
    "screen": "滤色", "lighten": "变亮", "darken": "变暗", "overlay": "叠加", "hard_light": "强光",
    "soft_light": "柔光", "multiply": "正片叠底", "linear_burn": "线性加深", "color_dodge": "颜色减淡",
    "color_burn": "颜色加深", "normal": "正常",
}
# 描边类参数 (面板 0-100): 粗细 / 不透明度 / 间距 / 纹理 / 水平、垂直偏移
STROKE_PARAMS = ("size", "alpha", "distance", "texture", "horizontal_shift", "vertical_shift")
# 剪映内置的阴影资源 (resource_id, md5): 蒙版阴影 / 画面阴影
MASK_SHADOW_RES = ("7563934338723712295", "c172eacf2b2e819390d8657f8657ed8d")
VIDEO_SHADOW_RES = ("7564727861634944292", "a16293a01d4e7980b953406fd06358bb")
# 美颜美体分类 (panel_beauty.csv 的 kind) -> (category_id, sub_type, item_effect_type), 与剪映 11.7 展开预设时的写法一致
BEAUTY_KINDS = {
    "beauty_skin": ("auto-beauty2", "auto_beauty", 21),
    "beauty_reshape": ("auto-beauty", "auto_beauty", 20),
    "beauty_makeup": ("makeup", "exclusion_face", 22),
    "beauty_body": ("auto-beauty3", "none", 23),
}
# 美妆子类 (面板标签)
MAKEUP_PARTS = ("套装", "口红", "腮红", "修容", "卧蚕", "眉毛", "睫毛", "眼线", "眼影", "美瞳", "高光", "雀斑")
# 数值写在 value 里的美颜项 (其余写 adjust_params[{"name": "0"}]; 美体全部写 value)
BEAUTY_VALUE_ITEMS = {"磨皮", "美白"}
MAKEUP_ROOT = ("7406180793103420724", "89ad943ef61e4509b877db7105e3216e", "7273096354098844221")
MANUAL_STRETCH = ("7406180541361392896", "29751419a20da04f67cb0b6ea19a5cbf", "7002806881164988942")
MANUAL_SLIM = ("7406017234474175796", "a772c406e9977330f155f58d0dbcfc6f", "7002806924408263205")
MANUAL_FIGURE_FLAG = 0x80000  # 视频素材 check_flag: 启用手动美体


def _blend_modes() -> dict:
    """名称 -> {resource_id, effect_id, path}: 来自剪映安装包内置的 MixMode/MixMode.json, 与面板名称按 resource_id 对应。"""
    names = {r["resource_id"]: r["name"] for r in load_panel_rows("panel_blend_modes.csv")}
    out = {}
    index = find_jianying_app_resource("MixMode", "MixMode.json")
    if index:
        with open(index, "r", encoding="utf-8") as f:
            for item in json.load(f).get("resourceList", []):
                name = names.get(str(item.get("resourceId")))
                if name:
                    out[name] = {"resource_id": str(item["resourceId"]), "effect_id": str(item.get("effectId", "")),
                                 "path": os.path.join(os.path.dirname(index), item.get("path", ""))}
    for rid, name in names.items():  # 找不到安装包时退回面板索引 (剪映打开时按 ID 获取)
        out.setdefault(name, {"resource_id": rid, "effect_id": rid, "path": ""})
    return out


def _rgb(color) -> Tuple[float, float, float]:
    """'#RRGGBB' / (r, g, b) 0-1 或 0-255 -> (r, g, b) 0-1。"""
    if isinstance(color, str):
        c = color.strip().lstrip("#")
        if len(c) >= 6:
            return tuple(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
        return (1.0, 1.0, 1.0)
    if isinstance(color, (list, tuple)) and len(color) >= 3:
        vals = [float(v) for v in color[:3]]
        return tuple(v / 255 if max(vals) > 1 else v for v in vals)
    return (1.0, 1.0, 1.0)


def _hex_color(color) -> str:
    return "#" + "".join(f"{round(v * 255):02x}" for v in _rgb(color))


def _pct(value, name: str) -> float:
    v = float(value)
    if not 0 <= v <= 100:
        raise ValueError(f"{name} 取值 0-100 (剪映面板数值), 收到 {value}")
    return v / 100


def _local_path(md5: str) -> str:
    return _local_md5_index().get(md5 or "", "")


def _stroke_row(style: str, prefix: str) -> Optional[dict]:
    rows = [r for r in load_panel_rows("panel_mask_strokes.csv") if r.get("sub_type", "").startswith(prefix)]
    key = str(style).strip()
    bare = key[:-2] if key.endswith("描边") else key
    return next((r for r in rows if key in (r["name"], r["sub_type"], r["resource_id"])
                 or r["name"] in (bare, bare + "描边")), None), rows


class PictureOpsMixin:
    """JyProject 的画面设置 Mixin。"""

    # ------------------------------------------------------------------ 内部工具
    def _own_video_material(self, segment):
        """写在视频素材上的设置 (蒙版描边/阴影、变形...) 只应影响该片段: 首次修改时给它单独一份素材。"""
        if getattr(segment, "_own_material", False):
            return segment.material_instance
        mat = copy.deepcopy(segment.material_instance)
        mat.material_id = uuid.uuid4().hex
        segment.material_instance = mat
        segment.material_id = mat.material_id
        segment._own_material = True
        self.script.add_material(mat)
        return mat

    def _set_segment_raw(self, segment, slot: str, list_name: str, material: Optional[dict]):
        """在 materials.<list_name> 中放一条素材并挂到片段上; 同一 slot 重复设置时替换, material=None 时移除。"""
        refs = getattr(segment, "_raw_refs", None)
        if refs is None:
            refs = segment._raw_refs = {}
        raw = self.script.materials.raw.setdefault(list_name, [])
        old = refs.pop(slot, None)
        if old:
            raw[:] = [m for m in raw if m.get("id") != old]
            segment.extra_material_refs = [r for r in segment.extra_material_refs if r != old]
        if material is not None:
            material.setdefault("id", str(uuid.uuid4()).upper())
            raw.append(material)
            segment.extra_material_refs.append(material["id"])
            refs[slot] = material["id"]
        return segment

    @staticmethod
    def _require_video(segment, what: str) -> bool:
        if not isinstance(segment, draft.VideoSegment):
            print(f"⚠️ {what}只能用于视频/图片片段")
            return False
        return True

    # ------------------------------------------------------------------ 混合模式
    def set_blend_mode(self, segment, mode: str = "滤色", opacity: Optional[float] = None):
        """
        设置画面混合模式 (对上层轨道的画中画/叠加素材生效)。
        mode: 变亮/变暗/叠加/强光/柔光/正片叠底/滤色/线性加深/颜色减淡/颜色加深 (或英文 screen/multiply/...),
              "正常" 表示去掉混合模式。opacity: 片段不透明度 0-1 (可选, 与 add_overlay 的 alpha 一致)。
        """
        if segment is None:
            return None
        name = BLEND_ALIASES.get(str(mode).strip().lower(), str(mode).strip())
        if opacity is not None:
            segment.clip_settings.alpha = max(0.0, min(1.0, float(opacity)))
        if name == "正常":
            return self._set_segment_raw(segment, "mix_mode", "effects", None)
        modes = _blend_modes()
        info = modes.get(name)
        if not info:
            print(f"⚠️ 未知混合模式: {mode} (可选: {'、'.join(modes)})")
            return None
        return self._set_segment_raw(segment, "mix_mode", "effects", {
            "apply_target_type": 0, "category_id": "", "category_name": "", "effect_id": info["effect_id"],
            "name": name, "path": info["path"], "platform": "all", "resource_id": info["resource_id"],
            "source_platform": 0, "sub_type": "none", "type": "mix_mode", "value": 1.0, "visible": True,
        })

    # ------------------------------------------------------------------ 蒙版描边 / 蒙版阴影
    def add_mask_stroke(self, segment, style: str = "单层", color="#ffffff", **params):
        """
        为已加蒙版的视频片段添加蒙版描边。
        style: 单层/三层/发光/手绘/虚线/撕纸/偏移 (也接受 "发光描边" 这类写法、resource_type 或 resource_id)
        color: "#RRGGBB" 或 (r, g, b)
        params (剪映面板数值 0-100, 不传则用该样式默认值): size 粗细, alpha 不透明度, distance 间距,
            texture 纹理 (撕纸), horizontal_shift / vertical_shift 偏移 (偏移)
        """
        if segment is None or not self._require_video(segment, "蒙版描边"):
            return None
        if segment.mask is None:
            print("ℹ️ 片段还没有蒙版, 描边要配合蒙版使用: 先调用 add_mask_simple(seg, \"圆形\")")
        row, rows = _stroke_row(style, "mask-stroke")
        if row is None:
            print(f"⚠️ 未知蒙版描边样式: {style} (可选: {'、'.join(r['name'] for r in rows)})")
            return None
        unknown = set(params) - set(STROKE_PARAMS)
        if unknown:
            raise ValueError(f"不支持的描边参数: {', '.join(sorted(unknown))}")
        values = {k: 0 for k in STROKE_PARAMS}
        values.update(json.loads(row.get("params") or "{}").get("params") or {})
        values.update({k: _pct(v, k) for k, v in params.items()})
        mat = self._own_video_material(segment)
        mat.mask_stroke = {"type": row["sub_type"], "resource_id": row["resource_id"],
                           "path": _local_path(row.get("md5")), "color": _hex_color(color), **values}
        if not mat.mask_stroke["path"]:
            print(f"ℹ️ 描边「{row['name']}」本地未下载, 剪映打开草稿时会按 ID 获取")
        return segment

    def add_mask_shadow(self, segment, color="#000000", alpha: float = 50, blur: float = 30,
                        distance: float = 20, angle: float = 45):
        """为已加蒙版的视频片段添加蒙版阴影。alpha/blur/distance 为面板数值 0-100, angle 为角度。"""
        if segment is None or not self._require_video(segment, "蒙版阴影"):
            return None
        if segment.mask is None:
            print("ℹ️ 片段还没有蒙版, 蒙版阴影要配合蒙版使用: 先调用 add_mask_simple(seg, \"圆形\")")
        mat = self._own_video_material(segment)
        mat.extra_json["video_mask_shadow"] = {
            "resource_id": MASK_SHADOW_RES[0], "path": _local_path(MASK_SHADOW_RES[1]), "color": _hex_color(color),
            "alpha": _pct(alpha, "alpha"), "blur": _pct(blur, "blur"), "distance": _pct(distance, "distance"),
            "angle": float(angle),
        }
        return segment

    # ------------------------------------------------------------------ 画面描边 / 阴影
    def add_video_stroke(self, segment, style: str = "单层描边", color="#ffffff", **params):
        """
        画面描边 (「画面 → 基础 → 描边」, 沿画面内容的轮廓描边)。
        style: 单层描边/发光描边/手绘描边/虚线描边; params (面板 0-100): size 大小, alpha 不透明度, distance 间距
        """
        if segment is None or not self._require_video(segment, "画面描边"):
            return None
        row, rows = _stroke_row(style, "video-stroke")
        if row is None:
            print(f"⚠️ 未知画面描边样式: {style} (可选: {'、'.join(r['name'] for r in rows)})")
            return None
        defaults = json.loads(row.get("params") or "{}").get("params") or {}
        unknown = set(params) - set(defaults)
        if unknown:
            raise ValueError(f"「{row['name']}」支持的参数: {', '.join(defaults)}; 不支持: {', '.join(sorted(unknown))}")
        adjust = [{"name": "min_scale", "default_value": 320, "value": 320}]
        for k, default in defaults.items():
            value = _pct(params[k], k) if k in params else default
            adjust.append({"name": f"effects_adjust_{k}", "default_value": default, "value": value})
        r, g, b = _rgb(color)
        return self._set_segment_raw(segment, "video_stroke", "video_strokes", {
            "type": "video_stroke", "enable_video_stroke": True, "resource_id": row["resource_id"],
            "resource_name": row["name"], "path": _local_path(row.get("md5")), "source_platform": 1,
            "color": [1, r, g, b],  # 剪映存为 [A, R, G, B]
            "adjust_params": adjust,
        })

    def add_video_shadow(self, segment, color="#000000", alpha: float = 60, blur: float = 30,
                         distance: float = 20, angle: float = 45):
        """画面阴影 (「画面 → 基础 → 阴影」)。alpha/blur/distance 为面板数值 0-100, angle 为角度。"""
        if segment is None or not self._require_video(segment, "画面阴影"):
            return None
        return self._set_segment_raw(segment, "video_shadow", "video_shadows", {
            "type": "video_shadow", "enable_video_shadow": True, "resource_id": VIDEO_SHADOW_RES[0],
            "resource_name": "阴影", "path": _local_path(VIDEO_SHADOW_RES[1]), "source_platform": 1,
            "color": _hex_color(color), "alpha": _pct(alpha, "alpha"), "ambiguity": _pct(blur, "blur"),
            "distance": _pct(distance, "distance"), "angle": float(angle),
        })

    # ------------------------------------------------------------------ 变形
    def set_corner_pin(self, segment, upper_left=(-1.0, -1.0), upper_right=(1.0, -1.0),
                       lower_left=(-1.0, 1.0), lower_right=(1.0, 1.0)):
        """
        变形 (四角定位, 「画面 → 基础 → 变形」)。四个角点坐标与剪映存储一致: 画面范围 -1~1,
        默认是原始矩形 左上(-1,-1) 右上(1,-1) 左下(-1,1) 右下(1,1)。传 None 关闭变形。
        """
        if segment is None or not self._require_video(segment, "变形"):
            return None
        mat = self._own_video_material(segment)
        if upper_left is None:
            mat.extra_json.pop("corner_pin", None)
            return segment
        info = {}
        for name, (x, y) in (("upper_left", upper_left), ("upper_right", upper_right),
                             ("lower_left", lower_left), ("lower_right", lower_right)):
            info[f"{name}_x"], info[f"{name}_y"] = float(x), float(y)
        mat.extra_json["corner_pin"] = {"id": str(uuid.uuid4()).upper(), "enable_corner_pin": True,
                                        "corner_pin_info": info}
        return segment

    # ------------------------------------------------------------------ 色度抠图
    def set_chroma_key(self, segment, color="#00ff00", intensity: float = 40, shadow: float = 20):
        """色度抠图 (「画面 → 抠像 → 色度抠图」): 抠掉指定颜色。intensity 强度 / shadow 阴影为面板数值 0-100。"""
        if segment is None or not self._require_video(segment, "色度抠图"):
            return None
        bundle = find_jianying_app_resource("Chroma2") or ""
        if not bundle:
            print("⚠️ 没找到剪映内置的色度抠图资源 (Resources/Chroma2), 剪映打开草稿时可能不生效")
        return self._set_segment_raw(segment, "chroma", "chromas", {
            "type": "chroma", "version": "v2", "resource_id": "", "path": bundle, "should_transfer_color": True,
            "color": _hex_color(color) + "ff", "intensity_value": _pct(intensity, "intensity"),
            "shadow_value": _pct(shadow, "shadow"),
        })

    # ------------------------------------------------------------------ 美颜美体
    def set_beauty(self, segment, **values):
        """
        美颜 / 美型 / 美妆 / 智能美体 (「画面 → 美颜美体」), 名称见 panel_beauty.csv, 面板数值 0~100 (0 = 清除):
            p.set_beauty(seg, 磨皮=50, 美白=30, 瘦脸=30, 大眼=20, 瘦腰=40, 长腿=30)
            p.set_beauty(seg, 橘子汽水=80, 柔和粉=60)   # 美妆 (套装/口红/腮红...), 数值为浓度 (面板默认 80)
        剪映打开草稿时会自动计算; 画面里需要有清晰的人脸 / 人体。category_id 用面板分组的 key (皮肤管理 =
        skin_management, 脸型 = face_shape, 五官 = facial_features ...), 美妆子类 (套装/口红/腮红...) 见 categories 列。
        """
        if segment is None or not self._require_video(segment, "美颜美体"):
            return None
        rows = [r for r in load_panel_rows("panel_beauty.csv") if r["kind"] in BEAUTY_KINDS]
        order = list(BEAUTY_KINDS)  # 同名项 (如「磨皮」「美白」在美颜和美体里都有) 按 美颜 > 美型 > 美妆 > 美体 取
        rows.sort(key=lambda r: order.index(r["kind"]))
        for name, value in values.items():
            row = next((r for r in rows if r["name"] == name), None)
            if row is None:
                raise ValueError(f"未知美颜美体项: {name} (见 data/panel_beauty.csv, 先运行 jy_wrapper.py sync --only panel)")
            if row.get("is_vip") == "1" and not getattr(self, "allow_vip", True):
                print(f"⛔ 「{name}」为 VIP 素材，已按 allow_vip=False 跳过")
                continue
            kind = row["kind"]
            category, sub_type, effect_type = BEAUTY_KINDS[kind]
            info = json.loads(row.get("params") or "{}")
            v = float(value)
            lo = -100 if float(info.get("slider_minValue", 0) or 0) < 0 else 0
            if not lo <= v <= 100:
                raise ValueError(f"「{name}」取值 {lo}~100 (剪映面板数值), 收到 {value}")
            mat = None
            if v:
                mat = {"type": "figure", "name": name, "resource_id": row["resource_id"],
                       "third_resource_id": info.get("third_resource_id") or row["resource_id"],
                       "path": _local_path(row.get("md5")), "category_id": info.get("category_key") or category,
                       "sub_type": "none" if name == "美白" else sub_type, "item_effect_type": effect_type,
                       "source_platform": 1}
                if info.get("intensity_key"):
                    mat["intensity_key"] = info["intensity_key"]
                if kind == "beauty_makeup":
                    tag = info.get("intensity_key") or info.get("beautify_tag") or ""
                    mat.update({"intensity_key": tag, "version": "v2", "exclusion_group": info.get("exclusion_group", []),
                                "face_adjust_params": [{"face_id": "0",
                                                        "adjust_params": [{"name": tag, "value": v / 100}]}]})
                    cat = [c for c in (row.get("categories") or "").split("|") if c in MAKEUP_PARTS]
                    if cat:
                        mat["category_name"] = cat[0]
                elif kind == "beauty_body" or name in BEAUTY_VALUE_ITEMS:
                    mat["value"] = v / 100
                else:
                    mat["adjust_params"] = [{"name": "0", "value": v / 100}]
            self._set_segment_raw(segment, f"beauty:{name}", "effects", mat)
        root = next((r for r in load_panel_rows("panel_beauty.csv") if r["name"] == "makeup-root"), None)
        if any(k.startswith("beauty:") for k in getattr(segment, "_raw_refs", {})):
            info = json.loads((root or {}).get("params") or "{}")
            self._set_segment_raw(segment, "makeup_root", "effects", {
                "type": "makeup_root", "name": "makeup-root", "sub_type": "auto_beauty", "source_platform": 1,
                "resource_id": (root or {}).get("resource_id", MAKEUP_ROOT[0]),
                "third_resource_id": info.get("third_resource_id", MAKEUP_ROOT[2]),
                "path": _local_path((root or {}).get("md5") or MAKEUP_ROOT[1])})
        else:
            self._set_segment_raw(segment, "makeup_root", "effects", None)
        return segment

    def set_manual_slim(self, segment, strength: float = 50, region=(0.33, 0.10, 0.33, 0.57), rotation: float = 0):
        """
        手动美体 → 瘦身瘦腿 (「画面 → 美颜美体 → 美体 → 手动美体」)。strength 面板数值 0~100;
        region = (x, y, 宽, 高), 画面比例 0~1, 左上角为原点, 框住要瘦的人体。
        """
        if segment is None or not self._require_video(segment, "手动美体"):
            return None
        x, y, w, h = (float(v) for v in region)
        mat = self._own_video_material(segment)
        mat.check_flag_bits |= MANUAL_FIGURE_FLAG
        self._set_segment_raw(segment, "manual_stretch", "effects", {
            "type": "figure", "sub_type": "manual_stretch", "resource_id": MANUAL_STRETCH[0],
            "third_resource_id": MANUAL_STRETCH[2], "path": _local_path(MANUAL_STRETCH[1]),
            "category_id": "manual-figure", "item_effect_type": 24, "source_platform": 1})
        return self._set_segment_raw(segment, "manual_slim", "effects", {
            "type": "figure", "sub_type": "manual_slim", "name": "瘦身瘦腿", "value": _pct(strength, "strength"),
            "resource_id": MANUAL_SLIM[0], "third_resource_id": MANUAL_SLIM[2], "path": _local_path(MANUAL_SLIM[1]),
            "category_id": "manual-figure", "item_effect_type": 24, "source_platform": 1,
            "adjust_params": [{"name": "x", "value": x}, {"name": "y", "value": y}, {"name": "width", "value": w},
                              {"name": "height", "value": h}, {"name": "rotation", "value": float(rotation)}]})

    # ------------------------------------------------------------------ 防抖 (剪映打开草稿时自动处理); 运动模糊见 quality_ops
    def set_stabilize(self, segment, level: int = 2):
        """视频防抖 (「画面 → 基础 → 视频防抖」)。level=2 为「推荐」(11.7 实测), 0 关闭; 其他档位编号未验证。"""
        if segment is None or not self._require_video(segment, "视频防抖"):
            return None
        mat = self._own_video_material(segment)
        if not level:
            mat.extra_json.pop("stable", None)
        else:
            mat.extra_json["stable"] = {"stable_level": int(level), "time_range": {}}
        return segment

    def set_smart_matting(self, segment):
        """
        智能抠像 (VIP, 「画面 → 抠像 → 智能抠像」)。剪映 11.7 实测: 只写开关时面板显示已勾选, 但不会开始计算;
        需要在剪映里对该片段「取消勾选 → 重新勾选」一次才会处理。保存时会列出需要这样操作的片段。
        """
        if segment is None or not self._require_video(segment, "智能抠像"):
            return None
        mat = self._own_video_material(segment)
        mat.extra_json["matting"] = {"flag": 3, "custom_matting_id": str(uuid.uuid4()).upper(), "path": ""}
        pending = getattr(self, "_pending_matting", None)
        if pending is None:
            pending = self._pending_matting = []
        pending.append(segment)
        return segment

    def _report_pending_matting(self):
        segs = getattr(self, "_pending_matting", None) or []
        if segs:
            times = "、".join(f"{s.target_timerange.start / 1e6:.1f}s" for s in segs)
            print(f"✂️ 智能抠像需要在剪映里对这些片段「取消勾选 → 重新勾选」一次才会开始处理: {times}")

