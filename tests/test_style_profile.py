# ruff: noqa: E402
"""style_profile.py 测试: 用临时目录合成草稿 JSON, 不依赖真实草稿。"""

import json
import os
import shutil
import sys
import tempfile
import unittest

current_dir = os.path.dirname(os.path.abspath(__file__))
skill_root = os.path.dirname(current_dir)
p = os.path.join(skill_root, "scripts")
if p not in sys.path:
    sys.path.insert(0, p)

import style_profile as sp

S = 1_000_000


def _seg(sid, mid, start, dur, **kw):
    seg = {
        "id": sid,
        "material_id": mid,
        "target_timerange": {"start": int(start * S), "duration": int(dur * S)},
        "extra_material_refs": kw.pop("refs", []),
    }
    seg.update(kw)
    return seg


def _text_mat(mid, text, size=8.0, color=(1, 1, 1), stroke=None, mtype="subtitle", **kw):
    style = {"size": size, "fill": {"content": {"solid": {"color": list(color)}}}, "font": {}}
    if stroke:
        style["strokes"] = [{"width": stroke, "content": {"solid": {"color": [0, 0, 0]}}}]
    m = {"id": mid, "type": mtype, "content": json.dumps({"text": text, "styles": [style]})}
    m.update(kw)
    return m


def vertical_draft():
    """竖屏 30s: 主轨 5 段 (6s), 字幕 10 条, 1 条花字标题, BGM / TTS / 音效, 1 个转场, 1 次推镜。"""
    mats = {
        "videos": [{"id": "V1", "type": "video"}, {"id": "P1", "type": "photo"}],
        "texts": [],
        "effects": [{"id": "FX1", "type": "text_effect", "name": "蓝色发光"}],
        "transitions": [{"id": "TR1", "name": "叠化", "duration": 500000}],
        "audios": [
            {"id": "BGM", "type": "music", "name": "动感科技"},
            {"id": "TTS", "type": "text_to_audio", "name": "大家好", "tone_type": "皇上Pro"},
            {"id": "SFX", "type": "sound", "name": "叮"},
        ],
        "audio_fades": [{"id": "FADE", "fade_in_duration": 0, "fade_out_duration": 2 * S}],
        "material_animations": [
            {
                "id": "ANI",
                "type": "sticker_animation",
                "animations": [{"type": "in", "name": "向上弹入", "duration": 300000}],
            }
        ],
    }
    main = []
    for i in range(5):
        refs = ["TR1"] if i == 1 else []
        main.append(_seg(f"m{i}", "V1" if i % 2 else "P1", i * 6, 6, refs=refs))
    main[2]["common_keyframes"] = [
        {
            "property_type": "KFTypeScaleX",
            "keyframe_list": [
                {"time_offset": 0, "values": [1.0]},
                {"time_offset": 3 * S, "values": [1.2]},
            ],
        }
    ]
    subs = []
    for i in range(10):
        mats["texts"].append(_text_mat(f"T{i}", "一二三四五六七八", size=10.0))
        subs.append(_seg(f"s{i}", f"T{i}", i * 3, 2.5, clip={"transform": {"x": 0, "y": -0.6}}))
    mats["texts"].append(_text_mat("H1", "重点", size=20.0, mtype="text", stroke=0.08))
    title = _seg(
        "h1",
        "H1",
        0.5,
        2.0,
        refs=["FX1", "ANI"],
        clip={"transform": {"x": 0, "y": 0.5}, "scale": {"x": 0.5, "y": 0.5}},
    )
    pip = _seg(
        "p1", "V1", 6, 6, clip={"transform": {"x": 0.4, "y": -0.3}, "scale": {"x": 0.35, "y": 0.35}}
    )
    tracks = [
        {"type": "video", "segments": main},
        {"type": "video", "segments": [pip]},
        {"type": "text", "segments": subs},
        {"type": "text", "segments": [title]},
        {"type": "audio", "segments": [_seg("b", "BGM", 0, 30, volume=0.15, refs=["FADE"])]},
        {"type": "audio", "segments": [_seg("t", "TTS", 0.8, 2, speed=1.2)]},
        {"type": "audio", "segments": [_seg("x", "SFX", 6.1, 0.5)]},
    ]
    return {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 30 * S,
        "tracks": tracks,
        "materials": mats,
    }


def tutorial_draft():
    """横屏 240s, 字幕轨名 Subtitles, 每 4s 一条 -> 长教程。素材 id 用 32 位十六进制 (像脚本生成)。"""
    hexid = "0123456789abcdef0123456789abcdef"
    mats = {"videos": [{"id": hexid, "type": "video"}], "texts": [], "audios": []}
    main = [_seg(f"m{i}", hexid, i * 20, 20) for i in range(12)]
    subs = []
    for i in range(60):
        mid = f"{i:032x}"
        mats["texts"].append(
            _text_mat(mid, "这是一条教程字幕", size=5.0, stroke=0.08, mtype="text")
        )
        subs.append(_seg(f"s{i}", mid, i * 4, 3.5, clip={"transform": {"x": 0, "y": -0.8}}))
    return {
        "canvas_config": {"width": 1920, "height": 1080},
        "duration": 240 * S,
        "tracks": [
            {"type": "video", "name": "VideoTrack", "segments": main},
            {"type": "text", "name": "Subtitles", "segments": subs},
        ],
        "materials": mats,
    }


def short_horizontal_draft():
    mats = {"videos": [{"id": "V", "type": "video"}], "texts": [], "audios": []}
    main = [_seg(f"m{i}", "V", i * 3, 3) for i in range(10)]
    mats["texts"].append(_text_mat("T", "片名", size=12.0, mtype="text"))
    return {
        "canvas_config": {"width": 1920, "height": 1080},
        "duration": 30 * S,
        "tracks": [
            {"type": "video", "segments": main},
            {"type": "text", "segments": [_seg("t", "T", 0, 3)]},
        ],
        "materials": mats,
    }


def _write(root, name, content, how="template"):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    if how == "template":
        with open(os.path.join(d, "draft_info.json"), "w") as f:
            f.write("d56jFBzdd57UGfjNpPzu77IHh3qsTc6E")  # 模拟剪映加密
        with open(os.path.join(d, "template.json"), "w", encoding="utf-8") as f:
            json.dump(content, f, ensure_ascii=False)
    elif how == "plain":
        with open(os.path.join(d, "draft_info.json"), "w", encoding="utf-8") as f:
            json.dump(content, f, ensure_ascii=False)
    elif how == "encrypted":
        with open(os.path.join(d, "draft_info.json"), "w") as f:
            f.write("d56jFBzdd57UGfjNpPzu77IHh3qsTc6E")
    return d


def _snapshot(root):
    out = {}
    for dp, _, files in os.walk(root):
        for fn in files:
            p = os.path.join(dp, fn)
            st = os.stat(p)
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


class TestStyleProfile(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="jy_style_")
        self.out = tempfile.mkdtemp(prefix="jy_style_out_")
        _write(self.root, "竖屏A", vertical_draft())
        _write(self.root, "教程A", tutorial_draft())
        _write(self.root, "横屏A", short_horizontal_draft(), how="plain")
        _write(self.root, "加密草稿", {}, how="encrypted")
        skill = _write(self.root, "skill生成", vertical_draft(), how="plain")
        with open(os.path.join(skill, sp.SKILL_CONTENT), "w", encoding="utf-8") as f:
            json.dump(vertical_draft(), f)
        _write(self.root, "RoughCut_x", vertical_draft())
        rec = {
            "canvas_config": {"width": 1920, "height": 1080},
            "duration": 60 * S,
            "tracks": [{"type": "video", "segments": [_seg("a", "V", 0, 60)]}],
            "materials": {"videos": [{"id": "V", "type": "video"}]},
        }
        _write(self.root, "纯录屏", rec)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.out, ignore_errors=True)

    def test_scan_filters_and_is_read_only(self):
        before = _snapshot(self.root)
        kept, excluded = sp.scan_drafts(self.root)
        self.assertEqual(_snapshot(self.root), before)
        self.assertEqual(sorted(d["name"] for d in kept), ["教程A", "横屏A", "竖屏A"])
        reasons = {e["name"]: e["reason"] for e in excluded}
        self.assertIn("加密", reasons["加密草稿"])
        self.assertIn("skill", reasons["skill生成"])
        self.assertIn("RoughCut_", reasons["RoughCut_x"])
        self.assertIn("1 个视频片段", reasons["纯录屏"])

    def test_categorize(self):
        self.assertEqual(sp.categorize(vertical_draft()), "short_vertical")
        self.assertEqual(sp.categorize(tutorial_draft()), "long_tutorial")
        self.assertEqual(sp.categorize(short_horizontal_draft()), "horizontal_short")

    def test_vertical_metrics(self):
        prof = sp.build_profile([{"name": "竖屏A", "content": vertical_draft()}])
        m = prof["categories"]["short_vertical"]["metrics"]
        self.assertEqual(m["pacing"]["shot_duration_s"]["median"], 6.0)
        self.assertEqual(m["pacing"]["shot_duration_s"]["n"], 5)
        self.assertEqual(m["pacing"]["shot_duration_s"]["drafts"], ["竖屏A"])
        self.assertAlmostEqual(m["pacing"]["cuts_per_min"]["median"], 8.0)
        self.assertEqual(m["subtitle"]["size"]["median"], 10.0)
        self.assertEqual(m["subtitle"]["transform_y"]["median"], -0.6)
        self.assertEqual(m["subtitle"]["chars"]["median"], 8)
        self.assertEqual(m["subtitle"]["duration_s"]["n"], 10)
        self.assertEqual(m["subtitle"]["stroke"]["share"], 0.0)
        # 标题: 字号 = size x scale, 花字和入场动画
        self.assertEqual(m["overlay"]["size"]["median"], 10.0)
        self.assertEqual(m["overlay"]["flower"]["top"][0]["value"], "蓝色发光")
        self.assertEqual(m["overlay"]["anim_in"]["top"][0]["value"], "向上弹入")
        self.assertEqual(m["transitions"]["name"]["top"][0]["value"], "叠化")
        self.assertEqual(m["transitions"]["duration_s"]["median"], 0.5)
        self.assertAlmostEqual(m["transitions"]["per_cut"]["median"], 0.25)
        self.assertEqual(m["keyframes"]["move"]["top"][0]["value"], "push_in")
        self.assertAlmostEqual(m["keyframes"]["zoom_ratio"]["median"], 1.2)
        self.assertEqual(m["audio"]["bgm"]["volume"]["median"], 0.15)
        self.assertEqual(m["audio"]["bgm"]["fade_out_s"]["median"], 2.0)
        self.assertEqual(m["audio"]["bgm"]["last_fade_out_s"]["median"], 2.0)
        self.assertEqual(m["audio"]["tts"]["speed"]["median"], 1.2)
        self.assertEqual(m["audio"]["sfx"]["count"]["median"], 1)
        self.assertEqual(m["audio"]["sfx"]["on_cut"]["share"], 1.0)
        self.assertEqual(m["pip"]["scale"]["median"], 0.35)
        self.assertTrue(m["structure"]["title_in_first_3s"]["share"] == 1.0)
        self.assertEqual(m["structure"]["first_voice_start_s"]["median"], 0.8)

    def test_minimal_defaults(self):
        """精简格式省略 volume / start: 按 1.0 / 0 处理。"""
        d = vertical_draft()
        for t in d["tracks"]:
            for s in t["segments"]:
                s.pop("volume", None)
        d["tracks"][0]["segments"][0]["target_timerange"].pop("start")
        prof = sp.build_profile([{"name": "x", "content": d}])
        m = prof["categories"]["short_vertical"]["metrics"]
        self.assertEqual(m["audio"]["bgm"]["volume"]["median"], 1.0)
        self.assertEqual(m["pacing"]["shot_duration_s"]["n"], 5)

    def test_provenance(self):
        self.assertTrue(
            sp.provenance(tutorial_draft(), "jianying_plain")["likely_script_generated"]
        )
        self.assertFalse(
            sp.provenance(vertical_draft(), "jianying_plain")["likely_script_generated"]
        )

    def test_compare_with_defaults(self):
        prof = sp.build_profile(
            [
                {"name": "竖屏A", "content": vertical_draft()},
                {"name": "教程A", "content": tutorial_draft()},
            ]
        )
        refs = {
            "formats/vertical.json": {
                "subtitle": {"size": 10.0},
                "slots": {"subtitle": {"y": -0.44}},
                "safe": {"bottom": -0.48},
            },
            "scenes/talking_head.json": {
                "pacing": {"avg_shot": [3, 6]},
                "audio": {"bgm_volume": 0.3},
            },
            "styles/tech.json": {"bgm": {"volume": 0.15}},
            "formats/horizontal.json": {"subtitle": {"size": 7.0}},
            "scenes/tutorial.json": {"duration": {"max": 300}},
        }
        diffs = sp.compare_with_defaults(prof, refs)
        by = {(d["category"], d["item"]): d for d in diffs}
        y = by[("short_vertical", "字幕位置 transform_y")]
        self.assertEqual(y["default"], -0.44)
        self.assertEqual(y["file"], "references/formats/vertical.json")
        self.assertEqual(y["field"], "slots.subtitle.y")
        self.assertEqual(y["n"], 10)
        self.assertIn("safe.bottom", y["note"])
        self.assertIn(("short_vertical", "BGM 音量"), by)
        self.assertEqual(by[("short_vertical", "BGM 音量")]["suggested"], 0.15)
        # 一致的不报: 字幕字号 10 = 10, 风格 BGM 0.15 = 0.15, 镜头 6s 在 [3, 6]
        self.assertNotIn(("short_vertical", "字幕字号"), by)
        self.assertNotIn(("short_vertical", "BGM 音量 (风格)"), by)
        self.assertNotIn(("short_vertical", "镜头时长 (主轨)"), by)
        sub = by[("long_tutorial", "字幕字号")]
        self.assertEqual(sub["suggested"], 5.0)
        self.assertEqual(sub["script_generated_share"], 1.0)
        self.assertNotIn(("long_tutorial", "时长 (对上限)"), by)  # 240s < 300

    def test_run_writes_outputs(self):
        before = _snapshot(self.root)
        rc = sp.main(["--root", self.root, "--out-dir", self.out])
        self.assertEqual(rc, 0)
        self.assertEqual(_snapshot(self.root), before)
        with open(os.path.join(self.out, "style_profile.json"), encoding="utf-8") as f:
            prof = json.load(f)
        self.assertEqual(set(prof["categories"]), set(sp.CATEGORIES))
        self.assertTrue(prof["excluded"])
        with open(os.path.join(self.out, "style_profile.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("与 skill 默认值的差异", md)
        self.assertIn("数据局限", md)

    def test_category_override(self):
        rc = sp.main(
            [
                "--root",
                self.root,
                "--no-write",
                "--category",
                "横屏A=long_tutorial",
                "--exclude",
                "教程A",
            ]
        )
        self.assertEqual(rc, 0)
        kept, _ = sp.scan_drafts(self.root, exclude=["教程A"])
        prof = sp.build_profile(kept, {"横屏A": "long_tutorial"})
        self.assertEqual(prof["categories"]["long_tutorial"]["drafts"], ["横屏A"])


if __name__ == "__main__":
    unittest.main()
