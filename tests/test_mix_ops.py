"""T4 自动 ducking: 包络计算、人声区间来源、音量关键帧写入草稿 (新建 / 重新加载的草稿)。"""

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "vendor"))

import pytest  # noqa: E402
from core.mix_ops import (  # noqa: E402
    MixOpsMixin,
    db_to_gain,
    duck_envelope,
    get_volume_keyframes,
    merge_ranges,
    voice_ranges_from_analysis,
)
from jy_wrapper import JyProject  # noqa: E402

AUDIO = os.path.join(ROOT, "assets", "audio.mp3")  # 5.2s


class P(JyProject, MixOpsMixin):
    pass


@pytest.fixture()
def drafts():
    root = tempfile.mkdtemp(prefix="jy_mix_")
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _reload(name, drafts):
    """重新加载草稿 (导入模式)。vendor pyJianYingDraft 的导入模式依赖类注解, Python 3.14 下读不到 -> skip。"""
    try:
        return P(name, drafts_root=drafts, overwrite=False)
    except KeyError as e:
        pytest.skip(f"pyJianYingDraft 导入模式在 Python {sys.version_info[:2]} 下不可用: {e}")


def _segments(p, track):
    with open(os.path.join(p.root, p.name, "draft_info.json"), encoding="utf-8") as f:
        content = json.load(f)
    return next(t for t in content["tracks"] if t["name"] == track)["segments"]


def _volume_kfs(seg_json):
    lists = [
        k for k in seg_json.get("common_keyframes", []) if k["property_type"] == "KFTypeVolume"
    ]
    return lists


def test_merge_ranges_and_gain():
    assert merge_ranges([(3, 4), {"start": 1, "end": 2}, (1.5, 2.5)]) == [(1.0, 2.5), (3.0, 4.0)]
    assert merge_ranges([(1, 2), (2.2, 3)], gap=0.3) == [(1.0, 3.0)]
    assert abs(db_to_gain(-10) - 0.316228) < 1e-5


def test_envelope_attack_release_and_hold():
    pts = duck_envelope(
        0, 10, [(2, 4), (4.5, 6), (8, 9)], depth_db=-10, attack=0.15, release=0.3, hold=0.3
    )
    d = db_to_gain(-10)
    times = [t for t, _ in pts]
    assert times == sorted(times)
    # 2~4 与 4.5~6 间隔 0.5 < attack+release+hold=0.75 -> 合并, 中间不恢复
    assert (1.85, 1.0) in pts and (2.0, round(d, 6)) in pts and (6.0, round(d, 6)) in pts
    assert (6.3, 1.0) in pts and not any(4.0 < t < 4.5 for t in times)
    assert (7.85, 1.0) in pts and (9.3, 1.0) in pts
    # 片段从人声中间开始 / 结束: 首尾点按插值给出
    pts2 = duck_envelope(5, 8, [(1, 6), (8.1, 20)], attack=0.2, release=0.4)
    assert pts2[0] == (0.0, round(d, 6))
    assert pts2[-1][0] == 3.0 and abs(pts2[-1][1] - (1 - (1 - d) * 0.5)) < 1e-4
    # 没有重叠的人声 -> 不写关键帧
    assert duck_envelope(0, 5, [(6, 7)]) == []
    # 基准音量非 1 时按比例
    pts3 = duck_envelope(0, 5, [(1, 2)], depth_db=-6, base_volume=0.5)
    assert min(v for _, v in pts3) == round(0.5 * db_to_gain(-6), 6)


def test_voice_ranges_from_analysis():
    a = {
        "video": {"duration": 10, "has_audio": True},
        "silences": [
            {"start": 0, "end": 1.2, "duration": 1.2},
            {"start": 5, "end": 6, "duration": 1},
        ],
        "transcript": [
            {"start": 1.3, "end": 3.0, "text": "a"},
            {"start": 3.0, "end": 4.9, "text": "b"},
            {"start": 7.0, "end": 7.05, "text": "嗯"},
        ],
    }
    assert voice_ranges_from_analysis(a) == [(1.3, 4.9)]  # 太短的句子丢弃
    assert voice_ranges_from_analysis(a, "transcript", offset=2) == [(3.3, 6.9)]
    assert voice_ranges_from_analysis(a, "silences") == [(1.2, 5.0), (6.0, 10.0)]
    a.pop("transcript")
    assert voice_ranges_from_analysis(a) == [(1.2, 5.0), (6.0, 10.0)]
    with pytest.raises(ValueError):
        voice_ranges_from_analysis({"silences": []}, "silences")


def test_auto_duck_writes_volume_keyframes(drafts):
    p = P("DuckDemo", drafts_root=drafts, overwrite=True)
    bgm = p.add_audio_safe(AUDIO, "0s", "5s", track_name="BGM", volume=0.8)
    p.add_audio_safe(AUDIO, "1s", "1s", track_name="配音")
    p.add_audio_safe(AUDIO, "3s", "1s", track_name="配音")
    assert bgm is not None
    res = p.auto_duck(depth_db=-10, attack=0.15, release=0.3, hold=0.3)
    assert res["bgm_tracks"] == ["BGM"] and res["voice_ranges"] == [(1.0, 2.0), (3.0, 4.0)]
    kfs = get_volume_keyframes(bgm)
    ducked = round(0.8 * db_to_gain(-10), 6)
    assert (850_000, 0.8) in kfs and (1_000_000, ducked) in kfs and (4_300_000, 0.8) in kfs
    p.save()
    seg = _segments(p, "BGM")[0]
    lists = _volume_kfs(seg)
    assert len(lists) == 1 and len(lists[0]["keyframe_list"]) == len(kfs)
    assert seg["volume"] == 0.8


def test_auto_duck_on_reloaded_draft_replaces_keyframes(drafts):
    p = P("DuckDemo", drafts_root=drafts, overwrite=True)
    p.add_audio_safe(AUDIO, "0s", "5s", track_name="BGM", volume=0.8)
    p.add_audio_safe(AUDIO, "1s", "1s", track_name="配音")
    p.auto_duck()
    p.save()
    # 重新加载 (导入模式片段) 再做一次: 替换而不是叠加; analysis 来源 + 偏移
    q = _reload("DuckDemo", drafts)
    analysis = {"video": {"duration": 5}, "transcript": [{"start": 0.5, "end": 1.5, "text": "x"}]}
    res2 = q.auto_duck(
        bgm_track="BGM", voice_tracks=["配音"], analysis=analysis, analysis_offset=2.0, depth_db=-12
    )
    assert res2["voice_ranges"] == [(1.0, 2.0), (2.5, 3.5)]
    q.save()
    lists = _volume_kfs(_segments(q, "BGM")[0])
    assert len(lists) == 1
    vals = [k["values"][0] for k in lists[0]["keyframe_list"]]
    assert min(vals) == round(0.8 * db_to_gain(-12), 6)


def test_keyframes_use_source_time(drafts):
    p = P("DuckSrc", drafts_root=drafts, overwrite=True)
    bgm = p.add_audio_safe(AUDIO, "2s", "2s", track_name="BGM")
    bgm.source_timerange.start = 1_000_000  # 从素材 1s 处截取
    res = p.duck_bgm(bgm, voice_ranges=[(2.5, 3.0)], attack=0.1, release=0.1)
    assert res["points"][1] == (0.4, 1.0)
    # 素材时间 = 截取起点 1s + 相对 0.4s
    assert (1_400_000, 1.0) in get_volume_keyframes(bgm)
    dry = p.duck_bgm(bgm, voice_ranges=[], dry_run=True)
    assert dry["points"] == [] and dry["keyframes"] == 0


def test_auto_duck_errors(drafts):
    p = P("DuckErr", drafts_root=drafts, overwrite=True)
    with pytest.raises(KeyError):
        p.auto_duck()
    p.add_audio_safe(AUDIO, "0s", "3s", track_name="BGM")
    with pytest.raises(ValueError):
        p.auto_duck()
    with pytest.raises(KeyError):
        p.auto_duck(voice_tracks=["不存在"])


def test_cli_dry_run(drafts, capsys):
    from core.mix_ops import _main

    p = P("DuckCli", drafts_root=drafts, overwrite=True)
    p.add_audio_safe(AUDIO, "0s", "5s", track_name="BGM")
    p.add_audio_safe(AUDIO, "1s", "2s", track_name="TTS")
    p.save()
    _reload("DuckCli", drafts)
    assert _main(["--draft", "DuckCli", "--root", drafts, "--json"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] and out["data"]["keyframes"] > 0
    assert _volume_kfs(_segments(p, "BGM")[0])
