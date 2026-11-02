# -*- coding: utf-8 -*-
"""
素材质量打分 (T6): 按固定间隔抽帧, 给每个采样点和每一段打 0–100 分, 并给出问题标签。

    python shot_quality.py <视频> [--every 0.5] [--ranges "0-3,5.5-8"] [--scenes analysis.json]
                           [--require-face] [-o quality.json] [--json]

每个采样点测量 (先把短边降到 360 像素):
  - 清晰度: 灰度 Laplacian 方差 (越大越清晰); 取 4x4 分块里最清晰的 4 块 (对焦主体), 对数映射到 0–100
  - 抖动:   采样点起连续 4 帧, 相邻帧相位相关 (cv2.phaseCorrelate) 求位移;
            位移的均值是运镜速度 (motion_px), 位移偏离均值的幅度是抖动 (shake_px)。
            平稳的推拉摇移位移一致, 不算抖; 手持抖动位移方向乱跳
  - 曝光:   平均亮度、过曝 (>= 250) / 欠曝 (<= 8) 像素占比、亮度标准差 (对比度)
  - 人脸:   OpenCV Haar 级联 (同 analyze_footage.py): 正脸 = 正对镜头, 只有侧脸 = 没正对;
            脸框上半部找眼睛, 判断眼睛是否可见。cv2 没带级联文件时 face_detector = "unavailable"
  - 转场:   这 4 帧里亮度或直方图突变 → 镜头切换 / 淡入淡出, 不适合当封面或关键帧

汇总分 = 清晰 40% + 稳定 25% + 曝光 35%; 检测到人脸时再按 8:2 混入人脸分。
黑帧 / 转场帧会被压分。--require-face 时没有人脸的采样点额外扣分 (口播素材挑镜头用)。

输出 (data):
  {"source", "video": {width, height, fps, duration, frames}, "every_s", "face_detector",
   "samples": [{t, score, sharpness{lap_var, lap_focus, score}, stability{shake_px, motion_px, score},
                exposure{mean, std, over, under, score}, face{count, box, facing, eyes, size, score} | null,
                transition, issues[]}],
   "summary": {score, median, p10, issues{标签: 占比}, best_t, worst_t},
   "segments": [{start, end, score, min, best_t, worst_t, issues[], n}]}   # 有 --ranges / --scenes 时
"""

import argparse
import json
import math
import os
import sys
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.cli_protocol import emit_result, make_result  # noqa: E402

SHORT_SIDE = 360  # 分析分辨率 (短边)
PAIR_FRAMES = 4  # 每个采样点连续读几帧 (算抖动 / 转场)
WEIGHTS = {"sharpness": 0.40, "stability": 0.25, "exposure": 0.35}
FACE_WEIGHT = 0.20
# 清晰度: Laplacian 方差 (360p 灰度) 的对数区间 → 0–100
LAP_LOW, LAP_HIGH = 15.0, 400.0
SHAKE_FULL = 4.0  # 抖动位移 (像素 / 帧, 360p) 达到它稳定分为 0
ISSUE_SHARE = 0.3  # 一段里某问题出现在 >= 30% 采样点就记到段的问题里

ISSUES = {
    "blur": "模糊",
    "shaky": "抖动",
    "fast_motion": "运镜过快",
    "overexposed": "过曝",
    "underexposed": "欠曝",
    "black": "黑帧",
    "low_contrast": "低对比",
    "transition": "转场",
    "no_face": "无人脸",
    "not_facing": "未正对镜头",
    "eyes_hidden": "看不到眼睛",
    "face_small": "人脸过小",
    "face_cut": "人脸贴边",
}


def _cv2():
    try:
        import cv2
    except ImportError as e:  # requirements.txt 里有 opencv-python, 这里只是给出清楚的提示
        raise RuntimeError("需要 opencv-python: pip install opencv-python") from e
    return cv2


# ---------------------------------------------------------------- 人脸级联


def _cascade_dirs() -> List[str]:
    cv2 = _cv2()
    dirs = []
    if os.environ.get("JY_HAARCASCADE_DIR"):
        dirs.append(os.environ["JY_HAARCASCADE_DIR"])
    data = getattr(cv2, "data", None)
    if data is not None and getattr(data, "haarcascades", None):
        dirs.append(data.haarcascades)
    return dirs


@lru_cache(maxsize=None)
def _cascade(name: str):
    """
    加载 OpenCV 自带的 Haar 级联; 找不到返回 None。
    opencv-python 5.x 去掉了 CascadeClassifier 和 data/ 下的级联文件, 这时人脸检测降级为不可用。
    """
    cv2 = _cv2()
    if not hasattr(cv2, "CascadeClassifier"):
        return None
    for d in _cascade_dirs():
        p = os.path.join(d, name)
        if os.path.exists(p):
            c = cv2.CascadeClassifier(p)
            if not c.empty():
                return c
    return None


def face_detector_status() -> str:
    return "haar" if _cascade("haarcascade_frontalface_default.xml") is not None else "unavailable"


def _skin_ratio(bgr, box) -> Optional[float]:
    """脸框中心区域里肤色像素 (YCrCb) 的占比; 画面接近黑白时返回 None (不做肤色过滤)。"""
    import numpy as np

    cv2 = _cv2()
    x, y, fw, fh = box
    roi = bgr[y + fh // 5 : y + fh * 4 // 5, x + fw // 5 : x + fw * 4 // 5]
    if roi.size == 0:
        return None
    if float(cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)[:, :, 1].mean()) < 18:
        return None
    ycc = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)
    cr, cb = ycc[:, :, 1], ycc[:, :, 2]
    skin = (cr >= 133) & (cr <= 180) & (cb >= 77) & (cb <= 135)
    return float(np.count_nonzero(skin)) / skin.size


def _count_eyes(g, box) -> int:
    """脸框上 60% 里找眼睛; ROI 先放大到 200 像素宽 (眼睛级联最小窗口 20 像素)。没有级联返回 -1。"""
    cv2 = _cv2()
    eye = _cascade("haarcascade_eye_tree_eyeglasses.xml") or _cascade("haarcascade_eye.xml")
    if eye is None:
        return -1
    x, y, fw, fh = box
    roi = g[y : y + int(fh * 0.6), x : x + fw]
    if roi.size == 0:
        return 0
    k = 200 / max(1, roi.shape[1])
    if k > 1:
        roi = cv2.resize(roi, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
    rw = roi.shape[1]
    found = eye.detectMultiScale(
        roi,
        1.1,
        5,
        minSize=(int(rw * 0.12), int(rw * 0.12)),
        maxSize=(int(rw * 0.5), int(rw * 0.5)),
    )
    return min(2, len(found))


def detect_face(gray, bgr=None) -> Optional[dict]:
    """
    最大的一张脸: {count, box[x0,y0,x1,y1] (画面比例, 左上为 0), facing, eyes, size, skin}。
    正脸级联命中 = 正对镜头; 只有侧脸级联命中 = 没正对。传了 bgr 时用肤色占比过滤误检
    (植物、手、纹理常被 Haar 当成脸)。没检测到返回 {"count": 0}; 级联不可用返回 None。
    """
    cv2 = _cv2()
    front = _cascade("haarcascade_frontalface_default.xml")
    if front is None:
        return None
    h, w = gray.shape[:2]
    g = cv2.equalizeHist(gray)
    m = int(min(w, h) * 0.1)

    def keep(found):
        out = []
        for f in found:
            f = tuple(int(v) for v in f)
            sr = _skin_ratio(bgr, f) if bgr is not None else None
            if sr is None or sr >= 0.3:
                out.append((f, sr))
        return out

    faces = keep(front.detectMultiScale(g, 1.1, 6, minSize=(m, m)))
    facing = bool(faces)
    if not faces:  # 侧脸: 级联只认一个朝向, 镜像再查一次
        prof = _cascade("haarcascade_profileface.xml")
        if prof is not None:
            faces = keep(prof.detectMultiScale(g, 1.1, 8, minSize=(m, m)))
            if not faces:
                flipped = prof.detectMultiScale(cv2.flip(g, 1), 1.1, 8, minSize=(m, m))
                faces = keep([(w - x - fw, y, fw, fh) for x, y, fw, fh in flipped])
    if not faces:
        return {"count": 0}
    (x, y, fw, fh), sr = max(faces, key=lambda f: f[0][2] * f[0][3])
    return {
        "count": len(faces),
        "box": [round(x / w, 3), round(y / h, 3), round((x + fw) / w, 3), round((y + fh) / h, 3)],
        "facing": facing,
        "eyes": _count_eyes(g, (x, y, fw, fh)),
        "size": round(fh / h, 3),
        "skin": None if sr is None else round(sr, 2),
    }


def face_score(face: Optional[dict]) -> Optional[float]:
    """正对镜头 + 两眼可见 + 大小合适 + 不贴边 → 高分。没有脸返回 None。"""
    if not face or not face.get("count"):
        return None
    s = 100.0 if face["facing"] else 55.0
    eyes = face.get("eyes", -1)
    if eyes == 0:
        s *= 0.6
    elif eyes == 1:
        s *= 0.85
    size = face.get("size", 0)
    if size < 0.12:  # 脸高不到画面 12%: 远景, 看不清表情
        s *= 0.6 + 0.4 * size / 0.12
    x0, y0, x1, y1 = face["box"]
    if min(x0, y0, 1 - x1, 1 - y1) < 0.01:
        s *= 0.75
    return round(s, 1)


# ---------------------------------------------------------------- 单帧指标


def _resize(frame, short=SHORT_SIDE):
    cv2 = _cv2()
    h, w = frame.shape[:2]
    k = short / min(h, w)
    if k >= 1:
        return frame
    return cv2.resize(
        frame, (max(2, round(w * k)), max(2, round(h * k))), interpolation=cv2.INTER_AREA
    )


def sharpness(gray) -> dict:
    """
    Laplacian 方差。整帧方差会被纯色墙 / 天空 / 浅景深背景拉低, 所以按 4x4 分块,
    取方差最大的 4 块的均值 (lap_focus, 对焦主体所在的区域) 来打分; lap_var 是整帧方差, 仅供参考。
    """
    import numpy as np

    cv2 = _cv2()
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    h, w = lap.shape[:2]
    tiles = sorted(
        (
            float(lap[i * h // 4 : (i + 1) * h // 4, j * w // 4 : (j + 1) * w // 4].var())
            for i in range(4)
            for j in range(4)
        ),
        reverse=True,
    )
    focus = float(np.mean(tiles[:4]))
    lo, hi = math.log10(LAP_LOW), math.log10(LAP_HIGH)
    s = (math.log10(max(focus, 1e-3)) - lo) / (hi - lo)
    return {
        "lap_var": round(float(lap.var()), 1),
        "lap_focus": round(focus, 1),
        "score": round(100 * min(1.0, max(0.0, s)), 1),
    }


def exposure(gray) -> dict:
    import numpy as np

    mean, std = float(gray.mean()), float(gray.std())
    over = float(np.count_nonzero(gray >= 250)) / gray.size
    under = float(np.count_nonzero(gray <= 8)) / gray.size
    s = 1.0 - max(0.0, abs(mean - 125) - 40) / 85  # 均值在 85–165 不扣分
    s -= 1.5 * max(0.0, over - 0.02) + 1.5 * max(0.0, under - 0.05)
    if std < 20:  # 灰蒙蒙
        s -= (20 - std) / 40
    return {
        "mean": round(mean, 1),
        "std": round(std, 1),
        "over": round(over, 3),
        "under": round(under, 3),
        "score": round(100 * min(1.0, max(0.0, s)), 1),
    }


def stability(grays: Sequence) -> dict:
    """相邻帧相位相关位移: 均值 = 运镜, 偏离均值 = 抖动。"""
    import numpy as np

    cv2 = _cv2()
    vecs = []
    win = None
    for a, b in zip(grays, grays[1:]):
        if a.shape != b.shape or a.std() < 2 or b.std() < 2:  # 黑帧 / 纯色帧没法配准
            continue
        if win is None:
            win = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_32F)
        (dx, dy), resp = cv2.phaseCorrelate(a.astype(np.float32), b.astype(np.float32), win)
        if not (math.isfinite(dx) and math.isfinite(dy)) or resp < 0.02:
            continue
        vecs.append((dx, dy))
    if not vecs:
        return {"shake_px": None, "motion_px": None, "score": None}
    v = np.array(vecs)
    mean = v.mean(axis=0)
    motion = float(np.hypot(*mean))
    shake = float(np.hypot(*(v - mean).T).mean()) if len(v) > 1 else 0.0
    s = 1.0 - shake / SHAKE_FULL - max(0.0, motion - 12) / 30  # 每帧 > 12px 的运镜也会糊
    return {
        "shake_px": round(shake, 2),
        "motion_px": round(motion, 2),
        "score": round(100 * min(1.0, max(0.0, s)), 1),
    }


def is_transition(grays: Sequence) -> bool:
    """连续几帧里亮度跳变或直方图差异大 → 切换 / 淡入淡出。"""
    cv2 = _cv2()
    if len(grays) < 2:
        return False
    means = [float(g.mean()) for g in grays]
    if max(means) - min(means) > 18:
        return True
    hists = [cv2.calcHist([g], [0], None, [32], [0, 256]) for g in (grays[0], grays[-1])]
    for h in hists:
        cv2.normalize(h, h)
    return float(cv2.compareHist(hists[0], hists[1], cv2.HISTCMP_BHATTACHARYYA)) > 0.35


def measure(frames: Sequence, require_face: bool = False) -> dict:
    """对一个采样点 (连续几帧, BGR) 打分。frames[0] 是采样帧。"""
    cv2 = _cv2()
    small = [_resize(f) for f in frames]
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) if f.ndim == 3 else f for f in small]
    g0 = grays[0]
    sh, ex, st = sharpness(g0), exposure(g0), stability(grays)
    face = detect_face(g0, small[0] if small[0].ndim == 3 else None)
    trans = is_transition(grays)

    parts = {"sharpness": sh["score"], "exposure": ex["score"]}
    if st["score"] is not None:
        parts["stability"] = st["score"]
    total_w = sum(WEIGHTS[k] for k in parts)
    score = sum(WEIGHTS[k] * v for k, v in parts.items()) / total_w
    fs = face_score(face)
    if fs is not None:
        score = (1 - FACE_WEIGHT) * score + FACE_WEIGHT * fs

    issues = []
    black = ex["mean"] < 16 and ex["std"] < 8
    if sh["score"] < 35 and not black:
        issues.append(ISSUES["blur"])
    if st["shake_px"] is not None and st["shake_px"] > 1.5:
        issues.append(ISSUES["shaky"])
    if st["motion_px"] is not None and st["motion_px"] > 12:
        issues.append(ISSUES["fast_motion"])
    if black:
        issues.append(ISSUES["black"])
        score = min(score, 5.0)
    elif ex["over"] > 0.15 or ex["mean"] > 200:
        issues.append(ISSUES["overexposed"])
    elif ex["under"] > 0.3 or ex["mean"] < 45:
        issues.append(ISSUES["underexposed"])
    if not black and ex["std"] < 20:
        issues.append(ISSUES["low_contrast"])
    if trans:
        issues.append(ISSUES["transition"])
        score *= 0.6
    if face is not None:
        if not face.get("count"):
            if require_face:
                issues.append(ISSUES["no_face"])
                score *= 0.7
        else:
            if not face["facing"]:
                issues.append(ISSUES["not_facing"])
            if face.get("eyes") == 0:
                issues.append(ISSUES["eyes_hidden"])
            if face["size"] < 0.12:
                issues.append(ISSUES["face_small"])
            x0, y0, x1, y1 = face["box"]
            if min(x0, y0, 1 - x1, 1 - y1) < 0.01:
                issues.append(ISSUES["face_cut"])
    if face is not None and face.get("count"):
        face = dict(face, score=fs)
    elif face is not None:
        face = None
    return {
        "score": round(score, 1),
        "sharpness": sh,
        "stability": st,
        "exposure": ex,
        "face": face,
        "transition": trans,
        "issues": issues,
    }


# ---------------------------------------------------------------- 读帧


def video_info(path: str) -> dict:
    cv2 = _cv2()
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"打不开视频: {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    if fps <= 0 or fps > 1000:
        fps = 30.0
    return {
        "width": w,
        "height": h,
        "fps": round(fps, 3),
        "frames": n,
        "duration": round(n / fps, 3),
    }


def iter_frame_groups(path: str, times: Iterable[float], count: int = PAIR_FRAMES, full=False):
    """
    按时间顺序读每个采样点起的 count 帧, yield (t, [BGR 帧...])。
    相邻采样点挨得近时顺序 grab (不解码到 BGR), 隔得远时 seek, 长视频也不会太慢。
    full=False 时帧先降到 360p 再返回。
    """
    cv2 = _cv2()
    info = video_info(path)
    fps, nframes = info["fps"], info["frames"]
    cap = cv2.VideoCapture(path)
    pos = 0  # 下一次 read/grab 得到的帧号
    try:
        for t in sorted(set(round(float(t), 3) for t in times)):
            idx = int(round(t * fps))
            if nframes:
                idx = min(max(0, idx), max(0, nframes - count))
            if idx < pos or idx - pos > 3 * fps:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                pos = idx
            while pos < idx:
                if not cap.grab():
                    break
                pos += 1
            frames = []
            for _ in range(count):
                ok, fr = cap.read()
                if not ok:
                    break
                pos += 1
                frames.append(fr if full else _resize(fr))
            if frames:
                yield t, frames
    finally:
        cap.release()


def grab_frame(path: str, t: float):
    """t 秒处的一帧 (原分辨率 BGR); 读不到返回 None。"""
    for _t, frames in iter_frame_groups(path, [t], count=1, full=True):
        return frames[0]
    return None


# ---------------------------------------------------------------- 打分 API


def sample_times(start: float, end: float, every_s: float, min_samples: int = 1) -> List[float]:
    span = max(0.0, end - start)
    n = max(min_samples, int(span / every_s))
    if span <= 0:
        return [round(start, 3)]
    step = span / n
    return [round(start + step * (i + 0.5), 3) for i in range(n)]


def _measure_times(path: str, times: List[float], require_face: bool) -> List[dict]:
    out = []
    for t, frames in iter_frame_groups(path, times):
        out.append({"t": t, **measure(frames, require_face)})
    return out


def _percentile(vals: List[float], q: float) -> float:
    v = sorted(vals)
    if not v:
        return 0.0
    k = (len(v) - 1) * q
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def summarize(samples: List[dict]) -> dict:
    if not samples:
        return {
            "score": 0.0,
            "median": 0.0,
            "p10": 0.0,
            "issues": {},
            "best_t": None,
            "worst_t": None,
        }
    scores = [s["score"] for s in samples]
    counts: Dict[str, int] = {}
    for s in samples:
        for i in s["issues"]:
            counts[i] = counts.get(i, 0) + 1
    best = max(samples, key=lambda s: s["score"])
    worst = min(samples, key=lambda s: s["score"])
    return {
        "score": round(sum(scores) / len(scores), 1),
        "median": round(_percentile(scores, 0.5), 1),
        "p10": round(_percentile(scores, 0.1), 1),
        "issues": {
            k: round(v / len(samples), 2) for k, v in sorted(counts.items(), key=lambda x: -x[1])
        },
        "best_t": best["t"],
        "worst_t": worst["t"],
    }


def _segment(start: float, end: float, samples: List[dict]) -> dict:
    inside = [s for s in samples if start <= s["t"] <= end]
    sm = summarize(inside)
    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "score": sm["score"],
        "min": round(min((s["score"] for s in inside), default=0.0), 1),
        "best_t": sm["best_t"],
        "worst_t": sm["worst_t"],
        "issues": [k for k, share in sm["issues"].items() if share >= ISSUE_SHARE],
        "n": len(inside),
    }


def score_video(path: str, every_s: float = 0.5, require_face: bool = False) -> dict:
    """整段视频每 every_s 秒一个采样点。返回 {source, video, every_s, face_detector, samples, summary}。"""
    if every_s <= 0:
        raise ValueError("every_s 必须 > 0")
    info = video_info(path)
    samples = _measure_times(path, sample_times(0, info["duration"], every_s), require_face)
    return {
        "source": os.path.abspath(path),
        "video": info,
        "every_s": every_s,
        "face_detector": face_detector_status(),
        "samples": samples,
        "summary": summarize(samples),
    }


def score_segments(
    path: str,
    ranges: Sequence[Tuple[float, float]],
    every_s: float = 0.5,
    require_face: bool = False,
    min_samples: int = 3,
) -> dict:
    """
    给每段 [start, end] (秒) 打分: 段内至少 min_samples 个采样点。
    返回 score_video 的结构 + segments (顺序同 ranges)。
    """
    if every_s <= 0:
        raise ValueError("every_s 必须 > 0")
    info = video_info(path)
    dur = info["duration"]
    clipped = []
    for a, b in ranges:
        a, b = max(0.0, float(a)), min(dur, float(b)) if dur else float(b)
        if b < a:
            a, b = b, a
        clipped.append((a, b))
    times = sorted({t for a, b in clipped for t in sample_times(a, b, every_s, min_samples)})
    samples = _measure_times(path, times, require_face)
    return {
        "source": os.path.abspath(path),
        "video": info,
        "every_s": every_s,
        "face_detector": face_detector_status(),
        "samples": samples,
        "summary": summarize(samples),
        "segments": [_segment(a, b, samples) for a, b in clipped],
    }


def ranges_from_scenes(scenes: Sequence[float], duration: float, min_len: float = 0.3):
    """analysis.json 的镜头切换点 → 每个镜头的 [start, end]。"""
    cuts = [0.0] + sorted(float(s) for s in scenes if 0 < float(s) < duration) + [duration]
    return [(a, b) for a, b in zip(cuts, cuts[1:]) if b - a >= min_len]


def scene_points(ana: dict) -> List[float]:
    """切换点: analysis.json 的 scenes, 或 reference_analyzer 的 reference.json 的 cuts。
    两个键都没有时报错, 而不是悄悄退化成整段一个 segment。"""
    for key in ("scenes", "cuts"):
        if key in ana:
            return [float(x) for x in ana.get(key) or []]
    raise ValueError("--scenes 文件里没有 scenes (analysis.json) 或 cuts (reference.json)")


def parse_ranges(text: str) -> List[Tuple[float, float]]:
    """ "0-3,5.5-8" → [(0, 3), (5.5, 8)]"""
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        a, sep, b = part.partition("-")
        if not sep:
            raise ValueError(f"区间格式应为 起-止: {part}")
        out.append((float(a), float(b)))
    return out


# ---------------------------------------------------------------- CLI


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="素材质量打分: 清晰度 / 抖动 / 曝光 / 人脸")
    ap.add_argument("video")
    ap.add_argument("--every", type=float, default=0.5, help="采样间隔秒 (默认 0.5)")
    ap.add_argument("--ranges", help='按段打分, 如 "0-3,5.5-8" (秒)')
    ap.add_argument("--scenes", help="analysis.json / reference.json: 按其中的镜头切换点分段打分")
    ap.add_argument("--require-face", action="store_true", help="没有人脸的采样点扣分 (口播)")
    ap.add_argument("-o", "--output", help="把结果写到 JSON 文件")
    ap.add_argument("--json", action="store_true", help="stdout 输出 {ok, code, reason, data}")
    args = ap.parse_args(argv)

    try:
        ranges = parse_ranges(args.ranges) if args.ranges else None
        if args.scenes:
            with open(args.scenes, encoding="utf-8") as f:
                ana = json.load(f)
            dur = video_info(args.video)["duration"]
            ranges = (ranges or []) + ranges_from_scenes(scene_points(ana), dur)
        if ranges:
            res = score_segments(args.video, ranges, args.every, args.require_face)
        else:
            res = score_video(args.video, args.every, args.require_face)
    except (RuntimeError, ValueError, OSError) as e:
        if args.json:
            emit_result(make_result(False, "shot_quality_failed", str(e)), True)
        else:
            print(f"❌ {e}", file=sys.stderr)
        return 1

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    if args.json:
        emit_result(make_result(True, "ok", "", res), True)
        return 0
    sm, v = res["summary"], res["video"]
    print(
        f"🎬 {os.path.basename(args.video)}: {v['width']}x{v['height']}, {v['duration']:.1f}s, "
        f"{len(res['samples'])} 个采样点, 人脸检测 {res['face_detector']}"
    )
    print(f"   平均 {sm['score']} / 中位 {sm['median']} / P10 {sm['p10']}, 最好 {sm['best_t']}s")
    if sm["issues"]:
        print("   问题: " + "、".join(f"{k} {v:.0%}" for k, v in sm["issues"].items()))
    for seg in res.get("segments") or []:
        tag = "、".join(seg["issues"]) or "无"
        print(
            f"   [{seg['start']:.2f}-{seg['end']:.2f}] {seg['score']} (最低 {seg['min']}) 问题: {tag}"
        )
    if args.output:
        print(f"✅ {args.output}")
    else:
        print("   (加 -o quality.json 保存逐点结果, 或 --json 输出到 stdout)")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
