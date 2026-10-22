"""
调整类设置 (剪映 11.x「调整」面板): 基础调节 16 项、HSL、曲线、色轮、智能调色。

都使用剪映的调整资源包 (resource_id 7416258989467374091, 在剪映里打开过调整面板就会下载)。
字段来自剪映 11.7 写出的真实草稿 (references/jy11_samples/picture_adjust_ref.json)。
参数用剪映面板上显示的数值: 双向滑块 -50~50 (存储 = 数值/50), 单向滑块 0~100 (存储 = 数值/100)。
"""

import copy
import json
import os
import re
import shutil
import uuid
from typing import Dict, Iterable, Optional, Sequence, Tuple

import pyJianYingDraft as draft
from utils.panel_assets import _local_md5_index, find_jianying_app_resource, get_jianying_user_data_dirs

ADJUST_RES = ("7416258989467374091", "2a5048e9c3ab6d08ecd65fd781d276d5")

# 面板名称 -> (剪映 type, 是否单向滑块 0~100)
ADJUST_ITEMS: Dict[str, Tuple[str, bool]] = {
    "色温": ("temperature", False), "色调": ("tone", False), "饱和度": ("saturation", False),
    "自然饱和度": ("natural_saturation", False), "亮度": ("brightness", False), "对比度": ("contrast", False),
    "高光": ("highlight", False), "阴影": ("shadow", False), "白色": ("white", False), "黑色": ("black", False),
    "光感": ("light_sensation", False), "锐化": ("sharpen", True), "清晰": ("clear", True),
    "颗粒": ("particle", True), "褪色": ("fade", True), "暗角": ("vignetting", False),
}
_BY_TYPE = {t: (t, uni) for t, uni in ADJUST_ITEMS.values()}
# 剪映 11.7 写入的各调节项算法版本 (未列出的不写 version)
ADJUST_VERSIONS = {"brightness": "v2", "contrast": "v3", "highlight": "v3", "shadow": "v3", "temperature": "v3",
                   "tone": "v3", "saturation": "v1", "natural_saturation": "v1", "sharpen": "v1",
                   "vignetting": "v1", "particle": "v2"}
# HSL 颜色的 custom_color (剪映 11.7 草稿中红色为 #FFE64444; 其他颜色未知, 不写)
HSL_CUSTOM_COLORS = {1: "#FFE64444"}

# HSL 八种颜色 (面板顺序); 红色 = 1 由真实草稿确认, 蓝色 = 6 剪映内验证
HSL_COLORS = {"红": 1, "橙": 2, "黄": 3, "绿": 4, "青": 5, "蓝": 6, "紫": 7, "洋红": 8}


def _adjust_resource() -> dict:
    path = _local_md5_index().get(ADJUST_RES[1], "")
    return {"resource_id": ADJUST_RES[0], "path": path, "lumi_hub_path": f"{path}/lumi_hub_path" if path else "",
            "source_platform": 1}


def _read_cube(path: str):
    """.cube -> (size, [(r, g, b), ...]) (红色变化最快, 与 Pillow Color3DLUT 的顺序一致)。"""
    size, table = 0, []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            parts = line.split()
            if not parts or parts[0].startswith("#"):
                continue
            if parts[0] == "LUT_3D_SIZE":
                size = int(parts[1])
            elif len(parts) == 3 and parts[0][0] in "-.0123456789":
                table.append(tuple(float(v) for v in parts))
    return size, table


def import_lut(cube_path: str) -> str:
    """
    像剪映「导入 LUT」一样把 .cube 放进剪映的 LUT 库: User Data/Resources/Lut/<名称>/ 下放
    特效配置 (取自剪映安装包 Resources/lut)、.cube 和缩略图, 并登记到 LutImportCfg.json (改动前备份)。返回 .cube 的绝对路径。
    """
    dirs = get_jianying_user_data_dirs()
    if not dirs:
        raise FileNotFoundError("没找到剪映 User Data 目录, 无法导入 LUT")
    stem = re.sub(r"[^\w\-]+", "_", os.path.splitext(os.path.basename(cube_path))[0]).strip("_") or "lut"
    root = os.path.join(dirs[0], "Resources", "Lut")
    lib = os.path.join(root, stem)
    os.makedirs(lib, exist_ok=True)
    target = os.path.join(lib, f"{stem}.cube")
    if not os.path.exists(target) or os.path.getsize(target) != os.path.getsize(cube_path):
        shutil.copy2(cube_path, target)
    template = find_jianying_app_resource("lut")
    for name in ("config.json", "algorithmConfig.json"):
        if template and os.path.exists(os.path.join(template, name)) and not os.path.exists(os.path.join(lib, name)):
            shutil.copy2(os.path.join(template, name), os.path.join(lib, name))
    image = os.path.join(lib, f"{stem}.jpeg")
    if not os.path.exists(image):
        base = find_jianying_app_resource("lut-import.webp")  # 剪映导入 LUT 时用的示例照片 (2400x1600)
        size, table = _read_cube(target)
        if base and size and len(table) == size ** 3:
            try:
                from PIL import Image, ImageFilter

                img = Image.open(base).convert("RGB").resize((1200, 800))
                img.filter(ImageFilter.Color3DLUT(size, table)).save(image, "JPEG", quality=90)
            except Exception as e:  # 缩略图只影响剪映 LUT 列表的显示
                print(f"ℹ️ LUT 缩略图生成失败 ({e}), 不影响使用")
    cfg = os.path.join(root, "LutImportCfg.json")
    entries = []
    if os.path.exists(cfg):
        with open(cfg, "r", encoding="utf-8") as f:
            entries = json.load(f) or []
        if not os.path.exists(cfg + ".skill.bak"):
            shutil.copy2(cfg, cfg + ".skill.bak")
    if not any(e.get("path") == target for e in entries):
        entries.append({"imagePath": image if os.path.exists(image) else "", "originPath": os.path.abspath(cube_path),
                        "path": target, "title": os.path.basename(cube_path)})
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=4)
    return target


def _curve(points: Sequence[Tuple[float, float]]) -> list:
    """锚点 [(x, y), ...] (0~1, 自动补 (0,0)/(1,1)) -> 剪映的贝塞尔曲线点 (控制点按相邻点平滑生成)。"""
    pts = sorted({(round(float(x), 6), round(float(y), 6)) for x, y in points} | {(0.0, 0.0), (1.0, 1.0)})
    out = []
    for i, (x, y) in enumerate(pts):
        prev = pts[i - 1] if i > 0 else (x, y)
        nxt = pts[i + 1] if i + 1 < len(pts) else (x, y)
        tx, ty = (nxt[0] - prev[0]) / 2, (nxt[1] - prev[1]) / 2  # 切线
        lx = max(prev[0], x - tx / 3) if i > 0 else x
        rx = min(nxt[0], x + tx / 3) if i + 1 < len(pts) else x
        out.append({"anchor": {"x": x, "y": y},
                    "left_control": {"x": lx, "y": y - ty / 3 if i > 0 else y},
                    "right_control": {"x": rx, "y": y + ty / 3 if i + 1 < len(pts) else y}})
    return out


class AdjustOpsMixin:
    """JyProject 的调整 (调色) Mixin。依赖 PictureOpsMixin._set_segment_raw。"""

    def set_adjust(self, segment, **values):
        """
        基础调节, 参数用剪映面板名称或英文 type, 值为面板数值:
            p.set_adjust(seg, 色温=-14, 亮度=9, 锐化=34, 暗角=21)
            p.set_adjust(seg, temperature=-14, brightness=9)
        双向滑块 -50~50, 单向滑块 (锐化/清晰/颗粒/褪色) 0~100; 传 0 表示清除该项。
        注: 与 add_keyframes 的亮度/对比度/饱和度关键帧是两套机制, 同一片段请只用一种。
        """
        if segment is None or not isinstance(segment, (draft.VideoSegment,)):
            print("⚠️ 调节只能用于视频/图片片段")
            return None
        for key, value in values.items():
            item = ADJUST_ITEMS.get(key) or _BY_TYPE.get(key)
            if not item:
                raise ValueError(f"未知调节项: {key} (可选: {'、'.join(ADJUST_ITEMS)})")
            typ, unipolar = item
            v = float(value)
            lo, hi = (0, 100) if unipolar else (-50, 50)
            if not lo <= v <= hi:
                raise ValueError(f"{key} 取值 {lo}~{hi} (剪映面板数值), 收到 {value}")
            mat = None
            if v:
                mat = {"type": typ, "value": v / (100 if unipolar else 50), "effect_id": ADJUST_RES[0],
                       "sub_type": "none", "apply_target_type": 0, "platform": "all", **_adjust_resource()}
                if typ in ADJUST_VERSIONS:
                    mat["version"] = ADJUST_VERSIONS[typ]
            self._set_segment_raw(segment, f"adjust:{typ}", "effects", mat)
        return segment

    def set_hsl(self, segment, color: str = "红", hue: float = 0, saturation: float = 0, lightness: float = 0):
        """HSL: 对某一种颜色调整色相/饱和度/亮度 (面板数值 -100~100)。color: 红/橙/黄/绿/青/蓝/紫/洋红。"""
        if segment is None:
            return None
        ctype = HSL_COLORS.get(str(color).strip().rstrip("色"))
        if ctype is None:
            raise ValueError(f"未知 HSL 颜色: {color} (可选: {'、'.join(HSL_COLORS)})")
        for name, v in (("hue", hue), ("saturation", saturation), ("lightness", lightness)):
            if not -100 <= float(v) <= 100:
                raise ValueError(f"HSL {name} 取值 -100~100, 收到 {v}")
        mat = {"type": "hsl", "version": "1", "hsl_color_type": ctype, "interacting": True,
               "constant_material_id": str(uuid.uuid4()).upper(),
               "hue": float(hue), "saturation": float(saturation), "lightness": float(lightness), **_adjust_resource()}
        if ctype in HSL_CUSTOM_COLORS:
            mat["custom_color"] = HSL_CUSTOM_COLORS[ctype]
        return self._set_segment_raw(segment, f"hsl:{ctype}", "hsl", mat)

    def set_curves(self, segment, luma: Optional[Iterable] = None, red: Optional[Iterable] = None,
                   green: Optional[Iterable] = None, blue: Optional[Iterable] = None):
        """
        曲线: 各通道传锚点 [(x, y), ...] (0~1, 横轴为输入亮度, 纵轴为输出; 首尾 (0,0)/(1,1) 自动补上)。
        只传 luma 时 RGB 跟随亮度曲线 (与剪映"联动"一致)。例: 提亮中间调 luma=[(0.5, 0.6)]。
        """
        if segment is None:
            return None
        base = list(luma) if luma is not None else []
        linked = red is None and green is None and blue is None
        chans = {"luma": _curve(base)}
        for name, pts in (("red", red), ("green", green), ("blue", blue)):
            chans[name] = _curve(base if linked else (list(pts) if pts is not None else []))
        return self._set_segment_raw(segment, "color_curves", "color_curves", {
            "type": "color_curves", "panel": "adjust_collection", "link_status": linked, **chans,
            **_adjust_resource(),
        })

    def set_color_wheels(self, segment, lift=None, gamma=None, gain=None, offset=None):
        """
        一级色轮: lift 暗部 / gamma 中间调 / gain 亮部 / offset 偏移, 各传 (r, g, b) 偏移量 (约 -1~1, 0 为不变)。
        例: 暗部偏蓝 lift=(0, 0, 0.15)。
        """
        if segment is None:
            return None
        wheels = {}
        for name, v in (("lift", lift), ("gamma", gamma), ("gain", gain), ("offset", offset)):
            r, g, b = v if v is not None else (0, 0, 0)
            wheels[name] = {"red": float(r), "green": float(g), "blue": float(b)}
        return self._set_segment_raw(segment, "color_wheels", "primary_color_wheels", {
            "type": "primary_color_wheels", "panel": "adjust_collection", **wheels, **_adjust_resource(),
        })

    def set_lut(self, segment, cube_path: str, strength: float = 100, skin_protect: float = 0):
        """
        LUT (「调整 → 基础 → LUT」): 导入 .cube 文件。strength 强度 / skin_protect 肤色保护为面板数值 0~100。
        与在剪映里导入一样, 文件会复制到剪映的 LUT 库 (User Data/Resources/Lut/<名称>/), 之后也会出现在剪映的 LUT 列表中。
        """
        if segment is None:
            return None
        if not os.path.isfile(cube_path) or not cube_path.lower().endswith(".cube"):
            raise FileNotFoundError(f"需要 .cube 文件: {cube_path}")
        for name, v in (("strength", strength), ("skin_protect", skin_protect)):
            if not 0 <= float(v) <= 100:
                raise ValueError(f"LUT {name} 取值 0~100, 收到 {v}")
        target = import_lut(cube_path)
        res = _adjust_resource()
        self._set_segment_raw(segment, "lut", "effects", {
            "type": "lut", "name": ADJUST_RES[1], "value": float(strength) / 100, "effect_id": ADJUST_RES[0],
            "sub_type": "none", "apply_target_type": 0, "platform": "all", **res,
            # 剪映明文 template.json 里显示为 materials/lut/<名称>/<名称>.cube, 草稿本身需要绝对路径
            "path": target,
        })
        mat = None
        if float(skin_protect):
            mat = {"type": "skin_tone_correction", "version": "v3", "value": float(skin_protect) / 100,
                   "effect_id": ADJUST_RES[0], "sub_type": "none", "apply_target_type": 0, "platform": "all", **res}
        return self._set_segment_raw(segment, "skin_tone_correction", "effects", mat)

    def set_smart_color(self, segment, strength: float = 80):
        """智能调色 (VIP, 「调整 → 基础 → 智能调色」): strength 强度 0~100。剪映打开草稿时按开关自动计算 (11.7 实测)。"""
        if segment is None:
            return None
        if not 0 <= float(strength) <= 100:
            raise ValueError(f"智能调色强度取值 0~100, 收到 {strength}")
        segment.extra_flags = {**(getattr(segment, "extra_flags", None) or {}), "enable_smart_color_adjust": True}
        return self._set_segment_raw(segment, "smart_color", "effects", {
            "type": "smart_color_adjust", "name": "调色所有小项", "value": float(strength) / 100,
            "effect_id": ADJUST_RES[0], "sub_type": "none", "apply_target_type": 0, "platform": "all",
            **_adjust_resource(),
        })

    def add_local_adjust(self, segment, mask: str = "圆形", track_name: str = "LocalAdjust",
                         mask_kwargs: Optional[dict] = None, **adjust):
        """
        局部调色: 只调蒙版内的区域, 蒙版外保留原画面。做法是在上层轨道复制一份同样的片段,
        给复制品加蒙版 + 调节 (剪映的「调整 → 蒙版」实际就是画面蒙版, 见 rules/jianying-11.md)。
            p.add_local_adjust(seg, "圆形", mask_kwargs={"size": 0.5, "feather": 30}, 亮度=30)
        返回上层的复制片段。原片段之后的剪辑 (移动/裁剪) 不会自动同步到复制片段。
        """
        if segment is None or not isinstance(segment, draft.VideoSegment):
            print("⚠️ 局部调色只能用于视频/图片片段")
            return None
        src = segment.source_timerange
        copy_seg = draft.VideoSegment(
            segment.material_instance, draft.Timerange(segment.target_timerange.start, segment.target_timerange.duration),
            source_timerange=draft.Timerange(src.start, src.duration) if src else None,
            speed=segment.speed.speed, clip_settings=copy.deepcopy(segment.clip_settings),
        )
        copy_seg.volume = 0.0  # 复制片段静音, 避免声音叠加
        self._ensure_track(draft.TrackType.video, track_name)
        self.script.add_segment(copy_seg, track_name)
        self.add_mask_simple(copy_seg, mask, **(mask_kwargs or {}))
        if adjust:
            self.set_adjust(copy_seg, **adjust)
        return copy_seg

