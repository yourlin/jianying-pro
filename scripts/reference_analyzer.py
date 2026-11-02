"""
参考视频结构反推 (T9): 输入一条参考短视频, 反推它的剪辑结构, 生成可以「照着这条剪」的 scene / style 草案。

    python reference_analyzer.py ref.mp4 [-o 输出目录] [--scene auto|vlog|...] [--style auto|tech|...]
                                 [--threshold 0.3] [--fps auto] [--no-beats | --beats beats.json]
                                 [--backend auto|librosa|numpy] [--ocr] [--no-sheet]
                                 [--install] [--name 草案名] [--json]

输出 (默认 <视频目录>/<文件名>.reference/):
  - reference.json: 镜头序列 / 镜头时长分布 / 转场分类 / 切点卡拍程度 / 字幕节奏 / 运动强度 /
                    钩子 (前 3s) 特征 / 节奏标签 / 场景与风格推断
  - scene_<id>.json / style_<id>.json: 与 references/scenes/*.json、references/styles/*.json 同结构的草案
    (以最接近的内置场景 / 风格为底, 用参考视频的实测值覆盖节奏、结构、转场、调色等字段)
  - contact_sheet.jpg: 每个镜头中间帧的缩略图联系表 (给调用方 agent 看图补充判断)
  - hook_strip.jpg: 前 3 秒每 0.5s 一帧

算法 (全部只依赖 ffmpeg + numpy; librosa / OCR 可选, 懒加载):
  - 镜头切换: analyze_footage.detect_scenes (ffmpeg scene) + 自己的帧差尖峰, 用「单帧尖峰」校验去掉快速运动的误检
  - 渐变转场: 闪黑 / 闪白用亮度分位数找黑 / 白帧, 再向两侧扩展到亮度单调段;
              叠化用「中间帧 = 两端帧线性混合」的残差检验
  - 快速推拉: 切点前后 0.3s 内逐帧估计缩放 (多尺度中心缩放 + 抛物线细化), 连续同向大幅缩放即推 / 拉
  - 运镜: 镜头内每 0.2s 估计缩放和平移 (相位相关), 分成 push_in / pull_out / punch_in / pan / tilt / shake / static
  - 卡拍: beat_detect.detect_beats 跑参考视频音轨, 切点到最近拍点的距离 vs 随机切点的期望命中率
  - 字幕: 顶部 / 中部 / 底部三个横带的强水平梯度行密度 (减去全帧中位数), 横带边缘签名突变 = 换字幕
"""

import argparse
import copy
import json
import math
import os
import re
import shutil
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from analyze_footage import detect_scenes, probe  # noqa: E402

SCHEMA_VERSION = 1
FEAT_W = 320  # 特征解码宽度 (字幕检测需要的分辨率)
DOWN = 4  # 小图 = 特征图 / 4 (80 宽), 用于帧差 / 运动 / 转场
HOOK_S = 3.0
BANDS = {"top": (0.04, 0.24), "middle": (0.36, 0.64), "bottom": (0.64, 0.94)}
EDGE_TH = 60.0  # 强水平梯度阈值 (0-255 灰度)
TEXT_TH = 0.05  # 横带文字分数阈值
SIG_SHAPE = (12, 80)
JY_TRANSITION = {
    "hard": None,
    "dissolve": "叠化",
    "dip_black": "闪黑",
    "dip_white": "闪白",
    "push": "推近",
    "pull": "拉远",
}
TRANSITION_CN = {
    "hard": "硬切",
    "dissolve": "叠化",
    "dip_black": "闪黑 / 黑场",
    "dip_white": "闪白",
    "push": "快速推近",
    "pull": "快速拉远",
}
SCENE_IDS = ("talking_head", "vlog", "tutorial", "product", "commentary", "beat_montage", "drama")
STYLE_IDS = ("guofeng", "tech", "fresh", "variety", "business")


class ReferenceAnalyzeError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 解码与逐帧特征
# ---------------------------------------------------------------------------


def _feat_size(w: int, h: int) -> Tuple[int, int]:
    """特征图尺寸: 宽 320, 高按比例取 4 的倍数。"""
    if not w or not h:
        return FEAT_W, 568
    fh = max(DOWN * 8, int(round(FEAT_W * h / w / DOWN)) * DOWN)
    return FEAT_W, fh


def iter_frames(path: str, fps: float, width: int, height: int, start: float = 0.0, count: int = 0):
    """ffmpeg 解码为 rgb24 帧 (H, W, 3) uint8, 按 fps 均匀抽帧。"""
    cmd = ["ffmpeg", "-hide_banner", "-v", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", path, "-an", "-vf", f"fps={fps},scale={width}:{height}:flags=area,format=rgb24"]
    if count:
        cmd += ["-frames:v", str(count)]
    cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    size = width * height * 3
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise ReferenceAnalyzeError(f"无法运行 ffmpeg: {e}")
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(height, width, 3)
    finally:
        proc.stdout.close()
        proc.wait()


def _block_mean(a: np.ndarray, k: int) -> np.ndarray:
    h, w = a.shape[0] // k * k, a.shape[1] // k * k
    a = a[:h, :w]
    return a.reshape(h // k, k, w // k, k, *a.shape[2:]).mean(axis=(1, 3))


def _band_rows(h: int, band: Tuple[float, float]) -> Tuple[int, int]:
    return int(band[0] * h), max(int(band[0] * h) + 2, int(band[1] * h))


def text_band_features(gray: np.ndarray) -> Dict[str, Tuple[float, float, np.ndarray]]:
    """
    每个横带的文字分数: 强水平梯度在若干连续行里的最大平均密度 - 全帧行密度中位数。
    返回 {band: (score, y 中心 (画面比例), 边缘签名 SIG_SHAPE)}。
    """
    h = gray.shape[0]
    strong = np.abs(np.diff(gray, axis=1)) > EDGE_TH
    row = strong.mean(axis=1)
    base = float(np.median(row))
    k = max(3, int(h * 0.03))
    csum = np.concatenate([[0.0], np.cumsum(row)])
    out = {}
    for name, band in BANDS.items():
        r0, r1 = _band_rows(h, band)
        if r1 - r0 <= k:
            win = np.array([row[r0:r1].mean()])
            best = r0
        else:
            win = (csum[r0 + k : r1 + 1] - csum[r0 : r1 - k + 1]) / k
            best = r0 + int(np.argmax(win))
        score = float(win.max()) - base
        sub = strong[r0:r1].astype(np.float32)
        sig = _resize_mean(sub, SIG_SHAPE)
        out[name] = (score, (best + k / 2) / h, sig)
    return out


def _resize_mean(a: np.ndarray, shape: Tuple[int, int]) -> np.ndarray:
    """按块平均缩到 shape (不要求整除)。"""
    ys = np.linspace(0, a.shape[0], shape[0] + 1).astype(int)
    xs = np.linspace(0, a.shape[1], shape[1] + 1).astype(int)
    rows = np.add.reduceat(a, ys[:-1], axis=0) / np.maximum(1, np.diff(ys))[:, None]
    return (np.add.reduceat(rows, xs[:-1], axis=1) / np.maximum(1, np.diff(xs))[None, :]).astype(
        np.float32
    )


def extract_features(path: str, fps: float, width: int, height: int) -> Dict[str, np.ndarray]:
    """流式解码, 每帧只保留小灰度图和统计量 (60s 30fps 约 20 MB)。"""
    small, luma, p10, p90, lstd, sat, rgb, hist = [], [], [], [], [], [], [], []
    bands = {b: ([], [], []) for b in BANDS}
    for fr in iter_frames(path, fps, width, height):
        f = fr.astype(np.float32)
        gray = f[..., 0] * 0.299 + f[..., 1] * 0.587 + f[..., 2] * 0.114
        srgb = _block_mean(f, DOWN)
        sg = _block_mean(gray, DOWN)
        small.append(sg.astype(np.uint8))
        luma.append(sg.mean() / 255.0)
        q = np.percentile(sg, [10, 90])
        p10.append(q[0] / 255.0)
        p90.append(q[1] / 255.0)
        lstd.append(sg.std() / 255.0)
        mx, mn = srgb.max(axis=2), srgb.min(axis=2)
        sat.append(float(np.mean((mx - mn) / np.maximum(mx, 1.0))))
        rgb.append(srgb.reshape(-1, 3).mean(axis=0))
        hh = [np.histogram(srgb[..., c], bins=16, range=(0, 256))[0] for c in range(3)]
        hv = np.concatenate(hh).astype(np.float32)
        hist.append(hv / max(1.0, hv.sum() / 3))
        for name, (sc, y, sig) in text_band_features(gray).items():
            bands[name][0].append(sc)
            bands[name][1].append(y)
            bands[name][2].append(sig)
    if not small:
        raise ReferenceAnalyzeError(f"解码不到视频帧: {path}")
    return {
        "small": np.stack(small),
        "luma": np.array(luma),
        "p10": np.array(p10),
        "p90": np.array(p90),
        "std": np.array(lstd),
        "sat": np.array(sat),
        "rgb": np.stack(rgb),
        "hist": np.stack(hist),
        "bands": {
            b: {"score": np.array(v[0]), "y": np.array(v[1]), "sig": np.stack(v[2])}
            for b, v in bands.items()
        },
    }


# ---------------------------------------------------------------------------
# 运动估计: 中心缩放 / 平移
# ---------------------------------------------------------------------------

_SCALE_GRID = np.exp(np.arange(-0.12, 0.1201, 0.015))
_WARP_CACHE: Dict[Tuple[int, int], List[Tuple[np.ndarray, ...]]] = {}


def _warp_tables(h: int, w: int):
    key = (h, w)
    if key not in _WARP_CACHE:
        cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        tabs = []
        for s in _SCALE_GRID:
            sy = np.clip(cy + (yy - cy) / s, 0, h - 1.001)
            sx = np.clip(cx + (xx - cx) / s, 0, w - 1.001)
            y0, x0 = sy.astype(int), sx.astype(int)
            tabs.append((y0, x0, sy - y0, sx - x0))
        _WARP_CACHE[key] = tabs
    return _WARP_CACHE[key]


def estimate_scale(a: np.ndarray, b: np.ndarray) -> Tuple[float, float]:
    """
    估计 b 相对 a 的中心缩放 (b ≈ 把 a 放大 s 倍)。返回 (log s, 置信度 0-1)。
    置信度 = 最优缩放相对不缩放的误差下降比例; 画面平坦 / 纯平移时接近 0。
    """
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    h, w = a.shape
    my, mx = int(h * 0.15), int(w * 0.15)
    bc = b[my : h - my, mx : w - mx]
    errs = []
    for y0, x0, fy, fx in _warp_tables(h, w):
        top = a[y0, x0] * (1 - fx) + a[y0, x0 + 1] * fx
        bot = a[y0 + 1, x0] * (1 - fx) + a[y0 + 1, x0 + 1] * fx
        wa = top * (1 - fy) + bot * fy
        errs.append(float(np.mean(np.abs(wa[my : h - my, mx : w - mx] - bc))))
    errs = np.array(errs)
    i = int(np.argmin(errs))
    e1 = errs[len(errs) // 2]
    logs = np.log(_SCALE_GRID)
    ls = logs[i]
    if 0 < i < len(errs) - 1:
        d = errs[i - 1] - 2 * errs[i] + errs[i + 1]
        if d > 1e-9:
            ls += 0.5 * (errs[i - 1] - errs[i + 1]) / d * (logs[1] - logs[0])
    conf = (e1 - errs[i]) / e1 if e1 > 0.5 else 0.0
    return float(ls), float(max(0.0, conf))


def estimate_shift(a: np.ndarray, b: np.ndarray) -> Tuple[float, float, float]:
    """相位相关估计平移: 返回 (dx, dy, 峰值置信度), b 的内容相对 a 向右 / 下移动为正 (像素)。"""
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    h, w = a.shape
    win = np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)
    fa = np.fft.fft2((a - a.mean()) * win)
    fb = np.fft.fft2((b - b.mean()) * win)
    r = fb * np.conj(fa)
    r /= np.maximum(np.abs(r), 1e-6)
    c = np.real(np.fft.ifft2(r))
    py, px = np.unravel_index(int(np.argmax(c)), c.shape)
    peak = float(c[py, px])

    def sub(arr, i, n):
        lft, m, rr = arr[(i - 1) % n], arr[i], arr[(i + 1) % n]
        d = lft - 2 * m + rr
        return 0.5 * (lft - rr) / d if abs(d) > 1e-9 else 0.0

    dy = py + sub(c[:, px], py, h)
    dx = px + sub(c[py, :], px, w)
    if dy > h / 2:
        dy -= h
    if dx > w / 2:
        dx -= w
    return float(dx), float(dy), peak


# ---------------------------------------------------------------------------
# 镜头边界: 硬切 / 闪黑 / 闪白 / 叠化 / 快速推拉
# ---------------------------------------------------------------------------


def frame_diffs(feat: Dict) -> Tuple[np.ndarray, np.ndarray]:
    """相邻帧差: (像素平均差 0-1, 直方图差 0-1), 第 0 帧为 0。"""
    s = feat["small"].astype(np.float32)
    dp = np.zeros(len(s))
    dp[1:] = np.mean(np.abs(s[1:] - s[:-1]), axis=(1, 2)) / 255.0
    h = feat["hist"]
    dh = np.zeros(len(h))
    dh[1:] = 0.5 * np.sum(np.abs(h[1:] - h[:-1]), axis=1) / 3.0
    return dp, dh


def _spike_ratio(x: np.ndarray, i: int, r: int = 3) -> float:
    nb = np.concatenate([x[max(1, i - r) : i], x[i + 1 : i + 1 + r]])
    return float(x[i] / max(1e-4, np.median(nb) if len(nb) else 0.0))


def hard_cuts(
    feat: Dict, dp: np.ndarray, dh: np.ndarray, ff_cuts: Sequence[float], fps: float
) -> List[int]:
    """
    硬切帧号 (新镜头第一帧)。候选 = ffmpeg scene 切点 ∪ 帧差尖峰;
    校验: 该帧帧差是前后 3 帧中位数的 2.5 倍以上 (快速运动 / 渐变的帧差是连续的, 不是尖峰)。
    """
    n = len(dp)
    cand = set()
    for t in ff_cuts:
        i0 = int(round(t * fps))
        lo, hi = max(1, i0 - 2), min(n, i0 + 3)
        if lo < hi:
            cand.add(lo + int(np.argmax(dh[lo:hi] + dp[lo:hi])))
    for i in range(1, n):
        if dh[i] > 0.3 or dp[i] > 0.08:
            cand.add(i)
    out = []
    for i in sorted(cand):
        big = dh[i] >= 0.15 or dp[i] >= 0.06
        if big and (_spike_ratio(dp, i) >= 2.5 or _spike_ratio(dh, i) >= 2.5):
            if out and i - out[-1] <= 2:
                if dp[i] + dh[i] > dp[out[-1]] + dh[out[-1]]:
                    out[-1] = i
                continue
            out.append(i)
    return out


EDGE_MAX_HOLD = {"dip_black": 5.0, "dip_white": 1.5}  # 片头 / 片尾黑白场最长停留 (秒)
MIN_ALIGN_CUTS = 6  # 少于这么多切点不判断卡点


def dips(feat: Dict, fps: float) -> List[Dict]:
    """
    黑 / 白帧段: p90 亮度 < 0.1 为黑, p10 > 0.9 为白 (用分位数, 叠在上面的字幕不影响)。
    向两侧扩展到亮度单调变化的帧, 得到整个淡出 + 淡入窗口。
    """
    luma = feat["luma"]
    n = len(luma)
    black = feat["p90"] < 0.1
    white = feat["p10"] > 0.9
    out = []
    for kind, mask in (("dip_black", black), ("dip_white", white)):
        i = 0
        while i < n:
            if not mask[i]:
                i += 1
                continue
            j = i
            while j + 1 < n and mask[j + 1]:
                j += 1
            run_len = (j - i + 1) / fps
            limit = 1.5 if kind == "dip_black" else 0.6
            at_edge = i == 0 or j == n - 1
            # 片头 / 片尾的黑白场可以停得久一点, 但不能无限长: 白底课件 / 网页录屏整段 p10 > 0.9,
            # 以前会被当成几十秒的「片尾淡出」, 并吞掉窗口里的所有切点
            if run_len <= limit or (at_edge and run_len <= EDGE_MAX_HOLD[kind]):
                sgn = 1 if kind == "dip_black" else -1  # 黑: 向外亮度上升; 白: 向外下降
                s, e = i, j
                maxext = int(round(fps * 1.0))
                while s - 1 >= 0 and i - s < maxext and sgn * (luma[s - 1] - luma[s]) > 0.004:
                    s -= 1
                while e + 1 < n and e - j < maxext and sgn * (luma[e + 1] - luma[e]) > 0.004:
                    e += 1
                pos = "opening" if i == 0 else ("ending" if j == n - 1 else "between")
                out.append(
                    {
                        "style": kind,
                        "start": s,
                        "end": e,
                        "center": (i + j) / 2.0,
                        "hold": round(run_len, 3),
                        "pos": pos,
                    }
                )
            i = j + 1
    return sorted(out, key=lambda d: d["start"])


def dissolves(
    feat: Dict, dp: np.ndarray, fps: float, blocked: np.ndarray, max_resid: float = 0.2
) -> List[Dict]:
    """
    叠化: 窗口 [i-k, i+k] 两端差别大, 中间帧 ≈ (1-α)·首 + α·尾 (线性混合残差小, α 单调平滑)。
    blocked: 已被硬切 / 黑白场占用的帧, 窗口不能跨过它们。
    """
    sig = feat["small"][:, ::2, ::2].astype(np.float32) / 255.0
    n = len(sig)
    ks = sorted({max(2, int(round(fps * h))) for h in (0.1, 0.17, 0.25, 0.37, 0.5)})
    bl_cum = np.concatenate([[0], np.cumsum(blocked.astype(int))])
    cands = []
    for k in ks:
        if n <= 2 * k + 1:
            continue
        D = np.mean(np.abs(sig[2 * k :] - sig[: -2 * k]), axis=(1, 2))  # D[j] = 窗口中心 j+k
        for j in range(len(D)):
            i = j + k
            if D[j] < 0.06:
                continue
            lo, hi = max(0, j - k), min(len(D), j + k + 1)
            if D[j] < D[lo:hi].max():
                continue
            if bl_cum[i + k + 1] - bl_cum[i - k] > 0:
                continue
            if dp[i - k + 1 : i + k + 1].max() > 0.6 * D[j] + 0.01:
                continue
            hd = 0.5 * float(np.sum(np.abs(feat["hist"][i + k] - feat["hist"][i - k]))) / 3.0
            if hd < 0.15:  # 两端颜色分布差不多: 多半是同一镜头里的运动, 不是换镜头
                continue
            sx, sy, peak = estimate_shift(feat["small"][i - k], feat["small"][i + k])
            if peak > 0.3 and math.hypot(sx, sy) >= 0.5:  # 两端只是平移关系
                continue
            a, b = sig[i - k], sig[i + k]
            ab = b - a
            den = float(np.sum(ab * ab))
            if den <= 0:
                continue
            alphas, res = [], []
            for m in range(i - k + 1, i + k):
                f = sig[m] - a
                al = float(np.sum(f * ab) / den)
                alphas.append(al)
                res.append(float(np.mean(np.abs(f - al * ab))))
            alphas = np.array(alphas)
            resid = float(np.mean(res)) / float(np.mean(np.abs(ab)))
            steps = np.diff(np.concatenate([[0.0], alphas, [1.0]]))
            if resid > max_resid or steps.min() < -0.08 or steps.max() > 0.6:
                continue
            if alphas[0] > 0.45 or alphas[-1] < 0.55:
                continue
            idx = np.arange(i - k + 1, i + k)
            center = float(np.interp(0.5, alphas, idx)) if np.all(np.diff(alphas) > 0) else i
            cands.append(
                {
                    "style": "dissolve",
                    "start": i - k,
                    "end": i + k,
                    "center": center,
                    "resid": resid,
                }
            )
    cands.sort(key=lambda c: (c["resid"], -(c["end"] - c["start"])))
    out: List[Dict] = []
    for c in cands:
        if all(c["end"] < o["start"] or c["start"] > o["end"] for o in out):
            out.append(c)
    return sorted(out, key=lambda c: c["start"])


def zoom_around(small: np.ndarray, cut: int, fps: float, cuts: Sequence[int]) -> Optional[Dict]:
    """切点前后 0.3s 内逐帧缩放; 一侧连续同向且速度 ≥ 35%/s 判为快速推 / 拉转场。"""
    ws = max(3, int(round(0.3 * fps)))
    th = 0.35 / fps
    other = [c for c in cuts if c != cut]
    sides = []
    for lo, hi in ((cut - ws, cut - 1), (cut, cut + ws)):
        lo = max(0, lo)
        hi = min(len(small) - 1, hi)
        if any(lo < c <= hi for c in other):
            continue
        vals = []
        for j in range(lo, hi):
            ls, conf = estimate_scale(small[j], small[j + 1])
            if conf > 0.15:
                vals.append(ls)
        if len(vals) >= 2:
            sides.append(np.array(vals))
    best = None
    for v in sides:
        med = float(np.median(v))
        same = float(np.mean(np.sign(v) == np.sign(med)))
        if abs(med) >= th and same >= 0.7 and (best is None or abs(med) > abs(best)):
            best = med
    if best is None:
        return None
    return {"style": "push" if best > 0 else "pull", "rate_per_s": round(best * fps, 3)}


def detect_boundaries(
    feat: Dict, fps: float, ff_cuts: Sequence[float]
) -> Tuple[List[Dict], List[Dict], np.ndarray]:
    """返回 (镜头之间的转场列表, 片头 / 片尾黑白场, 帧差 dp)。"""
    n = len(feat["luma"])
    dp, dh = frame_diffs(feat)
    cuts = hard_cuts(feat, dp, dh, ff_cuts, fps)
    dps = dips(feat, fps)
    edges = [d for d in dps if d["pos"] != "between"]
    mids = [d for d in dps if d["pos"] == "between"]
    # 落在黑白场窗口里的硬切由黑白场代表 (淡入淡出的首尾帧常被当成硬切)
    cuts = [c for c in cuts if not any(d["start"] - 2 <= c <= d["end"] + 2 for d in dps)]
    blocked = np.zeros(n, bool)
    for c in cuts:
        blocked[max(0, c - 1) : c + 1] = True
    for d in dps:
        blocked[d["start"] : d["end"] + 1] = True
    dis = dissolves(feat, dp, fps, blocked)
    trans = []
    for c in cuts:
        z = zoom_around(feat["small"], c, fps, cuts)
        item = {"style": "hard", "start": c - 1, "end": c, "center": c - 0.5}
        if z:
            ws = int(round(0.3 * fps))
            item = {**item, **z, "start": max(0, c - ws), "end": min(n - 1, c + ws)}
        trans.append(item)
    trans += mids + dis
    out = []
    for t in sorted(trans, key=lambda x: x["center"]):
        typ = {"hard": "cut", "push": "zoom", "pull": "zoom"}.get(t["style"], "fade")
        dur = 0.0 if t["style"] == "hard" else (t["end"] - t["start"] + 1) / fps
        rec = {
            "t": round((t["center"] + 0.5) / fps, 3),
            "type": typ,
            "style": t["style"],
            "label": TRANSITION_CN[t["style"]],
            "jy_transition": JY_TRANSITION[t["style"]],
            "duration": round(dur, 3),
            "window": [round(t["start"] / fps, 3), round((t["end"] + 1) / fps, 3)],
        }
        if "rate_per_s" in t:
            rec["zoom_rate_per_s"] = t["rate_per_s"]
        out.append(rec)
    edge_out = [
        {
            "pos": d["pos"],
            "style": d["style"],
            "window": [round(d["start"] / fps, 3), round((d["end"] + 1) / fps, 3)],
        }
        for d in edges
    ]
    return out, edge_out, dp


# ---------------------------------------------------------------------------
# 镜头、运镜、运动强度
# ---------------------------------------------------------------------------


def _hex(rgb) -> str:
    r, g, b = (int(max(0, min(255, round(float(x))))) for x in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def camera_move(
    small: np.ndarray, f0: int, f1: int, fps: float, mask_rows: Optional[np.ndarray] = None
) -> Dict:
    """
    镜头内运镜: 每 0.2s 一对帧估计缩放 + 平移, 汇总成速度并分类。
    mask_rows: 有静止叠加文字 (字幕 / 标题) 的小图行; 这些行填成均值, 否则相位相关会锁在不动的字上。
    """
    st = max(1, int(round(fps / 5)))
    h, w = small.shape[1:]
    zs, xs, ys, span = [], [], [], 0
    for j in range(f0, f1 - st + 1, st):
        a, b = small[j].astype(np.float32), small[j + st].astype(np.float32)
        if mask_rows is not None and mask_rows.any() and not mask_rows.all():
            a, b = a.copy(), b.copy()
            a[mask_rows] = a[~mask_rows].mean()
            b[mask_rows] = b[~mask_rows].mean()
        if a.std() < 2.0 and b.std() < 2.0:  # 纯色画面, 估不了运动
            continue
        dx, dy, peak = estimate_shift(a, b)
        # 先抵消平移再估缩放, 否则平移会被误判成推拉
        bb = np.roll(b, (-int(round(dy)), -int(round(dx))), axis=(0, 1)) if peak > 0.12 else b
        ls, conf = estimate_scale(a, bb)
        # 峰值低 = 画面内容变了 (换字幕 / 遮挡), 不是整体平移
        ok = peak > 0.12 and abs(dx) < 0.25 * w and abs(dy) < 0.25 * h
        zs.append(ls if conf > 0.15 else 0.0)
        xs.append(dx / w if ok else 0.0)
        ys.append(dy / h if ok else 0.0)
        span += st
    if not zs or span <= 0:
        return {"move": "static", "zoom_per_s": 0.0, "pan_per_s": 0.0, "tilt_per_s": 0.0}
    # 用逐对速度的中位数: 个别帧对 (换字幕、闪光) 的离群值不影响整体判断
    per = fps / st
    zr, xr, yr = (float(np.median(v)) * per for v in (zs, xs, ys))
    mad = [float(np.median(np.abs(np.array(v) - np.median(v)))) for v in (xs, ys)]
    jitter = (mad[0] + mad[1]) * per if len(xs) >= 3 else 0.0
    move = "static"
    if abs(zr) >= 0.03 and abs(zr) >= max(abs(xr), abs(yr)):
        if zr > 0:
            move = "punch_in" if zr >= 0.4 else "push_in"
        else:
            move = "pull_out"
    elif max(abs(xr), abs(yr)) >= 0.04:
        if abs(xr) >= abs(yr):
            # 画面内容向左移 = 左摇 (同 storyboard_builder 的定义)
            move = "pan_left" if xr < 0 else "pan_right"
        else:
            move = "tilt_up" if yr < 0 else "tilt_down"
    elif jitter >= 0.06:  # 每秒抖动 ≥ 6% 画宽 (手持 / 震动)
        move = "shake"
    return {
        "move": move,
        "zoom_per_s": round(zr, 3),
        "pan_per_s": round(xr, 3),
        "tilt_per_s": round(yr, 3),
        "jitter": round(jitter, 3),
    }


def build_shots(
    feat: Dict, trans: List[Dict], dp: np.ndarray, fps: float, dur: float
) -> List[Dict]:
    n = len(feat["luma"])
    bounds = [0.0] + [t["t"] for t in trans] + [dur]
    busy = np.zeros(n, bool)
    for t in trans:
        busy[int(t["window"][0] * fps) : int(math.ceil(t["window"][1] * fps)) + 1] = True
    shots = []
    for i in range(len(bounds) - 1):
        s, e = bounds[i], bounds[i + 1]
        f0, f1 = int(math.ceil(s * fps)), min(n - 1, max(int(math.ceil(s * fps)), int(e * fps) - 1))
        frames = [j for j in range(f0, f1 + 1) if not busy[j]] or list(range(f0, f1 + 1))
        frames = [j for j in frames if 0 <= j < n] or [min(n - 1, f0)]
        inner = [j for j in frames[1:] if j - 1 in frames]
        motion = float(np.mean(dp[inner])) * 100 if inner else 0.0
        clean = [j for j in range(f0, f1 + 1) if 0 <= j < n and not busy[j]]
        mask = np.zeros(feat["small"].shape[1], bool)
        sh = feat["small"].shape[1]
        for bname, (b0, b1) in BANDS.items():
            if clean and np.median(feat["bands"][bname]["score"][clean]) > TEXT_TH:
                mask[max(0, int(b0 * sh) - 1) : int(b1 * sh) + 1] = True
        cam = (
            camera_move(feat["small"], clean[0], clean[-1], fps, mask)
            if len(clean) >= 3
            else {"move": "static"}
        )
        mid = frames[len(frames) // 2]
        shots.append(
            {
                "index": i,
                "start": round(s, 3),
                "end": round(e, 3),
                "duration": round(e - s, 3),
                "thumb_t": round(min(dur - 0.02, max(0.0, mid / fps)), 3),
                "transition_in": trans[i - 1]["style"] if i > 0 else None,
                "motion": round(motion, 2),
                "camera": cam,
                "luma": round(float(np.mean(feat["luma"][frames])), 3),
                "saturation": round(float(np.mean(feat["sat"][frames])), 3),
                "color": _hex(np.mean(feat["rgb"][frames], axis=0)),
            }
        )
    return shots


def duration_stats(durs: Sequence[float]) -> Dict:
    d = np.asarray(durs, float)
    if not len(d):
        return {}
    q = np.percentile(d, [10, 25, 50, 75, 90])
    edges = [0, 0.5, 1, 2, 3, 5, 8, 1e9]
    names = ["<0.5s", "0.5-1s", "1-2s", "2-3s", "3-5s", "5-8s", ">=8s"]
    hist = {nm: int(np.sum((d >= lo) & (d < hi))) for nm, lo, hi in zip(names, edges, edges[1:])}
    return {
        "count": int(len(d)),
        "mean": round(float(d.mean()), 3),
        "median": round(float(q[2]), 3),
        "p10": round(float(q[0]), 3),
        "p25": round(float(q[1]), 3),
        "p75": round(float(q[3]), 3),
        "p90": round(float(q[4]), 3),
        "min": round(float(d.min()), 3),
        "max": round(float(d.max()), 3),
        "cv": round(float(d.std() / d.mean()), 3) if d.mean() > 0 else 0.0,
        "histogram": hist,
    }


def motion_summary(shots: List[Dict], dp: np.ndarray, fps: float) -> Dict:
    w = max(1, int(round(fps * 0.5)))
    curve = [round(float(np.mean(dp[i : i + w])) * 100, 2) for i in range(0, len(dp), w)]
    vals = [s["motion"] for s in shots]
    weights = [s["duration"] for s in shots]
    avg = float(np.average(vals, weights=weights)) if shots and sum(weights) > 0 else 0.0
    moves: Dict[str, int] = {}
    for s in shots:
        mv = s["camera"]["move"]
        moves[mv] = moves.get(mv, 0) + 1
    level = "low" if avg < 1.0 else ("mid" if avg < 3.0 else "high")
    return {
        "avg": round(avg, 2),
        "level": level,
        "curve_step_s": round(w / fps, 3),
        "curve": curve,
        "camera_moves": moves,
    }


# ---------------------------------------------------------------------------
# 切点 vs 音乐节拍
# ---------------------------------------------------------------------------


def _nearest(arr: np.ndarray, t: float) -> float:
    if not len(arr):
        return float("inf")
    i = int(np.searchsorted(arr, t))
    best = float("inf")
    for j in (i - 1, i):
        if 0 <= j < len(arr) and abs(arr[j] - t) < abs(best):
            best = float(t - arr[j])
    return best


def beat_alignment(
    cuts: Sequence[float], beats: Dict, fps: float, shot_durs: Sequence[float]
) -> Dict:
    """切点到最近拍点 / 强拍的距离; 和随机切点的期望命中率比较, 给出卡点程度。"""
    bt = np.asarray(beats.get("beats") or [], float)
    db = np.asarray(beats.get("downbeats") or [], float)
    on = np.asarray(beats.get("onsets") or [], float)
    ibi = float(beats.get("beat_interval") or (np.median(np.diff(bt)) if len(bt) > 1 else 0))
    res = {
        "tempo": beats.get("tempo"),
        "beat_interval": round(ibi, 4) if ibi else None,
        "backend": beats.get("backend"),
        "n_beats": int(len(bt)),
    }
    if not len(cuts) or len(bt) < 4 or ibi <= 0:
        return {**res, "label": "无法判断", "aligned_ratio": None}
    if len(cuts) < MIN_ALIGN_CUTS:
        # 1–2 个切点碰巧落在拍上不说明任何问题 (随机命中率常在 50% 以上), 以前会给出「强卡点」
        return {**res, "label": "无法判断", "aligned_ratio": None, "n_cuts": len(cuts),
                "note": f"切点少于 {MIN_ALIGN_CUTS} 个, 不做卡点判断"}  # fmt: skip
    tol = max(0.07, 2.0 / fps)
    off = np.array([_nearest(bt, c) for c in cuts])
    offd = np.array([_nearest(db, c) for c in cuts]) if len(db) else np.full(len(cuts), np.inf)
    offo = np.array([_nearest(on, c) for c in cuts]) if len(on) else np.full(len(cuts), np.inf)
    hit = np.abs(off) <= tol
    ratio = float(hit.mean())
    chance = min(1.0, 2 * tol / ibi)
    score = (ratio - chance) / (1 - chance) if chance < 1 else 0.0
    if ratio >= 0.7 and score >= 0.5:
        label = "强卡点"
    elif ratio >= 0.4 and score >= 0.25:
        label = "部分卡点"
    else:
        label = "不卡点"
    per = [float(d) / ibi for d in shot_durs if d > 0]
    every = max(1, int(round(float(np.median(per))))) if per else None
    sections = beats.get("energy_sections") or []
    by_level: Dict[str, List[float]] = {}
    for c in cuts:
        sec = next((s for s in sections if s["start"] <= c < s["end"]), None)
        if sec:
            by_level.setdefault(sec["level"], []).append(c)
    rate = {
        lv: round(
            len(v) / max(1e-6, sum(s["end"] - s["start"] for s in sections if s["level"] == lv)), 3
        )
        for lv, v in by_level.items()
    }
    return {
        **res,
        "label": label,
        "tolerance_s": round(tol, 3),
        "aligned_ratio": round(ratio, 3),
        "downbeat_ratio": round(float(np.mean(np.abs(offd) <= tol)), 3),
        "onset_ratio": round(float(np.mean(np.abs(offo) <= tol)), 3),
        "chance_ratio": round(chance, 3),
        "alignment_score": round(score, 3),
        "mean_offset_ms": round(float(np.mean(off[hit])) * 1000, 1) if hit.any() else None,
        "cut_every_beats": every,
        "cuts_per_s_by_energy": rate,
        "climax": beats.get("climax"),
        "offsets_s": [round(float(x), 3) for x in off],
    }


def run_beats(
    path: str, backend: str = "auto", beats_path: Optional[str] = None
) -> Tuple[Optional[Dict], str]:
    """跑 beat_detect (或读已有 beats.json)。返回 (结果 | None, 说明)。"""
    from beat_detect import BeatDetectError, detect_beats, load_beats

    if beats_path:
        return load_beats(beats_path), f"读取 {beats_path}"
    try:
        res = detect_beats(path, backend=backend)
    except (BeatDetectError, OSError, ValueError) as e:
        return None, f"节拍检测失败: {e}"
    secs = res.get("energy_sections") or []
    if len(res.get("beats") or []) < 4 or (
        secs and max(s.get("loudness_db", -99) for s in secs) < -50
    ):
        return None, "音轨太安静或没有稳定节拍, 跳过卡点分析"
    return res, f"beat_detect ({res.get('backend')})"


# ---------------------------------------------------------------------------
# 字幕 / 文字节奏
# ---------------------------------------------------------------------------


def _runs(mask: np.ndarray) -> List[Tuple[int, int]]:
    out, i, n = [], 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j + 1 < n and mask[j + 1]:
                j += 1
            out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def _median_filter(x: np.ndarray, k: int) -> np.ndarray:
    if k <= 1 or len(x) < k:
        return x
    pad = k // 2
    xp = np.pad(x, pad, mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(xp, k), axis=1)


def text_rhythm(feat: Dict, fps: float, cut_times: Sequence[float]) -> Dict:
    """各横带的文字出现占比、出现段、换字时刻和间隔; 推断字幕类型。"""
    bands = {}
    k = max(1, int(round(fps * 0.15))) | 1
    min_seg = max(2, int(round(fps * 0.3)))
    n = len(feat["luma"])
    for name, b in feat["bands"].items():
        score = _median_filter(b["score"], k)
        present = score > TEXT_TH
        segs = [(s, e) for s, e in _runs(present) if e - s + 1 >= min_seg]
        pm = np.zeros(n, bool)
        for s, e in segs:
            pm[s : e + 1] = True
        sig = b["sig"]
        ch = np.zeros(n)
        tot = sig.reshape(n, -1).sum(axis=1)
        ch[1:] = np.abs(sig[1:] - sig[:-1]).reshape(n - 1, -1).sum(axis=1) / np.maximum(
            1e-6, tot[1:] + tot[:-1]
        )
        events = []
        for s, e in segs:
            events.append(s)
            for i in range(s + 1, e + 1):
                if ch[i] > 0.18 and i - events[-1] >= int(fps * 0.25):
                    events.append(i)
        evt = sorted(set(events))
        times = [round(i / fps, 3) for i in evt]
        intervals = [round(b2 - a2, 3) for a2, b2 in zip(times, times[1:]) if b2 - a2 > 0]
        ys = b["y"][pm]
        bands[name] = {
            "present_ratio": round(float(pm.mean()), 3),
            "segments": [[round(s / fps, 3), round((e + 1) / fps, 3)] for s, e in segs],
            "events": [
                {"t": t, "at_cut": any(abs(t - c) <= 1.5 / fps + 0.02 for c in cut_times)}
                for t in times
            ],
            "median_interval": round(float(np.median(intervals)), 3) if intervals else None,
            "per_min": round(len(times) / (n / fps / 60), 2) if n else 0.0,
            "y": round(float(np.median(ys)), 3) if len(ys) else None,
            "transform_y": round((0.5 - float(np.median(ys))) * 2, 3) if len(ys) else None,
        }
    bottom, middle, top = bands["bottom"], bands["middle"], bands["top"]
    if bottom["present_ratio"] >= 0.5:
        kind = "full"  # 全程字幕 (多半是口播 / 旁白)
    elif middle["present_ratio"] >= 0.3 or bottom["present_ratio"] >= 0.2:
        kind = "labels"
    elif max(b["present_ratio"] for b in bands.values()) >= 0.05:
        kind = "titles"
    else:
        kind = "none"
    main = max(("bottom", "middle"), key=lambda k2: bands[k2]["present_ratio"])
    return {
        "kind": kind,
        "main_band": main if bands[main]["present_ratio"] > 0 else None,
        "bands": bands,
        "top_title": top["present_ratio"] >= 0.5,
    }


def _load_ocr() -> Optional[Callable[[np.ndarray], str]]:
    """可选 OCR: rapidocr_onnxruntime 优先, 其次 pytesseract (需要 tesseract 程序)。都没有返回 None。"""
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore

        eng = RapidOCR()

        def _rapid(img: np.ndarray) -> str:
            res, _ = eng(img)
            return "".join(r[1] for r in (res or []))

        return _rapid
    except Exception:  # noqa: BLE001 (可选依赖, 任何导入错误都降级)
        pass
    try:
        import pytesseract  # type: ignore

        if shutil.which("tesseract"):
            return lambda img: pytesseract.image_to_string(img, lang="chi_sim+eng").strip()
    except Exception:  # noqa: BLE001
        pass
    return None


def grab_frame(path: str, t: float, width: int, height: int) -> Optional[np.ndarray]:
    frames = list(iter_frames(path, 1000, width, height, start=max(0.0, t), count=1))
    return frames[0] if frames else None


def ocr_events(
    path: str, text: Dict, vw: int, vh: int, max_events: int = 40
) -> Tuple[List[Dict], str]:
    ocr = _load_ocr()
    if ocr is None:
        return (
            [],
            "没有可用的 OCR (pip install rapidocr_onnxruntime 或安装 tesseract + pytesseract)",
        )
    w = min(720, vw or 720)
    h = int(round(w * (vh or 1280) / (vw or 720) / 2)) * 2
    out = []
    for band, b in text["bands"].items():
        for ev in b["events"][:max_events]:
            fr = grab_frame(path, ev["t"] + 0.1, w, h)
            if fr is None:
                continue
            r0, r1 = _band_rows(h, BANDS[band])
            try:
                txt = ocr(np.ascontiguousarray(fr[r0:r1]))
            except Exception as e:  # noqa: BLE001
                return out, f"OCR 失败: {e}"
            out.append({"band": band, "t": ev["t"], "text": txt})
    return out, "ok"


# ---------------------------------------------------------------------------
# 钩子 / 节奏 / 场景与风格推断
# ---------------------------------------------------------------------------


def hook_features(
    shots: List[Dict],
    trans: List[Dict],
    edges: List[Dict],
    text: Dict,
    feat: Dict,
    dp: np.ndarray,
    fps: float,
    dur: float,
    beats: Optional[Dict],
) -> Dict:
    end = min(HOOK_S, dur)
    nf = max(1, int(end * fps))
    cuts = [t["t"] for t in trans if t["t"] < end]
    motion_hook = float(np.mean(dp[1:nf])) * 100 if nf > 1 else 0.0
    motion_all = float(np.mean(dp[1:])) * 100 if len(dp) > 1 else 0.0
    text_hook = {}
    for name, b in text["bands"].items():
        cov = sum(max(0.0, min(e, end) - s) for s, e in b["segments"])
        text_hook[name] = round(cov / end, 3) if end else 0.0
    first = shots[0] if shots else {}
    opening = next((e for e in edges if e["pos"] == "opening"), None)
    first_onset = None
    if beats and beats.get("onsets"):
        first_onset = float(beats["onsets"][0])
    luma_h = float(np.mean(feat["luma"][:nf]))
    sat_h = float(np.mean(feat["sat"][:nf]))
    types, issues = [], []
    if len(cuts) >= 3:
        types.append("快切蒙太奇")
    if text_hook.get("middle", 0) >= 0.4 or text_hook.get("top", 0) >= 0.6:
        types.append("大字标题")
    if text_hook.get("bottom", 0) >= 0.5:
        types.append("字幕口播直入")
    if motion_all > 0 and motion_hook >= 1.5 * motion_all and motion_hook >= 1.0:
        types.append("强动作开场")
    if any(t["style"] in ("dip_white", "push", "pull") for t in trans if t["t"] < end):
        types.append("转场冲击")
    if not types:
        types.append("单镜头平铺")
    if opening and opening["window"][1] > 0.3:
        issues.append(
            f"开头 {opening['window'][1]:.1f}s 黑 / 白场淡入 (黄金 3 秒规则: 0.3s 内要出画面)"
        )
    if first and first.get("duration", 0) > 3.0 and len(cuts) == 0 and not text_hook.get("middle"):
        issues.append("前 3 秒没有切点也没有标题字, 钩子偏弱")
    return {
        "window": [0.0, round(end, 3)],
        "cuts": len(cuts),
        "cut_times": cuts,
        "first_shot_duration": first.get("duration"),
        "motion": round(motion_hook, 2),
        "motion_vs_overall": round(motion_hook / motion_all, 2) if motion_all > 0 else None,
        "text_coverage": text_hook,
        "luma": round(luma_h, 3),
        "luma_vs_overall": round(luma_h / max(1e-6, float(np.mean(feat["luma"]))), 2),
        "saturation": round(sat_h, 3),
        "opening_fade": opening,
        "first_sound_s": round(first_onset, 3) if first_onset is not None else None,
        "transitions": [t["style"] for t in trans if t["t"] < end],
        "types": types,
        "issues": issues,
    }


def rhythm_label(stats: Dict, align: Optional[Dict], shots: List[Dict], dur: float) -> Dict:
    med = stats.get("median") or dur
    if med < 0.8:
        pace = "快闪"
    elif med < 1.8:
        pace = "快节奏"
    elif med < 3.5:
        pace = "中等"
    elif med < 7:
        pace = "舒缓"
    else:
        pace = "长镜头"
    tags = [pace]
    beat_cut = bool(align and align.get("label") == "强卡点")
    if beat_cut:
        tags.insert(0, "卡点")
    half = dur / 2
    a = sum(1 for s in shots if s["start"] < half)
    b = len(shots) - a
    trend = "均匀"
    if a and b:
        if b >= 1.5 * a:
            trend = "越剪越快"
        elif a >= 1.5 * b:
            trend = "前快后慢"
    if trend != "均匀":
        tags.append(trend)
    if stats.get("cv", 0) >= 0.8:
        tags.append("长短错落")
    spm = len(shots) / (dur / 60) if dur else 0
    return {
        "pace": pace,
        "label": "".join(tags[:2]),
        "tags": tags,
        "trend": trend,
        "shots_per_min": round(spm, 1),
    }


def guess_scene(stats: Dict, align: Optional[Dict], text: Dict, motion: Dict) -> str:
    med = stats.get("median") or 99
    sub = text["bands"]["bottom"]["present_ratio"]
    if align and align.get("label") == "强卡点" and med < 1.5:
        return "beat_montage"
    if align and align.get("label") in ("强卡点", "部分卡点") and med < 1.0 and sub < 0.3:
        return "beat_montage"
    if sub >= 0.5:
        if med >= 5 and motion["level"] == "low":
            return "tutorial"
        if med >= 3:
            return "talking_head"
        return "product" if med < 2 else "commentary"
    if med < 1.0:
        return "beat_montage"
    return "vlog"


def color_profile(feat: Dict) -> Dict:
    rgb = feat["rgb"].mean(axis=0)
    luma = float(feat["luma"].mean())
    sat = float(feat["sat"].mean())
    contrast = float(feat["std"].mean())
    warm = float((rgb[0] - rgb[2]) / 255.0)
    # 调色板: 每帧均值色按亮度 / 色相粗量化后计数
    q = (feat["rgb"] // 48).astype(int)
    keys, counts = np.unique(q, axis=0, return_counts=True)
    order = np.argsort(-counts)[:5]
    palette = []
    for i in order:
        m = np.all(q == keys[i], axis=1)
        palette.append(
            {
                "color": _hex(feat["rgb"][m].mean(axis=0)),
                "share": round(float(counts[i] / len(q)), 3),
            }
        )
    return {
        "mean_rgb": _hex(rgb),
        "luma": round(luma, 3),
        "saturation": round(sat, 3),
        "contrast": round(contrast, 3),
        "warmth": round(warm, 3),
        "palette": palette,
    }


def guess_style(color: Dict, trans_counts: Dict[str, int]) -> str:
    sat, luma, warm = color["saturation"], color["luma"], color["warmth"]
    if sat >= 0.45 or trans_counts.get("dip_white", 0) >= 2:
        return "variety"
    if luma < 0.35 and warm < 0:
        return "tech"
    if sat < 0.18:
        return "business"
    if warm > 0.08 and sat < 0.4 and luma < 0.6:
        return "guofeng"
    return "fresh"


# ---------------------------------------------------------------------------
# scene / style 草案
# ---------------------------------------------------------------------------


def load_base(kind: str, key: str, refs_root: Optional[str] = None) -> Dict:
    path = os.path.join(refs_root or os.path.join(SKILL, "references"), kind, f"{key}.json")
    if not os.path.exists(path):
        raise ReferenceAnalyzeError(f"没有这个{'场景' if kind == 'scenes' else '风格'}: {key}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def slugify(name: str) -> str:
    s = re.sub(r"[^0-9A-Za-z_]+", "_", name).strip("_").lower()
    return s[:40] or "ref"


def _snap_to_cut(t: float, cuts: Sequence[float], radius: float = 0.6) -> float:
    near = [c for c in cuts if abs(c - t) <= radius]
    return min(near, key=lambda c: abs(c - t)) if near else t


def _split_pace(shots: List[Dict], max_parts: int = 3, min_shots: int = 3) -> List[List[Dict]]:
    """按镜头时长 (log) 做最多 max_parts 段的二分切分: SSE 至少下降 35% 且两段均值差 1.5 倍才切。"""

    def sse(xs):
        return float(np.sum((xs - xs.mean()) ** 2)) if len(xs) else 0.0

    parts = [shots]
    while len(parts) < max_parts:
        best = None
        for pi, p in enumerate(parts):
            x = np.log([max(0.05, s["duration"]) for s in p])
            tot = sse(x)
            if len(p) < 2 * min_shots or tot <= 1e-6:
                continue
            for k in range(min_shots, len(p) - min_shots + 1):
                red = 1 - (sse(x[:k]) + sse(x[k:])) / tot
                ratio = math.exp(abs(x[:k].mean() - x[k:].mean()))
                if red >= 0.35 and ratio >= 1.5 and (best is None or red > best[0]):
                    best = (red, pi, k)
        if best is None:
            break
        _, pi, k = best
        p = parts.pop(pi)
        parts[pi:pi] = [p[:k], p[k:]]
    return parts


def _dominant_move(shots: List[Dict]) -> Optional[str]:
    cnt: Dict[str, float] = {}
    for s in shots:
        mv = s["camera"]["move"]
        if mv != "static":
            cnt[mv] = cnt.get(mv, 0) + s["duration"]
    return max(cnt, key=cnt.get) if cnt else None


def structure(shots, trans, text, hook, align, dur) -> List[Dict]:
    """结构分段: 开场钩子 / (主体 | 快切段 | 铺陈段 | 高潮) / 收尾, 时间对齐到切点。"""
    cuts = [t["t"] for t in trans]
    h_end = _snap_to_cut(min(HOOK_S, dur * 0.3), cuts, 0.5)
    last = shots[-1] if shots else None
    e_start = last["start"] if last and dur - last["start"] <= 0.25 * dur else dur * 0.88
    e_start = max(e_start, h_end)
    spans: List[Tuple[str, float, float]] = [("开场钩子", 0.0, h_end)]
    mid = [s for s in shots if s["start"] >= h_end - 1e-3 and s["end"] <= e_start + 1e-3]
    climax = (align or {}).get("climax")
    if climax and e_start - h_end > 3:
        cs = max(h_end, _snap_to_cut(climax["start"], cuts))
        ce = min(e_start, _snap_to_cut(climax["end"], cuts))
        if ce - cs >= 1.5:
            if cs - h_end >= 1.0:
                spans.append(("主体", h_end, cs))
            spans.append(("高潮", cs, ce))
            if e_start - ce >= 1.0:
                spans.append(("主体", ce, e_start))
            else:
                e_start = ce
            mid = []
    if mid:
        med_all = float(np.median([s["duration"] for s in shots]))
        parts = _split_pace(mid)
        for p in parts:
            med = float(np.median([s["duration"] for s in p]))
            nm = (
                "主体"
                if len(parts) == 1
                else (
                    "快切段"
                    if med < med_all * 0.8
                    else ("铺陈段" if med > med_all * 1.25 else "主体")
                )
            )
            spans.append((nm, p[0]["start"], p[-1]["end"]))
        spans[1] = (spans[1][0], h_end, spans[1][2])
        spans[-1] = (spans[-1][0], spans[-1][1], e_start)
    elif e_start - h_end > 0.2 and spans[-1][2] < e_start - 1e-3:
        spans.append(("主体", spans[-1][2], e_start))
    if dur - e_start > 0.2:
        spans.append(("收尾", e_start, dur))
    else:
        spans[-1] = (spans[-1][0], spans[-1][1], dur)
    out = []
    for nm, s, e in spans:
        if e - s <= 0.05:
            continue
        sh = [x for x in shots if x["start"] < e - 1e-3 and x["end"] > s + 1e-3]
        tr = [t for t in trans if s <= t["t"] < e and t["style"] != "hard"]
        allowed, required = [], []
        tx = {
            b: sum(max(0.0, min(e, se[1]) - max(s, se[0])) for se in v["segments"]) / (e - s)
            for b, v in text["bands"].items()
        }
        if tx["middle"] >= 0.3 or tx["top"] >= 0.5:
            (required if nm == "开场钩子" else allowed).append("title")
        if tx["middle"] > 0.05 and nm != "开场钩子":
            allowed.append("keyword")
        if any(t["style"] in ("dip_white", "push", "pull") for t in tr):
            allowed.append("effect")
        durs = [x["duration"] for x in sh]
        avg = float(np.median(durs)) if durs else e - s
        note = f"参考 {s:.1f}–{e:.1f}s: {len(sh)} 个镜头, 中位 {avg:.2f}s"
        if align and align.get("beat_interval"):
            note += f" (≈ 每 {max(1, round(avg / align['beat_interval']))} 拍切)"
        if tr:
            note += "; 转场 " + "、".join(sorted({TRANSITION_CN[t["style"]] for t in tr}))
        purpose = {
            "开场钩子": "钩子: " + "、".join(hook["types"]),
            "高潮": "音乐高潮, 镜头最密",
            "收尾": "收束 / CTA",
            "快切段": "加速推进",
            "铺陈段": "放慢交代信息",
        }.get(nm, "展开内容")
        out.append(
            {
                "name": nm,
                "ratio": round((e - s) / dur, 3),
                "purpose": purpose,
                "required": required,
                "allowed": allowed,
                "forbidden": [],
                "camera": _dominant_move(sh),
                "note": note,
                "span": [round(s, 3), round(e, 3)],
                "avg_shot": round(avg, 3),
            }
        )
    tot = sum(b["ratio"] for b in out)
    if out and tot > 0:
        for b in out:
            b["ratio"] = round(b["ratio"] / tot, 3)
        out[-1]["ratio"] = round(1 - sum(b["ratio"] for b in out[:-1]), 3)
    return out


def make_scene_draft(rep: Dict, base: Dict, draft_id: str, name: str) -> Dict:
    d = copy.deepcopy(base)
    st, dur = rep["shot_stats"], rep["video"]["duration"]
    align, text, mot = rep.get("beat") or {}, rep["text"], rep["motion"]
    d["id"] = draft_id
    d["name"] = f"参考·{name}"
    d["derived_from"] = {
        "scene": base["id"],
        "reference": rep["source"],
        "generated_by": "reference_analyzer.py",
    }
    d["duration"] = {"min": round(dur * 0.8, 1), "max": round(dur * 1.2, 1), "ideal": round(dur, 1)}
    p = d.setdefault("pacing", {})
    lo, hi = max(0.2, st["p25"]), max(st["p25"] + 0.1, st["p75"])
    p["avg_shot"] = [round(lo, 2), round(hi, 2)]
    p["median_shot"] = st["median"]
    p["pattern"] = (
        f"参考: {rep['rhythm']['label']}, 中位 {st['median']}s, {rep['rhythm']['shots_per_min']} 镜/分, "
        f"前 3s {rep['hook']['cuts']} 刀"
    )
    if align.get("cut_every_beats") and align.get("label") in ("强卡点", "部分卡点"):
        ceb = dict(p.get("cut_every_beats") or {})
        ceb["参考"] = align["cut_every_beats"]
        p["cut_every_beats"] = ceb
    d["beats"] = rep["structure"]
    cam = d.setdefault("camera", {})
    moves = [m for m, c in mot["camera_moves"].items() if m != "static" for _ in range(c)]
    per_min = len(moves) / (dur / 60) if dur else 0
    if moves:
        cam["allowed"] = sorted(
            set(moves) | ({"punch_in"} if rep["transition_counts"].get("push") else set())
        )
        cam["per_minute"] = [int(per_min * 0.7), max(1, int(math.ceil(per_min * 1.3)))]
    subs = d.setdefault("subtitles", {})
    kind = text["kind"]
    if kind == "full":
        if subs.get("default") is not True:
            subs.update({"max_chars_zh": 15, "max_chars_en": 38, "lead": 0.1})
        subs["default"] = True
        mi = text["bands"]["bottom"]["median_interval"]
        if mi:
            subs["max_dur"] = round(min(4.0, max(1.2, mi * 1.2)), 1)
    elif kind == "labels":
        subs["default"] = "labels"
    else:
        subs["default"] = False
    if text["main_band"] and text["bands"][text["main_band"]]["transform_y"] is not None:
        subs["reference_transform_y"] = text["bands"][text["main_band"]]["transform_y"]
    if align.get("tempo"):
        d.setdefault("audio", {})["reference_tempo"] = align["tempo"]
    tc = rep["transition_counts"]
    d["transitions"] = {
        "counts": tc,
        "dominant": rep["dominant_transition"],
        "hard_cut_ratio": rep["hard_cut_ratio"],
    }
    checks = list(d.get("checks") or [])
    checks.append(f"镜头中位时长 ≈ {st['median']}s (参考 {st['p25']}–{st['p75']}s)")
    checks.append(
        f"前 3 秒 {rep['hook']['cuts']} 个切点, 钩子类型: {'、'.join(rep['hook']['types'])}"
    )
    if align.get("label") == "强卡点":
        checks.append(f"切点卡拍 (参考 {align['aligned_ratio']:.0%} 切点落在拍上, 误差 ≤ 2 帧)")
    if rep["hard_cut_ratio"] >= 0.8:
        checks.append("以硬切为主, 不要随便加转场")
    d["checks"] = checks
    d["reference"] = {
        "duration": dur,
        "shots": st["count"],
        "rhythm": rep["rhythm"],
        "hook": {k: rep["hook"][k] for k in ("cuts", "types", "issues")},
        "beat_label": align.get("label"),
    }
    return d


def make_style_draft(rep: Dict, base: Dict, draft_id: str, name: str) -> Dict:
    d = copy.deepcopy(base)
    col, trans = rep["color"], rep["transitions"]
    d["id"] = draft_id
    d["name"] = f"参考·{name}"
    d["derived_from"] = {
        "style": base["id"],
        "reference": rep["source"],
        "generated_by": "reference_analyzer.py",
    }
    soft = [t for t in trans if t["style"] != "hard"]
    if rep["hard_cut_ratio"] >= 0.7 or not soft:
        d["cut_transition"] = None
    else:
        dom = rep["dominant_transition"]
        ds = [t["duration"] for t in soft if t["style"] == dom and t["duration"] > 0]
        d["cut_transition"] = {
            "name": JY_TRANSITION[dom],
            "duration": round(min(1.0, max(0.2, float(np.median(ds)) if ds else 0.4)), 2),
        }
    others = sorted({t["style"] for t in soft} - {rep["dominant_transition"]})
    if others:
        ds = [t["duration"] for t in soft if t["style"] == others[0]]
        d["chapter_transition"] = {
            "name": JY_TRANSITION[others[0]],
            "duration": round(min(1.0, max(0.2, float(np.median(ds)))), 2),
        }

    def clamp(v, a):
        return int(round(max(-a, min(a, v))))

    grade = d.setdefault("grade", {}) or {}
    grade["adjust"] = {
        "亮度": clamp((col["luma"] - 0.5) * 40, 15),
        "对比度": clamp((col["contrast"] - 0.22) * 80, 15),
        "饱和度": clamp((col["saturation"] - 0.3) * 60, 20),
        "色温": clamp(col["warmth"] * 60, 20),
    }
    d["grade"] = grade
    d["palette"] = col["palette"]
    cam = d.setdefault("camera", {})
    zooms = [
        abs(s["camera"]["zoom_per_s"])
        for s in rep["shots"]
        if s["camera"]["move"] in ("push_in", "pull_out")
    ]
    if zooms:
        cam["amount"] = round(min(0.15, max(0.03, float(np.median(zooms)) * 2)), 3)
    if rep["transition_counts"].get("push") or any(
        s["camera"]["move"] == "punch_in" for s in rep["shots"]
    ):
        cam["punch"] = max(float(cam.get("punch", 0) or 0), 0.2)
    tx = rep["text"]
    if tx["main_band"]:
        b = tx["bands"][tx["main_band"]]
        d["subtitle_position"] = {"band": tx["main_band"], "transform_y": b["transform_y"]}
    d["reference"] = {
        "color": {k: col[k] for k in ("mean_rgb", "luma", "saturation", "contrast", "warmth")}
    }
    return d


# ---------------------------------------------------------------------------
# 缩略图联系表
# ---------------------------------------------------------------------------


def _sheet_font(text: str, default):
    """表头含中文等非 ASCII 字符 (视频文件名) 时换成 CJK 字体, 否则默认字体会画成豆腐块。"""
    if all(ord(ch) < 128 for ch in text):
        return default
    try:
        from layout_preview import CJK_FONTS
        from PIL import ImageFont
    except ImportError:
        return default
    for fp in CJK_FONTS:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, 13)
            except OSError:
                continue
    return default


def contact_sheet(
    path: str,
    items: List[Tuple[float, str]],
    out: str,
    vw: int,
    vh: int,
    header: str = "",
    cols: Optional[int] = None,
) -> Optional[str]:
    """items: [(时间, 标签)]。用 Pillow 拼图 (标签只用 ASCII, 不依赖中文字体)。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    portrait = (vh or 1) > (vw or 1)
    tw = 200 if portrait else 300
    th = int(round(tw * (vh or 9) / (vw or 16) / 2)) * 2
    cols = cols or (6 if portrait else 4)
    cols = max(1, min(cols, len(items)))
    rows = max(1, math.ceil(len(items) / cols))
    lab, head = 36, (28 if header else 0)
    sheet = Image.new("RGB", (cols * (tw + 6) + 6, head + rows * (th + lab + 6) + 6), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=13)
    except TypeError:
        font = ImageFont.load_default()
    if header:
        draw.text((8, 6), header, fill=(240, 240, 240), font=_sheet_font(header, font))
    for k, (t, label) in enumerate(items):
        fr = grab_frame(path, t, tw, th)
        x = 6 + (k % cols) * (tw + 6)
        y = head + 6 + (k // cols) * (th + lab + 6)
        if fr is not None:
            sheet.paste(Image.fromarray(fr), (x, y))
        for li, line in enumerate(label.split("\n")[:2]):
            draw.text((x + 2, y + th + 3 + li * 16), line, fill=(230, 230, 230), font=font)
    sheet.save(out, quality=88)
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def analyze_reference(
    video_path: str,
    out_dir: Optional[str] = None,
    scene: str = "auto",
    style: str = "auto",
    threshold: float = 0.3,
    fps: Optional[float] = None,
    beats: bool = True,
    beats_path: Optional[str] = None,
    backend: str = "auto",
    ocr: bool = False,
    sheet: bool = True,
    name: Optional[str] = None,
    install: bool = False,
    refs_root: Optional[str] = None,
    max_thumbs: int = 48,
) -> Dict:
    """分析参考视频并写出 reference.json + scene / style 草案 + 联系表。返回报告 dict (含 files)。"""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ReferenceAnalyzeError(
            "需要 ffmpeg / ffprobe (brew install ffmpeg 或 winget install ffmpeg)"
        )
    if not os.path.exists(video_path):
        raise ReferenceAnalyzeError(f"找不到视频: {video_path}")
    video = probe(video_path)
    if not video or not video.get("duration"):
        raise ReferenceAnalyzeError(f"读不到视频信息: {video_path}")
    stem = os.path.splitext(os.path.basename(video_path))[0]
    name = name or stem
    out_dir = out_dir or os.path.join(
        os.path.dirname(os.path.abspath(video_path)), f"{stem}.reference"
    )
    os.makedirs(out_dir, exist_ok=True)
    notes: List[str] = []

    src_fps = video.get("fps") or 30.0
    if not fps:
        fps = min(src_fps, 30.0 if video["duration"] <= 90 else 15.0)
    fw, fh = _feat_size(video["width"], video["height"])
    feat = extract_features(video_path, fps, fw, fh)
    n = len(feat["luma"])
    dur = float(video["duration"])
    dur = min(dur, n / fps + 1.0 / fps) if n / fps < dur - 0.5 else dur

    ff = detect_scenes(video_path, threshold)
    trans, edges, dp = detect_boundaries(feat, fps, ff)
    trans = [t for t in trans if 0.05 < t["t"] < dur - 0.05]
    shots = build_shots(feat, trans, dp, fps, dur)
    stats = duration_stats([s["duration"] for s in shots])
    tc: Dict[str, int] = {}
    for t in trans:
        tc[t["style"]] = tc.get(t["style"], 0) + 1
    soft = {k: v for k, v in tc.items() if k != "hard"}
    dominant = max(soft, key=soft.get) if soft else "hard"
    hard_ratio = round(tc.get("hard", 0) / len(trans), 3) if trans else 1.0

    beat_res, beat_note = None, "未分析 (--no-beats)"
    if beats or beats_path:
        if beats_path or video.get("has_audio"):
            beat_res, beat_note = run_beats(video_path, backend, beats_path)
        else:
            beat_note = "没有音轨"
    align = (
        beat_alignment([t["t"] for t in trans], beat_res, fps, [s["duration"] for s in shots])
        if beat_res
        else None
    )
    if beat_note:
        notes.append(f"节拍: {beat_note}")

    text = text_rhythm(feat, fps, [t["t"] for t in trans])
    motion = motion_summary(shots, dp, fps)
    hook = hook_features(shots, trans, edges, text, feat, dp, fps, dur, beat_res)
    rhythm = rhythm_label(stats, align, shots, dur)
    color = color_profile(feat)
    scene_guess = guess_scene(stats, align, text, motion)
    style_guess = guess_style(color, tc)
    base_scene = scene_guess if scene in (None, "", "auto") else scene
    base_style = style_guess if style in (None, "", "auto") else style

    rep = {
        "schema": SCHEMA_VERSION,
        "source": os.path.abspath(video_path),
        "video": {**video, "duration": round(dur, 3), "analysis_fps": fps},
        "shots": shots,
        "shot_stats": stats,
        "cuts": [t["t"] for t in trans],
        "transitions": trans,
        "transition_counts": tc,
        "dominant_transition": dominant,
        "hard_cut_ratio": hard_ratio,
        "edges": edges,
        "beat": align,
        "text": text,
        "motion": motion,
        "hook": hook,
        "rhythm": rhythm,
        "color": color,
        "scene_guess": scene_guess,
        "style_guess": style_guess,
        "notes": notes,
    }
    rep["structure"] = structure(shots, trans, text, hook, align, dur)
    if ocr:
        lines, msg = ocr_events(video_path, text, video["width"], video["height"])
        rep["ocr"] = lines
        if msg != "ok":
            notes.append(msg)

    did = f"ref_{slugify(name)}"
    scene_d = make_scene_draft(rep, load_base("scenes", base_scene, refs_root), did, name)
    style_d = make_style_draft(rep, load_base("styles", base_style, refs_root), did, name)
    files = {
        "report": os.path.join(out_dir, "reference.json"),
        "scene": os.path.join(out_dir, f"scene_{did}.json"),
        "style": os.path.join(out_dir, f"style_{did}.json"),
    }
    if sheet:
        sel = shots
        if len(sel) > max_thumbs:
            idx = np.linspace(0, len(sel) - 1, max_thumbs).round().astype(int)
            sel = [shots[i] for i in sorted(set(idx))]
        items = [
            (
                s["thumb_t"],
                f"#{s['index']} {s['start']:.2f}s d={s['duration']:.2f}\n{s['transition_in'] or 'start'} {s['camera']['move']}",
            )
            for s in sel
        ]
        en = {"强卡点": "on-beat", "部分卡点": "partly-on-beat", "不卡点": "off-beat"}
        bpm = (
            f" {float(align['tempo']):.0f}BPM {en.get(align.get('label'), '')}"
            if align and align.get("tempo")
            else ""
        )
        header = f"{stem}  {dur:.1f}s  {len(shots)} shots  median {stats.get('median')}s{bpm}"
        cs = contact_sheet(
            video_path,
            items,
            os.path.join(out_dir, "contact_sheet.jpg"),
            video["width"],
            video["height"],
            header,
        )
        hs = contact_sheet(
            video_path,
            [(t, f"{t:.1f}s") for t in np.arange(0, min(HOOK_S, dur - 0.05) + 1e-6, 0.5)],
            os.path.join(out_dir, "hook_strip.jpg"),
            video["width"],
            video["height"],
            cols=7,
        )
        if cs:
            files["contact_sheet"] = cs
        if hs:
            files["hook_strip"] = hs
        if not cs:
            notes.append("没有 Pillow, 跳过联系表")
    if install:
        root = refs_root or os.path.join(SKILL, "references")
        files["installed_scene"] = os.path.join(root, "scenes", f"{did}.json")
        files["installed_style"] = os.path.join(root, "styles", f"{did}.json")
    rep["drafts"] = {"id": did, "scene_base": base_scene, "style_base": base_style}
    rep["files"] = files
    for key, obj in (("report", rep), ("scene", scene_d), ("style", style_d)):
        with open(files[key], "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    if install:
        for key, obj in (("installed_scene", scene_d), ("installed_style", style_d)):
            os.makedirs(os.path.dirname(files[key]), exist_ok=True)
            with open(files[key], "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=1)
    return rep


def summarize(rep: Dict) -> List[str]:
    st, rh, hk = rep["shot_stats"], rep["rhythm"], rep["hook"]
    lines = [
        f"🎬 {os.path.basename(rep['source'])}: {rep['video']['duration']:.1f}s, {st['count']} 个镜头, "
        f"中位 {st['median']}s (p25 {st['p25']} / p75 {st['p75']}), {rh['shots_per_min']} 镜/分 → {rh['label']}",
        (
            "   转场: "
            + ", ".join(f"{TRANSITION_CN[k]} {v}" for k, v in rep["transition_counts"].items())
            if rep["transition_counts"]
            else "   转场: 无 (单镜头)"
        ),
    ]
    b = rep.get("beat")
    if b and b.get("aligned_ratio") is not None:
        lines.append(
            f"   卡拍: {b['tempo']} BPM, {b['aligned_ratio']:.0%} 切点在拍上 (随机 {b['chance_ratio']:.0%}) → {b['label']}, 约每 {b['cut_every_beats']} 拍一刀"
        )
    t = rep["text"]
    lines.append(
        f"   文字: {t['kind']} (底部 {t['bands']['bottom']['present_ratio']:.0%}, 中部 {t['bands']['middle']['present_ratio']:.0%}, "
        f"顶部 {t['bands']['top']['present_ratio']:.0%})"
    )
    lines.append(
        f"   运动: {rep['motion']['level']} ({rep['motion']['avg']}), 运镜 {rep['motion']['camera_moves']}"
    )
    lines.append(
        f"   钩子: 前 3s {hk['cuts']} 刀, {'、'.join(hk['types'])}"
        + (f"; ⚠️ {'; '.join(hk['issues'])}" if hk["issues"] else "")
    )
    lines.append(
        "   结构: "
        + " → ".join(
            f"{s['name']}({s['span'][0]:.1f}-{s['span'][1]:.1f}s)" for s in rep["structure"]
        )
    )
    lines.append(
        f"   场景推断 {rep['scene_guess']} / 风格推断 {rep['style_guess']} → 草案 {rep['drafts']['id']}"
    )
    for nt in rep["notes"]:
        lines.append(f"   - {nt}")
    return lines


def _main(argv=None) -> int:
    from utils.cli_protocol import emit_result, make_result

    ap = argparse.ArgumentParser(description="参考视频结构反推 → scene / style 草案 + 镜头联系表")
    ap.add_argument("video", help="参考短视频 (mp4 / mov ...)")
    ap.add_argument("-o", "--out-dir", help="输出目录 (默认 <视频目录>/<文件名>.reference/)")
    ap.add_argument("--scene", default="auto", help="草案底板场景 id (默认按分析结果推断)")
    ap.add_argument("--style", default="auto", help="草案底板风格 id (默认按色调推断)")
    ap.add_argument("--threshold", type=float, default=0.3, help="ffmpeg scene 阈值")
    ap.add_argument("--fps", type=float, help="分析帧率 (默认 ≤30, 超过 90s 的视频 15)")
    ap.add_argument("--no-beats", action="store_true", help="不做音乐节拍分析")
    ap.add_argument("--beats", help="复用已有 beats.json (beat_detect.py 的输出)")
    ap.add_argument("--backend", default="auto", choices=["auto", "librosa", "numpy"])
    ap.add_argument(
        "--ocr",
        action="store_true",
        help="对换字时刻做 OCR (需要 rapidocr_onnxruntime 或 tesseract)",
    )
    ap.add_argument("--no-sheet", action="store_true", help="不生成缩略图联系表")
    ap.add_argument("--name", help="草案名 (默认视频文件名)")
    ap.add_argument(
        "--install",
        action="store_true",
        help="同时写入 references/scenes 和 references/styles (script_compiler 可直接用)",
    )
    ap.add_argument("--json", action="store_true", help="按 CLI 协议输出 JSON 到 stdout")
    args = ap.parse_args(argv)
    try:
        rep = analyze_reference(
            args.video,
            args.out_dir,
            args.scene,
            args.style,
            args.threshold,
            args.fps,
            beats=not args.no_beats,
            beats_path=args.beats,
            backend=args.backend,
            ocr=args.ocr,
            sheet=not args.no_sheet,
            name=args.name,
            install=args.install,
        )
    except ReferenceAnalyzeError as e:
        if args.json:
            emit_result(make_result(False, "reference_analyze_failed", str(e)), True)
        else:
            print(f"❌ {e}")
        return 1
    if args.json:
        data = {
            k: rep[k]
            for k in (
                "files",
                "drafts",
                "shot_stats",
                "rhythm",
                "hook",
                "beat",
                "scene_guess",
                "style_guess",
                "transition_counts",
                "notes",
            )
        }
        data["text_kind"] = rep["text"]["kind"]
        emit_result(make_result(True, "ok", "", data), True)
        return 0
    for line in summarize(rep):
        print(line)
    for k, v in rep["files"].items():
        print(f"✅ {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
