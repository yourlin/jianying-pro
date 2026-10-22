import os
import re
from typing import List, Optional, Union

from utils.formatters import safe_tim


def _split_long_text(text: str, max_chars: int) -> List[str]:
    """按标点优先、长度兜底切分长句，避免单条字幕过长。"""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    parts = [p for p in re.split(r"(?<=[，。！？；,.!?;])", text) if p.strip()]
    out: List[str] = []
    buf = ""
    for p in parts:
        if len(buf) + len(p) <= max_chars:
            buf += p
            continue
        if buf:
            out.append(buf)
        while len(p) > max_chars:
            out.append(p[:max_chars])
            p = p[max_chars:]
        buf = p
    if buf:
        out.append(buf)
    return [o.strip() for o in out if o.strip()]


_CJK_SPACE = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")


def _prompt_kwargs(prompt: Optional[str], language: Optional[str]) -> dict:
    """
    实测 (small 模型): 通用中文句子作 initial_prompt 保留标点, 关键词走 hotwords 纠正专有名词;
    hotwords 会引入词间空格, 由 _CJK_SPACE 统一清理。
    """
    kw = {}
    if language and language.startswith("zh"):
        kw["initial_prompt"] = "以下是简体中文普通话的句子。"
    if prompt:
        kw["hotwords"] = " ".join(w for w in re.split(r"[\s,，、;；]+", prompt) if w)
    return kw


def transcribe_media(
    media_path: str,
    model: str = "small",
    language: Optional[str] = "zh",
    max_chars: int = 18,
    prompt: Optional[str] = None,
) -> List[dict]:
    """
    本地 AI 语音识别 (faster-whisper)，返回 [{start_us, duration_us, text}]。
    prompt: 专有名词/关键词 (如 "剪映 自动化 智能字幕")，用于纠正人名、产品名等识别错误。
    依赖: pip install faster-whisper "av<16"   (首次运行会下载模型)
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            '智能字幕需要 faster-whisper：pip install faster-whisper "av<16"'
        ) from e

    whisper = WhisperModel(model, device="auto", compute_type="auto")
    segments, _info = whisper.transcribe(
        media_path, language=language, vad_filter=True, word_timestamps=False,
        **_prompt_kwargs(prompt, language),
    )
    cues: List[dict] = []
    for seg in segments:
        text = _CJK_SPACE.sub("", seg.text).replace(",", "，")
        pieces = _split_long_text(text, max_chars)
        if not pieces:
            continue
        start, end = float(seg.start), float(seg.end)
        total = sum(len(p) for p in pieces) or 1
        cursor = start
        for p in pieces:
            span = (end - start) * len(p) / total
            cues.append(
                {
                    "start_us": int(cursor * 1e6),
                    "duration_us": max(int(span * 1e6), 300000),
                    "text": p.rstrip("，。,."),
                }
            )
            cursor += span
    return cues


class AiOpsMixin:
    """
    JyProject 的 AI 能力 Mixin (本地运行，无需剪映云端)。
    """

    def add_auto_subtitles(
        self,
        media_path: str,
        start_time: Union[str, int] = 0,
        track_name: str = "Subtitles",
        model: str = "small",
        language: Optional[str] = "zh",
        max_chars: int = 18,
        export_srt: bool = True,
        prompt: Optional[str] = None,
        **text_kwargs,
    ) -> List[dict]:
        """
        智能字幕：对视频/音频做本地语音识别，按时间轴生成字幕片段。
        start_time 为该素材在时间线上的起点 (字幕整体偏移)。
        text_kwargs 透传给 add_text_simple (如 anim_in / style / font)。
        """
        if not os.path.exists(media_path):
            raise FileNotFoundError(media_path)
        offset = safe_tim(start_time)
        cues = transcribe_media(
            media_path, model=model, language=language, max_chars=max_chars, prompt=prompt
        )
        for c in cues:
            self.add_text_simple(
                c["text"],
                start_time=offset + c["start_us"],
                duration=c["duration_us"],
                track_name=track_name,
                **text_kwargs,
            )
        if export_srt and cues:
            from utils.formatters import format_srt_time

            srt_path = os.path.join(self.root, self.name, "auto_subtitles.srt")
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, c in enumerate(cues, 1):
                    s = offset + c["start_us"]
                    e = s + c["duration_us"]
                    f.write(f"{i}\n{format_srt_time(s)} --> {format_srt_time(e)}\n{c['text']}\n\n")
            print(f"📝 SRT saved: {srt_path}")
        print(f"✅ Auto subtitles: {len(cues)} cues")
        return cues
