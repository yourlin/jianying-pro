import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import variants as vr  # noqa: E402

LINES = [
    "你觉得汉字难记吗",
    "其实有个窍门",
    "今天讲人字",
    "人字是一个侧身站立的人",
    "甲骨文里像弯腰的人",
    "后来笔画变直",
    "你看这个字形",
    "再看从字",
    "两个人一前一后",
    "众字是三个人",
    "人多就是众",
    "所以记字先看图",
    "这就是象形",
    "记住了吗",
    "评论区告诉我",
    "关注我下期见",
]


def _project(tmp_path, extra_beat_fields=None):
    """合成转写 (每句 3.5s, 句间 0.3s) + 7 段剧本。"""
    segs, t = [], 0.0
    for x in LINES:
        d = 3.5
        words = [
            {"w": ch, "s": round(t + j * d / len(x), 3), "e": round(t + (j + 1) * d / len(x), 3)}
            for j, ch in enumerate(x)
        ]
        segs.append({"start": round(t, 3), "end": round(t + d, 3), "text": x, "words": words})
        t += d + 0.3
    analysis = {
        "video": {
            "duration": round(t, 2),
            "width": 1080,
            "height": 1920,
            "orientation": "portrait",
        },
        "transcript": segs,
    }
    (tmp_path / "analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False), encoding="utf-8"
    )
    beats = [
        {
            "id": "S01",
            "purpose": "钩子",
            "time": [0, 7.6],
            "elements": [{"kind": "title", "text": "汉字难记?"}],
        },
        {
            "id": "S02",
            "purpose": "点题",
            "time": {"from": "今天讲人字"},
            "elements": [{"kind": "hero", "text": "人"}],
        },
        {
            "id": "S03",
            "purpose": "干货-溯源",
            "time": {"from": "人字是一个", "to": "后来笔画变直"},
            "elements": [{"kind": "keyword", "text": "甲骨文", "time": "甲骨文"}],
        },
        {
            "id": "S04",
            "purpose": "干货-从",
            "time": {"from": "你看这个字形", "to": "两个人一前一后"},
        },
        {"id": "S05", "purpose": "干货-众", "time": {"from": "众字是三个人", "to": "人多就是众"}},
        {"id": "S06", "purpose": "总结", "time": {"from": "所以记字先看图", "to": "这就是象形"}},
        {
            "id": "S07",
            "purpose": "CTA",
            "time": {"from": "记住了吗"},
            "elements": [{"kind": "cta", "text": "关注我", "last": 3}],
        },
    ]
    for k, v in (extra_beat_fields or {}).items():
        next(b for b in beats if b["id"] == k).update(v)
    script = {
        "title": "人字篇",
        "name": "人字篇V1",
        "scene": "talking_head",
        "style": "guofeng",
        "format": "vertical",
        "analysis": "analysis.json",
        "sources": {"main": "main.mp4"},
        "beats": beats,
    }
    p = tmp_path / "script.json"
    p.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    return p


def test_beat_priority_inference():
    assert vr.beat_priority({"purpose": "钩子"}, 3, 7) == 0
    assert vr.beat_priority({"purpose": "CTA"}, 3, 7) == 0
    assert vr.beat_priority({"purpose": "点题"}, 1, 7) == 1
    assert vr.beat_priority({"purpose": "总结"}, 5, 7) == 2
    assert vr.beat_priority({"purpose": "干货"}, 2, 7) == 3
    assert vr.beat_priority({"purpose": "彩蛋"}, 2, 7) == 4
    assert vr.beat_priority({"purpose": "xx", "tags": ["optional"]}, 2, 7) == 4
    assert vr.beat_priority({"purpose": "干货", "priority": 1}, 2, 7) == 1
    assert vr.beat_priority({"elements": [{"kind": "cta"}]}, 4, 7) == 0
    assert vr.beat_priority({}, 0, 7) == 0


def test_plan_keeps_hook_and_cta_and_fits_budget(tmp_path):
    p = _project(tmp_path)
    units, full = vr.units_from_script(json.load(open(p, encoding="utf-8")), str(tmp_path))
    assert round(full, 1) == 60.8
    for target in (15, 30):
        plan = vr.plan_variant(units, target)
        ids = [b["id"] for b in plan["beats"]]
        assert ids[0] == "S01" and ids[-1] == "S07"
        assert plan["duration"] <= target * 1.1 + 1e-6
        assert ids == sorted(ids)  # 原时间顺序
    p30 = vr.plan_variant(units, 30)
    assert "S02" in [b["id"] for b in p30["beats"]]  # 点题优先于干货
    assert set(p30["dropped"]) >= {"S03", "S04"}
    p15 = vr.plan_variant(units, 15)
    cta = p15["beats"][-1]
    assert cta["trimmed"] and cta["range"][1] < 60.8  # 按句子截短 CTA


def test_trim_cuts_on_sentence_boundary(tmp_path):
    units, _ = vr.units_from_script(
        json.load(open(_project(tmp_path), encoding="utf-8")), str(tmp_path)
    )
    s03 = next(u for u in units if u["id"] == "S03")
    t = vr._trimmed(s03, 5.0)
    ends = [e for _, e in s03["sentences"]]
    assert t and any(abs(t["end"] - (e + 0.25)) < 1e-6 or abs(t["end"] - e) < 0.31 for e in ends)
    assert t["end"] - t["start"] <= 5.0


def test_keep_in_and_drop_in(tmp_path):
    p = _project(tmp_path, {"S05": {"keep_in": ["15s"]}, "S02": {"drop_in": [30]}})
    units, _ = vr.units_from_script(json.load(open(p, encoding="utf-8")), str(tmp_path))
    assert "S05" in [b["id"] for b in vr.plan_variant(units, 15)["beats"]]
    assert "S02" not in [b["id"] for b in vr.plan_variant(units, 30)["beats"]]


def test_make_variants_compiles_each_format(tmp_path):
    p = _project(tmp_path)
    res = vr.make_variants(
        str(p), targets=[15, 30, 90], formats=["竖屏", "horizontal"], out_dir=str(tmp_path / "out")
    )
    assert res["formats"] == ["vertical", "horizontal"]
    v15, v30, v90 = res["variants"]
    assert v90.get("skipped")
    for v in (v15, v30):
        assert len(v["builds"]) == 2 and os.path.exists(v["script"])
        for b in v["builds"]:
            sb = json.load(open(b["storyboard"], encoding="utf-8"))
            keep = sb["main"]["keep"]
            total = sum(e - s for s, e in keep)
            assert total <= v["target"] * 1.1 + 1e-6 and abs(total - b["output_duration"]) < 0.01
            for sh in sb["shots"]:  # 镜头和元素都落在保留区间
                assert any(a - 0.06 <= sh["time"][0] < e for a, e in keep)
                for t in sh.get("texts", []):
                    assert any(
                        a - 0.06 <= t["start"] and t["start"] + t["dur"] <= e + 1e-3
                        for a, e in keep
                    )
            for ln in sb["subtitles"]["lines"]:
                assert any(a - 0.06 <= ln["start"] < e for a, e in keep)
            assert sb["compiled_from"]["variant"]["label"] == v["label"]
            assert os.path.isabs(sb["sources"]["main"])
            assert "storyboard_builder.py" in b["build_cmd"]
    names = {b["format"]: b["draft_name"] for b in v15["builds"]}
    assert names == {"vertical": "人字篇V1_15s", "horizontal": "人字篇V1_15s_horizontal"}
    sb15 = json.load(open(v15["builds"][0]["storyboard"], encoding="utf-8"))
    cta = [t for sh in sb15["shots"] for t in sh.get("texts", []) if t.get("content") == "关注我"]
    assert cta  # CTA 的 last 按版本结尾算, 没被裁掉


def test_plan_only_writes_nothing(tmp_path):
    p = _project(tmp_path)
    res = vr.make_variants(str(p), targets=[15], plan_only=True, out_dir=str(tmp_path / "none"))
    assert not os.path.exists(tmp_path / "none")
    assert res["variants"][0]["builds"] == [{"format": "vertical", "style": None}]


def test_storyboard_input_and_format_guard(tmp_path):
    p = _project(tmp_path)
    full = vr.make_variants(str(p), targets=[60], out_dir=str(tmp_path / "full"))
    sb_path = full["variants"][0]["builds"][0]["storyboard"]
    res = vr.make_variants(sb_path, targets=[20], out_dir=str(tmp_path / "sb"))
    v = res["variants"][0]
    assert res["kind"] == "storyboard" and v["builds"][0]["output_duration"] <= 22.0 + 1e-6
    assert v["beats"][0]["id"] == "S01"
    with pytest.raises(vr.VariantError):
        vr.make_variants(
            sb_path, targets=[20], formats=["horizontal"], out_dir=str(tmp_path / "sb2")
        )


def test_restrict_storyboard_without_analysis_uses_ranges():
    sb = {
        "main": {"keep": [[0.0, 0.0]], "camera": [{"at": 1, "dur": 5, "move": "push_in"}]},
        "shots": [
            {
                "id": "A",
                "time": [0, 4],
                "texts": [{"content": "x", "start": 0.3, "dur": 10}],
                "sfx": [{"name": "a", "at": 3.5}, {"name": "b", "at": 9}],
            },
            {"id": "B", "time": [5, 9]},
        ],
        "subtitles": {
            "lines": [{"start": 0, "end": 3.0, "text": "a"}, {"start": 6, "end": 7, "text": "b"}]
        },
    }
    out = vr.restrict_storyboard(sb, [[0.0, 4.0]])
    assert out["main"]["keep"] == [[0.0, 4.0]]
    assert [s["id"] for s in out["shots"]] == ["A"]
    assert out["shots"][0]["texts"][0]["dur"] == 3.7 and len(out["shots"][0]["sfx"]) == 1
    assert out["main"]["camera"][0]["dur"] == 3.0
    assert [ln["text"] for ln in out["subtitles"]["lines"]] == ["a"]


def test_cli_json(tmp_path, capsys):
    p = _project(tmp_path)
    rc = vr._main([str(p), "--targets", "15", "--plan-only", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] and out["data"]["variants"][0]["label"] == "15s"
    rc = vr._main([str(tmp_path / "missing.json"), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["ok"] is False
