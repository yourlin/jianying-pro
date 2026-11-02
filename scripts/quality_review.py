#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成片质量审片 (Quality Review): 查"好不好看、留不留得住人", 不查版面 (版面见 layout_preview.py)。

检查项 (规则来源 references/methodology/{hook,pacing,edit_grammar,review_rules}.md):
  - 前 3 秒钩子: 首帧有无画面 / 文字 / 人声, 0–3s 视觉变化量与信息密度, 「大家好」式开场
  - 节奏: 镜头 (画面变化) 时长分布、按场景类型对照经验区间、每秒节奏曲线
  - 死区: 连续 N 秒画面无明显变化且没有新字幕 / 新插卡 / 切镜
  - 连贯: 相邻镜头亮度 / 色温 / 色偏跳变
  - 音频: 静音空洞、BGM 压人声 (人声段 BGM 应低 ≥ 12dB)、配乐提前结束 / 未淡出
  - 结尾: 最后一个新信息之后拖了多久、片尾静音、有无 CTA

输出 report.json (评分 + 问题列表带时间点 + 修改建议) 和 viewer_sheet.png
("观众视角"联系表: 每秒 / 每镜头一帧 + 时间戳 + 问题标注), 供调用方用多模态读图判断"哪里会划走"。

用法:
  python quality_review.py --draft "草稿名" [--drafts-root <草稿根目录>]
  python quality_review.py --video 成片.mp4 [--analysis analysis.json] [--subtitles 字幕.srt]
  python quality_review.py --draft "草稿名" --video 导出.mp4      # 画面和声音用成片, 字幕 / 轨道语义用草稿

退出码 (与 layout_preview 一致): 0 = 通过, 1 = 有问题 (严重度 ≥ --fail-on), 2 = 输入错误。
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.cli_protocol import emit_result, make_result  # noqa: E402
from utils.draft_store import EncryptedDraftError, read_draft_content  # noqa: E402

try:
    from PIL import Image, ImageDraw

    HAS_PIL = True
except ImportError:  # 缺 Pillow 时只出 JSON 报告, 不出联系表 / 不渲染草稿画面
    HAS_PIL = False

# ---------------- 阈值 (可用 CLI 覆盖的写在 argparse 里) ----------------
ANALYSIS_W = 96  # 分析用缩略帧宽度 (像素)
TILE_W = 240  # 联系表单帧宽度
HOP = 0.1  # 音频包络步长 (秒)
SILENCE_DB = -45.0  # 低于此电平算静音 (dBFS)
HOLE_S = 0.8  # 中段静音 ≥ 此值算空洞 (pacing.md: 段间换气约 0.3s)
BGM_MARGIN_DB = 12.0  # 人声段 BGM 至少低 12dB (edit_grammar.md)
STILL_THR = 0.06  # 4x4 网格任一格平均亮度变化 ≥ 此值算"画面有变化"
CUT_HIST = 0.35  # 相邻帧直方图距离 ≥ 此值且整体差 ≥ CUT_DIFF 算切镜
CUT_DIFF = 0.10
# 自适应硬切: 归一化灰度差 (按帧对比度归一, 暗场同色调切镜也看得见) 是局部尖峰
CUT_NORM_MIN = 0.30  # 尖峰 ≥ 0.30 且 ≥ 前后 1s 中位数 × CUT_RATIO
CUT_RATIO = 4.0
CUT_NORM_BIG = 0.60  # 或尖峰 ≥ 0.60 且 ≥ 中位数 × CUT_RATIO_BIG (运动很大的镜头之间)
CUT_RATIO_BIG = 2.0
CUT_COARSE_MIN = 0.15  # 4×6 粗网格均值也要变 (排除 AI 视频抖动 / 细节闪烁)
CUT_HUE = 0.30  # 色相直方图距离 (同亮度换主色调)
HUE_MIN_CHROMA = 0.04  # 平均色度低于此值的帧不比色相
CUT_BASE_FLOOR = 0.03  # 局部中位数下限 (静止录屏里的微小变化不算切镜)
CUT_LOCAL_S = 1.0
NORM_EPS = 0.02
SOFT_HALF_S = 0.4  # 软切 (叠化 / 淡入淡出) 看前后各 0.4s
SOFT_COARSE_MIN = 0.25
SOFT_LUMA_MIN = 0.25
SOFT_STRAIGHT = 0.6
SOFT_CELL_FRAC = 0.5  # 粗网格里至少一半的格子变了
DIP_MAX_S = 0.8  # 闪黑 / 闪白: 进出两刀间隔 ≤ 0.8s 且中间几乎纯色 → 合并
DIP_FLAT_STD = 0.06
VIDEO_FPS = 10.0  # 视频模式默认采样帧率 (0.2s 的快切也能分开)
MAX_FRAMES = 6000  # 视频模式最多分析这么多帧 (> 600s 的视频自动降采样率, 最低 4fps)
# 片尾 CTA 卡: 最后一个镜头静止 2–5s, 画面有大字或二维码 → 是有意的停留, 不算死区 / 拖沓
CTA_MIN_S = 2.0
CTA_MAX_S = 5.0
CTA_QR_DENSITY = 0.25  # 某个方块里强边缘像素占比 (二维码 / 密集文字)
CTA_TEXT_SCORE = 0.12  # reference_analyzer.text_band_features 的横带文字分数 (大字)
HOOK_TILES = (0.0, 1.0, 2.0, 3.0)  # 联系表无论哪种模式都带上这几帧 (钩子区)
JUMP_DL = 25.0  # 相邻镜头 Lab 亮度差 (0–100)
JUMP_REL = 0.35  # 或相对差 ≥ 35% (review_rules.md #9)
JUMP_DL_MIN = 8.0  # 相对差还要求绝对差 ≥ 8 (暗部 10 → 15 不算)
SECTION_MIN_JUMPS = 6  # 跳变切点 ≥ 6 个且占全部切点 ≥ 40%: 视为段落式配色, 合并成一条 low
SECTION_RATIO = 0.4
HOLD_PAD_S = 0.3  # 有意停留区间前后各放宽 0.3s (叠化进出)
SECTION_SNAP_S = 0.35  # 切点离分镜表段落边界 ≤ 0.35s 算段落之间
JUMP_DB = 10.0  # 色温 (Lab b*, 蓝↔黄) 差
JUMP_DA = 12.0  # 色偏 (Lab a*, 绿↔品红) 差
TAIL_DRAG_S = 3.0  # 最后一个新信息之后还拖 ≥ 3s 算拖沓
HOOK_S = 3.0

# 平均镜头 / 景别变化间隔 (pacing.md 表格)
GENRE_SHOT = {
    "口播知识": (3.0, 6.0),
    "vlog": (2.0, 4.0),
    "教程录屏": (5.0, 10.0),
    "带货种草": (1.5, 3.0),
    "影视解说": (2.0, 4.0),
    "卡点混剪": (0.3, 1.0),
}
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
DEDUCT = {"high": 30, "medium": 15, "low": 5}
WEIGHTS = {
    "hook": 0.30,
    "dead_zone": 0.20,
    "pacing": 0.15,
    "audio": 0.15,
    "continuity": 0.10,
    "ending": 0.10,
}
CATEGORY_CN = {
    "hook": "钩子",
    "pacing": "节奏",
    "dead_zone": "死区",
    "continuity": "连贯",
    "audio": "音频",
    "ending": "结尾",
}
RULE_REF = {
    "hook": "references/methodology/hook.md#黄金 3 秒",
    "pacing": "references/methodology/pacing.md#平均镜头时长",
    "dead_zone": "references/methodology/edit_grammar.md#六要素",
    "continuity": "references/methodology/review_rules.md#机位与连贯",
    "audio": "references/methodology/edit_grammar.md#声音",
    "ending": "references/methodology/hook.md#结构与信息密度",
}
BAD_OPENING = re.compile(
    r"大家好|哈喽大家|hello\s*大家|今天(我们)?(来)?(讲|聊|说|分享)一下|欢迎来到"
)
CTA_WORDS = re.compile(r"关注|评论|点赞|收藏|转发|下期|私信|链接|订阅|三连|主页")
BGM_KEYS = ("bgm", "music", "配乐", "音乐", "歌")
SFX_KEYS = ("sfx", "音效", "sound", "whoosh")


class InputError(Exception):
    """输入有误 (找不到草稿 / 视频读不出), CLI 退出码 2。"""


# ---------------- ffmpeg 读取 ----------------
def _run(cmd, timeout=900):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def has_ffmpeg() -> bool:
    r = _run(["ffmpeg", "-version"], timeout=20)
    return bool(r) and r.returncode == 0


def probe_video(path: str) -> dict:
    """宽高 / 时长 / 有无音轨 (复用 analyze_footage.probe)。"""
    from analyze_footage import probe

    return probe(path)


def decode_video(path: str, fps: float, width: int, height: int):
    """按 fps 解码整段视频为缩略帧, 返回 (times, frames[N,h,w,3] uint8)。"""
    r = _run(
        [
            "ffmpeg", "-v", "error", "-skip_loop_filter", "all", "-i", path, "-an",
            "-vf", f"fps={fps},scale={width}:{height}",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ]
    )  # fmt: skip
    if not r or r.returncode != 0:
        return np.zeros(0), np.zeros((0, height, width, 3), np.uint8)
    frame_bytes = width * height * 3
    n = len(r.stdout) // frame_bytes
    frames = np.frombuffer(r.stdout[: n * frame_bytes], np.uint8).reshape(n, height, width, 3)
    return np.arange(n) / fps, frames


def grab_frame(path: str, t: float, width: int, height: int):
    """抽一帧 (联系表用), 失败返回 None。"""
    r = _run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", path,
            "-frames:v", "1", "-vf", f"scale={width}:{height}",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        timeout=60,
    )  # fmt: skip
    if not r or r.returncode != 0 or len(r.stdout) < width * height * 3:
        return None
    return np.frombuffer(r.stdout[: width * height * 3], np.uint8).reshape(height, width, 3)


def decode_audio(path: str, start: float = 0.0, dur=None, sr: int = 8000) -> np.ndarray:
    """解码为单声道 float32 (-1..1); 没有音轨 / 失败返回空数组。"""
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.3f}"]
    if dur is not None:
        cmd += ["-t", f"{max(0.01, dur):.3f}"]
    cmd += ["-i", path, "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    r = _run(cmd)
    if not r or r.returncode != 0 or not r.stdout:
        return np.zeros(0, np.float32)
    return np.frombuffer(r.stdout[: len(r.stdout) // 2 * 2], np.int16).astype(np.float32) / 32768


def rms_db(samples: np.ndarray, sr: int = 8000, hop: float = HOP) -> np.ndarray:
    """每 hop 秒一个 RMS 电平 (dBFS), 下限 -90。"""
    n = int(sr * hop)
    if len(samples) < n:
        return np.full(0, -90.0)
    k = len(samples) // n
    rms = np.sqrt(np.mean(samples[: k * n].reshape(k, n) ** 2, axis=1))
    return np.maximum(-90.0, 20 * np.log10(rms + 1e-9))


# ---------------- 画面特征 ----------------
def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB (0..1) -> CIELab (D65)。"""
    c = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = (c @ m.T) / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack(
        [116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1
    )


def _gray(frame: np.ndarray) -> np.ndarray:
    f = frame.astype(np.float32) / 255
    return f[..., 0] * 0.299 + f[..., 1] * 0.587 + f[..., 2] * 0.114


def cell_diff(a: np.ndarray, b: np.ndarray, grid: int = 4) -> float:
    """两帧灰度差在 grid×grid 网格里最大那格的均值 (0..1): 字幕换行、局部动作都能抓到。"""
    d = np.abs(_gray(a) - _gray(b))
    h, w = d.shape
    gh, gw = max(1, h // grid), max(1, w // grid)
    d = d[: gh * grid, : gw * grid].reshape(grid, gh, grid, gw).mean(axis=(1, 3))
    return float(d.max())


def hist_dist(a: np.ndarray, b: np.ndarray, bins: int = 16) -> float:
    """三通道直方图全变差距离 (0..1)。"""
    out = 0.0
    for ch in range(3):
        ha = np.histogram(a[..., ch], bins=bins, range=(0, 256))[0].astype(np.float64)
        hb = np.histogram(b[..., ch], bins=bins, range=(0, 256))[0].astype(np.float64)
        out += 0.5 * np.abs(ha / max(1, ha.sum()) - hb / max(1, hb.sum())).sum()
    return out / 3


def _norm_gray(frame: np.ndarray) -> np.ndarray:
    """对比度归一化的灰度小图 (减均值、除标准差): 暗场里同色调的切镜也能和亮场一样比较。"""
    g = _gray(frame)
    h, w = g.shape[0] // 2 * 2, g.shape[1] // 2 * 2
    g = g[:h, :w].reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))
    return ((g - g.mean()) / (g.std() + NORM_EPS)).astype(np.float32)


def _coarse(a: np.ndarray, rows: int = 4, cols: int = 6) -> np.ndarray:
    """rows×cols 网格均值: 抖动 / 细节闪烁在粗网格上抵消, 构图变化 (换镜头) 留下来。"""
    h, w = a.shape
    gh, gw = max(1, h // rows), max(1, w // cols)
    return a[: gh * rows, : gw * cols].reshape(rows, gh, cols, gw).mean(axis=(1, 3))


def hue_hist(frame: np.ndarray, bins: int = 12) -> np.ndarray:
    """色相直方图 (按饱和度 × 明度加权, 归一化): 亮度相近但换了主色调的切镜靠它识别。"""
    x = frame.astype(np.float32) / 255
    mx, mn = x.max(axis=2), x.min(axis=2)
    c = mx - mn
    r, g, b = x[..., 0], x[..., 1], x[..., 2]
    safe = np.maximum(c, 1e-6)
    h = np.where(
        mx == r, ((g - b) / safe) % 6, np.where(mx == g, (b - r) / safe + 2, (r - g) / safe + 4)
    )
    wgt = np.where(c > 1e-3, c, 0.0)  # = 饱和度 × 明度
    hh = np.histogram(h, bins=bins, range=(0, 6), weights=wgt)[0]
    # 几乎没有颜色 (白底 UI / 黑白画面) 时色相是噪声, 返回全 0 表示「无色相」
    if float(wgt.mean()) < HUE_MIN_CHROMA:
        return np.zeros(bins)
    return hh / hh.sum()


def frame_features(times: np.ndarray, frames: np.ndarray) -> dict:
    """逐帧: 亮度 / Lab 均值 / 与上一帧的差 (像素 / 直方图 / 归一化灰度 / 粗网格 / 色相)。"""
    n = len(frames)
    lab = np.zeros((n, 3))
    luma_std = np.zeros(n)
    diff, hist, ndiff, cdiff, hdiff = (np.zeros(n) for _ in range(5))
    small = None
    raw = np.zeros((n, 4, 6), np.float32)  # 原始灰度粗网格 (软切判断「整幅都在变」用)
    prev_hue = prev_coarse = None
    for i, fr in enumerate(frames):
        f = fr.astype(np.float32) / 255
        lab[i] = rgb_to_lab(f).reshape(-1, 3).mean(0)
        luma_std[i] = float(_gray(fr).std())
        ng = _norm_gray(fr)
        if small is None:
            small = np.zeros((n, *ng.shape), np.float32)
        small[i] = ng
        hue, coarse = hue_hist(fr), _coarse(ng)
        raw[i] = _coarse(_gray(fr))
        if i:
            diff[i] = float(np.abs(f - frames[i - 1].astype(np.float32) / 255).mean())
            hist[i] = hist_dist(fr, frames[i - 1])
            ndiff[i] = float(np.abs(ng - small[i - 1]).mean())
            cdiff[i] = float(np.abs(coarse - prev_coarse).mean())
            if hue.any() and prev_hue.any():
                hdiff[i] = 0.5 * float(np.abs(hue - prev_hue).sum())
        prev_hue, prev_coarse = hue, coarse
    return {"times": times, "lab": lab, "luma_std": luma_std, "diff": diff, "hist": hist,
            "ndiff": ndiff, "cdiff": cdiff, "hdiff": hdiff, "ngray": small, "graw": raw}  # fmt: skip


def _local_base(x: np.ndarray, i: int, r: int) -> float:
    """第 i 帧前后 r 帧 (不含紧邻的 ±1 帧) 的中位数: 当前镜头自身的运动水平。"""
    nb = np.concatenate([x[max(1, i - r) : max(1, i - 1)], x[i + 2 : i + 1 + r]])
    return float(np.median(nb)) if len(nb) else 0.0


def _is_peak(x: np.ndarray, i: int) -> bool:
    return x[i] >= x[i - 1] and (i + 1 >= len(x) or x[i] >= x[i + 1])


def _hard_cut_frames(feat: dict, fps: float) -> list:
    """
    硬切帧号 (新镜头第一帧), 满足任一条:
      - 归一化灰度差是局部尖峰 (自适应阈值: 高于前后 1s 中位数的 N 倍), 且粗网格构图也变了;
      - 色相直方图突变 (同亮度换主色调, 如游戏关卡切换);
      - 直方图 + 整体像素差都很大 (纯色块切换, 归一化灰度看不到)。
    """
    dn, cd, hd, dp, hh = (feat[k] for k in ("ndiff", "cdiff", "hdiff", "diff", "hist"))
    n = len(dn)
    r = max(3, int(round(fps * CUT_LOCAL_S)))
    out = []
    for i in range(1, n):
        base = max(_local_base(dn, i, r), CUT_BASE_FLOOR)
        spike = (
            _is_peak(dn, i)
            and cd[i] >= CUT_COARSE_MIN
            and (
                (dn[i] >= CUT_NORM_BIG and dn[i] >= CUT_RATIO_BIG * base)
                or (dn[i] >= CUT_NORM_MIN and dn[i] >= CUT_RATIO * base)
            )
        )
        hue = (
            _is_peak(hd, i)
            and hd[i] >= CUT_HUE
            and hd[i] >= CUT_RATIO * max(_local_base(hd, i, r), 0.02)
            and dn[i] >= 1.5 * base
        )
        flat = _is_peak(dp, i) and hh[i] >= CUT_HIST and dp[i] >= CUT_DIFF
        if (spike or hue) and _band_only(feat["graw"][i], feat["graw"][i - 1]):
            spike = hue = False
        if spike or hue or flat:
            out.append(i)
    return out


def _band_only(a: np.ndarray, b: np.ndarray, thr: float = 0.04) -> bool:
    """变化只落在粗网格的一行、且不满一整行: 字幕条 / 下三分之一标签出现消失, 不是换镜头。"""
    d = np.abs(a - b) > thr
    return int(d.any(axis=1).sum()) <= 1 and float(d.mean()) < 0.25


def _soft_cut_frames(feat: dict, fps: float, hard: list) -> list:
    """叠化 / 淡入淡出: 跨 2k 帧的变化大, 而且路径「直」(帧间差累计 ≈ 首尾差, 运动镜头远大于首尾差),
    两端画面相对静止。"""
    small, dn = feat["ngray"], feat["ndiff"]
    n = len(small)
    k = max(2, int(round(fps * SOFT_HALF_S)))
    if n < 2 * k + 3:
        return []
    lum = feat["lab"][:, 0] / 100
    score = np.zeros(n)
    for i in range(2 * k, n - k):
        a, b = i - k, i + k
        span_n = float(np.abs(_coarse(small[b]) - _coarse(small[a])).mean())
        # 整幅画面都在变 (不是黑底上局部插图淡入; 用原始灰度, 归一化会把局部变化摊到全图)
        wide = float((np.abs(feat["graw"][b] - feat["graw"][a]) > 0.04).mean()) >= SOFT_CELL_FRAC
        path_n = float(feat["cdiff"][a + 1 : b + 1].sum())
        span_l = abs(float(lum[b] - lum[a]))
        path_l = float(np.abs(np.diff(lum[a : b + 1])).sum())
        best = 0.0
        if wide and span_n >= SOFT_COARSE_MIN and span_n >= SOFT_STRAIGHT * path_n:
            best = span_n
        if wide and span_l >= SOFT_LUMA_MIN and span_l >= SOFT_STRAIGHT * path_l:
            best = max(best, span_l)
        if not best:
            continue
        # 两端都要相对静止 (持续运动的镜头不算), 中间每帧都在变 (不是一次硬切)
        mid = dn[a + 1 : b + 1]
        pre = float(np.median(dn[max(1, a - k) : a + 1]))
        post = float(np.median(dn[b : min(n, b + k + 1)]))
        if max(pre, post) > 0.5 * float(mid.mean()) or float(mid.max()) > 0.6 * float(mid.sum()):
            continue
        score[i] = best
    out = []
    for i in range(k, n - k):
        if score[i] <= 0 or score[i] < score[max(0, i - k) : i + k + 1].max():
            continue
        if any(abs(i - h) <= k + 1 for h in hard):
            continue
        if out and i - out[-1] <= k:
            continue
        out.append(i)
    return out


def _merge_dips(cuts: list, feat: dict) -> list:
    """闪黑 / 闪白 / 白屏加载: 进出各被检成一刀, 中间 ≤ DIP_MAX_S 秒几乎是纯黑 / 纯白 → 合并成一刀 (取中点)。"""
    t, std, L = feat["times"], feat["luma_std"], feat["lab"][:, 0]
    out = []
    for c in cuts:
        if out and c - out[-1] <= DIP_MAX_S:
            sel = (t > out[-1]) & (t < c)
            dark_or_white = sel.any() and (float(L[sel].max()) < 15 or float(L[sel].min()) > 88)
            if dark_or_white and float(std[sel].max()) < DIP_FLAT_STD:
                out[-1] = round((out[-1] + c) / 2, 3)
                continue
        out.append(c)
    return out


def detect_cuts(feat: dict, min_gap: float = None) -> list:
    """切镜时间点: 硬切 (自适应尖峰) + 软切 (叠化 / 淡入淡出), 闪黑闪白合并; 硬切取两侧帧中点。"""
    t = feat["times"]
    if len(t) < 2:
        return []
    fps = 1.0 / max(1e-6, float(np.median(np.diff(t))))
    if "ndiff" not in feat:  # 旧特征 (无归一化灰度): 只用固定阈值
        idx = [
            i
            for i in range(1, len(t))
            if feat["hist"][i] >= CUT_HIST and feat["diff"][i] >= CUT_DIFF
        ]
        soft = []
    else:
        idx = _hard_cut_frames(feat, fps)
        soft = _soft_cut_frames(feat, fps, idx)
    gap = min_gap if min_gap is not None else 1.5 / fps
    pts = sorted([float(t[i - 1] + t[i]) / 2 for i in idx] + [float(t[i]) for i in soft])
    cuts = []
    for c in pts:
        c = round(c, 3)
        if not cuts or c - cuts[-1] >= gap:
            cuts.append(c)
    return _merge_dips(cuts, feat) if "ndiff" in feat else cuts


def change_times(times: np.ndarray, frames: np.ndarray, thr: float = STILL_THR) -> list:
    """锚点法: 当前帧和上一次"有变化"的帧相比变化 ≥ thr 就记一次画面变化 (慢推也能累积出来)。"""
    out = []
    if not len(frames):
        return out
    anchor = frames[0]
    for i in range(1, len(frames)):
        if cell_diff(frames[i], anchor) >= thr:
            out.append(round(float(times[i]), 3))
            anchor = frames[i]
    return out


# ---------------- 字幕 / 转写 ----------------
def parse_srt(path: str) -> list:
    """SRT -> [{start, end, text}]。"""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        raw = f.read()
    out = []
    pat = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = block.strip().splitlines()
        for i, ln in enumerate(lines):
            m = pat.search(ln)
            if not m:
                continue
            g = [int(x) for x in m.groups()]
            s = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 10 ** len(m.group(4))
            e = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 10 ** len(m.group(8))
            text = " ".join(x.strip() for x in lines[i + 1 :] if x.strip())
            out.append({"start": round(s, 3), "end": round(e, 3), "text": text, "track": "srt"})
            break
    return out


def load_transcript(path: str):
    """analyze_footage 的 analysis.json 或纯转写列表 -> (transcript, hints)。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data, {}
    return data.get("transcript") or [], data.get("hints") or {}


# ---------------- 分镜表 review 提示 (script_compiler 写入) ----------------
def _span(x):
    """[s, e] / {"time": [s, e]} / {"start", "end"} -> (s, e) 或 None。"""
    if isinstance(x, dict):
        x = x.get("time") or [x.get("start"), x.get("end")]
    try:
        s, e = float(x[0]), float(x[1])
    except (TypeError, ValueError, IndexError):
        return None
    return (s, e) if e > s else None


def load_review_hints(path: str) -> dict:
    """读分镜表 (storyboard JSON) 的 review 块, 归一化为
    {hook_type, hook_hold_s, holds: [{start, end, why}], continuity, sections: [(s, e)], source}。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            sb = json.load(f)
    except (OSError, ValueError) as e:
        raise InputError(f"分镜表读不了: {path} ({e})") from e
    rv = sb.get("review") if isinstance(sb, dict) else None
    if not isinstance(rv, dict):
        return {}
    hook = rv.get("hook") or {}
    hold_s = hook.get("hold_s")
    if isinstance(hold_s, (int, float)):
        hold_s = [float(hold_s), float(hold_s)]
    holds = []
    for h in rv.get("intentional_holds") or []:
        sp = _span(h)
        if sp:
            holds.append({"start": sp[0], "end": sp[1],
                          "why": (h.get("why") if isinstance(h, dict) else None) or "有意停留"})  # fmt: skip
    sections = [sp for sp in (_span(x) for x in rv.get("sections") or []) if sp]
    return {
        "hook_type": hook.get("type"),
        "hook_hold_s": hold_s,
        "holds": holds,
        "continuity": rv.get("continuity"),
        "sections": sections,
        "source": os.path.abspath(path),
    }


def find_storyboard(draft_dir: str, draft_name: str = None):
    """草稿目录里的分镜表: storyboard.json / *storyboard*.json; 多个时优先 project.name 与草稿同名的。"""
    try:
        names = sorted(n for n in os.listdir(draft_dir)
                       if n.lower().endswith(".json") and "storyboard" in n.lower())  # fmt: skip
    except OSError:
        return None
    paths = [os.path.join(draft_dir, n) for n in names]
    if not paths:
        return None
    want = draft_name or os.path.basename(os.path.normpath(draft_dir))
    for pth in paths:
        try:
            with open(pth, "r", encoding="utf-8") as f:
                if ((json.load(f).get("project") or {}).get("name")) == want:
                    return pth
        except (OSError, ValueError, AttributeError):
            continue
    return max(paths, key=os.path.getmtime)


def _covered(a: float, b: float, spans) -> float:
    """[a, b] 被 spans 覆盖的总长度 (spans 可重叠)。"""
    pts = sorted((max(a, s), min(b, e)) for s, e in spans if min(b, e) > max(a, s))
    tot, cur_s, cur_e = 0.0, None, None
    for s, e in pts:
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                tot += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        tot += cur_e - cur_s
    return tot


def _hold_spans(tl):
    """有意停留区间: 分镜表 intentional_holds + 自动识别的片尾 CTA 卡。"""
    spans = [(h["start"], h["end"]) for h in (tl.get("review_hints") or {}).get("holds") or []]
    if tl.get("cta_card"):
        spans.append((tl["cta_card"]["start"], tl["cta_card"]["end"]))
    return spans


# ---------------- 草稿时间线 ----------------
def find_draft_dir(name: str, drafts_root=None) -> str:
    if drafts_root:
        cand = os.path.join(drafts_root, name)
        if os.path.isdir(cand):
            return cand
        raise InputError(f"找不到草稿: {cand}")
    if os.path.isdir(name):
        return name
    roots = []
    try:
        from utils.formatters import get_default_drafts_root

        roots.append(get_default_drafts_root())
    except Exception:  # 探测失败就只用 layout_preview 的默认路径
        pass
    from layout_preview import DRAFT_ROOTS

    for root in roots + list(DRAFT_ROOTS):
        cand = os.path.join(root, name)
        if os.path.isdir(cand):
            return cand
    raise InputError(f"找不到草稿: {name} (可用 --drafts-root 指定草稿根目录)")


def _tr(seg, key="target_timerange"):
    r = seg.get(key) or {}
    return (r.get("start") or 0) / 1e6, (r.get("duration") or 0) / 1e6


def _stem_of(track, is_main_video, material):
    name = (track.get("name") or "").lower()
    if any(k in name for k in BGM_KEYS) or (material or {}).get("type") == "music":
        return "bgm"
    if any(k in name for k in SFX_KEYS):
        return "sfx"
    if track.get("type") == "video":
        return "voice" if is_main_video else "other"
    return "voice"


def draft_timeline(draft: dict) -> dict:
    """从草稿 JSON 取: 主轨切点、覆盖层、文字、音频片段 (分 voice / bgm / sfx / other)。"""
    from layout_preview import text_of

    mats = draft.get("materials") or {}
    vids = {m["id"]: m for m in mats.get("videos", [])}
    auds = {m["id"]: m for m in mats.get("audios", [])}
    texts = {m["id"]: m for m in mats.get("texts", [])}
    fades = {m["id"]: m for m in mats.get("audio_fades", [])}
    tracks = draft.get("tracks") or []
    vtracks = [t for t in tracks if t.get("type") == "video"]
    end = 0.0
    cuts, overlays, txt, audio = [], [], [], []
    for vi, track in enumerate(vtracks):
        segs = sorted(track.get("segments", []), key=lambda s: _tr(s)[0])
        prev = None
        for s in segs:
            st, du = _tr(s)
            end = max(end, st + du)
            mat = vids.get(s.get("material_id")) or {}
            if vi == 0:
                if prev is not None:
                    pst, pdu = _tr(prev)
                    pm = vids.get(prev.get("material_id")) or {}
                    ps, pd = _tr(prev, "source_timerange")
                    ss, _sd = _tr(s, "source_timerange")
                    same_shot = (
                        pm.get("path") == mat.get("path")
                        and abs(ps + pd - ss) < 0.05
                        and (prev.get("clip") or {}).get("scale")
                        == (s.get("clip") or {}).get("scale")
                    )
                    if not same_shot:
                        cuts.append(round(st, 3))
                elif st > 0.04:
                    cuts.append(round(st, 3))
                prev = s
            else:
                overlays.append(
                    {"start": round(st, 3), "end": round(st + du, 3),
                     "name": mat.get("material_name") or track.get("name") or "overlay"}
                )  # fmt: skip
            if (s.get("volume", 1.0) or 0) > 0 and mat.get("path") and mat.get("type") != "photo":
                audio.append(_audio_item(s, mat, _stem_of(track, vi == 0, mat), fades))
    for track in tracks:
        if track.get("type") == "audio":
            for s in track.get("segments", []):
                mat = auds.get(s.get("material_id")) or {}
                st, du = _tr(s)
                end = max(end, st + du)
                if (s.get("volume", 1.0) or 0) > 0 and mat.get("path"):
                    audio.append(_audio_item(s, mat, _stem_of(track, False, mat), fades))
        elif track.get("type") == "text":
            for s in track.get("segments", []):
                parsed = text_of(texts.get(s.get("material_id"), {}))
                st, du = _tr(s)
                end = max(end, st + du)
                if parsed and parsed[0].strip():
                    txt.append(
                        {"start": round(st, 3), "end": round(st + du, 3),
                         "text": parsed[0].replace("\n", " "), "track": track.get("name") or "text"}
                    )  # fmt: skip
    duration = max((draft.get("duration") or 0) / 1e6, end)
    canvas = draft.get("canvas_config") or {}
    return {
        "duration": round(duration, 3),
        "canvas": [canvas.get("width", 1080), canvas.get("height", 1920)],
        "cuts": sorted(set(cuts)),
        "overlays": overlays,
        "texts": sorted(txt, key=lambda x: x["start"]),
        "audio_items": audio,
    }


def _audio_item(seg, mat, stem, fades):
    st, du = _tr(seg)
    ss, sd = _tr(seg, "source_timerange")
    fi = fo = 0.0
    for ref in seg.get("extra_material_refs") or []:
        if ref in fades:
            fi = (fades[ref].get("fade_in_duration") or 0) / 1e6
            fo = (fades[ref].get("fade_out_duration") or 0) / 1e6
    return {
        "stem": stem, "path": mat.get("path"), "start": st, "dur": du, "src_start": ss,
        "src_dur": sd or du, "volume": float(seg.get("volume", 1.0) or 0), "fade_in": fi,
        "fade_out": fo,
    }  # fmt: skip


def mix_stems(items: list, duration: float, hop: float = HOP) -> dict:
    """把各音频片段的 RMS 包络按时间线 / 音量 / 淡入淡出叠成 voice / bgm / sfx / other / mix (dB)。"""
    n = max(1, int(math.ceil(duration / hop)))
    power = {k: np.zeros(n) for k in ("voice", "bgm", "sfx", "other")}
    decoded = 0
    for it in items:
        if not os.path.exists(it["path"] or ""):
            continue
        env = rms_db(decode_audio(it["path"], it["src_start"], it["src_dur"]), hop=hop)
        if not len(env):
            continue
        decoded += 1
        m = max(1, int(round(it["dur"] / hop)))
        idx = np.minimum(len(env) - 1, (np.arange(m) * len(env) / m).astype(int))
        db = env[idx] + 20 * math.log10(max(1e-6, it["volume"]))
        tt = np.arange(m) * hop
        gain = np.ones(m)
        if it["fade_in"] > 0:
            gain = np.minimum(gain, np.clip(tt / it["fade_in"], 0, 1))
        if it["fade_out"] > 0:
            gain = np.minimum(gain, np.clip((it["dur"] - tt) / it["fade_out"], 0, 1))
        p = 10 ** (db / 10) * gain**2
        i0 = int(round(it["start"] / hop))
        i1 = min(n, i0 + m)
        if i1 > i0:
            power[it["stem"]][i0:i1] += p[: i1 - i0]
    out = {k: np.maximum(-90.0, 10 * np.log10(v + 1e-9)) for k, v in power.items()}
    out["mix"] = np.maximum(-90.0, 10 * np.log10(sum(power.values()) + 1e-9))
    out["decoded"] = decoded
    return out


def active_intervals(db: np.ndarray, thr: float, hop: float = HOP, merge=0.3, min_len=0.2):
    """电平 > thr 的区间 [(s, e)], 合并短间隙、丢弃过短片段。"""
    out = []
    on = None
    for i, v in enumerate(list(db) + [-999]):
        if v > thr and on is None:
            on = i
        elif v <= thr and on is not None:
            s, e = on * hop, i * hop
            if out and s - out[-1][1] <= merge:
                out[-1] = (out[-1][0], e)
            else:
                out.append((s, e))
            on = None
    return [(round(s, 2), round(e, 2)) for s, e in out if e - s >= min_len]


def render_draft_frames(draft: dict, times, width: int):
    """用 layout_preview.build_frame 伪渲染草稿画面, 返回 (分析帧[N,h,w,3], {t: 联系表帧})。"""
    from layout_preview import PX_PER_SIZE_AT_1920, build_frame

    canvas = draft.get("canvas_config") or {}
    cw, ch = canvas.get("width", 1080), canvas.get("height", 1920)
    h = max(2, int(round(width * ch / cw / 2)) * 2)
    th = max(2, int(round(TILE_W * ch / cw / 2)) * 2)
    small, tiles = [], {}
    for t in times:
        try:
            img, _ = build_frame(draft, float(t), PX_PER_SIZE_AT_1920, show_guides=False)
        except Exception:  # 单帧渲染失败不影响其余检查
            img = Image.new("RGB", (cw, ch), (18, 18, 18))
        rgb = img.convert("RGB")
        small.append(np.asarray(rgb.resize((width, h), Image.BILINEAR)))
        tiles[round(float(t), 2)] = np.asarray(rgb.resize((TILE_W, th), Image.BILINEAR))
    return np.stack(small) if small else np.zeros((0, h, width, 3), np.uint8), tiles


# ---------------- 检查 ----------------
def _issue(category, kind, severity, start, end, message, suggestion):
    return {
        "category": category, "type": kind, "severity": severity,
        "start": round(float(start), 2), "end": round(float(end), 2),
        "message": message, "suggestion": suggestion, "rule": RULE_REF[category],
    }  # fmt: skip


def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def speech_ratio(speech, s, e):
    if e <= s:
        return 0.0
    return sum(_overlap(a, b, s, e) for a, b in speech) / (e - s)


def looks_like_screen_recording(feat) -> bool:
    """白底低饱和、画面大多静止: 像录屏 / 课件 (Lab 亮度高、色度低、帧差中位数很小)。"""
    if feat is None or not len(feat["times"]):
        return False
    lab = feat["lab"]
    chroma = float(np.median(np.hypot(lab[:, 1], lab[:, 2])))
    return (
        float(np.median(lab[:, 0])) >= 75 and chroma < 8 and float(np.median(feat["diff"])) < 0.005
    )


def guess_genre(duration, shots_mean, speech, hint=None, feat=None):
    if hint in GENRE_SHOT:
        return hint
    talk = speech_ratio(speech, 0, duration) if duration else 0
    if looks_like_screen_recording(feat):
        return "教程录屏"
    if shots_mean and shots_mean < 1.2 and talk < 0.4:
        return "卡点混剪"
    if talk >= 0.5:
        return "口播知识"
    return "vlog"


def info_events(tl: dict) -> list:
    """所有"新信息"时间点: 切镜、画面变化、新字幕、插卡出现 / 消失、新一句话。"""
    ev = set(tl["cuts"]) | set(tl.get("changes") or [])
    ev |= {x["start"] for x in tl["texts"]}
    for o in tl["overlays"]:
        ev |= {o["start"], o["end"]}
    ev |= {s for s, _e in tl["speech_segments"]}
    return sorted(round(t, 2) for t in ev if 0 < t < tl["duration"])


def check_hook(tl, feat, audio, issues):
    """前 3 秒: 首帧、淡入、人声 / 文字、视觉变化、信息密度、开场白。"""
    dur = tl["duration"]
    win = min(HOOK_S, dur)
    m = {"window": [0, round(win, 2)]}
    if feat is not None and len(feat["times"]):
        L0 = float(feat["lab"][0, 0])
        std0 = float(feat["luma_std"][0])
        i1 = int(np.argmin(np.abs(feat["times"] - min(1.0, dur))))
        L1 = float(feat["lab"][i1, 0])
        sel = feat["times"] <= win
        m.update(
            first_frame_L=round(L0, 1), first_frame_std=round(std0, 3),
            visual_change_0_3=round(float(feat["diff"][sel].sum()), 3),
        )  # fmt: skip
        if L0 < 6 and std0 < 0.03:
            lit = np.where((feat["lab"][:, 0] >= 6) | (feat["luma_std"] >= 0.03))[0]
            black_end = float(feat["times"][lit[0]]) if len(lit) else dur
            m["black_until"] = round(black_end, 2)
            issues.append(_issue("hook", "首帧黑场", "high", 0, max(0.5, black_end),
                                 f"开头 {black_end:.1f}s 是黑场 / 纯色, 观众第一眼什么都没看到",
                                 "删掉片头黑场和淡入, 第 0 帧直接给最有冲击力的画面或大字"))  # fmt: skip
        elif L1 > 20 and L0 < L1 * 0.5:
            issues.append(_issue("hook", "慢淡入", "medium", 0, 1.0,
                                 f"开头从暗处淡入 (亮度 {L0:.0f} → {L1:.0f}), 前 0.3s 没有有效画面",
                                 "去掉开头淡入, 短视频第 0 帧必须有画面"))  # fmt: skip
    cuts3 = [c for c in tl["cuts"] if c < win]
    texts3 = [x for x in tl["texts"] if x["start"] < win]
    first_text = min((x["start"] for x in tl["texts"]), default=None)
    first_speech = min((s for s, _e in tl["speech_segments"]), default=None)
    m.update(cuts_0_3=len(cuts3), texts_0_3=len(texts3),
             first_text=first_text, first_speech=first_speech)  # fmt: skip
    if tl["texts_known"]:
        m["text_at_0"] = any(x["start"] <= 0.3 < x["end"] or x["start"] <= 0.3 for x in texts3)
    voice_known = tl["speech_known"]
    if voice_known or tl["texts_known"]:
        has_voice = first_speech is not None and first_speech < win
        has_text = first_text is not None and first_text < win
        if not has_voice and not has_text:
            issues.append(_issue("hook", "前3秒无人声无文字", "high", 0, win,
                                 "前 3 秒既没有人声也没有文字, 观众不知道这条视频讲什么",
                                 "开头 0.3s 内上一行钩子大字, 或把人声提前到第 0 秒 (冷开场)"))  # fmt: skip
        elif (
            first_speech is not None
            and first_speech > 1.5
            and not (first_text is not None and first_text <= 0.5)
        ):
            issues.append(_issue("hook", "人声进入太晚", "medium", 0, first_speech,
                                 f"人声 {first_speech:.1f}s 才出现, 且开头没有文字钩子",
                                 "剪掉开口前的空白, 或在第 0 帧加一行钩子文字"))  # fmt: skip
    events = [e for e in info_events(tl) if e < win]
    density = round((len(events) + 1) / max(0.5, win), 2)  # +1: 第 0 帧本身
    m["info_events_0_3"] = len(events)
    m["info_density_per_s"] = density
    hints = tl.get("review_hints") or {}
    if hints.get("hook_type") == "title_card":
        m["hook_type"] = "title_card"  # 分镜表声明: 开头 3–4s 静止标题卡是有意的, 不算钩子弱
    elif feat is not None and len(feat["times"]) and len(events) == 0 and not cuts3:
        if m.get("visual_change_0_3", 0) < 0.15:
            issues.append(_issue("hook", "前3秒画面静止", "medium", 0, win,
                                 "前 3 秒没有切镜、没有新字幕 / 插卡, 画面几乎不动",
                                 "前 3 秒给 1–3 个变化: 推近 (punch_in)、证据画面 0.3–0.8s 闪出、关键词大字"))  # fmt: skip
    early = " ".join(x["text"] for x in tl["texts"] if x["start"] < 5)
    early += " " + " ".join(t for s, t in tl["speech_text"] if s < 5)
    hit = BAD_OPENING.search(early)
    if hit:
        issues.append(_issue("hook", "寒暄式开场", "medium", 0, 5,
                             f"开头出现「{hit.group(0)}」式寒暄, 浪费黄金 3 秒",
                             "删掉寒暄, 用结论先行 / 反常识 / 痛点问题开场 (hook.md 钩子五类)"))  # fmt: skip
    return m


def shots_from(tl):
    """镜头 = 相邻两次画面级变化 (切镜、插卡出现 / 消失) 之间的区间。"""
    ev = set(tl["cuts"])
    for o in tl["overlays"]:
        ev |= {o["start"], o["end"]}
    pts = [0.0]
    for t in sorted(ev):
        if 0.2 <= t <= tl["duration"] - 0.2 and t - pts[-1] >= 0.2:
            pts.append(round(t, 3))
    pts.append(tl["duration"])
    return [{"index": i + 1, "start": round(a, 2), "end": round(b, 2), "dur": round(b - a, 2)}
            for i, (a, b) in enumerate(zip(pts, pts[1:])) if b > a]  # fmt: skip


def pacing_curve(tl, feat, hop=1.0):
    """每秒: 切镜数 / 新字幕数 / 人声占比 / 画面运动量 / 综合"能量" (0..1, 3s 平滑)。"""
    n = max(1, int(math.ceil(tl["duration"] / hop)))
    rows = []
    events = info_events(tl)
    for i in range(n):
        s, e = i * hop, min(tl["duration"], (i + 1) * hop)
        motion = 0.0
        if feat is not None and len(feat["times"]):
            sel = (feat["times"] >= s) & (feat["times"] < e)
            motion = float(feat["diff"][sel].mean()) if sel.any() else 0.0
        cuts = sum(1 for c in tl["cuts"] if s <= c < e)
        texts = sum(1 for x in tl["texts"] if s <= x["start"] < e)
        ev = sum(1 for t in events if s <= t < e)
        sp = speech_ratio(tl["speech_segments"], s, e)
        energy = 0.45 * min(1.0, ev) + 0.35 * min(1.0, motion / 0.06) + 0.2 * sp
        rows.append({"t": round(s, 2), "cuts": cuts, "new_texts": texts, "events": ev,
                     "speech": round(sp, 2), "motion": round(motion, 4), "energy": energy})  # fmt: skip
    raw = np.array([r["energy"] for r in rows])
    k = min(3, len(raw))
    smooth = np.convolve(
        np.pad(raw, (k // 2, k - 1 - k // 2), mode="edge"), np.ones(k) / k, "valid"
    )
    for r, v in zip(rows, smooth):
        r["energy"] = round(float(v), 3)
    return rows


def check_pacing(tl, feat, genre, issues):
    shots = shots_from(tl)
    d = np.array([s["dur"] for s in shots]) if shots else np.zeros(0)
    stats = {}
    if len(d):
        stats = {
            "count": len(d), "mean": round(float(d.mean()), 2), "median": round(float(np.median(d)), 2),
            "p90": round(float(np.percentile(d, 90)), 2), "max": round(float(d.max()), 2),
            "cv": round(float(d.std() / max(1e-6, d.mean())), 2),
            "per_min": round(len(d) / max(1e-6, tl["duration"]) * 60, 1),
        }  # fmt: skip
    lo, hi = GENRE_SHOT[genre]
    if len(d) >= 2 or tl["duration"] > hi * 2:
        mean = float(d.mean()) if len(d) else tl["duration"]
        if mean > hi * 1.3:
            issues.append(_issue("pacing", "节奏偏慢", "medium", 0, tl["duration"],
                                 f"平均 {mean:.1f}s 才有一次画面变化, {genre}经验值 {lo:g}–{hi:g}s",
                                 f"每 {hi:g}s 内给一次景别变化: 假景别 1.25x 推近、插 B-roll 或图卡"))  # fmt: skip
        elif mean < lo * 0.6 and genre != "卡点混剪":
            issues.append(_issue("pacing", "节奏过碎", "low", 0, tl["duration"],
                                 f"平均 {mean:.1f}s 就切一次, 对{genre}来说太碎, 观众来不及看清",
                                 f"合并相邻碎镜头, 让平均镜头时长回到 {lo:g}–{hi:g}s"))  # fmt: skip
    for s in shots:
        if s["dur"] > max(hi * 2, 6.0):
            issues.append(_issue("pacing", "长镜头", "low", s["start"], s["end"],
                                 f"第 {s['index']} 个镜头 {s['dur']:.1f}s 没有换景别 ({genre}建议 ≤ {hi:g}s)",
                                 "中间加一次假景别推近 / B-roll / 图表画中画, 或剪短"))  # fmt: skip
    if stats.get("count", 0) >= 6 and stats["cv"] < 0.15 and genre != "卡点混剪":
        issues.append(_issue("pacing", "节奏单调", "low", 0, tl["duration"],
                             f"镜头时长几乎一样 (变异系数 {stats['cv']}), 节奏像节拍器",
                             "用「两短一长」打破均匀: 特写 1–1.5s + 特写 1–1.5s + 全景约 3s"))  # fmt: skip
    curve = pacing_curve(tl, feat)
    return {
        "genre": genre,
        "target_shot_s": [lo, hi],
        "shots": shots,
        "stats": stats,
        "curve": curve,
    }


def card_evidence(frame: np.ndarray) -> dict:
    """结尾卡画面证据: 二维码 / 密集文字 (方块内强边缘占比) 和居中大字 (横带文字分数)。"""
    g = _gray(frame) * 255
    strong = np.abs(np.diff(g, axis=1)) > 60
    h, w = strong.shape
    cs = max(4, h // 4)
    dens = [
        float(strong[y : y + cs, x : x + cs].mean())
        for y in range(0, h - cs + 1, max(1, cs // 2))
        for x in range(0, w - cs + 1, max(1, cs // 2))
    ]
    text = 0.0
    try:
        from reference_analyzer import text_band_features

        text = max(v[0] for v in text_band_features(g).values())
    except Exception:  # reference_analyzer 不可用时只看二维码 / 密集文字
        pass
    qr = max(dens, default=0.0)
    return {"qr_density": round(qr, 3), "text_score": round(float(text), 3),
            "has_card_content": qr >= CTA_QR_DENSITY or text >= CTA_TEXT_SCORE}  # fmt: skip


def detect_cta_card(tl, feat, frame_at) -> dict:
    """
    最后一个镜头是不是片尾 CTA 卡: 镜头 ≤ CTA_MAX_S + 1s (允许叠化进入), 其中静止部分 2–5s,
    画面里有二维码或大字。frame_at(t) 返回该时刻的一帧 (RGB uint8), 取不到返回 None。
    """
    if feat is None or not len(feat["times"]):
        return None
    dur = tl["duration"]
    last_cut = max([0.0] + [c for c in tl["cuts"] if c < dur - 0.2])
    if dur - last_cut > CTA_MAX_S + 1.0:
        return None
    moves = [c for c in tl.get("changes") or [] if last_cut < c < dur - 0.05]
    start = max([last_cut] + moves)
    still = dur - start
    if not (CTA_MIN_S <= still <= CTA_MAX_S):
        return None
    fr = frame_at(start + still / 2)
    if fr is None:
        return None
    ev = card_evidence(fr)
    if not ev["has_card_content"]:
        return None
    return {"start": round(start, 2), "end": round(dur, 2), "dur": round(still, 2),
            "qr_density": ev["qr_density"], "text_score": ev["text_score"]}  # fmt: skip


def check_dead_zones(tl, dead_s, issues):
    """死区: 相邻两个新信息之间 ≥ dead_s 秒 (画面没变、没有新字幕 / 插卡 / 切镜)。"""
    if not tl["visual_known"] and not tl["texts_known"]:
        return []
    pts = [0.0] + info_events(tl) + [tl["duration"]]
    zones = []
    holds = _hold_spans(tl)
    for a, b in zip(pts, pts[1:]):
        # 有意停留 (标题卡 / 片尾 CTA 卡, hook.md: CTA 卡至少停 3s) 不算; 只看没被覆盖的部分
        if b - a - _covered(a, b, [(s - HOLD_PAD_S, e + HOLD_PAD_S) for s, e in holds]) < dead_s:
            continue
        if b - a >= dead_s:
            sp = speech_ratio(tl["speech_segments"], a, b)
            sev = "medium" if sp >= 0.6 or not tl["speech_known"] else "high"
            z = {
                "start": round(a, 2),
                "end": round(b, 2),
                "dur": round(b - a, 2),
                "speech": round(sp, 2),
            }
            zones.append(z)
            if not tl["speech_known"]:
                what = "画面静止"
            else:
                what = "画面静止但一直有人声" if sp >= 0.6 else "画面静止且没有人声"
            issues.append(_issue("dead_zone", "死区", sev, a, b,
                                 f"{a:.1f}–{b:.1f}s 共 {b - a:.1f}s {what}, 没有新字幕 / 插卡 / 切镜",
                                 f"在 {a + min(2.0, (b - a) / 2):.1f}s 附近插 B-roll、关键词大字或 1.25x 推近; "
                                 "没有内容就剪短"))  # fmt: skip
    return zones


def _flat(feat, i) -> bool:
    return feat["luma_std"][i] < 0.04 and (feat["lab"][i, 0] < 8 or feat["lab"][i, 0] > 92)


def _flat_flash(feat, i) -> bool:
    """这一帧是闪黑 / 闪白: 几乎纯色 (极暗或极亮), 而且这段纯色很短 (≤ DIP_MAX_S, 不是白底镜头)。"""
    if not _flat(feat, i):
        return False
    t, n = feat["times"], len(feat["times"])
    lo = hi = i
    while lo > 0 and _flat(feat, lo - 1) and t[i] - t[lo] <= DIP_MAX_S:
        lo -= 1
    while hi + 1 < n and _flat(feat, hi + 1) and t[hi] - t[lo] <= DIP_MAX_S:
        hi += 1
    return float(t[hi] - t[lo]) <= DIP_MAX_S


def check_continuity(tl, feat, issues):
    """相邻镜头亮度 / 色温 / 色偏跳变 (切点前后 0.15s 外各取一帧比较)。
    闪黑 / 闪白两侧不算; 暗部要求绝对差 ≥ JUMP_DL_MIN; 跳变占切点多数时视为有意的段落配色, 合并成一条。"""
    out = []
    if feat is None or not len(feat["times"]):
        return out
    t = feat["times"]
    found = []
    hints = tl.get("review_hints") or {}
    bounds = []
    if hints.get("continuity") == "per_section":
        bounds = sorted({round(x, 3) for s, e in hints.get("sections") or [] for x in (s, e)})
    for c in tl["cuts"]:
        before = np.where(t <= c - 0.15)[0]
        after = np.where(t >= c + 0.15)[0]
        if not len(before) or not len(after):
            continue
        ib, ia = before[-1], after[0]
        a, b = feat["lab"][ib], feat["lab"][ia]
        dL, da, db = float(b[0] - a[0]), float(b[1] - a[1]), float(b[2] - a[2])
        row = {"t": c, "dL": round(dL, 1), "da": round(da, 1), "db": round(db, 1)}
        out.append(row)
        if bounds and any(abs(c - x) <= SECTION_SNAP_S for x in bounds):
            row["section_boundary"] = True  # 分镜表声明按段落配色: 段与段之间的跳变不报
            continue
        if min(a[0], b[0]) < 8 or _flat_flash(feat, ib) or _flat_flash(feat, ia):
            continue  # 有一侧是黑场 / 白场 (闪黑闪白、章节黑场), 不算
        # 闪白 / 闪黑夹在中间 (切点前后 0.6s 内有纯色帧): 两侧是两个独立的段落, 不比
        near = np.where(np.abs(t - c) <= 0.6)[0]
        if any(_flat_flash(feat, j) for j in near):
            continue
        rel = abs(dL) / max(a[0], b[0], 10)
        if abs(dL) >= JUMP_DL or (rel >= JUMP_REL and abs(dL) >= JUMP_DL_MIN):
            found.append(_issue("continuity", "亮度跳变", "medium", c - 0.5, c + 0.5,
                                f"{c:.1f}s 切镜前后亮度 {a[0]:.0f} → {b[0]:.0f} (差 {rel:.0%})",
                                "两个镜头做调色匹配 (曝光 / 亮度), 或中间插过渡镜头"))  # fmt: skip
        if abs(db) >= JUMP_DB:
            side = "变暖 (偏黄)" if db > 0 else "变冷 (偏蓝)"
            found.append(_issue("continuity", "色温跳变", "low", c - 0.5, c + 0.5,
                                f"{c:.1f}s 切镜后色温突然{side}, b* 差 {db:+.0f}",
                                "统一白平衡 / 用同一滤镜, 或按参考帧做调色匹配"))  # fmt: skip
        if abs(da) >= JUMP_DA:
            found.append(_issue("continuity", "色偏跳变", "low", c - 0.5, c + 0.5,
                                f"{c:.1f}s 切镜后色偏 (绿↔品红) 变化 a* {da:+.0f}",
                                "校正色调 (Tint), 让相邻镜头肤色一致"))  # fmt: skip
    jump_cuts = sorted({round((i["start"] + i["end"]) / 2, 2) for i in found})
    if len(jump_cuts) >= SECTION_MIN_JUMPS and len(jump_cuts) >= SECTION_RATIO * max(1, len(out)):
        # 大多数切点都在换色: 段落式配色 (游戏关卡色 / MV 段落) 多半是有意的, 只提示一次
        issues.append(_issue("continuity", "段落配色切换", "low", jump_cuts[0] - 0.5,
                             jump_cuts[-1] + 0.5,
                             f"{len(jump_cuts)}/{len(out)} 个切点前后亮度或色调明显变化, "
                             "像是有意的段落式配色",
                             "确认是有意设计就忽略; 否则统一调色。分镜表 review.continuity = per_section 时只查段内"))  # fmt: skip
    else:
        issues.extend(found)
    return out


def _runs(mask: np.ndarray, hop: float = HOP):
    """布尔序列里连续为 True 的区间 [(s, e)] (秒)。"""
    out, on = [], None
    for i, v in enumerate(list(mask) + [False]):
        if v and on is None:
            on = i
        elif not v and on is not None:
            out.append((round(on * hop, 2), round(i * hop, 2)))
            on = None
    return out


def check_audio(tl, audio, issues):
    """静音空洞、BGM 压人声、配乐提前结束 / 没有淡出。"""
    m = {"evaluated": audio is not None}
    if audio is None:
        return m
    mix = audio["mix"]
    dur = tl["duration"]
    if not len(mix) or float(mix.max()) <= SILENCE_DB:
        issues.append(_issue("audio", "全片无声", "high", 0, dur, "整条视频没有可听见的声音",
                             "加配乐或旁白; 短视频静音播放也要有 BGM 兜底"))  # fmt: skip
        m["silent"] = True
        return m
    m["mix_peak_db"] = round(float(mix.max()), 1)
    m["mix_median_db"] = round(float(np.median(mix)), 1)
    holes = []
    for s, e in _runs(mix < SILENCE_DB):
        if e - s >= HOLE_S and s > 0.3 and e < dur - 0.3:
            holes.append({"start": s, "end": e, "dur": round(e - s, 2)})
            issues.append(_issue("audio", "静音空洞", "medium" if e - s >= 1.5 else "low", s, e,
                                 f"{s:.1f}–{e:.1f}s 有 {e - s:.1f}s 完全没有声音",
                                 "剪掉这段停顿, 或用环境声 / BGM 垫底 (环境声断档 ≤ 0.3s)"))  # fmt: skip
    m["holes"] = holes
    conflicts = []
    if audio.get("stems") and audio["voice"] is not None and audio["bgm"] is not None:
        voice, bgm = audio["voice"], audio["bgm"]
        vthr = max(-45.0, float(voice.max()) - 30)
        active = voice > vthr
        if active.any() and float(bgm.max()) > -80:
            gap = bgm - voice
            bad = active & (gap > -BGM_MARGIN_DB)
            for s, e in _runs(bad):
                if e - s < 1.0:
                    continue
                g = float(np.median(gap[int(s / HOP) : int(e / HOP)]))
                conflicts.append({"start": s, "end": e, "bgm_minus_voice_db": round(g, 1)})
            m["bgm_minus_voice_db"] = round(float(np.median(gap[active])), 1)
    elif audio.get("words"):
        g = _bed_vs_voice(mix, audio["words"], dur)
        if g is not None:
            m["bed_minus_voice_db"] = round(g, 1)
            if g > -BGM_MARGIN_DB:
                conflicts.append({"start": 0.0, "end": dur, "bgm_minus_voice_db": round(g, 1)})
    else:
        m["bgm_conflict"] = "skipped (没有分轨或转写, 无法区分人声和 BGM)"
    vol = audio.get("bgm_volume")
    for c in conflicts:
        g = c["bgm_minus_voice_db"]
        need = g + BGM_MARGIN_DB
        tip = f"BGM 再降 {need:.0f}dB 以上"
        if vol:
            tip += f" (音量 {vol:.2f} → {vol * 10 ** (-need / 20):.2f})"
        issues.append(_issue("audio", "BGM压人声", "high" if g > -6 else "medium", c["start"], c["end"],
                             f"{c['start']:.1f}–{c['end']:.1f}s 人声段 BGM 只比人声低 {-g:.0f}dB "
                             f"(要求 ≥ {BGM_MARGIN_DB:.0f}dB)",
                             tip + "; 或给 BGM 做人声段自动压低 (ducking)"))  # fmt: skip
    m["bgm_conflicts"] = conflicts
    for it in audio.get("bgm_items") or []:
        end = it["start"] + it["dur"]
        if end < dur - 0.5 and not any(
            o["start"] + o["dur"] >= dur - 0.5 for o in audio["bgm_items"]
        ):
            issues.append(_issue("ending", "配乐提前结束", "medium", end, dur,
                                 f"BGM 在 {end:.1f}s 就结束了, 最后 {dur - end:.1f}s 没有配乐",
                                 "配乐从结尾倒推对齐, 让最后一小节落在最后一个镜头上再淡出"))  # fmt: skip
        elif end >= dur - 0.5 and it["fade_out"] < 0.3:
            issues.append(_issue("ending", "配乐未淡出", "low", max(0, dur - 2), dur,
                                 "BGM 在片尾被硬切断, 没有淡出",
                                 "给 BGM 加 0.5–2s 淡出 (fade_out)"))  # fmt: skip
    return m


def _bed_vs_voice(mix, transcript, dur):
    """没有分轨时: 用转写的词间 / 句间停顿估 BGM 底噪, 和说话时电平比 (dB 差)。"""
    n = len(mix)
    word = np.zeros(n, bool)
    span = np.zeros(n, bool)
    segs = sorted(transcript, key=lambda s: s.get("start", 0))
    for seg in segs:
        span[int(seg["start"] / HOP) : int(seg["end"] / HOP) + 1] = True
        ws = seg.get("words") or []
        if not ws:
            word[int(seg["start"] / HOP) : int(seg["end"] / HOP) + 1] = True
        for w in ws:
            word[int(w["s"] / HOP) : max(int(w["s"] / HOP) + 1, int(w["e"] / HOP))] = True
    for a, b in zip(segs, segs[1:]):  # 句间短停顿也算底噪
        if 0 < b["start"] - a["end"] <= 1.5:
            span[int(a["end"] / HOP) : int(b["start"] / HOP)] = True
    gap = span & ~word
    if gap.sum() < 10 or word.sum() < 20:
        return None
    return float(np.median(mix[gap]) - np.median(mix[word]))


def check_ending(tl, audio, issues):
    dur = tl["duration"]
    m = {}
    last_info = max([0.0] + info_events(tl) + [e for _s, e in tl["speech_segments"]])
    last_info = min(last_info, dur)
    m["last_info_at"] = round(last_info, 2)
    m["tail_after_last_info"] = round(dur - last_info, 2)
    tail_texts = [x for x in tl["texts"] if x["end"] > dur - 3]
    cta_card = tl.get("cta_card")
    if cta_card:
        m["cta_card"] = cta_card
    sb_cta = [h for h in (tl.get("review_hints") or {}).get("holds") or []
              if "cta" in h["why"].lower() or "结尾" in h["why"]]  # fmt: skip
    if sb_cta:
        m["storyboard_cta"] = sb_cta
    tail_held = _covered(
        last_info, dur, [(s - HOLD_PAD_S, e + HOLD_PAD_S) for s, e in _hold_spans(tl)]
    )
    if dur - last_info - tail_held >= TAIL_DRAG_S and not tail_texts:
        issues.append(_issue("ending", "结尾拖沓", "medium", last_info, dur,
                             f"最后一个新信息在 {last_info:.1f}s, 之后又拖了 {dur - last_info:.1f}s",
                             f"把成片裁到 {last_info + 1.0:.1f}s 左右, 或在结尾放 3s CTA"))  # fmt: skip
    if audio is not None and len(audio["mix"]):
        quiet = audio["mix"] < SILENCE_DB
        k = 0
        while k < len(quiet) and quiet[len(quiet) - 1 - k]:
            k += 1
        m["tail_silence_s"] = round(k * HOP, 2)
        # 整条都静音时 check_audio 已经报了「全片无声」, 这里不再重复扣结尾分
        if k * HOP >= 1.5 and k < len(quiet):
            issues.append(_issue("ending", "片尾静音", "low", dur - k * HOP, dur,
                                 f"最后 {k * HOP:.1f}s 没有声音", "裁掉片尾静音或让 BGM 延续到结尾淡出"))  # fmt: skip
    if (cta_card or sb_cta) and not tl["texts_known"]:
        m["has_cta"] = True
    if tl["texts_known"]:
        cta = [x for x in tl["texts"] if x["end"] > dur * 0.8 and CTA_WORDS.search(x["text"])]
        cta += [(s, t) for s, t in tl["speech_text"] if s > dur * 0.8 and CTA_WORDS.search(t)]
        m["has_cta"] = bool(cta) or bool(cta_card) or bool(sb_cta)
        if not m["has_cta"] and dur >= 10:
            issues.append(_issue("ending", "没有CTA", "low", max(0, dur - 3), dur,
                                 "结尾没有行动号召 (关注 / 评论 / 下期预告)",
                                 "最后 2–3s 放 CTA 卡, 至少停留 3s"))  # fmt: skip
    return m


def score(issues, evaluated):
    by = {}
    for cat in WEIGHTS:
        if cat not in evaluated:
            by[cat] = None
            continue
        pts = 100 - sum(DEDUCT[i["severity"]] for i in issues if i["category"] == cat)
        by[cat] = max(0, pts)
    w = {k: v for k, v in WEIGHTS.items() if by[k] is not None}
    total = sum(by[k] * v for k, v in w.items()) / max(1e-6, sum(w.values())) if w else 0
    total = int(round(total))
    grade = "A" if total >= 85 else "B" if total >= 70 else "C" if total >= 55 else "D"
    return {"overall": total, "grade": grade, "by_category": by}


# ---------------- 组装 ----------------
def _video_size(info, width):
    w, h = info.get("width") or 16, info.get("height") or 9
    return width, max(2, int(round(width * h / w / 2)) * 2)


def build_timeline(draft_name=None, video=None, drafts_root=None, analysis=None, subtitles=None,
                   sample_fps=None, storyboard=None):  # fmt: skip
    """读取输入, 返回 (tl, feat, audio, ctx)。ctx 里放联系表取帧用的东西和 notes。"""
    if not draft_name and not video:
        raise InputError("需要 --draft 或 --video")
    notes = []
    ff = has_ffmpeg()
    draft = dtl = None
    ctx = {"notes": notes, "draft": None, "video": None, "tiles": {}}
    if draft_name:
        ddir = find_draft_dir(draft_name, drafts_root)
        try:
            draft, src = read_draft_content(ddir)
        except EncryptedDraftError as e:
            raise InputError(str(e)) from e
        dtl = draft_timeline(draft)
        ctx.update(draft=draft, draft_dir=ddir, draft_source=src)
    info = {}
    if video:
        if not os.path.exists(video):
            raise InputError(f"找不到视频: {video}")
        if not ff:
            raise InputError("--video 模式需要 ffmpeg / ffprobe")
        info = probe_video(video)
        if not info or not info.get("duration"):
            raise InputError(f"读不到视频信息: {video}")
        ctx["video"] = video
        ctx["video_info"] = info
    duration = float(info.get("duration") or (dtl or {}).get("duration") or 0)
    if duration <= 0:
        raise InputError("时长为 0, 没有可审的内容")

    # 画面
    times, frames = np.zeros(0), None
    if video:
        # 默认 10fps (0.2s 的快翻也能分开); 很长的视频把总帧数封顶, 内存 / 耗时不随时长无限涨
        fps = sample_fps or min(VIDEO_FPS, max(4.0, MAX_FRAMES / max(1.0, duration)))
        w, h = _video_size(info, ANALYSIS_W)
        times, frames = decode_video(video, fps, w, h)
    elif HAS_PIL:
        fps = sample_fps or 2.0
        times = np.round(np.arange(0, duration, 1 / fps), 3)
        frames, ctx["tiles"] = render_draft_frames(draft, times, ANALYSIS_W)
        media = [m.get("path") for m in (draft.get("materials") or {}).get("videos", [])]
        if not any(p and os.path.exists(p) for p in media):
            notes.append("草稿里的视频 / 图片素材都找不到, 画面类检查 (首帧 / 死区 / 色调) 跳过")
            frames, times, ctx["tiles"] = None, np.zeros(0), {}
    else:
        notes.append("未安装 Pillow, 不能伪渲染草稿画面; 画面类检查只用时间线 (或传 --video 成片)")
    feat = frame_features(times, frames) if frames is not None and len(frames) else None
    if video and feat is None:
        notes.append("视频画面解码失败, 画面类检查跳过")

    # 时间线语义
    tl = {"duration": round(duration, 3), "overlays": [], "texts": [], "speech_segments": [],
          "speech_text": [], "texts_known": False, "speech_known": False}  # fmt: skip
    if dtl:
        tl.update(cuts=dtl["cuts"], overlays=dtl["overlays"], texts=dtl["texts"], texts_known=True)
        tl["canvas"] = dtl["canvas"]
    else:
        tl["cuts"] = detect_cuts(feat) if feat is not None else []
        tl["canvas"] = [info.get("width"), info.get("height")]
    if subtitles:
        tl["texts"] = sorted(tl["texts"] + parse_srt(subtitles), key=lambda x: x["start"])
        tl["texts_known"] = True
    tl["changes"] = change_times(times, frames) if feat is not None else []
    tl["visual_known"] = feat is not None
    hint_genre, words = None, None
    if analysis:
        tr, hints = load_transcript(analysis)
        hint_genre = hints.get("scene_guess")
        tl["speech_segments"] = [(float(s["start"]), float(s["end"])) for s in tr]
        tl["speech_text"] = [(float(s["start"]), s.get("text") or "") for s in tr]
        tl["speech_known"] = True
        words = tr

    # 声音
    audio = None
    if not ff:
        notes.append("没有 ffmpeg, 音频检查跳过")
    else:
        stems = None
        if dtl and dtl["audio_items"]:
            stems = mix_stems(dtl["audio_items"], duration)
            if not stems["decoded"]:
                stems = None
                notes.append("草稿里的音频素材读不到, 分轨音频检查跳过")
        bgm_items = [i for i in (dtl or {}).get("audio_items", []) if i["stem"] == "bgm"]
        if video:
            mix = rms_db(decode_audio(video))
        elif stems is not None:
            mix = stems["mix"]
        elif dtl is not None and not dtl["audio_items"]:
            mix = np.full(max(1, int(duration / HOP)), -90.0)  # 草稿确实没有声音
        else:
            mix = None
        if mix is not None:
            audio = {"mix": mix, "stems": stems is not None, "words": words,
                     "voice": stems["voice"] if stems else None, "bgm": stems["bgm"] if stems else None,
                     "bgm_items": bgm_items,
                     "bgm_volume": bgm_items[0]["volume"] if bgm_items else None}  # fmt: skip
            if stems is not None and not tl["speech_known"] and float(stems["voice"].max()) > -80:
                thr = max(-45.0, float(stems["voice"].max()) - 30)
                tl["speech_segments"] = active_intervals(stems["voice"], thr)
                tl["speech_known"] = True
            elif stems is not None and not tl["speech_known"]:
                tl["speech_known"] = True  # 有分轨但没有人声轨: 确认无人声
    if not tl["speech_known"]:
        notes.append(
            "没有转写也没有人声分轨, 「前 3 秒人声」只能按文字判断 (传 --analysis analysis.json)"
        )
    # 分镜表 review 提示: --storyboard 优先, 草稿模式下再找草稿目录里的分镜表
    sb_path = storyboard
    if sb_path and not os.path.exists(sb_path):
        raise InputError(f"找不到分镜表: {sb_path}")
    if not sb_path and ctx.get("draft_dir"):
        sb_path = find_storyboard(ctx["draft_dir"], draft_name)
    tl["review_hints"] = load_review_hints(sb_path) if sb_path else {}
    if sb_path and not tl["review_hints"]:
        notes.append(f"分镜表 {os.path.basename(sb_path)} 没有 review 块, 按通用规则审")
    ctx["hint_genre"] = hint_genre
    ctx["times"] = times
    return tl, feat, audio, ctx


def review(draft=None, video=None, drafts_root=None, analysis=None, subtitles=None, genre="auto",
           dead_zone=3.5, sample_fps=None, out_dir=None, make_sheet=True, sheet_mode="auto",
           max_tiles=120, storyboard=None) -> dict:  # fmt: skip
    """审片主入口, 返回报告 dict (同时写 report.json / viewer_sheet.png 到 out_dir)。"""
    tl, feat, audio, ctx = build_timeline(
        draft, video, drafts_root, analysis, subtitles, sample_fps, storyboard
    )
    issues = []
    evaluated = {"pacing", "ending"}
    if tl["visual_known"] or tl["texts_known"] or tl["speech_known"]:
        evaluated |= {"hook"}
    hook = check_hook(tl, feat, audio, issues)
    tl["cta_card"] = detect_cta_card(tl, feat, lambda t: _card_frame(ctx, t))
    shots0 = shots_from(tl)
    mean0 = float(np.mean([s["dur"] for s in shots0])) if shots0 else None
    g = genre if genre in GENRE_SHOT else guess_genre(tl["duration"], mean0, tl["speech_segments"],
                                                      ctx["hint_genre"], feat)  # fmt: skip
    pacing = check_pacing(tl, feat, g, issues)
    zones = check_dead_zones(tl, dead_zone, issues)
    if tl["visual_known"] or tl["texts_known"]:
        evaluated.add("dead_zone")
    cont = check_continuity(tl, feat, issues)
    if feat is not None:
        evaluated.add("continuity")
    aud = check_audio(tl, audio, issues)
    if audio is not None:
        evaluated.add("audio")
    ending = check_ending(tl, audio, issues)
    issues.sort(key=lambda i: (i["start"], -SEVERITY_RANK[i["severity"]]))
    for k, i in enumerate(issues, 1):
        i["id"] = f"Q{k:03d}"
    report = {
        "version": 1,
        "tool": "quality_review",
        "source": {
            "mode": "+".join(x for x in ("draft" if draft else "", "video" if video else "") if x),
            "draft": ctx.get("draft_dir"), "draft_source": ctx.get("draft_source"),
            "video": os.path.abspath(video) if video else None,
            "duration": tl["duration"], "canvas": tl.get("canvas"),
        },
        "genre": g,
        "review_hints": tl["review_hints"] or None,
        "score": score(issues, evaluated),
        "hook": hook,
        "pacing": pacing,
        "dead_zones": zones,
        "continuity": {"cuts": cont},
        "audio": aud,
        "ending": ending,
        "issues": issues,
        "timeline": {
            "cuts": tl["cuts"], "overlays": tl["overlays"], "texts": tl["texts"],
            "speech": [{"start": s, "end": e} for s, e in tl["speech_segments"]],
            "speech_text": [{"start": s, "text": t} for s, t in tl["speech_text"]],
        },
        "notes": ctx["notes"],
        "sheet": None,
    }  # fmt: skip
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        if make_sheet and HAS_PIL:
            report["sheet"] = contact_sheet(report, ctx, os.path.join(out_dir, "viewer_sheet.png"),
                                            sheet_mode, max_tiles)  # fmt: skip
        with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        report["report_path"] = os.path.join(out_dir, "report.json")
    return report


# ---------------- 观众视角联系表 ----------------
SEV_COLOR = {"high": (235, 64, 52), "medium": (245, 150, 40), "low": (230, 210, 60)}


def tile_times(report, mode, max_tiles):
    """联系表取帧 [(t, 起, 止)]。按镜头取帧时也固定带上 0/1/2/3s, 钩子区不会被跳过。"""
    dur = report["source"]["duration"]
    shots = report["pacing"]["shots"]
    if mode == "auto":
        mode = "second" if dur <= max_tiles else "shot"
    if mode == "shot" and shots:
        hook = [(t, t, min(dur, t + 1.0)) for t in HOOK_TILES if t < dur]
        room = max(1, max_tiles - len(hook))
        step = max(1, math.ceil(len(shots) / room))
        picks = list(hook)
        for s in shots[::step]:
            t = round((s["start"] + s["end"]) / 2, 2)
            if all(abs(t - h[0]) >= 0.5 for h in hook):
                picks.append((t, s["start"], s["end"]))
        return sorted(picks)
    step = max(1, math.ceil(dur / max_tiles))
    return [
        (float(t), float(t), min(dur, float(t + step))) for t in range(0, int(math.ceil(dur)), step)
    ]


def _card_frame(ctx, t):
    """片尾卡判断用的一帧: 视频模式抽 320 宽, 草稿模式用联系表伪渲染帧。"""
    if ctx.get("video"):
        w, h = _video_size(ctx.get("video_info") or {}, 320)
        return grab_frame(ctx["video"], t, w, h)
    if ctx.get("draft") is not None and HAS_PIL:
        _small, more = render_draft_frames(ctx["draft"], [t], ANALYSIS_W)
        return next(iter(more.values()), None)
    return None


def _tile_frame(ctx, t, th):
    if ctx.get("video"):
        return grab_frame(ctx["video"], t, TILE_W, th)
    key = round(t, 2)
    if key not in ctx["tiles"] and ctx.get("draft") is not None and HAS_PIL and ctx["tiles"]:
        _small, more = render_draft_frames(ctx["draft"], [t], ANALYSIS_W)
        ctx["tiles"].update(more)
    return ctx["tiles"].get(key)


def contact_sheet(report, ctx, out_path, mode="auto", max_tiles=120, cols=6):
    """每秒 / 每镜头一帧: 时间戳、镜头号、节奏能量条、问题标注 (边框颜色 = 最严重的问题)。"""
    from layout_preview import pick_font

    cw, ch = report["source"].get("canvas") or [1080, 1920]
    cw, ch = cw or 1080, ch or 1920
    th = max(2, int(round(TILE_W * ch / cw / 2)) * 2)
    picks = tile_times(report, mode, max_tiles)
    if not picks:
        return None
    if ctx.get("video"):  # 并发抽帧 (每帧一个 ffmpeg 进程, 串行时是联系表的主要耗时)
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as pool:
            got = list(pool.map(lambda p: _tile_frame(ctx, p[0], th), picks))
        frames = dict(zip(range(len(picks)), got))
    else:
        frames = None
    curve = {int(r["t"]): r["energy"] for r in report["pacing"]["curve"]}
    shots = report["pacing"]["shots"]
    cap_h, bar_h, pad = 58, 8, 6
    cell_w, cell_h = TILE_W + pad * 2, th + bar_h + cap_h + pad * 2
    rows = math.ceil(len(picks) / cols)
    head = 64
    sheet = Image.new("RGB", (cols * cell_w, head + rows * cell_h), (24, 24, 24))
    d = ImageDraw.Draw(sheet)
    f_big, f, f_small = pick_font(24), pick_font(17), pick_font(15)
    sc = report["score"]
    d.text((10, 8), f"观众视角联系表  总分 {sc['overall']} ({sc['grade']})  {report['genre']}  "
                    f"{report['source']['duration']:.1f}s  问题 {len(report['issues'])} 个",
           font=f_big, fill=(240, 240, 240))  # fmt: skip
    d.text((10, 38), "边框: 红=高 橙=中 黄=低 · 顶部蓝条=前 3 秒钩子区 · 底部绿条=节奏能量",
           font=f_small, fill=(180, 180, 180))  # fmt: skip
    for k, (t, s, e) in enumerate(picks):
        r, c = divmod(k, cols)
        x, y = c * cell_w + pad, head + r * cell_h + pad
        fr = frames[k] if frames is not None else _tile_frame(ctx, t, th)
        if fr is not None:
            im = Image.fromarray(np.asarray(fr, np.uint8)).resize((TILE_W, th))
            sheet.paste(im, (x, y))
        else:
            d.rectangle((x, y, x + TILE_W, y + th), fill=(50, 50, 50))
            d.text((x + 8, y + th // 2), "无画面", font=f, fill=(200, 200, 200))
        hits = [i for i in report["issues"] if i["start"] < max(e, s + 0.01) and i["end"] > s
                and i["end"] - i["start"] < report["source"]["duration"] * 0.9]  # fmt: skip
        if hits:
            worst = max(hits, key=lambda i: SEVERITY_RANK[i["severity"]])
            d.rectangle((x - 3, y - 3, x + TILE_W + 2, y + th + 2), outline=SEV_COLOR[worst["severity"]],
                        width=4)  # fmt: skip
        if t < HOOK_S:
            d.rectangle((x, y, x + TILE_W, y + 5), fill=(60, 140, 255))
        idx = next((sh["index"] for sh in shots if sh["start"] <= t < sh["end"]), None)
        label = f"{t:.1f}s" + (f"  #{idx}" if idx else "")
        d.rectangle((x, y + 6, x + 8 + d.textlength(label, font=f), y + 30), fill=(0, 0, 0))
        d.text((x + 4, y + 8), label, font=f, fill=(255, 230, 120))
        en = curve.get(int(t), 0.0)
        d.rectangle((x, y + th + 2, x + TILE_W, y + th + bar_h), fill=(55, 55, 55))
        d.rectangle(
            (x, y + th + 2, x + int(TILE_W * min(1.0, en)), y + th + bar_h), fill=(80, 200, 120)
        )
        tags = []
        for i in sorted(hits, key=lambda i: -SEVERITY_RANK[i["severity"]]):
            if i["type"] not in tags:
                tags.append(i["type"])
        for j, tag in enumerate(tags[:3]):
            sev = next(i["severity"] for i in hits if i["type"] == tag)
            d.text((x, y + th + bar_h + 4 + j * 18), "• " + tag, font=f_small, fill=SEV_COLOR[sev])
    sheet.save(out_path)
    return out_path


# ---------------- CLI ----------------
def summarize(report) -> str:
    sc = report["score"]
    lines = [
        f"成片审片: {report['source']['draft'] or report['source']['video']}",
        f"  时长 {report['source']['duration']:.1f}s  场景 {report['genre']}  "
        f"总分 {sc['overall']} ({sc['grade']})",
        "  分项: " + "  ".join(f"{CATEGORY_CN[k]} {'—' if v is None else v}"
                             for k, v in sc["by_category"].items()),
    ]  # fmt: skip
    st = report["pacing"]["stats"]
    if st:
        lines.append(
            f"  镜头 {st['count']} 个, 平均 {st['mean']}s, 中位 {st['median']}s, 最长 {st['max']}s"
        )
    if report["issues"]:
        lines.append("\n===== 问题 =====")
        for i in report["issues"]:
            lines.append(
                f"  [{i['severity']}] {i['start']:.1f}–{i['end']:.1f}s [{i['type']}] {i['message']}"
            )
            lines.append(f"      → {i['suggestion']}")
    else:
        lines.append("✅ 未发现钩子 / 节奏 / 死区 / 色调 / 音频 / 结尾问题")
    for n in report["notes"]:
        lines.append(f"  ⚠️ {n}")
    if report.get("sheet"):
        lines.append(f"\n观众视角联系表: {report['sheet']} (用 Read 看图, 判断哪里会划走)")
    if report.get("report_path"):
        lines.append(f"报告: {report['report_path']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="成片质量审片: 钩子 / 节奏 / 死区 / 色调跳变 / 音频 / 结尾"
    )
    ap.add_argument("--draft", help="草稿名或草稿目录")
    ap.add_argument("--drafts-root", help="草稿根目录 (默认自动探测剪映草稿目录)")
    ap.add_argument("--video", help="成片 mp4 (和 --draft 同时给时, 画面 / 声音取成片)")
    ap.add_argument(
        "--analysis", help="analyze_footage.py 的 analysis.json 或转写 JSON (人声 / 开场白检查)"
    )
    ap.add_argument("--subtitles", help="字幕 SRT (视频模式下当作屏幕文字)")
    ap.add_argument("--storyboard", help="分镜表 JSON (读 review 块: 钩子类型 / 有意停留 / 按段落查色调);"
                    " 草稿模式默认找草稿目录里的 *storyboard*.json")  # fmt: skip
    ap.add_argument(
        "--genre", default="auto", choices=["auto", *GENRE_SHOT], help="场景类型 (决定镜头时长区间)"
    )
    ap.add_argument("--dead-zone", type=float, default=3.5, help="死区阈值秒数 (默认 3.5)")
    ap.add_argument("--sample-fps", type=float, help="画面采样帧率 (默认视频 4 / 草稿 2)")
    ap.add_argument("--sheet-mode", default="auto", choices=["auto", "second", "shot"])
    ap.add_argument("--max-tiles", type=int, default=120)
    ap.add_argument("--out", help="输出目录 (默认 <tmp>/jy_quality_review/<名称>)")
    ap.add_argument("--report-only", action="store_true", help="不生成联系表")
    ap.add_argument("--fail-on", default="medium", choices=["low", "medium", "high"],
                    help="出现该严重度及以上的问题时退出码为 1")  # fmt: skip
    ap.add_argument("--json", action="store_true", help="stdout 输出 {ok, code, reason, data}")
    args = ap.parse_args(argv)
    name = os.path.basename(os.path.normpath(args.draft or os.path.splitext(args.video or "x")[0]))
    out = args.out or os.path.join(tempfile.gettempdir(), "jy_quality_review", name)
    try:
        report = review(args.draft, args.video, args.drafts_root, args.analysis, args.subtitles,
                        args.genre, args.dead_zone, args.sample_fps, out, not args.report_only,
                        args.sheet_mode, args.max_tiles, args.storyboard)  # fmt: skip
    except InputError as e:
        if args.json:
            emit_result(make_result(False, "input_error", str(e)), True)
        else:
            print(f"❌ {e}", file=sys.stderr)
        return 2
    bad = [
        i for i in report["issues"] if SEVERITY_RANK[i["severity"]] >= SEVERITY_RANK[args.fail_on]
    ]
    if args.json:
        code = "issues_found" if bad else "ok"
        emit_result(make_result(not bad, code, f"{len(bad)} 个问题" if bad else "", report), True)
    else:
        print(summarize(report))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
