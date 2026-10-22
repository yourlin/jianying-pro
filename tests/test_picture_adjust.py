import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "vendor"))

import pytest  # noqa: E402

from core.adjust_ops import _curve  # noqa: E402
from jy_wrapper import JyProject  # noqa: E402

VIDEO = os.path.join(ROOT, "assets", "video.mp4")


@pytest.fixture()
def project():
    root = tempfile.mkdtemp(prefix="jy_pic_")
    p = JyProject("Pic", drafts_root=root, overwrite=True)
    yield p
    shutil.rmtree(root, ignore_errors=True)


def _content(p):
    p.save()
    with open(os.path.join(p.root, p.name, "draft_info.json"), encoding="utf-8") as f:
        return json.load(f)


def _refs(content, seg_id, list_name):
    seg = next(s for t in content["tracks"] for s in t["segments"] if s["id"] == seg_id)
    ids = set(seg["extra_material_refs"])
    return seg, [m for m in content["materials"].get(list_name, []) if m["id"] in ids]


def test_adjust_values_use_panel_scale(project):
    seg = project.add_media_safe(VIDEO, "0s", "2s", track_name="Main")
    project.set_adjust(seg, 色温=-14, 锐化=34, 暗角=21)
    project.set_adjust(seg, 色温=-20)  # 重复设置替换, 不叠加
    _, fx = _refs(_content(project), seg.segment_id, "effects")
    vals = {e["type"]: e["value"] for e in fx}
    assert vals == {"temperature": -0.4, "sharpen": 0.34, "vignetting": 0.42}
    with pytest.raises(ValueError):
        project.set_adjust(seg, 锐化=-5)  # 单向滑块 0~100


def test_mask_stroke_shadow_corner_pin_get_own_material(project):
    a = project.add_media_safe(VIDEO, "0s", "2s", track_name="Main")
    b = project.add_media_safe(VIDEO, "2s", "2s", track_name="Main")
    project.add_mask_simple(a, "圆形")
    project.add_mask_stroke(a, "发光", color="#fff496", size=30, alpha=60)
    project.add_mask_shadow(a, alpha=50, blur=30, distance=20, angle=45)
    project.set_corner_pin(a, upper_left=(-0.5, -1))
    c = _content(project)
    mats = {m["id"]: m for m in c["materials"]["videos"]}
    seg_a, = [s for t in c["tracks"] for s in t["segments"] if s["id"] == a.segment_id]
    seg_b, = [s for t in c["tracks"] for s in t["segments"] if s["id"] == b.segment_id]
    ma, mb = mats[seg_a["material_id"]], mats[seg_b["material_id"]]
    assert ma["video_mask_stroke"]["size"] == 0.3 and ma["video_mask_stroke"]["alpha"] == 0.6
    assert ma["video_mask_shadow"]["alpha"] == 0.5 and ma["corner_pin"]["enable_corner_pin"]
    assert seg_a["enable_mask_stroke"] and seg_a["enable_mask_shadow"]
    assert "video_mask_stroke" not in mb and "corner_pin" not in mb  # 同一文件的其他片段不受影响


def test_blend_stroke_shadow_chroma_hsl(project):
    seg = project.add_media_safe(VIDEO, "0s", "2s", track_name="Main")
    project.set_blend_mode(seg, "滤色")
    project.set_blend_mode(seg, "multiply")  # 替换
    project.add_video_stroke(seg, "单层描边", color="#ff0000", size=50)
    project.add_video_shadow(seg, alpha=60)
    project.set_chroma_key(seg, color="#deeada")
    project.set_hsl(seg, "红", hue=20, saturation=-30, lightness=10)
    c = _content(project)
    _, fx = _refs(c, seg.segment_id, "effects")
    assert [e["name"] for e in fx if e["type"] == "mix_mode"] == ["正片叠底"]
    _, st = _refs(c, seg.segment_id, "video_strokes")
    assert st[0]["color"] == [1, 1.0, 0.0, 0.0]  # [A, R, G, B]
    assert {p["name"]: p["value"] for p in st[0]["adjust_params"]}["effects_adjust_size"] == 0.5
    _, sh = _refs(c, seg.segment_id, "video_shadows")
    assert sh[0]["alpha"] == 0.6
    _, ch = _refs(c, seg.segment_id, "chromas")
    assert ch[0]["color"] == "#deeadaff"
    _, hsl = _refs(c, seg.segment_id, "hsl")
    assert hsl[0]["hsl_color_type"] == 1 and hsl[0]["hue"] == 20


def test_curve_points():
    pts = _curve([(0.5, 0.7)])
    assert [(p["anchor"]["x"], p["anchor"]["y"]) for p in pts] == [(0.0, 0.0), (0.5, 0.7), (1.0, 1.0)]
    assert pts[0]["left_control"] == pts[0]["anchor"] and pts[-1]["right_control"] == pts[-1]["anchor"]


def test_set_lut_imports_like_jianying(project, tmp_path, monkeypatch):
    import core.adjust_ops as ao

    ud = tmp_path / "User Data"
    ud.mkdir()
    monkeypatch.setattr(ao, "get_jianying_user_data_dirs", lambda: [str(ud)])
    monkeypatch.setattr(ao, "find_jianying_app_resource", lambda *a: None)  # 无安装包: 只放 .cube, 不出缩略图
    cube = tmp_path / "my look.cube"
    cube.write_text("LUT_3D_SIZE 2\n" + "\n".join(f"{r} {g} {b}" for b in (0, 1) for g in (0, 1) for r in (0, 1)))
    seg = project.add_media_safe(VIDEO, "0s", "2s", track_name="Main")
    project.set_lut(seg, str(cube), strength=70, skin_protect=20)
    project.set_lut(seg, str(cube), strength=50)  # 重复设置: 替换, 去掉肤色保护
    _, fx = _refs(_content(project), seg.segment_id, "effects")
    assert [(e["type"], e["value"]) for e in fx] == [("lut", 0.5)]
    assert fx[0]["path"] == str(ud / "Resources" / "Lut" / "my_look" / "my_look.cube")
    assert (ud / "Resources" / "Lut" / "my_look" / "my_look.cube").exists()
    cfg = json.loads((ud / "Resources" / "Lut" / "LutImportCfg.json").read_text())
    assert [c["title"] for c in cfg] == ["my look.cube"]


def test_video_algorithms_stack_and_relight_flag(project):
    seg = project.add_media_safe(VIDEO, "0s", "2s", track_name="Main")
    project.set_denoise(seg)
    project.set_deflicker(seg)
    project.set_motion_blur(seg, blur=0.5)
    project.set_motion_blur(seg)  # 同类算法替换, 不重复
    project.set_denoise(seg, enable=False)
    project.set_smart_relight(seg)
    c = _content(project)
    s, = [x for t in c["tracks"] for x in t["segments"] if x["id"] == seg.segment_id]
    mat = next(m for m in c["materials"]["videos"] if m["id"] == s["material_id"])
    assert [a["type"] for a in mat["video_algorithm"]["algorithms"]] == ["Deflicker", "MotionBlur"]
    assert mat["video_algorithm"]["motion_blur_config"] == {"blur": 1.0}
    assert mat["check_flag"] & 0x20000
    _, rl = _refs(c, seg.segment_id, "smart_relights")
    assert rl[0]["resource_name"] == "氛围暖光"


def test_build_relight_from_package(tmp_path):
    """relit_default.json -> 草稿素材; 期望值取自剪映 11.7 写出的「青橙逆光」第一盏灯。"""
    from core.quality_ops import build_relight

    (tmp_path / "relit_default.json").write_text(json.dumps({
        "effects_adjust_lightMode": 2,
        "effects_adjust_lights": [[0.0, 2.0, 1.0, 0.17, 0.0, 1.0, -1.0, 0.0, 0.5, 0.7, 0.0, 0.5, 0.35, -0.5, -0.5, -0.5],
                                  [1.0, 0.0, 1.0, 1.0, 1.0, 0.5, 0.0, 0.0, 1.0, 0.25, 0.5, 0.5, 1.0, -0.5, -0.5, -0.5]],
        "effects_adjust_light_params": {"albedo_Color": [1, 1, 1, 1], "albedo_Intensity": 0.6, "albedo_Rate": 1,
                                        "glow_Intensity": 0.2}}))
    m = build_relight({"name": "青橙逆光", "resource_id": "7280500670535307832", "categories": "氛围彩光"}, str(tmp_path))
    assert m["category_id"] == "ambient" and m["light_mode"] == 2 and m["albedo_color"] == "#FFFFFFFF"
    a, b = m["light_sources"]
    assert (a["light_source_type"], a["action_object"], a["color"], a["radius"]) == (2, 2, "#FF2B00FF", 0.35)
    assert (b["light_source_type"], b["action_object"], b["color"], b["specular_light_intensity"]) == (1, 3, "#FFFFFF7F", 0.5)


def test_speed_curve_speed_keyframe_ease_and_glow(project):
    from core.motion_ops import _curve_mean_speed

    a = project.add_media_safe(VIDEO, "0s", "4s", track_name="Main")
    b = project.add_media_safe(VIDEO, "4s", "4s", track_name="Main")
    project.set_speed(a, 2.0, change_pitch=True)
    assert a.target_timerange.duration == 2_000_000 and b.target_timerange.start == 2_000_000  # 后面的片段前移
    assert abs(_curve_mean_speed([{"x": 0, "y": 2}, {"x": 1, "y": 2}]) - 2) < 1e-9
    project.add_keyframes(b, "x", [(0, -0.5), ("4s", 0.5)], ease="回弹离开")
    t = project.add_text_simple("glow", "0s", "2s")
    project.add_text_glow(t, "轮廓光", color="#ff0000", strength=70, range=40)
    c = _content(project)
    sa, = [x for tr in c["tracks"] for x in tr["segments"] if x["id"] == a.segment_id]
    assert sa["speed"] == 2.0 and sa["is_tone_modify"]
    kf = next(k for x in c["tracks"][0]["segments"] if x["id"] == b.segment_id for k in x["common_keyframes"]
              if k["property_type"] == "KFTypePositionX")["keyframe_list"]
    assert kf[0]["curveType"] == "FreeCurveInOut" and kf[0]["right_control"]["x"] == 680000  # 0.17 * 4s
    assert kf[1]["graphID"]
    glow = next(e for e in c["materials"]["effects"] if e["type"] == "bloom")
    assert glow["bloom_params"]["strength"] == 0.7
    assert next(m for m in c["materials"]["texts"])["check_flag"] & 64


def test_beauty_items_use_panel_keys(project):
    seg = project.add_media_safe(VIDEO, "0s", "2s", track_name="Main")
    project.set_beauty(seg, 磨皮=50, 瘦脸=30, 橘子汽水=80)
    _, fx = _refs(_content(project), seg.segment_id, "effects")
    by = {e.get("name"): e for e in fx}
    assert by["磨皮"]["item_effect_type"] == 21 and by["磨皮"]["value"] == 0.5  # 美颜里的磨皮, 不是美体的
    assert by["瘦脸"]["intensity_key"] == "face_adjust_TotalFace" and by["瘦脸"]["adjust_params"][0]["value"] == 0.3
    mk = by["橘子汽水"]
    assert mk["face_adjust_params"][0]["adjust_params"][0]["value"] == 0.8 and mk["exclusion_group"]
    assert "makeup-root" in by
