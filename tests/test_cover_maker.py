import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.dirname(__file__))
cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
pytest.importorskip("PIL")
import cover_maker as cm  # noqa: E402
import shot_quality as sq  # noqa: E402
from test_shot_quality import write_video  # noqa: E402


@pytest.fixture(scope="module")
def syn_video(tmp_path_factory):
    p = str(tmp_path_factory.mktemp("cover") / "syn.mp4")
    # 0-2 清晰 / 2-4 模糊 / 4-6 黑帧 / 6-8 清晰
    return write_video(p, [(2, "sharp"), (2, "blur"), (2, "black"), (2, "sharp")])


@pytest.fixture(scope="module")
def quality(syn_video):
    return sq.score_video(syn_video, every_s=0.5)


def test_candidates_prefer_sharp_and_skip_black(quality):
    cands = cm.pick_candidates(quality, n=4, min_gap_s=1.0)
    ts = [c["t"] for c in cands]
    assert not any(4 <= t < 6 for t in ts)  # 黑帧不当封面
    assert all(t < 2 or t >= 6 for t in ts[:2])  # 前两名都在清晰段
    assert [c["cover_score"] for c in cands] == sorted(
        (c["cover_score"] for c in cands), reverse=True
    )
    assert all(abs(a - b) >= 1.0 for i, a in enumerate(ts) for b in ts[i + 1 :])


def test_candidates_avoid_scene_cuts(quality):
    cands = cm.pick_candidates(quality, n=3, scenes=[0.75, 6.75], avoid_s=0.4)
    assert not any(abs(c["t"] - s) < 0.4 for c in cands for s in (0.75, 6.75))


def test_face_bonus_ranks_face_frame_first():
    base = {"issues": [], "transition": False}
    q = {
        "samples": [
            dict(base, t=1.0, score=80.0, face=None),
            dict(
                base,
                t=3.0,
                score=74.0,
                face={
                    "box": [0.4, 0.2, 0.6, 0.5],
                    "facing": True,
                    "eyes": 2,
                    "size": 0.3,
                    "count": 1,
                },
            ),
        ]
    }
    assert cm.pick_candidates(q, n=2)[0]["t"] == 3.0


def test_title_zone_avoids_face():
    fmt = cm.load_format("vertical")
    frame = np.full((1920, 1080, 3), 90, np.uint8)
    _img, info = cm.render_cover(frame, fmt, "人脸在上", face_box=[0.35, 0.12, 0.65, 0.35])
    assert info["zone"] == "bottom"
    _img, info = cm.render_cover(frame, fmt, "人脸在下", face_box=[0.35, 0.6, 0.65, 0.8])
    assert info["zone"] == "top"


def test_title_fits_inside_cover_safe_area():
    for fid in ("vertical", "horizontal", "square", "portrait_3x4"):
        fmt = cm.load_format(fid)
        w, h = fmt["canvas"]["width"], fmt["canvas"]["height"]
        img, info = cm.render_cover(
            np.full((720, 1280, 3), 200, np.uint8), fmt, "三个方法剪出好片", subtitle="新手也能学会"
        )
        assert img.size == (w, h)
        tl, z = info["title_layout"], info["zone_px"]
        assert tl["fits"] and tl["w"] <= z[2] - z[0] + 1
        ink = tl["ink_px"]
        assert ink[0] >= z[0] - 12 and ink[2] <= z[2] + 12  # 描边允许轻微出框
        assert info["contrast"] >= 1.0


def test_long_title_warns():
    fmt = cm.load_format("vertical")
    _img, info = cm.render_cover(
        np.full((720, 1280, 3), 60, np.uint8), fmt, "为什么你的视频总是没人看完"
    )
    assert any("个字" in w for w in info["warnings"])


def test_make_covers_outputs(syn_video, quality, tmp_path):
    out = tmp_path / "covers"
    analysis = {"scenes": [], "transcript": [{"text": "今天教你三个剪辑技巧"}]}
    res = cm.make_covers(
        syn_video,
        str(out),
        ["三个技巧", "别再这样剪"],
        subtitle="新手必看",
        n=3,
        analysis=analysis,
        quality=quality,
    )
    assert len(res["covers"]) == 3
    from PIL import Image

    for c in res["covers"]:
        assert os.path.exists(c["file"])
        assert Image.open(c["file"]).size == (1080, 1920)
        assert c["title"] in ("三个技巧", "别再这样剪")
    assert [c["style"] for c in res["covers"]] == cm.STYLE_ORDER
    assert os.path.exists(res["contact_sheet"]) and os.path.exists(out / "covers.json")
    assert "三个剪辑技巧" in res["title_prompt"]
    saved = json.loads((out / "covers.json").read_text(encoding="utf-8"))
    assert saved["covers"][0]["t"] == res["covers"][0]["t"]


def test_cli_prompt_only_and_titles_file(syn_video, tmp_path, capsys):
    out = tmp_path / "p"
    assert cm._main([syn_video, "-o", str(out), "--prompt-only", "--json"]) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] and "场景型" in env["data"]["title_prompt"]
    tf = tmp_path / "titles.txt"
    tf.write_text("三个技巧\n别再这样剪\n副标题: 新手必看\n", encoding="utf-8")
    assert (
        cm._main(
            [
                syn_video,
                "-o",
                str(out),
                "--titles-file",
                str(tf),
                "-n",
                "2",
                "--json",
                "--styles",
                "black_band",
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)["data"]
    assert [c["subtitle"] for c in data["covers"]] == ["新手必看"] * 2
    assert {c["style"] for c in data["covers"]} == {"black_band"}
    assert cm._main([syn_video, "-o", str(out), "--format", "nope", "--json"]) == 1


def test_auto_fit_when_face_fills_landscape_frame():
    fmt = cm.load_format("vertical")
    frame = np.full((720, 1280, 3), 120, np.uint8)
    img, info = cm.render_cover(frame, fmt, "她为什么震惊", face_box=[0.4, 0.15, 0.6, 0.85])
    assert info["mode"] == "fit" and info["zone"] == "top"
    assert not any("人脸重叠" in w for w in info["warnings"])
    assert img.size == (1080, 1920)
    _img, info = cm.render_cover(
        frame, fmt, "她为什么震惊", face_box=[0.4, 0.15, 0.6, 0.85], mode="fill"
    )
    assert info["mode"] == "fill" and any("人脸重叠" in w for w in info["warnings"])


def test_short_title_prefers_one_line():
    fmt = cm.load_format("vertical")
    _img, info = cm.render_cover(np.full((1920, 1080, 3), 60, np.uint8), fmt, "她为什么震惊")
    assert len(info["title_layout"]["lines"]) == 1
    _img, info = cm.render_cover(np.full((1920, 1080, 3), 60, np.uint8), fmt, "看完\n我也愣了")
    assert info["title_layout"]["lines"] == ["看完", "我也愣了"]
