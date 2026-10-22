import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from storyboard_builder import Timeline, split_transcript, wrap_text  # noqa: E402


def test_timeline_maps_source_to_output_across_cuts():
    tl = Timeline([(0, 37.55), (38.05, 86.4)])
    assert round(tl.total, 2) == 85.9
    assert tl.to_out(10) == 10
    assert round(tl.to_out(40), 2) == 39.5
    assert round(tl.to_out(37.8), 2) == 37.55  # 落在剪掉的气口里 -> 吸附到下一段开头
    s, d = tl.span(36, 4)  # 跨剪点: 源 36~40 -> 成片 36~39.5
    assert round(s, 2) == 36 and round(d, 2) == 3.5


def test_split_transcript_breaks_on_pause_and_length():
    tr = [{"start": 0, "end": 3, "text": "abc", "words": [
        {"w": "你好", "s": 0.0, "e": 0.4}, {"w": "世界", "s": 0.42, "e": 0.8},
        {"w": "今天", "s": 1.3, "e": 1.6}, {"w": "天气", "s": 1.62, "e": 2.0}]}]
    lines = split_transcript(tr, max_chars=15)
    assert [l["text"] for l in lines] == ["你好世界", "今天天气"]  # 0.5s 停顿断开
    lines = split_transcript(tr, max_chars=4, pause=9)  # 只按长度断: 每条 <= 4 字
    assert [l["text"] for l in lines] == ["你好世界", "今天天气"]


def test_wrap_text_english_two_lines():
    out = wrap_text("Do you think Chinese characters are difficult to remember?", 26)
    assert out.count("\n") == 1 and all(len(x) <= 40 for x in out.split("\n"))


def test_split_transcript_merges_fragments():
    tr = [{"start": 5, "end": 8.4, "text": "x", "words": [
        {"w": " Let's", "s": 5.04, "e": 5.48}, {"w": " start", "s": 5.48, "e": 5.78}, {"w": " with", "s": 5.78, "e": 6.0},
        {"w": " one", "s": 6.0, "e": 6.3}, {"w": " simple", "s": 6.3, "e": 6.8}, {"w": " character,", "s": 6.8, "e": 7.9},
        {"w": " 人.", "s": 8.22, "e": 8.34}]}]
    lines = split_transcript(tr, max_chars=38)
    assert lines[-1]["text"].endswith("人.") and len(lines) == 1


def test_keyframes_use_material_time_for_trimmed_segments():
    """剪映的关键帧按素材时间算: 从素材 10s 处截取的片段, 片段内 2s 的关键帧要写成 12s"""
    import types

    from core.track_ops import TrackOpsMixin

    seg = types.SimpleNamespace(source_timerange=types.SimpleNamespace(start=10_000_000),
                                speed=types.SimpleNamespace(speed=1.0))
    assert TrackOpsMixin._keyframe_base(seg) == (10_000_000, 1.0)
