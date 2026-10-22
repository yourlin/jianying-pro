# ruff: noqa: E402

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

current_dir = os.path.dirname(os.path.abspath(__file__))
skill_root = os.path.dirname(current_dir)
scripts_path = os.path.join(skill_root, "scripts")
vendor_path = os.path.join(scripts_path, "vendor")
for p in (scripts_path, vendor_path):
    if p not in sys.path:
        sys.path.insert(0, p)

import universal_tts
import utils.panel_assets as pa
from core.ai_ops import _CJK_SPACE, _prompt_kwargs, _split_long_text
from jy_wrapper import JyProject, draft


def _item(effect_type, title, rid, vip=False, **extra_fields):
    biz = json.dumps({"is_vip": vip})
    ca = {
        "id": rid, "effect_id": rid, "third_resource_id_str": rid, "effect_type": effect_type,
        "title": title, "md5": "m" + rid, "business_info": {"json_str": biz}, "extra": "{}",
    }
    ca.update(extra_fields)
    return {"common_attr": ca}


def _make_rp_db(path):
    panel = {
        "data": {
            "categories": [
                {"category_id": 1, "category_name": "入场"},
                {"category_id": 2, "category_name": "出场"},
                {"category_id": 3, "category_name": "热门"},
            ],
            "category_resources": {
                "1": {"effect_item_list": [
                    _item(13, "测试入场A", "9000000000000000001", vip=True,
                          sdk_extra='{"setting":{"animation_duration":1.2}}'),
                ]},
                "2": {"effect_item_list": [_item(13, "测试出场B", "9000000000000000002")]},
                "3": {"effect_item_list": [
                    _item(19, "测试转场C", "9000000000000000003", vip=True,
                          sdk_extra='{"transition":{"defaultDura":0.8,"isOverlap":false}}'),
                    _item(7, "测试特效D", "9000000000000000004",
                          sdk_extra='{"setting":{"effect_adjust_params":[{"effect_key":"effects_adjust_speed","default":0.3,"min":0,"max":1}]}}'),
                    _item(11, "测试音色E", "9000000000000000005", vip=True,
                          extra=json.dumps({"tonetype": json.dumps({"voice_type": "zh_test_vip_voice"})})),
                    _item(10, "测试字体F", "9000000000000000006"),
                    _item(1, "测试花字J", "9000000000000000010"),
                    _item(97, "测试异形G", "9000000000000000007",
                          extra=json.dumps({"complex_video_mask": "heterosexual"})),
                    _item(97, "爱心", "9000000000000000009",
                          extra=json.dumps({"complex_video_mask": "heart"})),
                    _item(97, "测试未下载H", "9000000000000000008",
                          extra=json.dumps({"complex_video_mask": "heterosexual"})),
                ]},
            },
        }
    }
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE http_cache (id INTEGER PRIMARY KEY, url TEXT, response_body TEXT)")
    con.execute("INSERT INTO http_cache (url, response_body) VALUES (?, ?)",
                ("/artist/v1/panel/get_panel_info_X", json.dumps(panel, ensure_ascii=False)))
    con.commit()
    con.close()


class TestPanelAssets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="jy_panel_test_")
        cls.data_dir = os.path.join(cls.tmp, "data")
        cls.drafts = os.path.join(cls.tmp, "drafts")
        os.makedirs(cls.drafts)
        db = os.path.join(cls.tmp, "rp.db")
        _make_rp_db(db)
        cls.rows = pa.extract_panel_rows([db])
        pa.write_panel_csvs(cls.rows, cls.data_dir)
        cls._patch = patch.object(pa, "DATA_DIR", cls.data_dir)
        cls._patch.start()
        cls.video = os.path.join(skill_root, "assets", "video.mp4")

    @classmethod
    def tearDownClass(cls):
        cls._patch.stop()
        pa._CATALOG_CACHE.clear()
        pa._VIP_INDEX.clear()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_01_extract_classifies_kinds_and_vip(self):
        anims = {r["name"]: r for r in self.rows["panel_video_animations.csv"]}
        self.assertEqual(anims["测试入场A"]["sub_type"], "intro")
        self.assertEqual(anims["测试入场A"]["is_vip"], "1")
        self.assertEqual(anims["测试入场A"]["duration_s"], "1.2")
        self.assertEqual(anims["测试出场B"]["sub_type"], "outro")
        tr = self.rows["panel_transitions.csv"][0]
        self.assertEqual((tr["duration_s"], tr["is_overlap"]), ("0.8", "0"))
        voice = self.rows["panel_tts_voices.csv"][0]
        self.assertEqual(voice["voice_type"], "zh_test_vip_voice")
        fx = json.loads(self.rows["panel_video_effects.csv"][0]["params"])
        self.assertEqual(fx[0]["name"], "effects_adjust_speed")

    def test_02_resolve_registers_enum_member(self):
        m = pa.resolve_panel_member(draft.TransitionType, "测试转场C", draft)
        self.assertIsInstance(m, draft.TransitionType)
        self.assertEqual(m.value.resource_id, "9000000000000000003")
        self.assertEqual(m.value.default_duration, 800000)
        self.assertFalse(m.value.is_overlap)
        # 重复解析返回同一成员, 不重复注册
        self.assertIs(pa.resolve_panel_member(draft.TransitionType, "测试转场C", draft), m)
        # 子类型隔离: 出场动画不应被解析为入场
        self.assertIsNone(pa.resolve_panel_member(draft.IntroType, "测试出场B", draft))
        self.assertIsInstance(pa.resolve_panel_member(draft.OutroType, "测试出场B", draft), draft.OutroType)

    def test_03_wrapper_uses_panel_assets_and_reports_vip(self):
        p = JyProject("PanelE2E", drafts_root=self.drafts, overwrite=True)
        s1 = p.add_media_safe(self.video, "0s", "2s", "Main")
        s2 = p.add_media_safe(self.video, "2s", "2s", "Main")
        self.assertIsNotNone(p.add_transition_simple("测试转场C", s1))
        self.assertIsNotNone(p.add_video_animation(s2, "测试入场A", "intro"))
        self.assertIsNotNone(p.add_segment_effect(s2, "测试特效D"))
        p.add_text_simple("hi", "0s", "1s", font="测试字体F")
        res = p.save()
        names = {v["name"] for v in res["vip_assets"]}
        self.assertTrue({"测试转场C", "测试入场A"} <= names)
        self.assertNotIn("测试特效D", names)

        with open(os.path.join(self.drafts, "PanelE2E", "draft_info.json"), encoding="utf-8") as f:
            content = json.load(f)
        self.assertEqual(content["materials"]["transitions"][0]["resource_id"], "9000000000000000003")
        # 视频素材包含剪映 11.x 迁移 122/123 补充的 check_flag 标记位
        self.assertTrue(all(v["check_flag"] & 0x3C00000 == 0x3C00000 for v in content["materials"]["videos"]))
        self.assertEqual(content["materials"]["video_effects"][0]["resource_id"], "9000000000000000004")
        text = json.loads(content["materials"]["texts"][0]["content"])
        self.assertEqual(text["styles"][0]["font"]["id"], "9000000000000000006")

    def test_04_allow_vip_false_skips_vip_assets(self):
        p = JyProject("PanelNoVip", drafts_root=self.drafts, overwrite=True, allow_vip=False)
        self.assertIsNone(p._resolve_enum(draft.TransitionType, "测试转场C"))
        self.assertIsNotNone(p._resolve_enum(draft.OutroType, "测试出场B"))

    def test_05_effect_track_registers_material(self):
        p = JyProject("PanelEffectTrack", drafts_root=self.drafts, overwrite=True)
        p.add_effect_simple("测试特效D", "0s", "1s")
        p.save()
        with open(os.path.join(self.drafts, "PanelEffectTrack", "draft_info.json"), encoding="utf-8") as f:
            content = json.load(f)
        seg = [t for t in content["tracks"] if t["type"] == "effect"][0]["segments"][0]
        self.assertIn(seg["material_id"], [e["id"] for e in content["materials"]["video_effects"]])

    def test_06_overlay_keyframes_and_split_screen(self):
        p = JyProject("PanelTracks", drafts_root=self.drafts, overwrite=True)
        p.add_media_safe(self.video, "0s", "3s", "Main")
        ov = p.add_overlay(self.video, "0s", "2s", position="bottom_right", scale=0.3)
        self.assertEqual((ov.clip_settings.transform_x, ov.clip_settings.scale_x), (0.6, 0.3))
        p.add_keyframes(ov, "不透明度", [("0s", 0.0), ("0.5s", 1.0)])
        self.assertEqual(len(ov.common_keyframes[0].keyframes), 2)
        segs = p.add_split_screen([self.video, self.video], "0s", "1s")
        self.assertEqual([s.clip_settings.transform_x for s in segs], [-0.5, 0.5])
        self.assertGreater(p.script.tracks["Overlay"].render_index, p.script.tracks["Main"].render_index)
        with self.assertRaises(ValueError):
            p.add_keyframes(ov, "unknown_prop", [(0, 1)])

    def test_07_new_vip_tts_voice_not_synthesized(self):
        self.assertEqual(universal_tts.speaker_vip_state("zh_test_vip_voice"), "vip_new")
        self.assertEqual(universal_tts.speaker_vip_state("not_in_panel"), "free")
        import asyncio

        with patch.object(universal_tts, "_run_sami_tts") as sami:
            path, backend = asyncio.run(
                universal_tts.generate_voice_with_meta(
                    "hi", os.path.join(self.tmp, "x.ogg"), "zh_test_vip_voice", backend="sami"
                )
            )
        sami.assert_not_called()
        self.assertIsNone(path)

    def test_09_masks_use_common_mask_and_local_resource(self):
        ud = os.path.join(self.tmp, "UserData")
        res = os.path.join(ud, "Cache", "effect", "115534231", "m9000000000000000007")
        os.makedirs(res)
        with open(os.path.join(res, "material.svg"), "w") as f:
            f.write('<svg viewBox="0 0 120 100"></svg>')
        os.makedirs(os.path.join(ud, "Cache", "effect", "83244815", "m9000000000000000009"))
        p = JyProject("PanelMasks", drafts_root=self.drafts, overwrite=True)
        p.add_media_safe(self.video, "0s", "3s", "Main")
        old = p.add_overlay(self.video, "0s", "1s", track_name="O1")
        new = p.add_overlay(self.video, "0s", "1s", track_name="O2")
        missing = p.add_overlay(self.video, "1s", "1s", track_name="O1")
        heart = p.add_overlay(self.video, "1s", "1s", track_name="O2")
        with patch.object(pa, "get_jianying_user_data_dirs", return_value=[ud]):
            self.assertIsNotNone(p.add_mask_simple(old, "圆形", feather=50))
            self.assertIsNotNone(p.add_mask_simple(new, "测试异形G"))
            self.assertIsNone(p.add_mask_simple(missing, "测试未下载H"))
            self.assertIsNotNone(p.add_mask_simple(heart, "爱心"))
        p.save()
        with open(os.path.join(self.drafts, "PanelMasks", "draft_info.json"), encoding="utf-8") as f:
            mats = json.load(f)["materials"]
        self.assertEqual(mats["masks"], [])
        masks = {m["name"]: m for m in mats["common_mask"]}
        self.assertTrue(masks["圆形"]["is_old_version"])
        self.assertEqual(masks["圆形"]["path"], "")
        # 11.x: 界面羽化 50 -> 存储 0.25 (界面值 = sqrt(存储值) * 100)
        self.assertAlmostEqual(masks["圆形"]["config"]["feather"], 0.25)
        g = masks["测试异形G"]
        self.assertFalse(g["is_old_version"])
        self.assertEqual(g["resource_type"], "heterosexual")
        self.assertEqual(g["path"], res + "/")
        self.assertEqual(g["config"]["aspectRatio"], 1.2)
        self.assertEqual(g["category"], "video")
        # 本地已下载 11.x 版爱心时优先使用 (旧版在 11.6 中不生效)
        h = masks["爱心"]
        self.assertEqual((h["resource_type"], h["is_old_version"]), ("heart", False))

    def test_08_split_long_text(self):
        parts = _split_long_text("第一句话很短，第二句话稍微长一点点。第三句", 10)
        self.assertTrue(all(len(p) <= 10 for p in parts))
        self.assertEqual("".join(parts), "第一句话很短，第二句话稍微长一点点。第三句")


    def test_10_flower_text_written_even_when_added_after_placement(self):
        p = JyProject("PanelFlower", drafts_root=self.drafts, overwrite=True)
        p.add_text_simple("建时就加", "0s", "1s", flower="测试花字J")
        late = p.add_text_simple("上轨后再加", "1s", "1s")
        p.add_flower_effect(late, "测试花字J")
        p.save()
        with open(os.path.join(self.drafts, "PanelFlower", "draft_info.json"), encoding="utf-8") as f:
            mats = json.load(f)["materials"]
        for t in mats["texts"]:
            style = json.loads(t["content"])["styles"][0]
            self.assertEqual(style["effectStyle"]["id"], "9000000000000000010", t["content"][:40])
        self.assertEqual(sum(1 for e in mats["effects"] if e["type"] == "text_effect"), 2)


class TestAnimationDetails(unittest.TestCase):
    def test_loop_animation_duration_is_one_cycle(self):
        seg = draft.TextSegment("x", draft.trange("0s", "3s"))
        seg.add_animation(draft.TextLoopAnim.晃动)
        loop = seg.animations_instance.animations[-1]
        self.assertEqual((loop.start, loop.duration), (0, 500000))
        seg2 = draft.TextSegment("y", draft.trange("0s", "3s"))
        seg2.add_animation(draft.TextIntro.打字机_I, duration="1s")
        seg2.add_animation(draft.TextLoopAnim.晃动, duration="0.8s")
        loop2 = seg2.animations_instance.animations[-1]
        self.assertEqual((loop2.start, loop2.duration), (1000000, 800000))

    def test_fill_local_resource_paths(self):
        tmp = tempfile.mkdtemp(prefix="jy_local_")
        try:
            res = os.path.join(tmp, "Cache", "effect", "1644275", "0dc90e490f15d6bac5fd1d778e501917")
            os.makedirs(res)
            content = {"materials": {"material_animations": [{"animations": [
                {"resource_id": "6724920249654710791", "path": ""},
                {"resource_id": "123", "path": ""}]}]}}
            pa._LOCAL_MD5_INDEX.clear()
            with patch.object(pa, "get_jianying_user_data_dirs", return_value=[tmp]), \
                 patch.object(pa, "_md5_candidates_by_resource",
                              return_value={"6724920249654710791": ["0dc90e490f15d6bac5fd1d778e501917"]}):
                self.assertEqual(pa.fill_local_resource_paths(content), 1)
            anims = content["materials"]["material_animations"][0]["animations"]
            self.assertEqual(anims[0]["path"], res)
            self.assertEqual(anims[1]["path"], "")
        finally:
            pa._LOCAL_MD5_INDEX.clear()
            shutil.rmtree(tmp, ignore_errors=True)


class TestAiPrompt(unittest.TestCase):
    def test_prompt_kwargs_and_cjk_space_cleanup(self):
        kw = _prompt_kwargs("剪映、自动化 智能字幕", "zh")
        self.assertEqual(kw["hotwords"], "剪映 自动化 智能字幕")
        self.assertIn("简体中文", kw["initial_prompt"])
        self.assertNotIn("initial_prompt", _prompt_kwargs(None, "en"))
        self.assertEqual(_CJK_SPACE.sub("", "欢迎使用剪映 自动化 技能 OK go"), "欢迎使用剪映自动化技能 OK go")


if __name__ == "__main__":
    unittest.main()
