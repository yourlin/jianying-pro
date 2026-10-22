"""
音频处理 (剪映 11.x「音频」面板): 响度统一、音频降噪、人声美化、人声分离, 以及按 dB 设音量。
字段对照 references/jy11_samples/audio_ref.json。人声美化 / 人声分离只写开关, 由剪映打开草稿时计算。
"""

import json
import os
import re
import subprocess
from typing import Optional, Tuple

import pyJianYingDraft as draft
from utils.panel_assets import find_jianying_app_resource

# 人声分离: 面板选项 -> choice (2 = 仅保留人声, 1 = 仅保留背景声; 剪映 11.7 实测)
VOCAL_SEPARATION_CHOICES = {"人声": 2, "仅保留人声": 2, "vocal": 2, "背景声": 1, "仅保留背景声": 1, "accompaniment": 1}


def measure_loudness(path: str, start_us: int = 0, duration_us: Optional[int] = None) -> Optional[Tuple[float, float]]:
    """用 ffmpeg ebur128 测 (平均响度 LUFS, 峰值 dBFS); 与剪映测得的值相差约 0.1。没有 ffmpeg 时返回 None。"""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start_us / 1e6:.3f}"]
    if duration_us:
        cmd += ["-t", f"{duration_us / 1e6:.3f}"]
    cmd += ["-i", path, "-af", "ebur128=peak=true", "-f", "null", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stderr
    except (OSError, subprocess.TimeoutExpired):
        return None
    summary = out[out.rfind("Summary:"):] if "Summary:" in out else out
    i = re.search(r"I:\s*(-?[\d.]+) LUFS", summary)
    pk = re.search(r"Peak:\s*(-?[\d.]+) dBFS", summary)
    return (float(i.group(1)), float(pk.group(1))) if i and pk else None


class AudioOpsMixin:
    """JyProject 的音频处理 Mixin。依赖 PictureOpsMixin._set_segment_raw。"""

    @staticmethod
    def _audio_like(segment) -> bool:
        return isinstance(segment, (draft.AudioSegment, draft.VideoSegment))

    def _time_range(self, segment) -> dict:
        src = segment.source_timerange
        return {"start": src.start if src else 0, "duration": src.duration if src else segment.target_timerange.duration}

    def set_volume_db(self, segment, db: float):
        """按 dB 设音量 (剪映面板显示 dB): -6 dB -> 0.501。"""
        if segment is None or not self._audio_like(segment):
            return None
        segment.volume = 10 ** (float(db) / 20)
        return segment

    def set_loudness_normalize(self, segment, target_lufs: float = -23, enable: bool = True):
        """
        响度统一 (「音频 → 基础 → 响度统一」), 目标响度默认 -23 LUFS (面板默认)。
        用 ffmpeg 测量片段的平均响度和峰值写入 (剪映也是这样记录的); 没有 ffmpeg 时只写开关。
        """
        if segment is None or not self._audio_like(segment):
            return None
        if not enable:
            return self._set_segment_raw(segment, "loudness", "loudnesses", None)
        tr = self._time_range(segment)
        mat = {"type": "loudness", "enable": True, "target_loudness": float(target_lufs), "file_id": "",
               "time_range": tr}
        path = getattr(segment.material_instance, "path", "")
        measured = measure_loudness(path, tr["start"], tr["duration"]) if path and os.path.exists(path) else None
        if measured:
            mat["loudness_param"] = {"avg_loudness": measured[0], "peak_loudness": measured[1]}
        else:
            print("ℹ️ 没有 ffmpeg 或无法读取音频, 响度统一只写开关 (由剪映测量)")
        return self._set_segment_raw(segment, "loudness", "loudnesses", mat)

    def set_audio_denoise(self, segment, strength: float = 85, enable: bool = True):
        """音频降噪 (实时处理, 模型在剪映安装包内), strength 降噪程度 0~100 (面板默认 85)。"""
        if segment is None or not self._audio_like(segment):
            return None
        if not enable:
            return self._set_segment_raw(segment, "denoise", "realtime_denoises", None)
        model = find_jianying_app_resource("audiosami", "unet_denoise_44k_music_model_v1.0.model") or ""
        if not model:
            print("⚠️ 没找到剪映内置的降噪模型 (Resources/audiosami), 剪映打开草稿时可能不生效")
        return self._set_segment_raw(segment, "denoise", "realtime_denoises", {
            "type": "realtime_denoise", "is_denoise": True, "denoise_rate": max(0.0, min(1.0, float(strength) / 100)),
            "denoise_mode": 1, "sami_type": 2, "sami_name": "denoise_v2", "sami_version": "1.0", "path": model,
        })

    def set_vocal_beautify(self, segment, ambient_level: float = 75, enable: bool = True):
        """人声美化 (「音频 → 基础 → 人声美化」), ambient_level 环境音保留 0~100 (面板默认 75); 剪映打开草稿时计算。"""
        if segment is None or not self._audio_like(segment):
            return None
        if not enable:
            return self._set_segment_raw(segment, "vocal_beautify", "vocal_beautifys", None)
        return self._set_segment_raw(segment, "vocal_beautify", "vocal_beautifys", {
            "type": "vocal_beautify", "enable": True, "ambient_sound_level": float(ambient_level),
            "production_path": "", "time_range": self._time_range(segment),
        })

    def set_vocal_separation(self, segment, keep: str = "人声"):
        """人声分离 (「音频 → 人声分离」): keep="人声" 仅保留人声, "背景声" 仅保留背景声; 剪映打开草稿时计算。"""
        if segment is None or not self._audio_like(segment):
            return None
        choice = VOCAL_SEPARATION_CHOICES.get(str(keep).strip())
        if choice is None:
            raise ValueError(f"未知人声分离选项: {keep} (可选: 人声 / 背景声)")
        return self._set_segment_raw(segment, "vocal_separation", "vocal_separations", {
            "type": "vocal_separation", "choice": choice, "final_algorithm": "vocal_separate",
            "enter_from": "instruments_splitter", "production_path": "", "removed_sounds": [],
            "time_range": self._time_range(segment),
        })
