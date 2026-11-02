"""帧对齐: 写入草稿前把时间对齐到整帧, 和剪映打开草稿时的规整结果一致。

剪映的取值规则 (对比 skill 副本和剪映 template.json 实测, 30fps 草稿 4 份):
  - 1 帧 = 1e6 / fps 微秒, 不是整数 (30fps 是 33333.33us)。
  - 第 n 帧的时间 = floor(n * 1e6 / fps): 110 帧 -> 3666666, 304 帧 -> 10133333。
    片段的起点和终点都落在这样的帧边界上, 时长 = 终点 - 起点 (所以有 1033333 和 1033334 两种 31 帧)。
  - 素材截取 (source_timerange) 的起点和终点也落在帧边界上。
  - 重叠转场: 时长 = 2 * floor(min(d, 相邻较短片段 / 2) / 2 / 帧长) 帧, 再按上面的规则换成微秒
    (500ms -> 466666, 700ms 在 1.03s 片段上 -> 466666, 600ms -> 600000); 22 个实测样本全部一致。

已对齐的值再对齐一次不变, 所以 skill 写入对齐后的值, 剪映打开时就不再改动。
主轨要按帧数累加 (accumulate_ranges): 每段的起点 / 终点都从累计的理想时间换算成帧, 相邻片段共用同一个
帧边界; 不能逐段把时长四舍五入后相加, 否则误差会沿主轨累积 (剪映自己就是这样, 实测最大偏移 41ms)。
"""

from fractions import Fraction
from typing import Iterable, List, Optional, Tuple

US = 1_000_000
DEFAULT_FPS = 30
# 转场被截短超过这个比例时警告 (600 -> 466.7ms 是 22%; 只取偶数帧的 500 -> 466.7ms 是 6.7%, 不警告)
TRANSITION_WARN_RATIO = 0.2


def _fps(fps) -> Fraction:
    try:
        f = Fraction(fps if fps else DEFAULT_FPS).limit_denominator(1001)
    except (TypeError, ValueError):
        f = Fraction(DEFAULT_FPS)
    return f if f > 0 else Fraction(DEFAULT_FPS)


def project_fps(obj=None) -> float:
    """工程帧率: JyProject (script.fps) / ScriptFile (fps) / 草稿 JSON dict ("fps"), 读不到时 30。"""
    for cand in (getattr(getattr(obj, "script", None), "fps", None), getattr(obj, "fps", None),
                 obj.get("fps") if isinstance(obj, dict) else None):  # fmt: skip
        try:
            if cand and float(cand) > 0:
                return float(cand)
        except (TypeError, ValueError):
            continue
    return float(DEFAULT_FPS)


def frame_us(fps=DEFAULT_FPS) -> float:
    """1 帧的微秒数 (不是整数, 30fps = 33333.33)。"""
    return float(US / _fps(fps))


def us_to_frames(us, fps=DEFAULT_FPS) -> int:
    """微秒 -> 最近的帧号 (四舍五入, .5 进位)。"""
    v = Fraction(us) * _fps(fps) / US + Fraction(1, 2)
    return v.numerator // v.denominator


def frames_to_us(n: int, fps=DEFAULT_FPS) -> int:
    """帧号 -> 微秒, 和剪映一样向下取整: 110 帧 @30fps = 3666666。"""
    v = Fraction(int(n)) * US / _fps(fps)
    return v.numerator // v.denominator


def snap_us(us, fps=DEFAULT_FPS) -> int:
    """时间点对齐到最近的帧边界。"""
    return frames_to_us(us_to_frames(us, fps), fps)


def seconds_to_us(sec: float, fps=DEFAULT_FPS) -> int:
    """秒 -> 对齐到帧边界的微秒。"""
    return snap_us(round(float(sec) * US), fps)


def snap_range(start_us, dur_us, fps=DEFAULT_FPS, min_frames: int = 1) -> Tuple[int, int]:
    """[start, start+dur] 的起点和终点分别对齐到帧边界, 返回 (起点, 时长) 微秒。dur > 0 时至少 min_frames 帧。"""
    sf = us_to_frames(start_us, fps)
    ef = us_to_frames(Fraction(start_us) + Fraction(dur_us), fps)
    if dur_us and dur_us > 0 and ef - sf < min_frames:
        ef = sf + min_frames
    s = frames_to_us(sf, fps)
    return s, max(0, frames_to_us(ef, fps) - s)


def range_frames(start_us, dur_us, fps=DEFAULT_FPS) -> Tuple[int, int]:
    """(起点帧, 帧数)。"""
    sf = us_to_frames(start_us, fps)
    return sf, us_to_frames(Fraction(start_us) + Fraction(dur_us), fps) - sf


def snap_source_range(src_start_us, n_frames: int, fps=DEFAULT_FPS) -> Tuple[int, int]:
    """素材截取范围: 起点对齐到帧边界, 截取 n_frames 帧, 终点也落在帧边界上。"""
    sf = us_to_frames(src_start_us, fps)
    s = frames_to_us(sf, fps)
    return s, frames_to_us(sf + int(n_frames), fps) - s


def accumulate_ranges(durations_us: Iterable, fps=DEFAULT_FPS, start_us=0) -> List[Tuple[int, int]]:
    """首尾相接的一串片段 (主轨): 按累计的理想时间换算帧边界, 返回每段 (起点, 时长) 微秒。

    相邻片段共用帧边界 (上一段终点 = 下一段起点), 第 k 段终点和理想累计时间的误差 ≤ 半帧, 不随段数累积。
    """
    out: List[Tuple[int, int]] = []
    cum = Fraction(start_us)
    f0 = us_to_frames(cum, fps)
    for d in durations_us:
        cum += Fraction(d)
        f1 = max(f0, us_to_frames(cum, fps))
        s = frames_to_us(f0, fps)
        out.append((s, frames_to_us(f1, fps) - s))
        f0 = f1
    return out


def transition_duration(
    requested_us, seg_dur_us, next_dur_us: Optional[int] = None, fps=DEFAULT_FPS
) -> int:
    """剪映规整后的重叠转场时长 (微秒)。

    规则: 时长 = 2 * floor(min(d, 相邻较短片段 / 2) / 2 / 帧长) 帧, 帧数换微秒向下取整。
    d 和片段时长先换成最近的帧数再算 (等价于按帧计算), 这样规整后的值再规整一次不变:
    466666us 是 14 帧, 不会被当成 13.99 帧再截成 12 帧。
    seg_dur_us 是挂转场的 (前一个) 片段, next_dur_us 是后一个片段 (没有时只看前一个)。
    片段太短放不下 2 帧时返回 0。
    """
    d = us_to_frames(requested_us, fps)
    seg = us_to_frames(seg_dur_us, fps)
    shorter = min(seg, us_to_frames(next_dur_us, fps)) if next_dur_us else seg
    half_frames = max(0, min(2 * d, shorter) // 4)  # floor(min(d, shorter / 2) / 2)
    return frames_to_us(2 * half_frames, fps)
