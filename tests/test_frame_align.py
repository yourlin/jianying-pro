"""帧对齐 + 转场规整: 写入草稿前按剪映的规则对齐, 剪映打开草稿后不再产生对齐噪声。

实测样本来自剪映规整过的宣传片草稿 (中文 / 英文两版, 30fps):
skill 副本里的值 -> 剪映 template.json 里的值。
"""

import copy
import json
import os
import random
import shutil
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "vendor"))

import edit_diff as ed  # noqa: E402
from utils.frame_align import (  # noqa: E402
    accumulate_ranges,
    frame_us,
    frames_to_us,
    project_fps,
    snap_range,
    snap_source_range,
    snap_us,
    transition_duration,
    us_to_frames,
)

VIDEO = os.path.join(ROOT, "assets", "video.mp4")
FRAME = 1e6 / 30


# ---------------------------------------------------------------- 对齐函数
def test_frame_to_us_floors_like_jianying():
    assert frame_us(30) == pytest.approx(33333.333, abs=1e-3)
    assert frames_to_us(110) == 3_666_666  # 剪映: 3654000 -> 3666666
    assert frames_to_us(304) == 10_133_333
    assert frames_to_us(550) == 18_333_333
    assert frames_to_us(30) == 1_000_000 and frames_to_us(0) == 0
    assert frames_to_us(1, 25) == 40_000 and frames_to_us(1, 60) == 16_666


def test_snap_matches_jianying_samples():
    assert snap_us(3_654_000) == 3_666_666  # video#0 时长
    assert snap_us(50_888_000) == 50_900_000  # audio#0 时长
    assert snap_us(5_067_000) == 5_066_666  # Diagnostic_Test
    assert snap_us(10_134_000) == 10_133_333
    # 已对齐的时间点 (剪映写回的 floor 值) 再对齐不变; 时长是两个帧边界的差, 31 帧有 1033333 / 1033334 两种
    for us in (3_666_666, 7_466_666, 16_266_666, 18_333_333, 0, 1_000_000):
        assert snap_us(us) == us
    assert us_to_frames(3_666_666) == 110 and us_to_frames(50_000) == 2  # .5 帧进位


def test_snap_range_and_source_range():
    # 起点 / 终点分别对齐: 16241000 + 1034000 -> [16233333, 17266666]
    s, d = snap_range(16_241_000, 1_034_000)
    assert (s, s + d) == (frames_to_us(487), frames_to_us(518))
    assert snap_range(0, 1_000)[1] == frames_to_us(1)  # 非零时长至少 1 帧
    assert snap_range(5_000_000, 0) == (5_000_000, 0)
    # 截取范围: 起点 400000 (12 帧) 截 31 帧 -> 终点 43 帧 1433333, 时长 1033333 (剪映实测)
    assert snap_source_range(400_000, 31) == (400_000, 1_033_333)
    assert snap_source_range(340_000, 114) == (333_333, 3_800_000)


def test_project_fps_defaults_to_30():
    class S:
        fps = 25

    class P:
        script = S()

    assert project_fps(P()) == 25 and project_fps({"fps": 60}) == 60
    assert project_fps(None) == 30 and project_fps({"fps": 0}) == 30


# ---------------------------------------------------------------- 主轨累积
def test_main_track_accumulates_frames_without_drift():
    rnd = random.Random(20261102)
    durs = [rnd.randint(300_000, 6_000_000) for _ in range(30)]  # 30 段任意时长 (微秒, 不对齐)
    ranges = accumulate_ranges(durs, 30)
    for (s, d), (s_next, _) in zip(ranges, ranges[1:] + [(None, None)]):
        if s_next is not None:
            assert s + d == s_next  # 首尾相接, 共用帧边界
        assert snap_us(s) == s and snap_us(s + d) == s + d  # 全在帧边界上
    for k in range(len(durs)):
        ideal = sum(durs[: k + 1])
        end = ranges[k][0] + ranges[k][1]
        assert abs(end - ideal) <= FRAME / 2 + 1  # 第 k 段终点误差 ≤ 半帧, 不随段数累积
    # 对照: 逐段四舍五入时长再相加会累积误差 (剪映自己就这样, 实测最大偏移 41ms)
    naive, worst = 0, 0
    for k, d in enumerate(durs):
        naive += snap_us(d)
        worst = max(worst, abs(naive - sum(durs[: k + 1])))
    assert worst > FRAME / 2


def test_accumulate_with_offset_and_zero_lengths():
    r = accumulate_ranges([1_000_000, 0, 500_000], 30, start_us=3_654_000)
    assert r[0][0] == 3_666_666 and r[1][1] == 0 and r[2][0] == r[0][0] + r[0][1]


# ---------------------------------------------------------------- 转场规整
# (转场, 请求时长, 挂转场的片段时长, 后一段时长) -> 剪映实测时长; 片段时长是剪映规整后的值
TRANSITION_SAMPLES = [
    ("叠化", 500_000, 3_666_666, 3_800_000, 466_666),
    ("泛光", 600_000, 3_800_000, 3_600_000, 600_000),
    ("变速推近", 600_000, 3_600_000, 5_200_000, 600_000),
    ("极速甩镜", 600_000, 5_200_000, 1_033_334, 466_666),  # 后一段 1.03s -> 上限 516.7ms
    ("光刃破空", 700_000, 1_033_333, 5_166_667, 466_666),  # 本段 1.03s
    ("电流冲击", 600_000, 5_166_667, 3_633_333, 600_000),
    ("分层甩动", 500_000, 3_633_333, 3_600_000, 466_666),
    ("条带甩动", 500_000, 3_600_000, 3_633_334, 466_666),
    ("分层甩动", 500_000, 3_633_334, 3_600_000, 466_666),
    ("交叠扫光", 600_000, 3_600_000, 1_566_666, 600_000),
    ("叠化", 700_000, 1_533_333, 4_133_334, 666_666),
]


@pytest.mark.parametrize("name,req,seg,nxt,expected", TRANSITION_SAMPLES)
def test_transition_rule_matches_jianying(name, req, seg, nxt, expected):
    assert transition_duration(req, seg, nxt, 30) == expected


def test_transition_rule_edges():
    assert transition_duration(600_000, 5_000_000, None) == 600_000  # 没有后一段: 只看本段
    assert transition_duration(400_000, 900_000, 5_000_000) == 400_000  # 上限 450ms -> 12 帧
    assert transition_duration(500_000, 100_000, 5_000_000) == 0  # 3 帧的片段放不下 2 帧
    assert transition_duration(500_000, 5_000_000, 5_000_000, 25) == 480_000  # 25fps: 12 帧
    # 幂等: 规整后的值 (466666 = 14 帧, 666666 = 20 帧) 再规整不变
    for _name, req, seg, nxt, expected in TRANSITION_SAMPLES:
        assert transition_duration(expected, seg, nxt, 30) == expected


# ---------------------------------------------------------------- 生成草稿 + 模拟剪映规整
def simulate_jianying(content: dict) -> dict:
    """按实测规则模拟剪映打开草稿: 每段时长取整帧, 主轨首尾相接逐段累加; 帧号换微秒向下取整;
    截取起点 / 终点落在帧边界; 重叠转场按 transition_duration 截短。"""
    d = copy.deepcopy(content)
    fps = float(d.get("fps") or 30)
    trans = {m["id"]: m for m in d["materials"].get("transitions", [])}
    for track in d["tracks"]:
        prev_ef = None
        for s in sorted(track["segments"], key=lambda x: x["target_timerange"]["start"]):
            tt, st = s["target_timerange"], s.get("source_timerange")
            sf = us_to_frames(tt["start"], fps)
            if prev_ef is not None and abs(sf - prev_ef) <= 1:  # 主轨吸附: 贴上一段终点
                sf = prev_ef
            ef = sf + us_to_frames(tt["duration"], fps)
            tt["start"], tt["duration"] = frames_to_us(sf, fps), frames_to_us(
                ef, fps
            ) - frames_to_us(sf, fps)
            if st:
                st["start"], st["duration"] = snap_source_range(st["start"], ef - sf, fps)
            prev_ef = ef
        segs = track["segments"]
        for i, s in enumerate(segs):
            for r in s.get("extra_material_refs", []):
                m = trans.get(r)
                if m and m.get("is_overlap"):
                    nxt = segs[i + 1]["target_timerange"]["duration"] if i + 1 < len(segs) else None
                    m["duration"] = transition_duration(
                        m["duration"], s["target_timerange"]["duration"], nxt, fps
                    )
    return d


@pytest.fixture()
def drafts_root():
    root = tempfile.mkdtemp(prefix="jy_frame_")
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _build_draft(drafts_root):
    from jy_wrapper import JyProject

    p = JyProject("FrameAlign", drafts_root=drafts_root, overwrite=True)
    # 和实测宣传片样本同样的不对齐时长; 起点用秒字符串逐段累加 (旧写法)
    plan = [(0.3, 3.654), (0.34, 3.789), (0.3, 3.622), (0.4, 1.034), (0.4, 1.045), (0.3, 3.621)]
    t, segs = 0.0, []
    for src, dur in plan:
        segs.append(
            p.add_media_safe(VIDEO, f"{t:.3f}s", f"{dur:.3f}s", "Main", source_start=f"{src:.3f}s")
        )
        t += dur
    for seg, dur in zip(segs[:-1], ("0.5s", "0.6s", "0.6s", "0.7s", "0.5s")):
        assert p.add_transition_simple("叠化", seg, duration=dur)
    p.add_audio_safe(os.path.join(ROOT, "assets", "audio.mp3"), "0.123s", "3.654s", "BGM")
    p.add_text_simple("标题", 1_000_000, 2_000_000, track_name="Text")
    p.add_effect_simple("Glitch", "1.234s", "2.345s")
    p.save()
    with open(os.path.join(drafts_root, "FrameAlign", "draft_info.json"), encoding="utf-8") as f:
        return p, json.load(f)


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="需要 ffprobe")
def test_generated_draft_is_fixed_point_of_jianying_normalization(drafts_root):
    p, content = _build_draft(drafts_root)
    main = next(t for t in content["tracks"] if t["type"] == "video")["segments"]
    assert len(main) == 6
    for a, b in zip(main, main[1:]):  # 首尾相接
        assert (
            a["target_timerange"]["start"] + a["target_timerange"]["duration"]
            == b["target_timerange"]["start"]
        )
    for s in main:
        for v in (s["target_timerange"]["start"], s["source_timerange"]["start"]):
            assert snap_us(v) == v
    assert main[0]["target_timerange"]["duration"] == 3_666_666
    # 转场已按剪映规则规整: 0.5s -> 466.7ms (偶数帧); 0.6s 接 1.03s 的片段、0.7s 挂在 1.03s 的片段上 -> 466.7ms
    durs = [m["duration"] for m in content["materials"]["transitions"]]
    assert sorted(durs) == [466_666, 466_666, 466_666, 466_666, 600_000]
    assert any("请求 700ms" in w for w in p.warnings)
    assert not any("请求 500ms" in w for w in p.warnings)  # 只取偶数帧 (6.7%) 不警告
    rep = {r["requested"]: r["duration"] for r in p.transition_report()}
    assert rep[0.7] == 0.4667

    audio = [s for t in content["tracks"] if t["type"] == "audio" for s in t["segments"]]
    assert len(audio) == 1 and audio[0]["target_timerange"]["start"] == snap_us(123_000)

    sim = simulate_jianying(content)
    res = ed.diff_drafts(content, sim)
    assert res["modified"] is False, res["changes"]
    assert res["noise"].get("frame_snap", 0) == 0
    assert res["noise"].get("transition_auto", 0) == 0

    # 比 edit_diff 的容差更严: 每个时间值逐微秒不变 (剪映规整的不动点)
    def times(c):
        out = [m["duration"] for m in c["materials"]["transitions"]]
        for t in c["tracks"]:
            for s in t["segments"]:
                out.append(tuple(s["target_timerange"].values()))
                out.append(tuple((s.get("source_timerange") or {}).values()))
        return out

    assert times(sim) == times(content)


def test_simulator_detects_unaligned_draft():
    """对照: 不对齐的旧草稿模拟规整后会产生对齐噪声 (证明上面的 0 不是模拟器的空操作)。"""
    seg = lambda s, d: {  # noqa: E731
        "id": f"s{s}",
        "material_id": "v",
        "extra_material_refs": [],
        "target_timerange": {"start": s, "duration": d},
        "source_timerange": {"start": 300_000, "duration": d},
    }
    content = {
        "fps": 30,
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 7_443_000,
        "materials": {"videos": [{"id": "v", "path": "/x/a.mp4"}], "transitions": []},
        "tracks": [{"type": "video", "segments": [seg(0, 3_654_000), seg(3_654_000, 3_789_000)]}],
    }
    res = ed.diff_drafts(content, simulate_jianying(content))
    assert res["modified"] is False and res["noise"].get("frame_snap", 0) > 0


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="需要 ffprobe")
def test_storyboard_builder_writes_frame_aligned(drafts_root, monkeypatch):
    import jy_wrapper
    from storyboard_builder import StoryboardBuilder

    class TmpProject(jy_wrapper.JyProject):  # 只写临时 drafts_root, 不碰真实剪映草稿目录
        def __init__(self, name, **kw):
            super().__init__(name, drafts_root=drafts_root, **kw)

    monkeypatch.setattr(jy_wrapper, "JyProject", TmpProject)
    sb = {
        "project": {"name": "SbFrame", "canvas": {"width": 1080, "height": 1920}},
        "sources": {"main": VIDEO},
        "main": {
            "source": "main",
            "keep": [[0.3, 1.334], [1.5, 2.545], [2.6, 3.633], [3.7, 4.95]],
            "transitions": [
                {"name": "叠化", "duration": 0.5},
                {"name": "叠化", "duration": 0.7},
                None,
            ],
        },
        "shots": [
            {
                "id": "s1",
                "time": [0.3, 2.0],
                "texts": [{"content": "标题", "start": 0.41, "dur": 1.234}],
            }
        ],
    }
    b = StoryboardBuilder(sb, ROOT)
    res = b.build()
    with open(os.path.join(drafts_root, "SbFrame", "draft_info.json"), encoding="utf-8") as f:
        content = json.load(f)
    main = next(t for t in content["tracks"] if t["type"] == "video")["segments"]
    assert len(main) == 4
    for a, c in zip(main, main[1:]):
        assert (
            a["target_timerange"]["start"] + a["target_timerange"]["duration"]
            == c["target_timerange"]["start"]
        )
    # 成片总长 = 4 段理想时长之和 (4.362s) 对齐到最近的帧, 不累积误差
    assert abs(res["report"]["total"] * 1e6 - 4_362_000) <= FRAME / 2 + 1000
    assert [t["duration"] for t in res["report"]["transitions"]] == [0.4667, 0.4667]
    assert any("请求 700ms" in w for w in res["report"]["warnings"])
    sim = simulate_jianying(content)
    noise = ed.diff_drafts(content, sim)["noise"]
    assert noise.get("frame_snap", 0) == 0 and noise.get("transition_auto", 0) == 0
