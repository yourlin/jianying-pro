import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "vendor"))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

cv2 = pytest.importorskip("cv2")

import color_match as cm  # noqa: E402


def _scene(h=120, w=160, seed=0) -> np.ndarray:
    """合成一张有亮度 / 颜色变化的 "中性" 画面 (RGB 0~1)。"""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w]
    img = np.stack([x / w, y / h, 0.5 + 0.3 * np.sin(x / 9.0)], axis=-1) * 0.8 + 0.1
    return np.clip(img + rng.normal(0, 0.02, img.shape), 0, 1).astype(np.float32)


def _cast(img, gains=(1, 1, 1), scale=1.0, desat=0.0):
    out = img * np.asarray(gains, dtype=np.float32) * scale
    if desat:
        out = (1 - desat) * out + desat * out.mean(axis=-1, keepdims=True)
    return np.clip(out, 0, 1).astype(np.float32)


@pytest.fixture(scope="module")
def ref_stats():
    return cm.color_stats([_scene()])


@pytest.mark.parametrize(
    "kwargs, key, sign",
    [
        ({"gains": (0.85, 0.95, 1.2)}, "色温", +1),  # 偏蓝 -> 色温调暖
        ({"gains": (1.15, 1.0, 0.8)}, "色温", -1),  # 偏黄 -> 色温调冷
        ({"gains": (0.9, 1.12, 0.9)}, "色调", +1),  # 偏绿 -> 色调往洋红
        ({"scale": 0.65}, "亮度", +1),  # 偏暗 -> 提亮
        ({"scale": 1.25}, "亮度", -1),  # 偏亮 -> 压暗
        ({"desat": 0.6}, "饱和度", +1),  # 灰 -> 加饱和
    ],
)
def test_suggestion_direction(ref_stats, kwargs, key, sign):
    shot = _cast(_scene(seed=1), **kwargs)
    res = cm.suggest_adjust([shot], ref_stats)
    assert res["adjust"].get(key, 0) * sign > 0, res
    for v in res["adjust"].values():
        assert -50 <= v <= 50 and isinstance(v, int)
    assert res["distance_after"] < res["distance_before"] * 0.5


def test_simulated_apply_reduces_difference(ref_stats):
    shot = _cast(_scene(seed=2), gains=(0.9, 0.97, 1.15), scale=0.8)
    before = cm.stats_distance(cm.color_stats([shot]), ref_stats)
    res = cm.suggest_adjust([shot], ref_stats)
    after_img = cm.simulate_adjust(shot, res["adjust"])  # 面板名参数直接可用
    after = cm.stats_distance(cm.color_stats([after_img]), ref_stats)
    assert after < before * 0.4
    # 平均色偏 (Lab b) 也被拉回参考附近
    assert abs(cm.color_stats([after_img])["b_mean"] - ref_stats["b_mean"]) < 2.0


def test_strength_scales_params(ref_stats):
    shot = _cast(_scene(seed=3), gains=(0.85, 0.95, 1.2))
    full = cm.suggest_adjust([shot], ref_stats, strength=1.0)["params"]
    half = cm.suggest_adjust([shot], ref_stats, strength=0.5)["params"]
    assert 0 < half["temperature"] < full["temperature"]


def test_simulate_unknown_param_raises():
    with pytest.raises(ValueError):
        cm.simulate_adjust(_scene(), {"锐化": 10})


def test_lut_roundtrip_and_reduces_difference(ref_stats, tmp_path):
    shot = _cast(_scene(seed=4), gains=(1.1, 1.0, 0.8), scale=0.85)
    st = cm.color_stats([shot])
    table = cm.build_lut(st, ref_stats, size=17)
    path = cm.write_cube(str(tmp_path / "m.cube"), table, 17)
    size, loaded = cm.read_cube(path)
    assert size == 17 and loaded.shape == (17**3, 3)
    after = cm.apply_lut(shot, loaded, size)
    assert cm.stats_distance(cm.color_stats([after]), ref_stats) < 0.3 * cm.stats_distance(
        st, ref_stats
    )
    # 恒等 LUT (src == ref) 基本不改变画面
    ident = cm.build_lut(ref_stats, ref_stats, size=9)
    img = _scene(seed=5)
    assert np.abs(cm.apply_lut(img, ident, 9) - img).mean() < 0.01


def test_match_shots_with_frames_and_auto_reference(tmp_path):
    base = _scene(seed=6)
    shots = [
        {"source": "a.png", "frames": [base]},
        {"source": "b.png", "frames": [_cast(base, gains=(0.85, 0.95, 1.2))]},
        {"source": "c.png", "frames": [_cast(base, scale=1.0)]},
        {"source": "d.png", "frames": [_cast(base, gains=(1.0, 1.0, 1.0), scale=0.98)]},
    ]
    res = cm.match_shots(shots, lut_dir=str(tmp_path / "luts"))
    assert res["reference"]["auto"] is True
    ref_i = res["reference"]["index"]
    assert ref_i != 1  # 偏色的那段不会被选为参考
    blue = res["shots"][1]
    assert blue["adjust"]["色温"] > 0 and "偏冷 (蓝)" in blue["diff"]["notes"]
    assert os.path.isfile(blue["lut"]) and blue["distance_after_lut"] < blue["distance_before"]
    assert res["shots"][ref_i]["adjust"] == {} and res["shots"][ref_i]["is_reference"]
    json.dumps(res)  # 可序列化


def test_parse_shot_spec_and_analysis(tmp_path):
    assert cm.parse_shot_spec("a.mp4@1.5-3") == {"source": "a.mp4", "start": 1.5, "end": 3.0}
    assert cm.parse_shot_spec("a.mp4@2-") == {"source": "a.mp4", "start": 2.0, "end": None}
    assert cm.parse_shot_spec("x@y.png")["source"] == "x@y.png"
    p = tmp_path / "analysis.json"
    p.write_text(json.dumps({"source": "/v.mp4", "video": {"duration": 10}, "scenes": [3, 3.1, 7]}))
    shots = cm.shots_from_analysis(str(p))
    assert [(s["start"], s["end"]) for s in shots] == [(0.0, 3.0), (3.1, 7.0), (7.0, 10.0)]


def _write_png(path, rgb):
    bgr = cv2.cvtColor((rgb * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
    assert cv2.imwrite(path, bgr)


def test_cli_suggest_images(tmp_path):
    base = _scene(seed=7)
    _write_png(str(tmp_path / "ref.png"), base)
    _write_png(str(tmp_path / "blue.png"), _cast(base, gains=(0.85, 0.95, 1.2)))
    out = tmp_path / "res.json"
    rc = cm._main(
        [
            "suggest",
            "--ref",
            str(tmp_path / "ref.png"),
            "--shots",
            str(tmp_path / "blue.png"),
            "-o",
            str(out),
            "--preview-dir",
            str(tmp_path / "pv"),
        ]
    )
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["shots"][0]["adjust"]["色温"] > 0
    assert os.path.isfile(data["shots"][0]["preview"])
    assert cm._main(["suggest", "--shots", str(tmp_path / "nope.png"), "--json"]) == 2


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="需要 ffmpeg 合成测试视频")
def test_sample_frames_from_video_range(tmp_path):
    path = str(tmp_path / "v.mp4")
    # 前 1 秒红, 后 1 秒蓝
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1:r=25",
        "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1:r=25",
        "-filter_complex", "[0][1]concat=n=2:v=1", "-pix_fmt", "yuv420p", path,
    ]  # fmt: skip
    subprocess.run(cmd, check=True)
    red = cm.color_stats(cm.sample_frames(path, 0, 1, samples=3))
    blue = cm.color_stats(cm.sample_frames(path, 1, 2, samples=3))
    assert red["a_mean"] > 30 and blue["b_mean"] < -30


def test_mixin_applies_adjust_to_segments(tmp_path):
    from core.color_match_ops import ColorMatchOpsMixin
    from jy_wrapper import JyProject

    class P(JyProject, ColorMatchOpsMixin):
        pass

    base = _scene(seed=8)
    _write_png(str(tmp_path / "ref.png"), base)
    _write_png(str(tmp_path / "blue.png"), _cast(base, gains=(0.85, 0.95, 1.2)))
    _write_png(str(tmp_path / "same.png"), base)
    root = tempfile.mkdtemp(prefix="jy_cm_")
    try:
        p = P("CM", drafts_root=root, overwrite=True)
        a = p.add_media_safe(str(tmp_path / "blue.png"), "0s", "2s", track_name="Main")
        b = p.add_media_safe(str(tmp_path / "same.png"), "2s", "2s", track_name="Main")
        dry = p.match_color(str(tmp_path / "ref.png"), track_name="Main", dry_run=True)
        assert [i["applied"] for i in dry] == [None, None]
        res = p.match_color(str(tmp_path / "ref.png"), track_name="Main")
        assert res[0]["applied"] == "adjust" and res[1]["applied"] is None
        p.save()
        with open(os.path.join(p.root, p.name, "draft_info.json"), encoding="utf-8") as f:
            content = json.load(f)
        seg = next(s for t in content["tracks"] for s in t["segments"] if s["id"] == a.segment_id)
        fx = [
            m for m in content["materials"]["effects"] if m["id"] in set(seg["extra_material_refs"])
        ]
        temp = next(m for m in fx if m["type"] == "temperature")
        assert temp["value"] > 0  # 偏蓝 -> 色温为正 (存储值 = 面板值 / 50)
        seg_b = next(s for t in content["tracks"] for s in t["segments"] if s["id"] == b.segment_id)
        assert not [
            m
            for m in content["materials"].get("effects", [])
            if m["id"] in seg_b["extra_material_refs"]
        ]
    finally:
        shutil.rmtree(root, ignore_errors=True)
