"""
音乐节拍检测 (卡点混剪第 1 步): 听懂 BGM 的节奏, 给卡点 / 高光镜头提供时间锚点。

    python beat_detect.py <音乐文件> [-o beats.json] [--backend auto|librosa|numpy]
                          [--beats-per-bar 4] [--no-cache] [--cache-dir DIR] [--json]

输出 (时间单位都是秒):
  - tempo: BPM
  - beats: 拍点
  - downbeats: 强拍 (每小节第 1 拍)
  - onsets: 起音点 (鼓点 / 音符起始, 比拍点密, 适合「卡鼓点」)
  - energy_sections: 段落能量 [{start, end, energy 0-1, loudness_db, level low/mid/high, is_climax}]
    副歌 / 高潮段 (is_climax) 用来放高光镜头、加快切速
  - climax: 能量最高的一段 {start, end}

后端:
  - librosa (装了就用): 拍点 / 起音点用 librosa; 强拍和段落能量用下面同一套 numpy 特征
  - numpy (降级, 只依赖 numpy + ffmpeg 解码; .wav 没有 ffmpeg 也能读):
    频谱通量 onset envelope -> 自相关估 tempo (对数高斯先验) -> 动态规划拍点跟踪 (Ellis 2007)

结果按「文件路径 + 大小 + 修改时间 + 参数」缓存为 JSON (默认 ~/.cache/jianying-pro/beats,
可用环境变量 JY_BEAT_CACHE 或 --cache-dir 改), 同一首歌第二次直接读缓存。
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import wave
from typing import Dict, List, Optional, Tuple

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCHEMA_VERSION = 1
DEFAULT_SR = 22050
N_FFT = 1024
HOP = 256
LOW_BAND_HZ = 160.0  # 底鼓 / 贝斯所在频段, 用于判强拍


class BeatDetectError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 解码
# ---------------------------------------------------------------------------


def _read_wav_stdlib(path: str, sr: int) -> np.ndarray:
    """没有 ffmpeg 时读 PCM wav (8/16/24/32 bit), 线性插值重采样到 sr。"""
    with wave.open(path, "rb") as w:
        ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width == 1:
        y = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        y = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        y = v.astype(np.float32) / float(1 << 23)
    elif width == 4:
        y = np.frombuffer(raw, dtype="<i4").astype(np.float32) / float(1 << 31)
    else:
        raise BeatDetectError(f"不支持的 wav 位深: {width * 8} bit")
    y = y.reshape(-1, ch).mean(axis=1)
    if rate != sr and len(y):
        t_new = np.arange(int(len(y) * sr / rate)) / sr
        y = np.interp(t_new, np.arange(len(y)) / rate, y).astype(np.float32)
    return y


def load_audio(path: str, sr: int = DEFAULT_SR) -> np.ndarray:
    """解码为单声道 float32 PCM。优先 ffmpeg (任意格式, 包括视频的音轨), 没有 ffmpeg 时只支持 .wav。"""
    if not os.path.exists(path):
        raise BeatDetectError(f"找不到音频: {path}")
    if shutil.which("ffmpeg"):
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sr),
            "-f",
            "f32le",
            "-",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise BeatDetectError(f"ffmpeg 解码失败: {e}") from e
        if res.returncode == 0 and res.stdout:
            return np.frombuffer(res.stdout, dtype="<f4").astype(np.float32)
        if not path.lower().endswith(".wav"):
            err = res.stderr.decode("utf-8", "replace").strip()[-300:]
            raise BeatDetectError(f"ffmpeg 解码失败: {err or '无音轨'}")
    if path.lower().endswith(".wav"):
        try:
            return _read_wav_stdlib(path, sr)
        except (wave.Error, EOFError) as e:
            raise BeatDetectError(f"读取 wav 失败: {e}") from e
    raise BeatDetectError("没有找到 ffmpeg, 只能读 .wav; 请安装 ffmpeg 或先转成 wav")


# ---------------------------------------------------------------------------
# 特征 (两个后端共用)
# ---------------------------------------------------------------------------


def _moving_average(x: np.ndarray, width: int) -> np.ndarray:
    if width <= 1 or len(x) == 0:
        return x.copy()
    k = np.ones(width) / width
    pad = width // 2
    xp = np.pad(x, (pad, width - 1 - pad), mode="edge")
    return np.convolve(xp, k, mode="valid")


def _smooth(env: np.ndarray, width: int = 7) -> np.ndarray:
    """汉宁窗平滑: 自相关 / 拍点跟踪前用, 避免尖峰因为帧量化 (半帧偏差) 对不上。"""
    if len(env) < width:
        return env
    k = np.hanning(width + 2)[1:-1]
    return np.convolve(env, k / k.sum(), mode="same")


def compute_features(
    y: np.ndarray, sr: int = DEFAULT_SR, n_fft: int = N_FFT, hop: int = HOP
) -> dict:
    """
    逐帧特征 (帧率 sr/hop):
      onset: 全频段频谱通量 (半波整流的对数幅度差), 已去局部均值并归一化
      low: 低频段 (< LOW_BAND_HZ) 通量, 底鼓所在, 判强拍用
      rms: 帧 RMS
    """
    if len(y) < n_fft:
        y = np.pad(y, (0, n_fft - len(y)))
    yp = np.pad(y, (n_fft // 2, n_fft // 2), mode="reflect" if len(y) > n_fft else "constant")
    n_frames = 1 + (len(yp) - n_fft) // hop
    win = np.hanning(n_fft).astype(np.float32)
    n_bins = n_fft // 2 + 1
    low_bins = max(2, int(LOW_BAND_HZ / (sr / n_fft)) + 1)
    logmag = np.empty((n_frames, n_bins), dtype=np.float32)
    rms = np.empty(n_frames, dtype=np.float32)
    chunk = 4096  # 分块做 STFT, 长歌也不爆内存
    for a in range(0, n_frames, chunk):
        b = min(n_frames, a + chunk)
        idx = np.arange(a, b)[:, None] * hop + np.arange(n_fft)[None, :]
        frames = yp[idx]
        rms[a:b] = np.sqrt(np.mean(frames**2, axis=1))
        mag = np.abs(np.fft.rfft(frames * win, axis=1))
        logmag[a:b] = np.log1p(100.0 * mag)
    diff = np.maximum(0.0, np.diff(logmag, axis=0))
    fr = sr / hop
    flux = np.concatenate([[0.0], diff.sum(axis=1)])  # 第 t 帧 = 第 t-1 帧到第 t 帧的增量
    low = np.concatenate([[0.0], diff[:, :low_bins].sum(axis=1)])

    def _norm(env):
        env = np.maximum(0.0, env - _moving_average(env, int(fr * 0.4) | 1))
        s = env.std()
        return env / s if s > 1e-9 else env

    return {
        "onset": _norm(flux),
        "low": _norm(low),
        "rms": rms,
        "fr": fr,
        "n_frames": n_frames,
        "duration": len(y) / sr,
    }


# ---------------------------------------------------------------------------
# numpy 后端: tempo + 拍点 + 起音点
# ---------------------------------------------------------------------------


def estimate_tempo(
    onset: np.ndarray, fr: float, start_bpm: float = 120.0, bpm_range=(40.0, 240.0)
) -> float:
    """onset envelope 自相关 + 对数高斯先验 (以 start_bpm 为中心, 1 个八度标准差) 选周期, 抛物线插值到亚帧。"""
    x = onset - onset.mean()
    n = len(x)
    if n < 8 or not np.any(x):
        return 0.0
    size = 1 << int(np.ceil(np.log2(2 * n)))
    f = np.fft.rfft(x, size)
    ac = np.fft.irfft(f * np.conj(f), size)[:n]
    ac = ac / (ac[0] + 1e-12)
    lag_min = max(1, int(np.floor(fr * 60.0 / bpm_range[1])))
    lag_max = min(n - 2, int(np.ceil(fr * 60.0 / bpm_range[0])))
    if lag_max <= lag_min:
        return 0.0
    lags = np.arange(lag_min, lag_max + 1)
    bpms = 60.0 * fr / lags
    prior = np.exp(-0.5 * (np.log2(bpms / start_bpm)) ** 2)
    # 叠加 2 倍周期的自相关, 抑制半拍 (八分音符) 被选成拍
    ac2 = np.array([ac[min(n - 1, 2 * lag)] for lag in lags])
    score = (ac[lags] + 0.5 * ac2) * prior
    i = int(np.argmax(score))
    lag = float(lags[i])
    if 0 < i < len(lags) - 1:
        a, b, c = score[i - 1], score[i], score[i + 1]
        den = a - 2 * b + c
        if abs(den) > 1e-12:
            lag += 0.5 * (a - c) / den
    return 60.0 * fr / lag


def track_beats(onset: np.ndarray, fr: float, bpm: float, tightness: float = 100.0) -> np.ndarray:
    """动态规划拍点跟踪: 累积得分 = 当前 onset + max(前一拍得分 - tightness * log(间隔/周期)^2)。返回帧号。"""
    n = len(onset)
    if bpm <= 0 or n == 0:
        return np.array([], dtype=int)
    period = fr * 60.0 / bpm
    lo, hi = int(round(period / 2)), int(round(period * 2))
    offsets = np.arange(lo, hi + 1)
    penalty = -tightness * np.log(offsets / period) ** 2
    local = onset / (onset.std() + 1e-9)
    score = local.astype(np.float64).copy()
    back = np.full(n, -1, dtype=int)
    for t in range(lo, n):
        prev = t - offsets
        valid = prev >= 0
        if not valid.any():
            continue
        cand = score[prev[valid]] + penalty[valid]
        j = int(np.argmax(cand))
        if cand[j] > 0:
            score[t] = local[t] + cand[j]
            back[t] = prev[valid][j]
    # 结尾: 最后一个周期内得分最高的帧
    tail = max(0, n - int(round(period)))
    t = tail + int(np.argmax(score[tail:]))
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    beats = np.array(beats[::-1], dtype=int)
    return _trim_weak(beats, onset)


def _trim_weak(beats: np.ndarray, onset: np.ndarray) -> np.ndarray:
    """去掉首尾落在静音里的拍 (onset 弱于全曲均值的一半)。"""
    if len(beats) == 0:
        return beats
    ref = 0.5 * np.sqrt(np.mean(onset[beats] ** 2))
    w = 3
    strength = np.array([onset[max(0, b - w) : b + w + 1].max() for b in beats])
    ok = np.where(strength >= ref)[0]
    if len(ok) == 0:
        return beats
    return beats[ok[0] : ok[-1] + 1]


def pick_onsets(
    onset: np.ndarray, fr: float, delta: float = 0.5, wait_s: float = 0.05
) -> np.ndarray:
    """起音点: 局部极大 (±30ms) 且高于局部均值 + delta 的帧, 相邻至少 wait_s。返回帧号。"""
    n = len(onset)
    if n < 3:
        return np.array([], dtype=int)
    w = max(1, int(round(0.03 * fr)))
    pad = np.pad(onset, (w, w), mode="constant")
    local_max = np.lib.stride_tricks.sliding_window_view(pad, 2 * w + 1).max(axis=1)
    avg = _moving_average(onset, int(0.2 * fr) | 1)
    cand = np.where((onset >= local_max) & (onset >= avg + delta) & (onset > 0))[0]
    out, last = [], -(10**9)
    wait = int(round(wait_s * fr))
    for c in cand:
        if c - last >= wait:
            out.append(c)
            last = c
    return np.array(out, dtype=int)


def _refine_peak(env: np.ndarray, frames: np.ndarray, radius: int = 2) -> np.ndarray:
    """把帧号对齐到 ±radius 内的 onset 峰值并做抛物线插值, 返回亚帧位置。"""
    out = []
    n = len(env)
    for f in frames:
        a, b = max(0, f - radius), min(n, f + radius + 1)
        p = a + int(np.argmax(env[a:b]))
        pos = float(p)
        if 0 < p < n - 1:
            y0, y1, y2 = env[p - 1], env[p], env[p + 1]
            den = y0 - 2 * y1 + y2
            if abs(den) > 1e-12:
                pos += max(-0.5, min(0.5, 0.5 * (y0 - y2) / den))
        out.append(pos)
    return np.array(out)


def _numpy_backend(feat: dict, start_bpm: float) -> Tuple[float, np.ndarray, np.ndarray]:
    fr, onset = feat["fr"], feat["onset"]
    smooth = _smooth(onset)
    bpm = estimate_tempo(smooth, fr, start_bpm)
    beat_frames = track_beats(smooth, fr, bpm)
    beats = _refine_peak(onset, beat_frames) / fr
    onsets = _refine_peak(onset, pick_onsets(onset, fr), radius=1) / fr
    return bpm, beats, onsets


def _librosa_backend(
    y: np.ndarray, sr: int, start_bpm: float
) -> Tuple[float, np.ndarray, np.ndarray]:
    import librosa

    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    # trim=False: librosa 默认按全曲 onset 均值裁掉首尾拍, 安静的前奏会整段丢拍; 首尾裁剪由外层统一做
    tempo, frames = librosa.beat.beat_track(
        onset_envelope=env, sr=sr, hop_length=HOP, start_bpm=start_bpm, trim=False
    )
    frames = _trim_weak(np.asarray(frames, dtype=int), env / (env.std() + 1e-9))
    beats = librosa.frames_to_time(frames, sr=sr, hop_length=HOP)
    onsets = librosa.onset.onset_detect(onset_envelope=env, sr=sr, hop_length=HOP, units="time")
    return float(np.atleast_1d(tempo)[0]), np.asarray(beats, float), np.asarray(onsets, float)


def _librosa_available() -> bool:
    try:
        import librosa  # noqa: F401
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# 强拍 / 段落能量
# ---------------------------------------------------------------------------


def _strength_at(env: np.ndarray, times: np.ndarray, fr: float, radius: int = 2) -> np.ndarray:
    n = len(env)
    out = np.zeros(len(times))
    for i, t in enumerate(times):
        f = int(round(t * fr))
        a, b = max(0, f - radius), min(n, f + radius + 1)
        out[i] = env[a:b].max() if b > a else 0.0
    return out


def find_downbeats(beats: np.ndarray, feat: dict, beats_per_bar: int = 4) -> Tuple[np.ndarray, int]:
    """
    强拍: 试 beats_per_bar 种相位, 选「低频重音 (底鼓) + 整体响度 + 起音强度」平均最强的那组。
    返回 (强拍时间, 第一个强拍在 beats 里的下标)。
    """
    if len(beats) == 0 or beats_per_bar <= 1:
        return beats.copy(), 0
    fr = feat["fr"]
    low = _strength_at(feat["low"], beats, fr)
    full = _strength_at(feat["onset"], beats, fr)
    rms = _strength_at(feat["rms"], beats, fr, radius=int(0.05 * fr))

    def z(x):
        s = x.std()
        return (x - x.mean()) / s if s > 1e-9 else np.zeros_like(x)

    accent = 1.0 * z(low) + 0.5 * z(rms) + 0.3 * z(full)
    scores = [
        accent[k::beats_per_bar].mean() if len(accent[k::beats_per_bar]) else -1e9
        for k in range(beats_per_bar)
    ]
    phase = int(np.argmax(scores))
    return beats[phase::beats_per_bar], phase


def energy_sections(
    feat: dict, downbeats: np.ndarray, min_bars: int = 2, flat_db: float = 3.0
) -> List[dict]:
    """
    段落能量: 按小节 (强拍到强拍) 算响度 dB, 中值平滑后分 low / mid / high 三档, 连续同档合并成段,
    太短的段并入相邻段。动态范围 < flat_db 视为全曲一个能量档 (mid)。
    没有拍点时按 2 秒一窗。
    """
    fr, rms, dur = feat["fr"], feat["rms"], feat["duration"]
    if dur <= 0:
        return []
    bounds = [float(t) for t in downbeats if 0 < t < dur - 0.05]
    if len(bounds) < 2:
        bounds = list(np.arange(0.0, dur, 2.0)[1:])
    edges = [0.0] + bounds + [dur]
    edges = [e for i, e in enumerate(edges) if i == 0 or e - edges[i - 1] > 1e-3]
    loud = []
    for a, b in zip(edges[:-1], edges[1:]):
        seg = rms[int(a * fr) : max(int(a * fr) + 1, int(b * fr))]
        loud.append(20 * np.log10(np.sqrt(np.mean(seg**2)) + 1e-6) if len(seg) else -120.0)
    loud = np.array(loud)
    if len(loud) >= 3:
        sm = np.array([np.median(loud[max(0, i - 1) : i + 2]) for i in range(len(loud))])
    else:
        sm = loud.copy()
    # 统计动态范围时只看完整小节: 开头的弱起、结尾的余音是半个小节, 会把范围拉大
    lens = np.diff(edges)
    full = lens >= 0.75 * np.median(lens)
    audible = sm[(sm > -80) & full] if np.any((sm > -80) & full) else sm[sm > -80]
    lo_db, hi_db = (
        (np.percentile(audible, 5), np.percentile(audible, 95)) if len(audible) else (0.0, 0.0)
    )
    span = hi_db - lo_db
    if span < flat_db:
        norm = np.full(len(sm), 0.5)
    else:
        norm = np.clip((sm - lo_db) / span, 0.0, 1.0)
    levels = np.where(norm >= 0.67, 2, np.where(norm >= 0.34, 1, 0))
    # 连续同档合并
    runs = []  # [start_idx, end_idx_exclusive, level]
    for i, lv in enumerate(levels):
        if runs and runs[-1][2] == lv:
            runs[-1][1] = i + 1
        else:
            runs.append([i, i + 1, int(lv)])
    # 短段并入能量更接近的邻段, 直到都 >= min_bars
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for k, r in enumerate(runs):
            if r[1] - r[0] >= min_bars:
                continue
            mean_k = norm[r[0] : r[1]].mean()
            cands = []
            if k > 0:
                cands.append((abs(norm[runs[k - 1][0] : runs[k - 1][1]].mean() - mean_k), k - 1))
            if k < len(runs) - 1:
                cands.append((abs(norm[runs[k + 1][0] : runs[k + 1][1]].mean() - mean_k), k + 1))
            _, j = min(cands)
            a, b = sorted((k, j))
            merged_lv = (
                runs[a][2] if runs[a][1] - runs[a][0] >= runs[b][1] - runs[b][0] else runs[b][2]
            )
            runs[a] = [runs[a][0], runs[b][1], merged_lv]
            del runs[b]
            changed = True
            break
        # 合并后相邻同档再合并
        i = 0
        while i < len(runs) - 1:
            if runs[i][2] == runs[i + 1][2]:
                runs[i][1] = runs[i + 1][1]
                del runs[i + 1]
            else:
                i += 1
    names = {0: "low", 1: "mid", 2: "high"}
    out = []
    for a, b, lv in runs:
        out.append(
            {
                "start": round(edges[a], 3),
                "end": round(edges[b], 3),
                "energy": round(float(norm[a:b].mean()), 3),
                "loudness_db": round(float(loud[a:b].mean()), 2),
                "level": names[lv],
                "bars": b - a,
                "is_climax": False,
            }
        )
    if out:
        # 高潮: 能量 x sqrt(时长) 最大的段 (避免一两小节的尖峰), 全曲平坦时不标
        if span >= flat_db and len(out) > 1:
            best = max(
                range(len(out)),
                key=lambda i: out[i]["energy"] * np.sqrt(out[i]["end"] - out[i]["start"]),
            )
            out[best]["is_climax"] = True
    return out


# ---------------------------------------------------------------------------
# 主入口 + 缓存
# ---------------------------------------------------------------------------


def default_cache_dir() -> str:
    return os.environ.get("JY_BEAT_CACHE") or os.path.join(
        os.path.expanduser("~"), ".cache", "jianying-pro", "beats"
    )


def _cache_key(path: str, params: dict) -> str:
    st = os.stat(path)
    raw = json.dumps(
        {
            "path": os.path.abspath(path),
            "size": st.st_size,
            "mtime": st.st_mtime_ns,
            "v": SCHEMA_VERSION,
            **params,
        },
        sort_keys=True,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _round_list(xs, nd: int = 3) -> List[float]:
    return [round(float(x), nd) for x in xs]


def detect_beats(
    audio_path: str,
    backend: str = "auto",
    beats_per_bar: int = 4,
    start_bpm: float = 120.0,
    sr: int = DEFAULT_SR,
    use_cache: bool = True,
    cache_dir: Optional[str] = None,
) -> Dict:
    """
    检测节拍。返回 {tempo, beats[], downbeats[], onsets[], energy_sections[], climax, ...} (秒)。

    backend: auto (有 librosa 用 librosa, 否则 numpy) / librosa / numpy
    """
    if backend not in ("auto", "librosa", "numpy"):
        raise BeatDetectError(f"未知 backend: {backend} (可选 auto / librosa / numpy)")
    if not os.path.exists(audio_path):
        raise BeatDetectError(f"找不到音频: {audio_path}")
    use = backend
    if backend == "auto":
        use = "librosa" if _librosa_available() else "numpy"
    elif backend == "librosa" and not _librosa_available():
        raise BeatDetectError("没有安装 librosa (pip install librosa), 或改用 --backend numpy")
    params = {"backend": use, "bpb": beats_per_bar, "start_bpm": start_bpm, "sr": sr}
    cache_file = None
    if use_cache:
        cdir = cache_dir or default_cache_dir()
        cache_file = os.path.join(cdir, _cache_key(audio_path, params) + ".json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    res = json.load(f)
                res["cached"] = True
                return res
            except (OSError, ValueError):
                pass

    y = load_audio(audio_path, sr)
    if len(y) == 0:
        raise BeatDetectError("音频为空")
    feat = compute_features(y, sr)
    if use == "librosa":
        tempo, beats, onsets = _librosa_backend(y, sr, start_bpm)
    else:
        tempo, beats, onsets = _numpy_backend(feat, start_bpm)
    beats = np.sort(beats[(beats >= 0) & (beats <= feat["duration"])])
    if len(beats) >= 4:  # 用拍间隔中位数修正 tempo (自相关的 BPM 受帧率量化影响)
        ibi = np.median(np.diff(beats))
        if ibi > 0:
            tempo = 60.0 / ibi
    downbeats, phase = find_downbeats(beats, feat, beats_per_bar)
    sections = energy_sections(feat, downbeats)
    climax = next(
        ({"start": s["start"], "end": s["end"]} for s in sections if s["is_climax"]), None
    )
    res = {
        "schema": SCHEMA_VERSION,
        "audio": os.path.abspath(audio_path),
        "backend": use,
        "duration": round(feat["duration"], 3),
        "tempo": round(float(tempo), 2),
        "beat_interval": round(60.0 / tempo, 4) if tempo else 0.0,
        "beats_per_bar": beats_per_bar,
        "downbeat_phase": phase,
        "beats": _round_list(beats),
        "downbeats": _round_list(downbeats),
        "onsets": _round_list(onsets),
        "energy_sections": sections,
        "climax": climax,
        "cached": False,
    }
    if cache_file:
        try:
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False)
        except OSError:
            pass
    return res


def load_beats(path_or_result) -> Dict:
    """读 beats.json (或直接传 detect_beats 的结果 / 拍点列表), 统一成 dict。"""
    if isinstance(path_or_result, dict):
        return path_or_result
    if isinstance(path_or_result, (list, tuple)):
        return {"beats": [float(x) for x in path_or_result], "downbeats": [], "energy_sections": []}
    with open(path_or_result, "r", encoding="utf-8") as f:
        return json.load(f)


def _main(argv=None) -> int:
    from utils.cli_protocol import emit_result, make_result

    ap = argparse.ArgumentParser(
        description="音乐节拍检测: tempo / 拍点 / 强拍 / 起音点 / 段落能量"
    )
    ap.add_argument("audio", help="音乐文件 (mp3 / wav / m4a / 视频文件的音轨均可)")
    ap.add_argument("-o", "--output", help="输出 JSON (默认 <音乐目录>/<文件名>.beats.json)")
    ap.add_argument("--backend", default="auto", choices=["auto", "librosa", "numpy"])
    ap.add_argument(
        "--beats-per-bar", type=int, default=4, help="每小节拍数 (4/4 拍为 4, 3/4 拍为 3)"
    )
    ap.add_argument("--start-bpm", type=float, default=120.0, help="tempo 先验中心 (慢歌可设 80)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--cache-dir", help="缓存目录 (默认 ~/.cache/jianying-pro/beats)")
    ap.add_argument("--json", action="store_true", help="按 CLI 协议输出 JSON 到 stdout")
    args = ap.parse_args(argv)
    try:
        res = detect_beats(
            args.audio,
            args.backend,
            args.beats_per_bar,
            args.start_bpm,
            use_cache=not args.no_cache,
            cache_dir=args.cache_dir,
        )
    except BeatDetectError as e:
        if args.json:
            emit_result(make_result(False, "beat_detect_failed", str(e)), True)
        else:
            print(f"❌ {e}")
        return 1
    out = args.output or os.path.splitext(os.path.abspath(args.audio))[0] + ".beats.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    if args.json:
        emit_result(make_result(True, "ok", "", {"output": out, **res}), True)
        return 0
    print(
        f"🎵 {os.path.basename(args.audio)}: {res['duration']:.1f}s, {res['tempo']:.1f} BPM "
        f"({res['backend']}{', 缓存' if res['cached'] else ''})"
    )
    print(
        f"   拍点 {len(res['beats'])} 个, 强拍 {len(res['downbeats'])} 个, 起音点 {len(res['onsets'])} 个"
    )
    for s in res["energy_sections"]:
        mark = "  ← 高潮" if s["is_climax"] else ""
        print(
            f"   {s['start']:7.2f}-{s['end']:7.2f}s  {s['level']:<4} 能量 {s['energy']:.2f}{mark}"
        )
    print(f"✅ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
