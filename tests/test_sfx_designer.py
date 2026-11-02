"""T5 音效设计: 规则映射、密度控制、事件来源 (文件 / analysis / 节拍 / 草稿)、写入草稿、CLI。"""

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "vendor"))

import pytest  # noqa: E402
import sfx_designer as sd  # noqa: E402
from core.mix_ops import MixOpsMixin  # noqa: E402
from jy_wrapper import JyProject, draft  # noqa: E402

AUDIO = os.path.join(ROOT, "assets", "audio.mp3")
VIDEO = os.path.join(ROOT, "assets", "video.mp4")


class P(JyProject, MixOpsMixin):
    pass


def _row(i, cat, dur, source="jianying_cloud", vip="0", path=""):
    return {
        "id": str(i),
        "name": f"{cat}{i}",
        "category": cat,
        "duration_s": dur,
        "source": source,
        "is_vip": vip,
        "available": "1" if vip == "0" else "0",
        "path": path,
        "tags": "",
    }


LIB = [
    _row(1, "whoosh", 0.4),
    _row(2, "whoosh", 1.0),
    _row(3, "whoosh", 3.0),
    _row(4, "pop", 0.3),
    _row(5, "hit", 1.0),
    _row(6, "ding", 0.8),
    _row(7, "riser", 4.0),
    _row(8, "click", 0.2),
    _row(9, "typing", 15.0),
    _row(10, "camera", 1.0),
    _row(11, "hit", 0.5, vip="1"),
]


@pytest.fixture()
def tmp():
    d = tempfile.mkdtemp(prefix="jy_sfx_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_rule_mapping_offset_volume_align():
    plan = sd.design(
        [
            {"type": "转场", "time": 3.0},
            {"type": "reveal", "time": 10.0},
            {"type": "typing", "time": 20.0, "duration": 2.5},
            {"type": "photo", "time": 30.0},
        ],
        library=LIB,
    )
    by = {x["event_type"]: x for x in plan["items"]}
    w = by["transition"]
    assert w["category"] == "whoosh" and w["start"] == pytest.approx(2.9)  # whoosh 提前 0.1s
    assert w["volume_db"] == -15 and w["volume"] == pytest.approx(10 ** (-15 / 20), abs=1e-4)
    assert by["reveal"]["category"] == "riser" and by["reveal"]["start"] == pytest.approx(
        6.0
    )  # 终点对齐
    t = by["typing"]
    assert t["duration"] == pytest.approx(2.5) and t["trimmed"]  # 截到事件时长
    assert by["photo"]["category"] == "camera"
    assert [x["start"] for x in plan["items"]] == sorted(x["start"] for x in plan["items"])


def test_density_limits_and_priority():
    beats = [{"type": "beat", "time": round(i * 0.25, 2)} for i in range(40)]  # 10s 内 40 个卡点
    plan = sd.design(beats + [{"type": "emphasis", "time": 5.0}], library=LIB, max_per_window=4)
    starts = [x["start"] for x in plan["items"]]
    assert any(x["event_type"] == "emphasis" for x in plan["items"])  # 高优先级先占位
    assert all(b - a >= sd.DEFAULT_MIN_GAP - 1e-9 for a, b in zip(starts, starts[1:]))
    for a in starts:
        assert sum(1 for b in starts if a <= b < a + 10) <= 4
    reasons = {d["reason"] for d in plan["dropped"]}
    assert reasons & {"too_close", "density_limit", "same_category_too_close"}
    assert plan["stats"]["placed"] == len(plan["items"]) and plan["stats"]["dropped"] == len(
        plan["dropped"]
    )


def test_quiet_rules_skip_voice_and_cut_disabled_by_default():
    evs = [
        {"type": "beat", "time": 2.0},
        {"type": "beat", "time": 8.0},
        {"type": "cut", "time": 12.0},
    ]
    plan = sd.design(evs, library=LIB, voice_ranges=[(1.0, 5.0)])
    assert [x["time"] for x in plan["items"]] == [8.0]
    assert {d["reason"] for d in plan["dropped"]} == {"during_voice", "no_rule"}
    on = sd.design(evs, library=LIB, rules={"cut": {"categories": ["whoosh"], "volume_db": -20}})
    assert any(x["event_type"] == "cut" for x in on["items"])
    off = sd.design([{"type": "transition", "time": 1}], library=LIB, rules={"transition": None})
    assert off["items"] == []


def test_rotation_trim_vip_override_and_timeline_clamp():
    plan = sd.design([{"type": "transition", "time": t} for t in (2, 6, 10)], library=LIB)
    ids = [x["sfx_id"] for x in plan["items"]]
    assert len(set(ids)) == 3  # 轮换, 不重复同一个
    long = next(x for x in plan["items"] if x["sfx_id"] == "3")
    assert long["duration"] == 1.5 and long["trimmed"]  # 超过 max_dur 截断
    # 会员音效默认不选
    hits = sd.design([{"type": "hook", "time": 0}, {"type": "emphasis", "time": 3}], library=LIB)
    assert "11" not in [x["sfx_id"] for x in hits["items"]]
    assert "11" in [
        x["sfx_id"]
        for x in sd.design(
            [{"type": "hook", "time": 0, "sfx_id": "11"}], library=LIB, allow_vip=True
        )["items"]
    ]
    # 指定 sfx_id / category
    p = sd.design(
        [
            {"type": "emphasis", "time": 1, "sfx_id": "10"},
            {"type": "emphasis", "time": 5, "category": "click"},
            {"type": "emphasis", "time": 9, "sfx_id": "nope"},
        ],
        library=LIB,
    )
    assert [x["sfx_id"] for x in p["items"]] == ["10", "8"]
    assert p["dropped"][0]["reason"] == "sfx_id_not_found:nope"
    # 时间线外的事件丢弃, 结尾音效不超出时间线
    end = sd.design(
        [{"type": "ending", "time": 10}, {"type": "pop", "time": 50}], library=LIB, duration=10
    )
    e = end["items"][0]
    assert e["start"] + e["duration"] <= 10 + 1e-6
    assert any(d["reason"] in ("out_of_timeline", "no_rule") for d in end["dropped"])


def test_load_events_and_aliases(tmp):
    path = os.path.join(tmp, "events.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "duration": 12,
                "events": [
                    {"type": "强调词", "time": 1},
                    {"type": "Transition", "start": 2},
                    {"time": 3},
                ],
            },
            f,
        )
    evs, dur = sd.load_events(path)
    assert dur == 12 and [e["type"] for e in evs] == ["emphasis", "transition"]


def test_events_from_beats_formats():
    assert [e["time"] for e in sd.events_from_beats([1, 2.5])] == [1.0, 2.5]
    assert [e["time"] for e in sd.events_from_beats([{"time": 1}, {"t": 2}], offset=1)] == [
        2.0,
        3.0,
    ]
    d = {"beats": [0.5, 1, 1.5, 2], "downbeats": [1, 2]}
    assert [e["time"] for e in sd.events_from_beats(d)] == [1.0, 2.0]
    assert len(sd.events_from_beats(d, strong_only=False)) == 4


def test_events_from_analysis_keywords_and_scenes():
    a = {
        "scenes": [4.2],
        "transcript": [
            {
                "start": 0.0,
                "end": 2.0,
                "text": "这个方法完全免费",
                "words": [
                    {"w": "这个", "s": 0.0, "e": 0.4},
                    {"w": "方法", "s": 0.4, "e": 0.9},
                    {"w": "完全", "s": 0.9, "e": 1.3},
                    {"w": "免费", "s": 1.3, "e": 2.0},
                ],
            },
            {"start": 3.0, "end": 5.0, "text": "重点来了", "words": []},
        ],
    }
    evs = sd.events_from_analysis(a, ["免费", "重点"], offset=10)
    emph = sorted((e["word"], e["time"]) for e in evs if e["type"] == "emphasis")
    assert emph == [("免费", 11.3), ("重点", 13.0)]
    assert {"type": "cut", "time": 14.2} in evs
    assert sd.events_from_analysis(a, [], scenes_as_cuts=False) == []


def test_events_from_draft(tmp):
    p = P("SfxEvents", drafts_root=tmp, overwrite=True)
    a = p.add_media_safe(VIDEO, "0s", "2s", track_name="Main")
    p.add_media_safe(VIDEO, "2s", "2s", track_name="Main")
    p.add_media_safe(VIDEO, "4s", "2s", track_name="Main")
    a.add_transition(next(iter(draft.TransitionType)))
    p.add_text_simple("大标题", "0s", "2s", track_name="Title")
    p.add_text_simple("要点", "3s", "1s", track_name="Title")
    for i in range(10):
        p.add_text_simple(f"字幕{i}", f"{i * 0.6:.1f}s", "0.5s", track_name="Captions")
    evs, total = sd.events_from_draft(p)
    assert total == pytest.approx(6.0)
    got = sorted((e["type"], e["time"]) for e in evs)
    assert ("transition", 2.0) in got and ("cut", 4.0) in got
    assert ("title", 0.0) in got and ("text_in", 3.0) in got and ("ending", 6.0) in got
    assert not any(e.get("track") == "Captions" for e in evs)  # 字幕轨不配音效


class _FakeCloud:
    def __init__(self, path):
        self.path = path
        self.calls = []

    def download_asset(self, query, force=False):
        self.calls.append(query)
        return self.path if query == "100" else None


def test_apply_plan_local_and_cloud(tmp):
    sfx = os.path.join(tmp, "whoosh.mp3")
    shutil.copy(AUDIO, sfx)
    lib = [
        _row("local:whoosh.mp3", "whoosh", 5.2, source="local", path=sfx),
        _row(100, "pop", 0.3),
        _row(200, "hit", 1.0),
    ]
    plan = sd.design(
        [
            {"type": "transition", "time": 1.0},
            {"type": "text_in", "time": 3.0},
            {"type": "emphasis", "time": 6.0},
        ],
        library=lib,
    )
    assert [x["source"] for x in plan["items"]] == ["local", "jianying_cloud", "jianying_cloud"]
    p = P("SfxApply", drafts_root=tmp, overwrite=True)
    p._cloud_manager = _FakeCloud(sfx)
    res = p.apply_sfx_plan(plan)
    assert (
        len(res["added"]) == 2 and len(res["failed"]) == 1 and res["failed"][0]["sfx_id"] == "200"
    )
    assert p._cloud_manager.calls == ["100", "200"]
    p.save()
    with open(os.path.join(tmp, "SfxApply", "draft_info.json"), encoding="utf-8") as f:
        content = json.load(f)
    segs = [s for t in content["tracks"] if t["name"].startswith("SFX") for s in t["segments"]]
    assert len(segs) == 2
    first = min(segs, key=lambda s: s["target_timerange"]["start"])
    assert (
        first["target_timerange"]["start"] == 900_000
        and first["target_timerange"]["duration"] == 1_500_000
    )
    assert first["volume"] == pytest.approx(10 ** (-15 / 20), abs=1e-3)


def test_cli_plan_and_write(tmp, capsys):
    sfx_dir = os.path.join(tmp, "sfx", "whoosh")
    os.makedirs(sfx_dir)
    shutil.copy(AUDIO, os.path.join(sfx_dir, "a.mp3"))
    empty_lib = os.path.join(tmp, "lib.csv")
    with open(empty_lib, "w", encoding="utf-8") as f:
        f.write(
            ",".join(
                [
                    "id",
                    "name",
                    "category",
                    "duration_s",
                    "source",
                    "is_vip",
                    "available",
                    "path",
                    "tags",
                ]
            )
            + "\n"
        )
    events = os.path.join(tmp, "events.json")
    with open(events, "w", encoding="utf-8") as f:
        json.dump([{"type": "transition", "time": 1.0}, {"type": "transition", "time": 4.0}], f)
    out = os.path.join(tmp, "plan.json")
    args = [
        "plan",
        "--events",
        events,
        "--library",
        empty_lib,
        "--sfx-dir",
        os.path.join(tmp, "sfx"),
        "-o",
        out,
        "--json",
    ]
    assert sd._main(args) == 0
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert res["ok"] and res["data"]["stats"]["placed"] == 2
    with open(out, encoding="utf-8") as f:
        assert len(json.load(f)["items"]) == 2
    assert sd._main(["plan", "--library", empty_lib]) == 1  # 没有事件
    assert sd._main(["rules", "--json"]) == 0
    assert "transition" in capsys.readouterr().out

    p = P("SfxCli", drafts_root=tmp, overwrite=True)
    p.add_media_safe(VIDEO, "0s", "5s", track_name="Main")
    p.save()
    try:
        JyProject("SfxCli", drafts_root=tmp, overwrite=False)
    except KeyError as e:  # vendor 导入模式在 Python 3.14 下不可用
        pytest.skip(f"pyJianYingDraft 导入模式不可用: {e}")
    assert sd._main(["apply", "--plan", out, "--draft", "SfxCli", "--root", tmp]) == 0
    with open(os.path.join(tmp, "SfxCli", "draft_info.json"), encoding="utf-8") as f:
        content = json.load(f)
    assert sum(len(t["segments"]) for t in content["tracks"] if t["name"].startswith("SFX")) == 2
