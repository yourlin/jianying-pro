import json
import os
import shutil
import subprocess
import sys
import wave

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import quality_review as qr  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="需要 ffmpeg / ffprobe"
)
W, H, FPS, SR = 180, 320, 10, 16000


def write_wav(path, samples, sr=SR):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())


def write_video(path, frames, audio=None, tmp=None):
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-"]  # fmt: skip
    if audio is not None:
        wav = os.path.join(str(tmp), os.path.basename(str(path)) + ".wav")
        write_wav(wav, audio)
        cmd += ["-i", wav, "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    p = subprocess.run(cmd, input=b"".join(f.tobytes() for f in frames), capture_output=True)
    assert p.returncode == 0, p.stderr


def solid(color, secs):
    return [np.full((H, W, 3), color, np.uint8)] * int(secs * FPS)


def moving(secs, seed=0, bg=(40, 90, 160)):
    """每帧都在变的画面 (移动的色块), 模拟有动作的镜头。"""
    out = []
    for i in range(int(secs * FPS)):
        f = np.full((H, W, 3), bg, np.uint8)
        x = (i * 17 + seed) % (W - 60)
        y = (i * 29) % (H - 60)
        f[y : y + 60, x : x + 60] = (250, 220, 60)
        out.append(f)
    return out


def tone(secs, freq=440, amp=0.3):
    t = np.arange(int(secs * SR)) / SR
    return amp * np.sin(2 * np.pi * freq * t)


# ---------------- 纯函数 ----------------
def test_parse_srt(tmp_path):
    p = tmp_path / "a.srt"
    p.write_text("1\n00:00:00,500 --> 00:00:02,000\n第一句\n\n2\n00:00:02.5 --> 00:00:04.0\n第二句\n",
                 encoding="utf-8")  # fmt: skip
    subs = qr.parse_srt(str(p))
    assert [(s["start"], s["end"], s["text"]) for s in subs] == [
        (0.5, 2.0, "第一句"),
        (2.5, 4.0, "第二句"),
    ]


def _tl(duration, **kw):
    tl = {"duration": duration, "cuts": [], "overlays": [], "texts": [], "speech_segments": [],
          "speech_text": [], "texts_known": True, "speech_known": True, "visual_known": True,
          "changes": []}  # fmt: skip
    tl.update(kw)
    return tl


def test_dead_zone_and_severity_by_speech():
    issues = []
    tl = _tl(20, cuts=[2.0, 12.0], speech_segments=[(12.0, 20.0)])
    zones = qr.check_dead_zones(tl, 3.5, issues)
    assert [(z["start"], z["end"]) for z in zones] == [(2.0, 12.0), (12.0, 20.0)]
    sev = [i["severity"] for i in issues]
    assert sev == ["high", "medium"]  # 第二段一直有人声 -> 中


def test_pacing_slow_for_genre_and_score():
    issues = []
    tl = _tl(40, cuts=[12.0, 25.0])
    res = qr.check_pacing(tl, None, "口播知识", issues)
    assert res["stats"]["count"] == 3 and res["stats"]["max"] == 15.0
    kinds = {i["type"] for i in issues}
    assert "节奏偏慢" in kinds and "长镜头" in kinds
    sc = qr.score(issues, {"pacing"})
    assert sc["by_category"]["pacing"] < 100 and sc["by_category"]["hook"] is None
    assert len(res["curve"]) == 40


def test_hook_flags_greeting_and_missing_voice():
    issues = []
    tl = _tl(10, texts=[{"start": 3.5, "end": 6, "text": "大家好今天聊一下", "track": "Sub"}])
    qr.check_hook(tl, None, None, issues)
    kinds = {i["type"] for i in issues}
    assert {"前3秒无人声无文字", "寒暄式开场"} <= kinds


def test_ending_drag_and_cta():
    issues = []
    tl = _tl(20, cuts=[5.0], texts=[{"start": 1, "end": 4, "text": "结论", "track": "Sub"}])
    m = qr.check_ending(tl, None, issues)
    assert m["last_info_at"] == 5.0 and m["has_cta"] is False
    assert {"结尾拖沓", "没有CTA"} <= {i["type"] for i in issues}
    issues = []
    tl["texts"].append({"start": 17, "end": 20, "text": "关注我, 下期见", "track": "CTA"})
    tl["cuts"].append(16.5)
    qr.check_ending(tl, None, issues)
    assert not issues


def test_bed_vs_voice_estimates_bgm_from_word_gaps():
    mix = np.full(100, -60.0)
    mix[10:20] = -10  # 词
    mix[30:40] = -10
    mix[20:30] = -14  # 词间: BGM 只低 4dB
    tr = [
        {
            "start": 1.0,
            "end": 4.0,
            "words": [{"w": "a", "s": 1.0, "e": 2.0}, {"w": "b", "s": 3.0, "e": 4.0}],
        }
    ]
    g = qr._bed_vs_voice(mix, tr, 10)
    assert g == pytest.approx(-4.0)


def test_lab_of_white_and_black():
    lab = qr.rgb_to_lab(np.array([[1.0, 1.0, 1.0], [0.0, 0.0, 0.0]]))
    assert lab[0, 0] == pytest.approx(100, abs=0.5) and abs(lab[0, 1]) < 1
    assert lab[1, 0] == pytest.approx(0, abs=0.5)


# ---------------- 视频模式 ----------------
@needs_ffmpeg
def test_video_mode_end_to_end(tmp_path):
    pytest.importorskip("PIL")
    v = tmp_path / "v.mp4"
    frames = solid((0, 0, 0), 2) + solid((128, 128, 128), 8) + solid((200, 40, 40), 2)
    frames += solid((240, 240, 240), 2)
    audio = tone(14)
    audio[6 * SR : 8 * SR] = 0
    write_video(v, frames, audio, tmp_path)
    out = tmp_path / "out"
    rc = qr.main(["--video", str(v), "--out", str(out)])
    assert rc == 1
    rep = json.loads((out / "report.json").read_text(encoding="utf-8"))
    kinds = {i["type"] for i in rep["issues"]}
    assert {"首帧黑场", "死区", "静音空洞", "亮度跳变"} <= kinds
    black = next(i for i in rep["issues"] if i["type"] == "首帧黑场")
    assert black["end"] == pytest.approx(2.0, abs=0.3)
    zone = rep["dead_zones"][0]
    assert zone["start"] == pytest.approx(2.0, abs=0.3) and zone["end"] == pytest.approx(
        10.0, abs=0.3
    )
    assert len(rep["timeline"]["cuts"]) == 3
    assert os.path.exists(out / "viewer_sheet.png")
    from PIL import Image

    assert Image.open(out / "viewer_sheet.png").size[0] > 1000
    assert all(
        i["id"].startswith("Q") and i["rule"].startswith("references/") for i in rep["issues"]
    )


@needs_ffmpeg
def test_video_mode_lively_clip_passes(tmp_path):
    v = tmp_path / "ok.mp4"
    frames = []
    bgs = [(40, 90, 160), (70, 120, 90), (40, 90, 160), (120, 80, 110), (70, 120, 90)]
    for k, bg in enumerate(bgs):  # 5 个 2s 镜头, 每个都在动, 亮度相近
        frames += moving(2, seed=k * 7, bg=bg)
    write_video(v, frames, tone(10, amp=0.2), tmp_path)
    rep = qr.review(video=str(v), genre="vlog", out_dir=str(tmp_path / "o"), make_sheet=False)
    assert not [i for i in rep["issues"] if i["severity"] in ("high", "medium")], rep["issues"]
    assert len(rep["timeline"]["cuts"]) == 4
    assert rep["score"]["overall"] >= 80


def test_input_errors_exit_2(tmp_path, capsys):
    assert qr.main(["--draft", "不存在", "--drafts-root", str(tmp_path), "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["code"] == "input_error"
    assert qr.main([]) == 2


# ---------------- 草稿模式 ----------------
def _text_mat(mid, text):
    return {
        "id": mid,
        "content": json.dumps({"text": text, "styles": [{"size": 8.0}]}, ensure_ascii=False),
    }


def _seg(mid, start, dur, src=0.0, volume=1.0, refs=None):
    us = lambda s: int(s * 1e6)  # noqa: E731
    return {"material_id": mid, "target_timerange": {"start": us(start), "duration": us(dur)},
            "source_timerange": {"start": us(src), "duration": us(dur)}, "volume": volume,
            "extra_material_refs": refs or [], "clip": {"scale": {"x": 1.0, "y": 1.0}}}  # fmt: skip


@needs_ffmpeg
def test_draft_mode_bgm_conflict_and_dead_zone(tmp_path):
    pytest.importorskip("PIL")
    media = tmp_path / "media"
    media.mkdir()
    write_video(media / "a.mp4", moving(8))
    write_video(media / "b.mp4", solid((60, 100, 150), 6))
    write_wav(media / "voice.wav", tone(8, 300, 0.2))
    rng = np.random.default_rng(0)
    write_wav(media / "bgm.wav", 0.2 * rng.standard_normal(12 * SR))
    draft = {
        "duration": 14_000_000,
        "canvas_config": {"width": W, "height": H},
        "materials": {
            "videos": [
                {
                    "id": "va",
                    "path": str(media / "a.mp4"),
                    "type": "video",
                    "material_name": "a.mp4",
                },
                {
                    "id": "vb",
                    "path": str(media / "b.mp4"),
                    "type": "video",
                    "material_name": "b.mp4",
                },
            ],
            "audios": [
                {"id": "au", "path": str(media / "voice.wav"), "type": "extract_music"},
                {"id": "bg", "path": str(media / "bgm.wav"), "type": "extract_music"},
            ],
            "texts": [_text_mat("t1", "大家好, 今天聊一下剪辑")],
            "audio_fades": [],
        },
        "tracks": [
            {
                "type": "video",
                "name": "Main",
                "segments": [_seg("va", 0, 8, volume=0), _seg("vb", 8, 6, volume=0)],
            },
            {"type": "text", "name": "SubMain", "segments": [_seg("t1", 0, 2.5)]},
            {"type": "audio", "name": "VoiceOver", "segments": [_seg("au", 0, 8)]},
            {"type": "audio", "name": "BGM", "segments": [_seg("bg", 0, 12, volume=0.8)]},
        ],
    }
    root = tmp_path / "drafts"
    (root / "测试草稿").mkdir(parents=True)
    (root / "测试草稿" / "draft_info.json").write_text(
        json.dumps(draft, ensure_ascii=False), encoding="utf-8"
    )
    out = tmp_path / "out"
    rc = qr.main(["--draft", "测试草稿", "--drafts-root", str(root), "--out", str(out)])
    assert rc == 1
    rep = json.loads((out / "report.json").read_text(encoding="utf-8"))
    kinds = {i["type"] for i in rep["issues"]}
    assert {"BGM压人声", "寒暄式开场", "配乐提前结束", "死区"} <= kinds
    assert rep["timeline"]["cuts"] == [8.0]
    assert rep["timeline"]["speech"] and rep["timeline"]["speech"][0]["start"] < 0.5
    conflict = next(i for i in rep["issues"] if i["type"] == "BGM压人声")
    assert "音量 0.80 →" in conflict["suggestion"]
    zone = next(z for z in rep["dead_zones"] if z["start"] >= 7.9)
    assert zone["end"] == pytest.approx(14.0, abs=0.6)
    assert rep["source"]["mode"] == "draft" and os.path.exists(out / "viewer_sheet.png")


def test_silent_video_not_double_counted_as_tail_silence():
    """回归: 全片无声的录屏, 以前在 audio 报「全片无声」之外, ending 又报一次「片尾静音 (最后 227s)」。"""
    tl = _tl(20, cuts=[5.0, 10.0, 15.0])
    audio = {"mix": np.full(200, -120.0)}
    issues = []
    qr.check_audio(tl, audio, issues)
    qr.check_ending(tl, audio, issues)
    kinds = [i["type"] for i in issues]
    assert "全片无声" in kinds and "片尾静音" not in kinds
    loud = np.full(200, -20.0)
    loud[-30:] = -120.0  # 最后 3s 静音: 仍然要报
    issues = []
    qr.check_ending(tl, {"mix": loud}, issues)
    assert "片尾静音" in [i["type"] for i in issues]


# ---------------- 切点检测回归 (benchmarks.md Q1 / Q2 / Q6) ----------------
def write_clip(path, frames, fps, size):
    """横屏合成视频 (任意尺寸 / 帧率), 不带音轨。"""
    w, h = size
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", str(path)]  # fmt: skip
    p = subprocess.run(cmd, input=b"".join(f.tobytes() for f in frames), capture_output=True)
    assert p.returncode == 0, p.stderr


def dark_shot(secs, fps, seed, size=(192, 108)):
    """暗场同色调镜头: 深棕底 + 几块略亮的棕色块 (位置随镜头变), 块缓慢漂移, 带一点噪声。"""
    w, h = size
    rng = np.random.default_rng(seed)
    blocks = [(int(rng.integers(0, w - 50)), int(rng.integers(0, h - 36)),
               int(rng.integers(24, 50)), int(rng.integers(16, 36))) for _ in range(4)]  # fmt: skip
    out = []
    for i in range(int(round(secs * fps))):
        f = np.full((h, w, 3), (26, 18, 15), np.int16)
        for k, (x, y, bw, bh) in enumerate(blocks):
            dx = (i + k) % 6
            f[y : y + bh, x + dx : x + dx + bw] = (52 + 6 * k, 36 + 4 * k, 28 + 3 * k)
        f += rng.integers(-2, 3, f.shape, dtype=np.int16)
        out.append(np.clip(f, 0, 255).astype(np.uint8))
    return out


def bright_shot(secs, fps, seed, size=(192, 108)):
    """亮场镜头: 不同底色 + 移动色块。"""
    w, h = size
    rng = np.random.default_rng(seed)
    bg = tuple(int(x) for x in rng.integers(60, 200, 3))
    fg = tuple(int(x) for x in rng.integers(0, 255, 3))
    x0, y0 = int(rng.integers(0, w - 60)), int(rng.integers(0, h - 40))
    out = []
    for i in range(int(round(secs * fps))):
        f = np.full((h, w, 3), bg, np.uint8)
        x = (x0 + 3 * i) % (w - 40)
        f[y0 : y0 + 40, x : x + 40] = fg
        f[(seed * 13) % (h - 20) : (seed * 13) % (h - 20) + 20, : w // 3] = fg[::-1]
        out.append(f)
    return out


def _cuts_of(path):
    tl, _feat, _audio, _ctx = qr.build_timeline(video=str(path))
    return tl["cuts"]


def _recall(truth, cuts, tol=0.15):
    hit = sum(1 for t in truth if any(abs(t - c) <= tol for c in cuts))
    extra = [c for c in cuts if not any(abs(t - c) <= tol for t in truth)]
    return hit / len(truth), extra


@needs_ffmpeg
def test_dark_same_tone_cuts_detected(tmp_path):
    """回归 Q1: 暗场、同色调的镜头切换 (MV 棕褐色、暗色游戏), 旧的固定直方图阈值几乎全漏。"""
    fps, durs = 30, [2.0, 1.5, 2.5, 1.2, 2.0, 1.8, 2.2]
    frames = []
    for k, d in enumerate(durs):
        frames += dark_shot(d, fps, seed=k + 1)
    v = tmp_path / "dark.mp4"
    write_clip(v, frames, fps, (192, 108))
    truth = list(np.cumsum(durs)[:-1])
    rec, extra = _recall(truth, _cuts_of(v))
    assert rec >= 0.85 and not extra, (rec, extra)


@needs_ffmpeg
def test_fast_short_shots_all_separated(tmp_path):
    """回归 Q1: 0.5–1s 的快切 (蒙太奇 / 幻灯片快翻) 每一刀都要分开检出, 不被 4fps 采样合并。"""
    fps, durs = 30, [1.0, 0.5, 0.7, 0.5, 1.0, 0.6, 0.8, 0.5, 0.9, 1.0]
    frames = []
    for k, d in enumerate(durs):
        frames += bright_shot(d, fps, seed=k + 3)
    v = tmp_path / "fast.mp4"
    write_clip(v, frames, fps, (192, 108))
    truth = list(np.cumsum(durs)[:-1])
    cuts = _cuts_of(v)
    rec, extra = _recall(truth, cuts)
    assert rec >= 0.85 and len(extra) <= 1, (cuts, truth)


@needs_ffmpeg
def test_subtitle_bar_change_is_not_a_cut(tmp_path):
    """白底录屏里只有底部字幕条出现 / 消失: 是新信息, 但不是换镜头。"""
    w, h, fps = 192, 108, 10
    base = np.full((h, w, 3), 245, np.uint8)
    base[10:14, 20:150] = 120  # 一行「界面文字」
    sub = base.copy()
    sub[92:100, 60:132] = 40  # 底部深色字幕条
    frames = [base] * 20 + [sub] * 20 + [base] * 20
    v = tmp_path / "ui.mp4"
    write_clip(v, frames, fps, (w, h))
    assert _cuts_of(v) == []


def cta_card(size=(640, 360), seed=0):
    """片尾 CTA 卡: 深色底 + 二维码 (随机黑白方块) + 两行白色「大字」(竖条纹)。"""
    w, h = size
    rng = np.random.default_rng(seed)
    f = np.full((h, w, 3), (40, 30, 28), np.uint8)
    qr_bits = rng.integers(0, 2, (21, 21)).astype(bool)
    big = np.kron(qr_bits, np.ones((7, 7), bool))
    f[100 : 100 + big.shape[0], 420 : 420 + big.shape[1]] = np.where(big[..., None], 0, 255)
    for row in (130, 190):
        for x in range(80, 360, 14):
            f[row : row + 34, x : x + 8] = 250
    return f


@needs_ffmpeg
def test_static_cta_end_card_not_dead_zone(tmp_path):
    """回归 Q2: 结尾静止 3.5s 的二维码 + 大字 CTA 卡是有意停留, 不报死区 / 结尾拖沓;
    同样长的结尾换成没有字 / 码的空镜, 仍要报拖沓。"""
    size, fps = (640, 360), 10
    head = []
    for k in range(4):
        head += bright_shot(1.5, fps, seed=k + 20, size=size)
    v1 = tmp_path / "cta.mp4"
    write_clip(v1, head + [cta_card(size)] * 35, fps, size)
    rep = qr.review(video=str(v1), genre="vlog", out_dir=str(tmp_path / "o1"), make_sheet=False)
    kinds = {i["type"] for i in rep["issues"]}
    assert "结尾拖沓" not in kinds and not [z for z in rep["dead_zones"] if z["end"] > 9]
    card = rep["ending"]["cta_card"]
    assert card["start"] == pytest.approx(6.0, abs=0.3) and 2 <= card["dur"] <= 5
    assert rep["ending"]["has_cta"] is True

    plain = np.full((size[1], size[0], 3), (90, 110, 130), np.uint8)
    v2 = tmp_path / "plain.mp4"
    write_clip(v2, head + [plain] * 35, fps, size)
    rep2 = qr.review(video=str(v2), genre="vlog", out_dir=str(tmp_path / "o2"), make_sheet=False)
    assert "结尾拖沓" in {i["type"] for i in rep2["issues"]}
    assert "cta_card" not in rep2["ending"]


def test_tile_times_shot_mode_keeps_hook_frames():
    """回归 Q6: 按镜头取帧 (> 120s) 时第一个镜头中点在 21.7s, 也要有 0/1/2/3s 的钩子帧。"""
    shots = [{"index": 1, "start": 0.0, "end": 43.4, "dur": 43.4},
             {"index": 2, "start": 43.4, "end": 180.0, "dur": 136.6}]  # fmt: skip
    report = {"source": {"duration": 180.0}, "pacing": {"shots": shots}}
    times = [t for t, _s, _e in qr.tile_times(report, "auto", 120)]
    assert times[:4] == [0.0, 1.0, 2.0, 3.0] and 21.7 in times and times == sorted(times)
    many = [{"index": i + 1, "start": i * 1.0, "end": i + 1.0, "dur": 1.0} for i in range(300)]
    report = {"source": {"duration": 300.0}, "pacing": {"shots": many}}
    picks = qr.tile_times(report, "shot", 20)
    assert len(picks) <= 20 and {0.0, 1.0, 2.0, 3.0} <= {p[0] for p in picks}


@needs_ffmpeg
def test_long_video_sheet_includes_hook_frames(tmp_path, monkeypatch):
    """回归 Q6 端到端: 130s 视频的联系表真的去抽了 0–3s 的帧。"""
    pytest.importorskip("PIL")
    fps, size = 2, (160, 90)
    frames = []
    for k in range(5):
        frames += bright_shot(26, fps, seed=k + 40, size=size)
    v = tmp_path / "long.mp4"
    write_clip(v, frames, fps, size)
    asked = []
    real = qr._tile_frame

    def spy(ctx, t, th):
        asked.append(round(t, 2))
        return real(ctx, t, th)

    monkeypatch.setattr(qr, "_tile_frame", spy)
    rep = qr.review(video=str(v), out_dir=str(tmp_path / "o"), sheet_mode="auto")
    assert rep["source"]["duration"] > 120 and rep["sheet"]
    assert {0.0, 1.0, 2.0} <= set(asked)


# ---------------- 分镜表 review 块 (script_compiler 写入) ----------------
STORYBOARD_REVIEW = {
    "hook": {"type": "title_card", "hold_s": [3.0, 4.0]},
    "continuity": "per_section",
    "sections": [[0.0, 4.0], [4.0, 8.0]],
    "intentional_holds": [{"time": [0.0, 4.0], "why": "标题卡"},
                          {"time": [16.0, 20.0], "why": "结尾卡 (CTA)"}],  # fmt: skip
}


def _hints(tmp_path, review=STORYBOARD_REVIEW, name="x.storyboard.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"project": {"name": "x"}, "shots": [], "review": review},
                            ensure_ascii=False), encoding="utf-8")  # fmt: skip
    return qr.load_review_hints(str(p))


def test_load_review_hints_normalizes(tmp_path):
    h = _hints(tmp_path)
    assert h["hook_type"] == "title_card" and h["continuity"] == "per_section"
    assert h["sections"] == [(0.0, 4.0), (4.0, 8.0)]
    assert [(x["start"], x["end"], x["why"]) for x in h["holds"]] == [
        (0.0, 4.0, "标题卡"),
        (16.0, 20.0, "结尾卡 (CTA)"),
    ]
    assert _hints(tmp_path, review=None, name="none.json") == {}


def test_compiled_promo_storyboard_review_block(tmp_path):
    """script_compiler 编译 fixtures 里的 promo 剧本, 输出的 review 块能被读懂。"""
    from script_compiler import compile_script

    fx = os.path.join(os.path.dirname(__file__), "fixtures", "scenes", "promo.script.json")
    out = compile_script(fx, out=str(tmp_path / "promo.storyboard.json"))
    h = qr.load_review_hints(out)
    assert h["hook_type"] == "title_card" and h["continuity"] == "per_section"
    assert h["sections"] and h["holds"][0]["start"] == 0.0
    assert any("CTA" in x["why"] for x in h["holds"])


def _static_feat(levels, fps=10):
    """[(秒数, 灰度)] -> 静止纯灰画面的特征 (亮度跳变 / 静止检查用)。"""
    frames = []
    for secs, g in levels:
        frames += [np.full((54, 96, 3), g, np.uint8)] * int(secs * fps)
    return qr.frame_features(np.arange(len(frames)) / fps, np.stack(frames))


def test_title_card_hook_not_weak(tmp_path):
    feat = _static_feat([(5, 90)])
    texts = [{"start": 0.0, "end": 3.7, "text": "游戏标题", "track": "Title"}]
    issues = []
    qr.check_hook(_tl(10, texts=texts, speech_known=False), feat, None, issues)
    assert "前3秒画面静止" in {i["type"] for i in issues}
    issues = []
    tl = _tl(10, texts=texts, speech_known=False, review_hints=_hints(tmp_path))
    m = qr.check_hook(tl, feat, None, issues)
    assert not issues and m["hook_type"] == "title_card"


def test_intentional_holds_skip_dead_zone_and_tail_drag(tmp_path):
    kw = dict(cuts=[4.0, 8.0, 12.0, 16.0], texts=[])
    issues = []
    tl = _tl(20, **kw)
    qr.check_dead_zones(tl, 3.5, issues)
    qr.check_ending(tl, None, issues)
    kinds = [i["type"] for i in issues]
    assert kinds.count("死区") == 5 and "结尾拖沓" in kinds
    issues = []
    tl = _tl(20, review_hints=_hints(tmp_path), **kw)
    zones = qr.check_dead_zones(tl, 3.5, issues)
    m = qr.check_ending(tl, None, issues)
    assert [(z["start"], z["end"]) for z in zones] == [(4.0, 8.0), (8.0, 12.0), (12.0, 16.0)]
    assert "结尾拖沓" not in {i["type"] for i in issues} and m["has_cta"] is True


def test_per_section_continuity_only_checks_inside_sections(tmp_path):
    feat = _static_feat([(2, 40), (2, 200), (4, 110)])  # 2s 处段内跳变, 4s 处段落边界跳变
    tl = _tl(8, cuts=[2.0, 4.0])
    issues = []
    qr.check_continuity(tl, feat, issues)
    assert sorted(i["start"] + 0.5 for i in issues if i["type"] == "亮度跳变") == [2.0, 4.0]
    issues = []
    tl = _tl(8, cuts=[2.0, 4.0], review_hints=_hints(tmp_path))
    rows = qr.check_continuity(tl, feat, issues)
    assert [i["start"] + 0.5 for i in issues if i["type"] == "亮度跳变"] == [2.0]
    assert next(r for r in rows if r["t"] == 4.0)["section_boundary"] is True


@needs_ffmpeg
def test_draft_mode_reads_storyboard_from_draft_dir(tmp_path):
    pytest.importorskip("PIL")
    media = tmp_path / "media"
    media.mkdir()
    write_video(media / "a.mp4", moving(6) + solid((60, 100, 150), 6))
    draft = {
        "duration": 12_000_000,
        "canvas_config": {"width": W, "height": H},
        "materials": {
            "videos": [{"id": "va", "path": str(media / "a.mp4"), "type": "video",
                        "material_name": "a.mp4"}],  # fmt: skip
            "audios": [], "texts": [_text_mat("t1", "关注我")], "audio_fades": [],
        },  # fmt: skip
        "tracks": [
            {"type": "video", "name": "Main", "segments": [_seg("va", 0, 12, volume=0)]},
            {"type": "text", "name": "Title", "segments": [_seg("t1", 0, 3.0)]},
        ],
    }
    ddir = tmp_path / "drafts" / "分镜草稿"
    ddir.mkdir(parents=True)
    (ddir / "draft_info.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    review = {"hook": {"type": "title_card"},
              "intentional_holds": [{"time": [6.0, 12.0], "why": "结尾卡 (CTA)"}]}  # fmt: skip
    (ddir / "分镜草稿.storyboard.json").write_text(
        json.dumps({"project": {"name": "分镜草稿"}, "review": review}, ensure_ascii=False),
        encoding="utf-8",
    )
    rep = qr.review(draft="分镜草稿", drafts_root=str(tmp_path / "drafts"),
                    out_dir=str(tmp_path / "o"), make_sheet=False)  # fmt: skip
    assert rep["review_hints"]["source"].endswith("分镜草稿.storyboard.json")
    assert not [z for z in rep["dead_zones"] if z["start"] >= 5.5]
    assert "结尾拖沓" not in {i["type"] for i in rep["issues"]}
