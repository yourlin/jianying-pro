import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
import shot_quality as sq  # noqa: E402

W, H, FPS = 640, 360, 25


def _texture(seed=0):
    rng = np.random.default_rng(seed)
    base = (rng.random((H * 2, W * 2)) * 255).astype(np.uint8)
    base = cv2.GaussianBlur(base, (0, 0), 1.0)
    base = cv2.cvtColor(base, cv2.COLOR_GRAY2BGR)
    for i in range(0, W * 2, 80):
        cv2.rectangle(base, (i, 0), (i + 30, H * 2), (30, 200, 30), -1)
    return base


def write_video(path, segments):
    """segments: [(秒数, kind)], kind ∈ sharp / blur / black / shaky / white。"""
    base = _texture()
    rng = np.random.default_rng(1)
    w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    if not w.isOpened():
        pytest.skip("cv2.VideoWriter 不可用")
    for dur, kind in segments:
        for _ in range(int(dur * FPS)):
            crop = base[100 : 100 + H, 100 : 100 + W]
            if kind == "blur":
                fr = cv2.GaussianBlur(crop, (0, 0), 6)
            elif kind == "black":
                fr = np.zeros((H, W, 3), np.uint8)
            elif kind == "white":
                fr = np.full((H, W, 3), 255, np.uint8)
            elif kind == "shaky":
                dx, dy = rng.integers(-12, 13, 2)
                fr = base[100 + dy : 100 + dy + H, 100 + dx : 100 + dx + W]
            else:
                fr = crop
            w.write(np.ascontiguousarray(fr))
    w.release()
    return path


@pytest.fixture(scope="module")
def syn_video(tmp_path_factory):
    p = str(tmp_path_factory.mktemp("sq") / "syn.mp4")
    return write_video(p, [(2, "sharp"), (2, "blur"), (2, "black"), (2, "shaky"), (2, "white")])


def test_segments_rank_sharp_over_blur_over_black(syn_video):
    res = sq.score_segments(syn_video, [(0, 2), (2, 4), (4, 6)])
    sharp, blur, black = res["segments"]
    assert sharp["score"] > blur["score"] > black["score"]
    assert sharp["score"] >= 70 and black["score"] <= 5
    assert "模糊" in blur["issues"] and "模糊" not in sharp["issues"]
    assert "黑帧" in black["issues"]
    assert all(seg["n"] >= 3 for seg in res["segments"])


def test_shake_and_overexposure_labels(syn_video):
    res = sq.score_segments(syn_video, [(0, 2), (6, 8), (8, 10)])
    static, shaky, white = res["segments"]
    assert "抖动" in shaky["issues"] and "抖动" not in static["issues"]
    st = [s["stability"]["score"] for s in res["samples"] if 6 < s["t"] < 8]
    assert max(st) < 60
    assert "过曝" in white["issues"]


def test_score_video_samples_every_half_second(syn_video):
    res = sq.score_video(syn_video, every_s=0.5)
    assert len(res["samples"]) == 20
    s = res["samples"][0]
    for k in ("t", "score", "sharpness", "stability", "exposure", "face", "transition", "issues"):
        assert k in s
    assert 0 <= res["summary"]["score"] <= 100
    assert res["face_detector"] in ("haar", "unavailable")
    assert res["summary"]["best_t"] < 2  # 最好的采样点在清晰段


def test_transition_detection():
    base = _texture()[:H, :W]
    assert not sq.is_transition([cv2.cvtColor(base, cv2.COLOR_BGR2GRAY)] * 4)
    dark = np.zeros_like(base)
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in (base, base, dark, dark)]
    assert sq.is_transition(grays)


def test_face_scoring_with_mocked_detector(monkeypatch):
    base = _texture()[:H, :W]
    good = {"count": 1, "box": [0.4, 0.2, 0.6, 0.55], "facing": True, "eyes": 2, "size": 0.35}
    monkeypatch.setattr(sq, "detect_face", lambda g, bgr=None: dict(good))
    r_good = sq.measure([base] * 4)
    side = dict(good, facing=False, eyes=0)
    monkeypatch.setattr(sq, "detect_face", lambda g, bgr=None: dict(side))
    r_side = sq.measure([base] * 4)
    assert r_good["face"]["score"] > r_side["face"]["score"]
    assert r_good["score"] > r_side["score"]
    assert "未正对镜头" in r_side["issues"] and "看不到眼睛" in r_side["issues"]
    monkeypatch.setattr(sq, "detect_face", lambda g, bgr=None: {"count": 0})
    r_none = sq.measure([base] * 4, require_face=True)
    assert r_none["face"] is None and "无人脸" in r_none["issues"]


def test_parse_ranges_and_scenes():
    assert sq.parse_ranges("0-3, 5.5-8") == [(0.0, 3.0), (5.5, 8.0)]
    with pytest.raises(ValueError):
        sq.parse_ranges("3")
    assert sq.ranges_from_scenes([2.0, 2.1, 5.0], 8.0) == [(0.0, 2.0), (2.1, 5.0), (5.0, 8.0)]


def test_cli_json(syn_video, tmp_path, capsys):
    out = tmp_path / "q.json"
    assert sq._main([syn_video, "--ranges", "0-2,4-6", "--json", "-o", str(out)]) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] and env["code"] == "ok" and len(env["data"]["segments"]) == 2
    assert json.loads(out.read_text(encoding="utf-8"))["segments"][0]["score"] > 50
    assert sq._main([str(tmp_path / "missing.mp4"), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_scenes_accepts_reference_json_cuts(syn_video, tmp_path, capsys):
    """回归: --scenes 传 reference_analyzer 的 reference.json (键是 cuts) 时要按切点分段,
    以前只读 scenes 键, 悄悄退化成整段一个 segment。两个键都没有时报错。"""
    assert sq.scene_points({"scenes": [1.0, 2.0]}) == [1.0, 2.0]
    assert sq.scene_points({"scenes": []}) == []
    assert sq.scene_points({"cuts": [2.0]}) == [2.0]
    with pytest.raises(ValueError):
        sq.scene_points({"shots": []})
    ref = tmp_path / "reference.json"
    ref.write_text(json.dumps({"cuts": [2.0, 4.0]}), encoding="utf-8")
    assert sq._main([syn_video, "--scenes", str(ref), "--json"]) == 0
    env = json.loads(capsys.readouterr().out)
    assert len(env["data"]["segments"]) == 3
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert sq._main([syn_video, "--scenes", str(bad), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "shot_quality_failed"
