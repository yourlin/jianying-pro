"""reference_analyzer: 用 ffmpeg 合成多镜头视频 (色块 + 纹理推拉 / 平移 + 闪黑 / 闪白 / 叠化 + 字幕 + 点击音轨) 验证结构反推。"""

import json
import os
import shutil
import subprocess
import sys
import wave

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import reference_analyzer as ra  # noqa: E402

W, H, FPS, DUR = 360, 640, 30, 12.0
SR = 22050
# 真值: 切点时间 -> 转场类型
TRUTH = [
    (1.0, "hard"),
    (2.0, "hard"),
    (3.0, "hard"),
    (4.5, "dip_black"),
    (6.0, "dissolve"),
    (7.5, "push"),
    (9.0, "dip_white"),
    (10.5, "hard"),
]
SUB_CHANGES = [0.0, 1.4, 2.8, 4.2, 5.6, 7.0, 8.4, 9.8, 11.2]

_YY, _XX = np.mgrid[0:H, 0:W].astype(np.float32)


def _texture(seed, scale=1.0, dx=0.0):
    """解析纹理 (多个正弦叠加), 可以无损缩放 / 平移。"""
    rng = np.random.default_rng(seed)
    cy, cx = H / 2, W / 2
    u = (_XX - cx) / scale + cx - dx
    v = (_YY - cy) / scale + cy
    img = np.zeros((H, W, 3), np.float32)
    for _ in range(6):
        fx, fy = rng.uniform(0.01, 0.05, 2) * rng.choice([-1, 1], 2)
        ph = rng.uniform(0, 6.28)
        col = rng.uniform(40, 110, 3)
        img += np.sin(u * fx + v * fy + ph)[..., None] * col / 3
    return np.clip(img + 128, 0, 255)


def _flat(rgb):
    return np.broadcast_to(np.array(rgb, np.float32), (H, W, 3)).copy()


def _glyphs(img, seed, y_center, n, cell):
    """伪文字: 随机 5x5 点阵字, 白字黑边。"""
    rng = np.random.default_rng(seed)
    gw = cell * 5
    total = n * gw + (n - 1) * cell
    x0, y0 = (W - total) // 2, int(y_center * H) - gw // 2
    for k in range(n):
        pat = rng.random((5, 5)) > 0.45
        pat[2, :] = True
        g = np.kron(pat, np.ones((cell, cell), bool))
        x = x0 + k * (gw + cell)
        outline = np.zeros((gw + 4, gw + 4), bool)
        for oy in range(5):
            for ox in range(5):
                outline[oy : oy + gw, ox : ox + gw] |= g
        region = img[y0 - 2 : y0 + gw + 2, x - 2 : x + gw + 2]
        region[outline] = 0
        img[y0 : y0 + gw, x : x + gw][g] = 255
    return img


def _frame(t):
    cols = {
        0: (200, 40, 40),
        1: (40, 170, 60),
        2: (40, 60, 190),
        3: (150, 100, 60),
        4: (30, 140, 150),
    }
    if t < 3.0:
        img = _flat(cols[int(t)])
    elif t < 4.5:  # 4.25-4.5 淡出到黑
        img = _flat(cols[3]) * float(np.clip((4.5 - t) / 0.25, 0, 1))
    elif t < 5.75:  # 4.5-4.75 从黑淡入
        img = _flat(cols[4]) * float(np.clip((t - 4.5) / 0.25, 0, 1))
    elif t < 6.25:  # 叠化到纹理
        a = (t - 5.75) / 0.5
        img = _flat(cols[4]) * (1 - a) + _texture(1) * a
    elif t < 7.5:  # 纹理静止, 7.2-7.5 快速推近 1 -> 1.5
        img = _texture(1, 1.0 + 0.5 * float(np.clip((t - 7.2) / 0.3, 0, 1)))
    elif t < 9.0:  # 新纹理: 7.5-7.7 继续推近, 之后向右平移; 8.9-9.0 变白
        s = 1.0 + 0.3 * float(np.clip((t - 7.5) / 0.2, 0, 1))
        img = _texture(2, s, dx=max(0.0, t - 7.7) * 60)
        a = float(np.clip((t - 8.9) / 0.1, 0, 1))
        img = img * (1 - a) + 255 * a
    elif t < 10.5:
        a = float(np.clip((t - 9.0) / 0.1, 0, 1))
        img = _flat((230, 140, 30)) * a + 255 * (1 - a)
    else:
        img = _flat((40, 200, 210))
    if t < 2.0:  # 开头大字标题 (画面中部)
        img = _glyphs(img, 99, 0.5, 4, 8)
    k = max(i for i, c in enumerate(SUB_CHANGES) if c <= t + 1e-6)
    return _glyphs(img, k, 0.8, 8, 4)


def _click_wav(path, bpm=120.0, offset=0.5):
    n = int(DUR * SR)
    y = np.zeros(n, np.float32)
    tt = np.arange(int(0.08 * SR)) / SR
    kick = np.sin(2 * np.pi * 70 * tt) * np.exp(-tt * 30)
    click = np.sin(2 * np.pi * 2000 * tt) * np.exp(-tt * 80)
    b, k = offset, 0
    while b < DUR - 0.1:
        sig = 0.9 * kick + 0.3 * click if k % 4 == 0 else 0.5 * click
        s = int(b * SR)
        e = min(n, s + len(sig))
        y[s:e] += sig[: e - s] * 0.8
        b += 60.0 / bpm
        k += 1
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())


def _encode(path, wav):
    for codec in (
        ["-c:v", "libx264", "-crf", "16", "-preset", "veryfast"],
        ["-c:v", "mpeg4", "-q:v", "2"],
    ):
        cmd = [
            "ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
            "-r", str(FPS), "-i", "-", "-i", str(wav), *codec, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ]  # fmt: skip
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for i in range(int(DUR * FPS)):
                p.stdin.write(_frame(i / FPS).astype(np.uint8).tobytes())
            p.stdin.close()
        except BrokenPipeError:
            pass
        if p.wait() == 0 and os.path.getsize(path) > 0:
            return path
    pytest.skip("ffmpeg 不能编码测试视频")


@pytest.fixture(scope="module")
def ref_video(tmp_path_factory):
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("需要 ffmpeg / ffprobe")
    d = tmp_path_factory.mktemp("ref")
    wav = d / "clicks.wav"
    _click_wav(wav)
    return str(_encode(d / "ref.mp4", wav))


@pytest.fixture(scope="module")
def report(ref_video, tmp_path_factory):
    out = tmp_path_factory.mktemp("out")
    os.environ["JY_BEAT_CACHE"] = str(tmp_path_factory.mktemp("beat_cache"))  # 不写用户缓存目录
    return ra.analyze_reference(ref_video, str(out), backend="numpy", name="synthetic")


def test_boundaries_and_transition_types(report):
    trans = report["transitions"]
    assert len(trans) == len(TRUTH), [(t["t"], t["style"]) for t in trans]
    for (t, style), got in zip(TRUTH, trans):
        assert abs(got["t"] - t) <= 0.1, (t, got)
        assert got["style"] == style, (t, got)
    assert report["transition_counts"]["hard"] == 4
    assert report["shot_stats"]["count"] == 9
    assert abs(report["shot_stats"]["median"] - 1.5) <= 0.1
    dis = next(t for t in trans if t["style"] == "dissolve")
    assert 0.3 <= dis["duration"] <= 0.8
    assert dis["jy_transition"] == "叠化"


def test_camera_moves(report):
    shots = report["shots"]
    assert shots[6]["camera"]["move"] == "pan_right", shots[6]["camera"]
    for i in (0, 1, 2, 7, 8):
        assert shots[i]["camera"]["move"] == "static"
    assert report["motion"]["avg"] > 0


def test_beat_alignment(report):
    b = report["beat"]
    assert b and abs(b["tempo"] - 120) < 3
    assert b["aligned_ratio"] >= 0.75
    assert b["label"] == "强卡点"
    assert b["cut_every_beats"] == 3


def test_subtitle_rhythm_and_hook(report):
    tx = report["text"]
    bottom = tx["bands"]["bottom"]
    assert bottom["present_ratio"] >= 0.8
    assert tx["kind"] == "full"
    assert abs(bottom["median_interval"] - 1.4) <= 0.15
    assert len(bottom["events"]) >= 7
    assert bottom["transform_y"] < -0.4  # 画面下方
    mid = tx["bands"]["middle"]
    assert (
        mid["segments"] and mid["segments"][0][0] < 0.1 and abs(mid["segments"][0][1] - 2.0) < 0.2
    )
    hook = report["hook"]
    assert hook["cuts"] == 2
    assert "大字标题" in hook["types"]
    assert hook["text_coverage"]["middle"] >= 0.6


def test_scene_and_style_drafts(report):
    files = report["files"]
    scene = json.load(open(files["scene"], encoding="utf-8"))
    style = json.load(open(files["style"], encoding="utf-8"))
    base_scene = ra.load_base("scenes", report["drafts"]["scene_base"])
    base_style = ra.load_base("styles", report["drafts"]["style_base"])
    assert set(base_scene) <= set(scene)
    assert set(base_style) <= set(style)
    assert scene["id"] == style["id"] == "ref_synthetic"
    assert abs(sum(b["ratio"] for b in scene["beats"]) - 1) < 1e-6
    assert scene["beats"][0]["name"] == "开场钩子" and "title" in scene["beats"][0]["required"]
    assert scene["beats"][-1]["span"][1] == pytest.approx(report["video"]["duration"], abs=0.05)
    for b in scene["beats"]:
        assert {
            "name",
            "ratio",
            "purpose",
            "required",
            "allowed",
            "forbidden",
            "camera",
            "note",
        } <= set(b)
    assert scene["pacing"]["avg_shot"][0] < scene["pacing"]["avg_shot"][1]
    assert scene["subtitles"]["default"] is True
    assert scene["pacing"]["cut_every_beats"]["参考"] == 3
    assert "roles" in style and "subtitle" in style["roles"]
    assert (
        style["cut_transition"] is None
        or style["cut_transition"]["name"] in ra.JY_TRANSITION.values()
    )
    assert set(style["grade"]["adjust"]) == {"亮度", "对比度", "饱和度", "色温"}


def test_contact_sheet(report):
    from PIL import Image

    im = Image.open(report["files"]["contact_sheet"])
    assert im.size[0] > 600 and im.size[1] > 300
    assert os.path.exists(report["files"]["hook_strip"])


def test_install_loads_with_script_compiler(ref_video, tmp_path, monkeypatch):
    import script_compiler as sc

    refs = tmp_path / "skill" / "references"
    for kind in ("scenes", "styles"):
        shutil.copytree(os.path.join(ROOT, "references", kind), refs / kind)
    rep = ra.analyze_reference(
        ref_video, str(tmp_path / "o"), beats=False, sheet=False, name="mine", install=True,
        refs_root=str(refs), scene="vlog", style="tech",
    )  # fmt: skip
    assert rep["beat"] is None
    monkeypatch.setattr(sc, "SKILL", str(tmp_path / "skill"))
    scene = sc.load_profile("scenes", "ref_mine")
    style = sc.load_profile("styles", "ref_mine")
    assert scene["derived_from"]["scene"] == "vlog" and style["derived_from"]["style"] == "tech"


def test_cli_json(ref_video, tmp_path, capsys):
    rc = ra._main([ref_video, "-o", str(tmp_path), "--no-beats", "--no-sheet", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["data"]["shot_stats"]["count"] == 9
    rc = ra._main([str(tmp_path / "missing.mp4"), "--json"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["code"] == "reference_analyze_failed"


def test_estimators_unit():
    rng = np.random.default_rng(0)
    base = rng.random((40, 24))
    big = np.kron(base, np.ones((4, 4))) * 255
    from numpy.fft import fft2  # noqa: F401

    img = big[::2, ::2][:80, :48]
    shifted = np.roll(img, 3, axis=1)
    dx, dy, _ = ra.estimate_shift(img, shifted)
    assert abs(dx - 3) < 0.5 and abs(dy) < 0.5
    tex = _texture(5)[..., 0][::8, ::8]
    tex2 = _texture(5, 1.06)[..., 0][::8, ::8]
    ls, conf = ra.estimate_scale(tex, tex2)
    assert abs(ls - np.log(1.06)) < 0.015 and conf > 0.2


def test_camera_move_push_in_and_static():
    frames = [_texture(7, 1.0 + 0.1 * k / 30)[..., 1][::8, ::8] for k in range(31)]
    small = np.stack(frames).astype(np.uint8)
    cam = ra.camera_move(small, 0, 30, 30.0)
    assert cam["move"] == "push_in" and 0.05 < cam["zoom_per_s"] < 0.15, cam
    still = np.stack([frames[0]] * 31).astype(np.uint8)
    assert ra.camera_move(still, 0, 30, 30.0)["move"] == "static"


def test_duration_stats_and_rhythm():
    st = ra.duration_stats([0.5, 0.6, 0.7, 0.8, 4.0])
    assert st["count"] == 5 and st["median"] == 0.7 and st["histogram"]["0.5-1s"] == 4
    shots = [{"start": i * 0.7, "duration": 0.7} for i in range(10)]
    rh = ra.rhythm_label(st, {"label": "强卡点"}, shots, 7.0)
    assert rh["label"].startswith("卡点") and rh["pace"] == "快闪"


def test_contact_sheet_header_cjk_font():
    """回归: 文件名是中文时, 联系表表头不能画成豆腐块 (默认字体没有 CJK 字形)。"""
    from layout_preview import CJK_FONTS
    from PIL import Image, ImageDraw, ImageFont

    if not any(os.path.exists(p) for p in CJK_FONTS):
        pytest.skip("本机没有 CJK 字体")
    try:
        default = ImageFont.load_default(size=13)
    except TypeError:
        default = ImageFont.load_default()
    assert ra._sheet_font("ascii only 12.0s", default) is default
    font = ra._sheet_font("中文宣传片 v1", default)
    assert font is not default

    def render(ch):
        im = Image.new("L", (40, 24))
        ImageDraw.Draw(im).text((2, 2), ch, fill=255, font=font)
        return np.asarray(im)

    assert (render("番") != render("茄")).any() and render("番").any()


def _dip_feat(lumas):
    lu = np.asarray(lumas, float)
    return {"luma": lu, "p10": lu - 0.02, "p90": lu + 0.02}


def test_white_ui_not_mistaken_for_long_edge_fade():
    """回归: 白底课件 / 网页录屏 (p10 > 0.9) 以前在片尾被当成 50s 的「白场淡出」, 片头被当成白场淡入。"""
    fps = 15.0
    # 片头白底标题页 3s, 中间正常画面, 片尾白底页 50s
    lumas = [0.95] * 45 + [0.5] * 300 + [0.95] * 750
    assert ra.dips(_dip_feat(lumas), fps) == []
    # 真正的片尾闪白 (0.5s) 和片头 3s 黑场照常识别
    lumas = [0.02] * 45 + [0.5] * 300 + [0.97] * 8
    got = {(d["pos"], d["style"]) for d in ra.dips(_dip_feat(lumas), fps)}
    assert got == {("opening", "dip_black"), ("ending", "dip_white")}


def test_beat_alignment_needs_enough_cuts():
    """回归: 1 个切点碰巧落在拍上, 以前给出「强卡点」(alignment_score 1.0)。"""
    beats = {"beats": list(np.arange(0, 60, 0.43)), "beat_interval": 0.43, "tempo": 139.5}
    one = ra.beat_alignment([0.43 * 50], beats, 15.0, [21.5, 38.5])
    assert one["label"] == "无法判断" and one["aligned_ratio"] is None
    many = ra.beat_alignment([0.43 * k for k in range(4, 40, 4)], beats, 30.0, [1.72] * 9)
    assert many["label"] == "强卡点"
