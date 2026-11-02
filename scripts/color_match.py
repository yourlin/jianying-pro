"""
统一调色 (T13): 按参考帧 / 参考镜头, 把若干镜头匹配到同一色调。

    python color_match.py suggest --ref ref.jpg --shots a.mp4 b.mp4@3.5-8 c.png -o color_match.json
    python color_match.py suggest --analysis analysis.json --ref-shot 0 --lut-dir luts/ --json
    python color_match.py suggest --ref auto --shots a.mp4 b.mp4 c.mp4 --preview-dir preview/
    python color_match.py stats clip.mp4@0-3 --json

做法:
  1. 每个镜头抽几帧 (默认 5 帧, 缩到最长边 320), 统计 Lab 均值 / 标准差、亮度分位数 (p5 / p50 / p95)、
     色度 (饱和度)、灰世界白平衡 (R/G、B/G)。
  2. 用一个近似的「剪映调节」模型 (simulate_adjust) 把面板参数 色温 / 色调 / 饱和度 / 亮度 / 对比度 /
     高光 / 阴影 映射为 Lab 上的变化, 迭代拟合出让镜头统计量接近参考的面板数值 (-50~50, 与 set_adjust 一致)。
  3. 可选生成每段的 .cube LUT (Lab 均值 / 方差迁移, Reinhard 法), 配合 set_lut 使用 (比面板参数更精确)。

镜头写法: 图片路径 / 视频路径 / 视频路径@开始-结束 (秒)。--analysis 读 analyze_footage.py 的 analysis.json,
按 scenes (镜头切换时间点) 把主视频切成若干镜头。--ref auto: 选统计量最接近全体中位数的镜头做参考。

注意: 剪映的调节算法没有公开, simulate_adjust 是按面板方向和大致幅度做的近似模型 (见 MODEL_GAINS),
建议参数方向可靠, 幅度需在剪映里预览微调; 要求精确匹配时用 LUT 模式。
"""

import argparse
import json
import math
import os
import re
import sys
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.cli_protocol import emit_result, make_result  # noqa: E402

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
MODEL_NAME = "jy_adjust_approx_v1"

# 近似模型: 面板数值每 1 格对应的 Lab 变化 (L 0~100, a/b 约 -128~127)
MODEL_GAINS = {
    "temperature": 0.45,  # 色温 +1 -> b (黄) +0.45; 正值偏暖, 负值偏冷 (蓝)
    "tone": 0.35,  # 色调 +1 -> a (洋红) +0.35; 正值偏洋红/紫, 负值偏绿
    "brightness": 0.4,  # 亮度 +1 -> L +0.4 (中间调最强, 两端递减)
    "contrast": 0.01,  # 对比度 +1 -> 以 L=50 为轴拉伸 1%
    "saturation": 0.02,  # 饱和度 +1 -> 色度 (a, b) 放大 2%; -50 为黑白
    "highlight": 0.3,  # 高光 +1 -> 亮部 L +0.3 (按亮度权重)
    "shadow": 0.3,  # 阴影 +1 -> 暗部 L +0.3 (按暗度权重)
}
# 面板名 <-> 英文 type (与 core/adjust_ops.ADJUST_ITEMS 一致)
PANEL_NAMES = {
    "temperature": "色温",
    "tone": "色调",
    "saturation": "饱和度",
    "brightness": "亮度",
    "contrast": "对比度",
    "highlight": "高光",
    "shadow": "阴影",
}
PARAM_KEYS = tuple(PANEL_NAMES)
PANEL_RANGE = (-50.0, 50.0)


# ------------------------------------------------------------------ 读帧
def _cv2():
    import cv2

    return cv2


def _resize_max(img: np.ndarray, max_side: int) -> np.ndarray:
    h, w = img.shape[:2]
    s = max(h, w)
    if max_side and s > max_side:
        k = max_side / s
        img = _cv2().resize(img, (max(1, int(w * k)), max(1, int(h * k))), interpolation=3)
    return img


def load_image(path: str, max_side: int = 320) -> np.ndarray:
    """读图片 -> RGB float32 (0~1)。用 imdecode 兼容中文路径。"""
    cv2 = _cv2()
    data = np.fromfile(path, dtype=np.uint8)
    bgr = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
    if bgr is None:
        raise ValueError(f"读不到图片: {path}")
    return _resize_max(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), max_side).astype(np.float32) / 255.0


def sample_frames(
    path: str,
    start: Optional[float] = None,
    end: Optional[float] = None,
    samples: int = 5,
    max_side: int = 320,
) -> List[np.ndarray]:
    """
    从图片或视频的 [start, end] (秒) 均匀抽 samples 帧, 返回 RGB float32 (0~1) 列表。
    图片直接返回 1 帧。避开首尾 10% (转场 / 黑场)。
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"找不到素材: {path}")
    if os.path.splitext(path)[1].lower() in IMAGE_EXTS:
        return [load_image(path, max_side)]
    cv2 = _cv2()
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise ValueError(f"打不开视频: {path}")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        dur = n / fps if n > 0 else 0.0
        s = max(0.0, float(start or 0.0))
        e = float(end) if end is not None else (dur or s + 1.0)
        if dur:
            e = min(e, dur)
        if e <= s:
            e = s + 1.0 / fps
        span = e - s
        k = max(1, int(samples))
        times = [s + span * (0.1 + 0.8 * (i + 0.5) / k) for i in range(k)]
        frames = []
        for t in times:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, bgr = cap.read()
            if ok and bgr is not None:
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                frames.append(_resize_max(rgb, max_side).astype(np.float32) / 255.0)
        if not frames:  # 有的编码 seek 失败, 退回顺序读第一帧
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, bgr = cap.read()
            if ok and bgr is not None:
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                frames.append(_resize_max(rgb, max_side).astype(np.float32) / 255.0)
    finally:
        cap.release()
    if not frames:
        raise ValueError(f"视频抽不到帧: {path}")
    return frames


def parse_shot_spec(spec: str) -> dict:
    """ "a.mp4" / "a.mp4@3.5-8" / "a.mp4@3.5-" -> {"source", "start", "end"} (秒, 可为 None)。"""
    m = re.match(r"^(.*)@([\d.]*)-([\d.]*)$", spec)
    if m and (m.group(2) or m.group(3)):
        return {
            "source": m.group(1),
            "start": float(m.group(2)) if m.group(2) else None,
            "end": float(m.group(3)) if m.group(3) else None,
        }
    return {"source": spec, "start": None, "end": None}


def shots_from_analysis(analysis_path: str) -> List[dict]:
    """analysis.json (analyze_footage.py) -> 按 scenes 切出的镜头列表。"""
    with open(analysis_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    src = data.get("source")
    if not src:
        raise ValueError("analysis.json 缺少 source 字段")
    dur = float((data.get("video") or {}).get("duration") or 0)
    cuts = sorted(float(c) for c in (data.get("scenes") or []) if 0 < float(c) < (dur or 1e12))
    edges = [0.0] + cuts + ([dur] if dur else [])
    if len(edges) < 2:
        return [{"source": src, "start": None, "end": None}]
    shots = []
    for a, b in zip(edges[:-1], edges[1:]):
        if b - a >= 0.2:  # 太短的闪切不单独调色
            shots.append({"source": src, "start": round(a, 3), "end": round(b, 3)})
    return shots or [{"source": src, "start": None, "end": None}]


# ------------------------------------------------------------------ 色彩空间
def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """RGB float (0~1, sRGB) -> Lab (L 0~100)。"""
    cv2 = _cv2()
    arr = np.clip(rgb, 0, 1).astype(np.float32)
    shape = arr.shape
    return cv2.cvtColor(arr.reshape(-1, 1, 3), cv2.COLOR_RGB2Lab).reshape(shape)


def lab_to_rgb(lab: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    arr = lab.astype(np.float32)
    shape = arr.shape
    return np.clip(cv2.cvtColor(arr.reshape(-1, 1, 3), cv2.COLOR_Lab2RGB).reshape(shape), 0, 1)


def _pixels(frames: Sequence[np.ndarray], limit: int = 60000) -> np.ndarray:
    """多帧像素拼成 (N, 3), 等间隔下采样到 limit 个以内。"""
    px = np.concatenate([f.reshape(-1, 3) for f in frames], axis=0)
    if len(px) > limit:
        px = px[:: int(math.ceil(len(px) / limit))]
    return px


# ------------------------------------------------------------------ 统计
def lab_stats_from_lab(lab: np.ndarray) -> dict:
    lab = lab.reshape(-1, 3)
    L, a, b = lab[:, 0], lab[:, 1], lab[:, 2]
    p05, p50, p95 = np.percentile(L, [5, 50, 95])
    return {
        "L_mean": float(L.mean()),
        "L_std": float(L.std()),
        "L_p05": float(p05),
        "L_p50": float(p50),
        "L_p95": float(p95),
        "a_mean": float(a.mean()),
        "b_mean": float(b.mean()),
        "a_std": float(a.std()),
        "b_std": float(b.std()),
        "chroma_mean": float(np.sqrt(a * a + b * b).mean()),
        "chroma_spread": float(math.sqrt(float(a.var()) + float(b.var()))),
    }


def color_stats(frames: Sequence[np.ndarray]) -> dict:
    """
    帧列表 (RGB 0~1) -> 统计量: Lab 均值/标准差、亮度分位数、色度,
    以及灰世界白平衡 rg / bg (R/G、B/G 均值比; 中性画面约为 1)。
    """
    px = _pixels(frames)
    st = lab_stats_from_lab(rgb_to_lab(px))
    mean = px.mean(axis=0) + 1e-6
    st["rgb_mean"] = [round(float(v), 4) for v in mean]
    st["wb_rg"] = float(mean[0] / mean[1])
    st["wb_bg"] = float(mean[2] / mean[1])
    return st


def stats_distance(s1: dict, s2: dict) -> float:
    """两组统计量的差异 (约等于 Lab ΔE 量级): 均值色差 + 亮度分布 + 色度差。"""
    keys = ("L_mean", "a_mean", "b_mean", "L_std", "chroma_spread")
    d2 = sum((s1[k] - s2[k]) ** 2 for k in keys)
    d2 += 0.5 * ((s1["L_p05"] - s2["L_p05"]) ** 2 + (s1["L_p95"] - s2["L_p95"]) ** 2)
    return float(math.sqrt(d2))


def describe_diff(src: dict, ref: dict) -> dict:
    """镜头相对参考的差异 (正值 = 镜头比参考更大) + 文字描述。"""
    d = {
        "L_mean": src["L_mean"] - ref["L_mean"],
        "L_std": src["L_std"] - ref["L_std"],
        "a_mean": src["a_mean"] - ref["a_mean"],
        "b_mean": src["b_mean"] - ref["b_mean"],
        "chroma": src["chroma_spread"] - ref["chroma_spread"],
        "wb_rg": src["wb_rg"] - ref["wb_rg"],
        "wb_bg": src["wb_bg"] - ref["wb_bg"],
    }
    notes = []
    if abs(d["L_mean"]) >= 3:
        notes.append("偏亮" if d["L_mean"] > 0 else "偏暗")
    if abs(d["b_mean"]) >= 2:
        notes.append("偏暖 (黄)" if d["b_mean"] > 0 else "偏冷 (蓝)")
    if abs(d["a_mean"]) >= 2:
        notes.append("偏洋红" if d["a_mean"] > 0 else "偏绿")
    if abs(d["L_std"]) >= 3:
        notes.append("对比度偏高" if d["L_std"] > 0 else "对比度偏低")
    if abs(d["chroma"]) >= 2:
        notes.append("饱和度偏高" if d["chroma"] > 0 else "饱和度偏低")
    out = {k: round(v, 3) for k, v in d.items()}
    out["notes"] = notes
    return out


# ------------------------------------------------------------------ 近似调节模型
def _smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def _norm_params(params: Optional[dict]) -> Dict[str, float]:
    """面板名或英文 type -> 英文 type 的 float 字典 (缺省 0)。"""
    by_name = {v: k for k, v in PANEL_NAMES.items()}
    out = {k: 0.0 for k in PARAM_KEYS}
    for k, v in (params or {}).items():
        key = k if k in out else by_name.get(k)
        if key is None:
            raise ValueError(f"不支持的调节项: {k} (可选: {'、'.join(PANEL_NAMES.values())})")
        out[key] = float(v)
    return out


def simulate_adjust_lab(lab: np.ndarray, params: Optional[dict]) -> np.ndarray:
    """在 Lab 上应用近似的剪映调节 (顺序: 白平衡 -> 饱和度 -> 对比度 -> 亮度 -> 高光/阴影)。"""
    p = _norm_params(params)
    g = MODEL_GAINS
    out = lab.astype(np.float32).copy()
    L, a, b = out[..., 0], out[..., 1], out[..., 2]
    a += g["tone"] * p["tone"]
    b += g["temperature"] * p["temperature"]
    s = max(0.0, 1.0 + g["saturation"] * p["saturation"])
    a *= s
    b *= s
    L[...] = (L - 50.0) * (1.0 + g["contrast"] * p["contrast"]) + 50.0
    mid = 1.0 - 0.5 * ((np.clip(L, 0, 100) - 50.0) / 50.0) ** 2
    L += g["brightness"] * p["brightness"] * mid
    L += g["highlight"] * p["highlight"] * _smoothstep(45.0, 95.0, L)
    L += g["shadow"] * p["shadow"] * (1.0 - _smoothstep(5.0, 55.0, L))
    np.clip(L, 0, 100, out=L)
    return out


def simulate_adjust(rgb: np.ndarray, params: Optional[dict]) -> np.ndarray:
    """RGB (0~1) 上模拟 set_adjust 的效果, 用于预览和验证建议参数。"""
    return lab_to_rgb(simulate_adjust_lab(rgb_to_lab(rgb), params))


def _clamp(v: float) -> float:
    return float(min(PANEL_RANGE[1], max(PANEL_RANGE[0], v)))


def fit_adjust(
    src_lab: np.ndarray, ref: dict, iterations: int = 8
) -> Tuple[Dict[str, float], dict]:
    """
    拟合面板参数, 使 simulate_adjust_lab(src) 的统计量接近参考 ref。
    返回 (英文 type -> 面板数值 (未取整), 拟合后的统计量)。
    """
    g = MODEL_GAINS
    src = lab_stats_from_lab(src_lab)
    p = {k: 0.0 for k in PARAM_KEYS}
    # 初值: 色度 / 对比度按比例, 白平衡 / 亮度按均值差
    sat = ref["chroma_spread"] / max(src["chroma_spread"], 1e-3)
    p["saturation"] = _clamp((sat - 1.0) / g["saturation"])
    con = ref["L_std"] / max(src["L_std"], 1e-3)
    p["contrast"] = _clamp((con - 1.0) / g["contrast"])
    s = 1.0 + g["saturation"] * p["saturation"]
    if s > 1e-3:
        p["temperature"] = _clamp((ref["b_mean"] / s - src["b_mean"]) / g["temperature"])
        p["tone"] = _clamp((ref["a_mean"] / s - src["a_mean"]) / g["tone"])
    # 迭代修正残差 (简单不动点迭代, 每轮按残差 / 增益修正, 带阻尼)
    sim = lab_stats_from_lab(simulate_adjust_lab(src_lab, p))
    for _ in range(max(1, iterations)):
        sim = lab_stats_from_lab(simulate_adjust_lab(src_lab, p))
        damp = 0.8
        p["brightness"] = _clamp(
            p["brightness"] + damp * (ref["L_p50"] - sim["L_p50"]) / (g["brightness"] * 0.9)
        )
        p["highlight"] = _clamp(
            p["highlight"] + damp * 0.6 * (ref["L_p95"] - sim["L_p95"]) / g["highlight"]
        )
        p["shadow"] = _clamp(p["shadow"] + damp * 0.6 * (ref["L_p05"] - sim["L_p05"]) / g["shadow"])
        if sim["L_std"] > 1e-3:
            ratio = ref["L_std"] / sim["L_std"]
            cur = 1.0 + g["contrast"] * p["contrast"]
            p["contrast"] = _clamp((cur * (1 + damp * (ratio - 1)) - 1.0) / g["contrast"])
        if sim["chroma_spread"] > 1e-3:
            ratio = ref["chroma_spread"] / sim["chroma_spread"]
            cur = 1.0 + g["saturation"] * p["saturation"]
            p["saturation"] = _clamp((cur * (1 + damp * (ratio - 1)) - 1.0) / g["saturation"])
        s = max(1e-3, 1.0 + g["saturation"] * p["saturation"])
        p["temperature"] = _clamp(
            p["temperature"] + damp * (ref["b_mean"] - sim["b_mean"]) / (g["temperature"] * s)
        )
        p["tone"] = _clamp(p["tone"] + damp * (ref["a_mean"] - sim["a_mean"]) / (g["tone"] * s))
    sim = lab_stats_from_lab(simulate_adjust_lab(src_lab, p))
    return p, sim


def suggest_adjust(
    src_frames: Sequence[np.ndarray],
    ref_stats: dict,
    strength: float = 1.0,
    min_step: float = 1.0,
) -> dict:
    """
    给镜头算 set_adjust 建议参数。
    strength: 0~1, 建议值整体缩放 (保守调色可用 0.7)。min_step: 绝对值小于它的项省略。
    返回 {"adjust": {面板名: 整数}, "params": {英文 type: 整数}, "distance_before", "distance_after", "warnings"}。
    """
    strength = float(min(1.0, max(0.0, strength)))
    px = _pixels(src_frames)
    src_lab = rgb_to_lab(px)
    src_stats = lab_stats_from_lab(src_lab)
    raw, _ = fit_adjust(src_lab, ref_stats)
    params = {k: int(round(v * strength)) for k, v in raw.items()}
    params = {k: v for k, v in params.items() if abs(v) >= min_step}
    after = lab_stats_from_lab(simulate_adjust_lab(src_lab, params))
    before_d = stats_distance(src_stats, ref_stats)
    after_d = stats_distance(after, ref_stats)
    warnings = []
    if after_d > before_d:  # 模型拟合失败 (极端素材): 不给建议, 免得越调越偏
        params, after_d = {}, before_d
        warnings.append("近似模型无法减小差异, 未给出调节参数, 建议用 LUT 模式")
    hit = [PANEL_NAMES[k] for k, v in raw.items() if abs(v) >= PANEL_RANGE[1] - 1e-6]
    if hit:
        warnings.append(f"{'、'.join(hit)} 已到面板上限 ±50, 差异过大, 建议配合 LUT 或换素材")
    if before_d > 25:
        warnings.append("与参考差异很大 (可能是不同场景/光线), 调色只能部分拉近")
    return {
        "adjust": {PANEL_NAMES[k]: v for k, v in params.items()},
        "params": params,
        "distance_before": round(before_d, 3),
        "distance_after": round(after_d, 3),
        "predicted_stats": {k: round(v, 3) for k, v in after.items()},
        "warnings": warnings,
    }


# ------------------------------------------------------------------ LUT
def transfer_lab(
    lab: np.ndarray, src: dict, ref: dict, strength: float = 1.0, max_ratio: float = 2.0
) -> np.ndarray:
    """Lab 均值 / 标准差迁移 (Reinhard): (x - μs) / σs * σr + μr, 按 strength 与原值混合。"""
    out = lab.astype(np.float32).copy()
    for i, ch in enumerate(("L", "a", "b")):
        ms, mr = src[f"{ch}_mean"], ref[f"{ch}_mean"]
        ss, sr = src[f"{ch}_std"], ref[f"{ch}_std"]
        k = sr / ss if ss > 1e-3 else 1.0
        k = min(max_ratio, max(1.0 / max_ratio, k))  # 防止平坦画面被过度拉伸
        mapped = (out[..., i] - ms) * k + mr
        out[..., i] = out[..., i] + strength * (mapped - out[..., i])
    out[..., 0] = np.clip(out[..., 0], 0, 100)
    return out


def build_lut(src: dict, ref: dict, size: int = 33, strength: float = 1.0) -> np.ndarray:
    """生成 3D LUT 表 (size^3, 3), 顺序为红色变化最快 (.cube 标准, 与 adjust_ops._read_cube 一致)。"""
    if not 2 <= int(size) <= 65:
        raise ValueError("LUT 尺寸取值 2~65")
    grid = np.linspace(0, 1, int(size), dtype=np.float32)
    b, g, r = np.meshgrid(grid, grid, grid, indexing="ij")
    rgb = np.stack([r.ravel(), g.ravel(), b.ravel()], axis=1)
    return lab_to_rgb(transfer_lab(rgb_to_lab(rgb), src, ref, strength)).reshape(-1, 3)


def write_cube(path: str, table: np.ndarray, size: int, title: str = "color_match") -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f'TITLE "{title}"\n')
        f.write(f"LUT_3D_SIZE {int(size)}\n")
        f.write("DOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n")
        for r, g, b in np.clip(table, 0, 1):
            f.write(f"{r:.6f} {g:.6f} {b:.6f}\n")
    return os.path.abspath(path)


def read_cube(path: str) -> Tuple[int, np.ndarray]:
    size, rows = 0, []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            parts = line.split()
            if not parts or parts[0].startswith("#"):
                continue
            if parts[0] == "LUT_3D_SIZE":
                size = int(parts[1])
            elif len(parts) == 3 and parts[0][0] in "-.0123456789":
                rows.append([float(v) for v in parts])
    table = np.asarray(rows, dtype=np.float32)
    if not size or len(table) != size**3:
        raise ValueError(f"无效的 .cube 文件: {path}")
    return size, table


def apply_lut(rgb: np.ndarray, table: np.ndarray, size: int) -> np.ndarray:
    """三线性插值应用 3D LUT (RGB 0~1), 用于预览 / 验证。"""
    lut = table.reshape(size, size, size, 3)  # [b][g][r]
    x = np.clip(rgb, 0, 1) * (size - 1)
    i0 = np.floor(x).astype(np.int32)
    i0 = np.minimum(i0, size - 2) if size > 1 else i0
    f = x - i0
    r0, g0, b0 = i0[..., 0], i0[..., 1], i0[..., 2]
    fr, fg, fb = f[..., 0:1], f[..., 1:2], f[..., 2:3]
    out = np.zeros_like(rgb, dtype=np.float32)
    for db in (0, 1):
        wb = fb if db else 1 - fb
        for dg in (0, 1):
            wg = fg if dg else 1 - fg
            for dr in (0, 1):
                wr = fr if dr else 1 - fr
                out += wr * wg * wb * lut[b0 + db, g0 + dg, r0 + dr]
    return np.clip(out, 0, 1)


# ------------------------------------------------------------------ 整体流程
def _shot_label(shot: dict) -> str:
    base = os.path.basename(shot["source"])
    if shot.get("start") is None and shot.get("end") is None:
        return base
    s = "" if shot.get("start") is None else f"{shot['start']:g}"
    e = "" if shot.get("end") is None else f"{shot['end']:g}"
    return f"{base}@{s}-{e}"


def _frames_for(shot: dict, samples: int) -> List[np.ndarray]:
    if "frames" in shot:
        return list(shot["frames"])
    return sample_frames(shot["source"], shot.get("start"), shot.get("end"), samples)


def pick_auto_reference(all_stats: Sequence[dict]) -> int:
    """选统计量最接近全体中位数的镜头 (最「典型」的一段) 作为参考。"""
    keys = ("L_mean", "a_mean", "b_mean", "L_std", "chroma_spread", "L_p05", "L_p95")
    med = {k: float(np.median([s[k] for s in all_stats])) for k in keys}
    return int(np.argmin([stats_distance(s, med) for s in all_stats]))


def match_shots(
    shots: Sequence[dict],
    reference: Optional[dict] = None,
    ref_index: Optional[int] = None,
    samples: int = 5,
    strength: float = 1.0,
    lut_dir: Optional[str] = None,
    lut_size: int = 33,
    min_distance: float = 2.0,
    preview_dir: Optional[str] = None,
) -> dict:
    """
    批量计算调色建议。
    shots: [{"source", "start", "end"}] (或带 "frames": [RGB 数组] 直接给帧)。
    reference: 参考镜头 (同 shots 元素格式); ref_index: 用 shots 中第几个做参考; 两者都不给 = 自动选。
    min_distance: 与参考差异小于它的镜头不调 (返回空参数)。
    lut_dir: 给出时为每个需要调的镜头写一份 .cube (Lab 均值/方差迁移)。
    返回 {"model", "reference", "shots": [...]} (结构见 docs/features/color_match.md)。
    """
    if not shots:
        raise ValueError("没有要匹配的镜头")
    frames = [_frames_for(s, samples) for s in shots]
    stats = [color_stats(f) for f in frames]
    if reference is not None:
        ref_frames = _frames_for(reference, samples)
        ref_stats = color_stats(ref_frames)
        ref_info = {"label": _shot_label(reference), "auto": False}
        ref_index = None
    else:
        if ref_index is None:
            ref_index = pick_auto_reference(stats)
            auto = True
        else:
            auto = False
            if not 0 <= ref_index < len(shots):
                raise ValueError(f"参考镜头序号越界: {ref_index} (共 {len(shots)} 段)")
        ref_frames, ref_stats = frames[ref_index], stats[ref_index]
        ref_info = {"label": _shot_label(shots[ref_index]), "index": ref_index, "auto": auto}
    ref_info["stats"] = {
        k: (round(v, 3) if isinstance(v, float) else v) for k, v in ref_stats.items()
    }

    out_shots = []
    for i, (shot, fr, st) in enumerate(zip(shots, frames, stats)):
        item = {
            "index": i,
            "label": _shot_label(shot),
            "source": os.path.abspath(shot["source"]) if shot.get("source") else None,
            "start": shot.get("start"),
            "end": shot.get("end"),
            "stats": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in st.items()},
            "diff": describe_diff(st, ref_stats),
            "is_reference": i == ref_index,
            "lut": None,
        }
        dist = stats_distance(st, ref_stats)
        if i == ref_index or dist < min_distance:
            item.update(
                adjust={},
                params={},
                distance_before=round(dist, 3),
                distance_after=round(dist, 3),
                warnings=[],
            )
        else:
            item.update(suggest_adjust(fr, ref_stats, strength))
            if lut_dir:
                table = build_lut(st, ref_stats, lut_size, strength)
                name = re.sub(r"[^\w\-]+", "_", f"match_{i:02d}_{item['label']}").strip("_")
                item["lut"] = write_cube(
                    os.path.join(lut_dir, f"{name}.cube"), table, lut_size, title=name
                )
                lut_px = apply_lut(_pixels(fr), table, lut_size)
                item["distance_after_lut"] = round(
                    stats_distance(color_stats([lut_px]), ref_stats), 3
                )
        if preview_dir:
            item["preview"] = write_preview(
                preview_dir, i, ref_frames[0], fr[0], item.get("params")
            )
        out_shots.append(item)
    return {"model": MODEL_NAME, "reference": ref_info, "shots": out_shots}


def write_preview(
    preview_dir: str, index: int, ref: np.ndarray, src: np.ndarray, params: Optional[dict]
) -> str:
    """参考 | 原镜头 | 按建议参数模拟后 三联图 (jpg)。"""
    cv2 = _cv2()
    os.makedirs(preview_dir, exist_ok=True)
    h = 180

    def fit(img):
        k = h / img.shape[0]
        return cv2.resize(img, (max(1, int(img.shape[1] * k)), h))

    after = simulate_adjust(src, params or {})
    row = np.concatenate([fit(ref), fit(src), fit(after)], axis=1)
    path = os.path.join(preview_dir, f"shot_{index:02d}.jpg")
    bgr = cv2.cvtColor((np.clip(row, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
    ok, buf = cv2.imencode(".jpg", bgr)
    if ok:
        buf.tofile(path)
    return os.path.abspath(path)


# ------------------------------------------------------------------ CLI
def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="统一调色: 按参考帧给各镜头算剪映调节参数 / LUT")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("suggest", help="计算每个镜头的调色建议")
    sp.add_argument("--shots", nargs="*", default=[], help="镜头: 路径 或 路径@开始-结束 (秒)")
    sp.add_argument("--analysis", help="analysis.json: 按 scenes 把主视频切成镜头")
    sp.add_argument("--ref", help="参考帧/镜头: 路径 / 路径@开始-结束 / auto (默认 auto)")
    sp.add_argument("--ref-shot", type=int, help="用第 N 个镜头 (从 0 起) 做参考")
    sp.add_argument("--samples", type=int, default=5, help="每个镜头抽几帧 (默认 5)")
    sp.add_argument("--strength", type=float, default=1.0, help="建议强度 0~1 (默认 1)")
    sp.add_argument("--min-distance", type=float, default=2.0, help="差异小于它的镜头不调")
    sp.add_argument("--lut-dir", help="为每个镜头生成 .cube LUT 的目录")
    sp.add_argument("--lut-size", type=int, default=33)
    sp.add_argument("--preview-dir", help="写出 参考|原|模拟后 三联预览图")
    sp.add_argument("-o", "--output", help="结果 JSON 路径")
    sp.add_argument("--json", action="store_true", help="stdout 输出 {ok, code, reason, data}")
    st = sub.add_parser("stats", help="打印一个镜头的色彩统计")
    st.add_argument("shot")
    st.add_argument("--samples", type=int, default=5)
    st.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "stats":
            shot = parse_shot_spec(args.shot)
            res = color_stats(_frames_for(shot, args.samples))
            if args.json:
                emit_result(
                    make_result(True, "ok", "", {"label": _shot_label(shot), "stats": res}), True
                )
            else:
                print(json.dumps(res, ensure_ascii=False, indent=1))
            return 0

        shots = [parse_shot_spec(s) for s in args.shots]
        if args.analysis:
            shots += shots_from_analysis(args.analysis)
        if not shots:
            raise ValueError("需要 --shots 或 --analysis")
        reference = None
        if args.ref and args.ref != "auto":
            reference = parse_shot_spec(args.ref)
        res = match_shots(
            shots,
            reference=reference,
            ref_index=args.ref_shot,
            samples=args.samples,
            strength=args.strength,
            lut_dir=args.lut_dir,
            lut_size=args.lut_size,
            min_distance=args.min_distance,
            preview_dir=args.preview_dir,
        )
    except (ValueError, FileNotFoundError) as e:
        if args.json:
            emit_result(make_result(False, "invalid_input", str(e)), True)
        else:
            print(f"❌ {e}")
        return 2
    except ImportError as e:
        if args.json:
            emit_result(make_result(False, "missing_dependency", f"需要 opencv-python: {e}"), True)
        else:
            print(f"❌ 需要 opencv-python: {e}")
        return 2

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    if args.json:
        emit_result(make_result(True, "ok", "", res), True)
        return 0
    print(
        f"🎨 参考: {res['reference']['label']}"
        + (" (自动选择)" if res["reference"]["auto"] else "")
    )
    for s in res["shots"]:
        tag = "参考" if s["is_reference"] else ("、".join(s["diff"]["notes"]) or "接近")
        adj = " ".join(f"{k}={v:+d}" for k, v in s["adjust"].items()) or "不调"
        print(
            f"  [{s['index']}] {s['label']}: {tag} -> {adj}  "
            f"(差异 {s['distance_before']:.1f} -> {s['distance_after']:.1f})"
        )
        for w in s.get("warnings") or []:
            print(f"      ⚠️ {w}")
        if s.get("lut"):
            print(f"      LUT: {s['lut']}")
    if args.output:
        print(f"✅ {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
