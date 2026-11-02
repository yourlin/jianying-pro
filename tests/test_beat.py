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
sys.path.insert(0, os.path.join(ROOT, "scripts", "vendor"))

import beat_detect as bd  # noqa: E402
import beat_snap as bs  # noqa: E402

SR = 22050


def synth_click_track(path, bpm=120.0, dur=24.0, offset=0.5, loud=(8.0, 16.0), phase=1, bpb=4):
    """合成点击音轨: 强拍 = 70Hz 底鼓 + 点击, 其余拍 = 2kHz 点击; loud 区间内音量 x4 (高能段)。返回拍点真值。"""
    n = int(dur * SR)
    y = np.zeros(n, np.float32)
    t = np.arange(int(0.08 * SR)) / SR
    kick = np.sin(2 * np.pi * 70 * t) * np.exp(-t * 30)
    click = np.sin(2 * np.pi * 2000 * t) * np.exp(-t * 80)
    beats, k, b = [], 0, offset
    while b < dur - 0.1:
        g = 1.0 if loud[0] <= b < loud[1] else 0.25
        s = int(b * SR)
        sig = (0.9 * kick + 0.3 * click) if (k - phase) % bpb == 0 else 0.5 * click
        e = min(n, s + len(sig))
        y[s:e] += g * sig[: e - s]
        beats.append(b)
        b += 60.0 / bpm
        k += 1
    pcm = (np.clip(y, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return np.array(beats), np.array(beats[phase::bpb])


def _match_err(found, truth):
    found = np.asarray(found)
    return np.array([np.min(np.abs(found - t)) for t in truth])


@pytest.fixture()
def no_librosa(monkeypatch):
    """让 librosa 不可用 (模拟 CI 没装)。"""
    monkeypatch.setitem(sys.modules, "librosa", None)
    assert not bd._librosa_available()


@pytest.mark.parametrize("bpm", [90.0, 128.0, 150.0])
def test_numpy_backend_tempo_and_beats(tmp_path, bpm, no_librosa):
    wav = tmp_path / "click.wav"
    truth, down_truth = synth_click_track(wav, bpm=bpm)
    res = bd.detect_beats(str(wav), backend="auto", use_cache=False)
    assert res["backend"] == "numpy"
    assert abs(res["tempo"] - bpm) < 1.0
    err = _match_err(res["beats"], truth[1:-1])
    assert err.max() < 0.04 and np.median(err) < 0.025
    assert abs(len(res["beats"]) - len(truth)) <= 1
    # 强拍: 底鼓所在的拍
    derr = _match_err(res["downbeats"], down_truth[1:-1])
    assert derr.max() < 0.04
    # 起音点覆盖每一拍
    assert _match_err(res["onsets"], truth).max() < 0.04


def test_energy_sections_find_loud_part(tmp_path, no_librosa):
    wav = tmp_path / "click.wav"
    synth_click_track(wav, bpm=120.0, loud=(8.0, 16.0))
    res = bd.detect_beats(str(wav), backend="numpy", use_cache=False)
    secs = res["energy_sections"]
    assert secs[0]["start"] == 0.0 and secs[-1]["end"] == pytest.approx(res["duration"], abs=0.01)
    for a, b in zip(secs[:-1], secs[1:]):
        assert a["end"] == b["start"]
    climax = res["climax"]
    assert climax and 7.0 <= climax["start"] <= 9.5 and 15.0 <= climax["end"] <= 17.5
    high = [s for s in secs if s["is_climax"]][0]
    assert high["level"] == "high"
    assert all(s["level"] == "low" for s in secs if not s["is_climax"])


def test_flat_track_has_no_climax(tmp_path, no_librosa):
    wav = tmp_path / "flat.wav"
    synth_click_track(wav, bpm=120.0, dur=12.0, loud=(0.0, 99.0))
    res = bd.detect_beats(str(wav), backend="numpy", use_cache=False)
    assert res["climax"] is None and {s["level"] for s in res["energy_sections"]} == {"mid"}


def test_librosa_backend(tmp_path):
    pytest.importorskip("librosa")
    wav = tmp_path / "click.wav"
    truth, down_truth = synth_click_track(wav, bpm=120.0)
    res = bd.detect_beats(str(wav), backend="librosa", use_cache=False)
    assert res["backend"] == "librosa"
    assert abs(res["tempo"] - 120.0) < 2.0
    err = _match_err(res["beats"], truth[2:-2])
    assert err.max() < 0.05
    assert _match_err(res["downbeats"], down_truth[1:-1]).max() < 0.05


def test_librosa_forced_but_missing(tmp_path, no_librosa):
    wav = tmp_path / "click.wav"
    synth_click_track(wav, dur=4.0)
    with pytest.raises(bd.BeatDetectError):
        bd.detect_beats(str(wav), backend="librosa", use_cache=False)


def test_wav_decoding_without_ffmpeg(tmp_path, monkeypatch, no_librosa):
    wav = tmp_path / "click.wav"
    truth, _ = synth_click_track(wav, bpm=120.0, dur=10.0)
    monkeypatch.setattr(bd.shutil, "which", lambda name: None)
    res = bd.detect_beats(str(wav), backend="numpy", use_cache=False)
    assert abs(res["tempo"] - 120.0) < 1.0
    assert _match_err(res["beats"], truth[1:-1]).max() < 0.04
    mp3 = tmp_path / "x.mp3"
    mp3.write_bytes(b"\x00" * 100)
    with pytest.raises(bd.BeatDetectError):
        bd.load_audio(str(mp3))


def test_cache_roundtrip(tmp_path, monkeypatch, no_librosa):
    wav = tmp_path / "click.wav"
    synth_click_track(wav, dur=8.0)
    cache = tmp_path / "cache"
    r1 = bd.detect_beats(str(wav), backend="numpy", cache_dir=str(cache))
    assert not r1["cached"] and len(os.listdir(cache)) == 1

    def boom(*a, **k):
        raise AssertionError("应该命中缓存")

    monkeypatch.setattr(bd, "load_audio", boom)
    r2 = bd.detect_beats(str(wav), backend="numpy", cache_dir=str(cache))
    assert r2["cached"] and r2["beats"] == r1["beats"]


def test_beat_detect_cli(tmp_path, capsys, no_librosa):
    wav = tmp_path / "click.wav"
    synth_click_track(wav, dur=8.0)
    out = tmp_path / "b.json"
    rc = bd._main([str(wav), "-o", str(out), "--backend", "numpy", "--no-cache", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] and payload["data"]["tempo"] == pytest.approx(120, abs=1)
    assert json.loads(out.read_text(encoding="utf-8"))["beats"]
    assert bd._main([str(tmp_path / "missing.wav"), "--json"]) == 1


# ---------------------------------------------------------------------------
# 卡点吸附
# ---------------------------------------------------------------------------


GRID = {
    "beats": [0.5 * i for i in range(1, 41)],  # 120 BPM, 0.5..20.0
    "downbeats": [0.5 + 2.0 * i for i in range(10)],
    "onsets": [0.5 * i for i in range(1, 41)] + [3.25],
    "energy_sections": [
        {"start": 0.0, "end": 6.5, "energy": 0.1, "level": "low", "is_climax": False},
        {"start": 6.5, "end": 14.5, "energy": 0.95, "level": "high", "is_climax": True},
        {"start": 14.5, "end": 20.5, "energy": 0.5, "level": "mid", "is_climax": False},
    ],
    "climax": {"start": 6.5, "end": 14.5},
    "tempo": 120.0,
    "duration": 20.5,
}


def test_snap_cuts_basic():
    out = bs.snap_cuts([1.08, 1.6, 2.3, 7.0], GRID["beats"], tolerance=0.15)
    assert out == [1.0, 1.5, 2.3, 7.0]  # 2.3 离 2.5 有 0.2 > tolerance, 不动


def test_snap_cuts_prefers_downbeat_and_avoids_collision():
    d = bs.snap_cuts([2.47, 2.53, 2.9], GRID, tolerance=0.3, min_gap=0.2, details=True)
    assert d[0]["time"] == 2.5 and d[0]["kind"] == "downbeat"
    assert d[1]["time"] == 2.53 and d[1]["kind"] is None  # 抢同一拍, 保持原位
    assert d[2]["time"] == 3.0 and d[2]["kind"] == "beat"
    # 强拍偏置: 2.47 更靠近普通拍 2.5? 用 4.38 测: 普通拍 4.5 (0.12) vs 强拍 4.5 -> 同一个; 用 6.27
    d2 = bs.snap_cuts([6.27], GRID, tolerance=0.3, downbeat_bias=0.04, details=True)
    assert d2[0]["time"] == 6.5 and d2[0]["kind"] == "downbeat"  # 6.0 距 0.27, 6.5 距 0.23
    d3 = bs.snap_cuts([3.2], GRID, tolerance=0.1, use_onsets=True, details=True)
    assert d3[0]["time"] == 3.25 and d3[0]["kind"] == "onset"


def test_snap_cuts_frame_quantize():
    out = bs.snap_cuts([0.98, 5.0101], [1.013, 9.0], tolerance=0.1, fps=30)
    assert out[0] == pytest.approx(30 / 30) and out[1] == pytest.approx(150 / 30)


# ---------------------------------------------------------------------------
# 混剪规划
# ---------------------------------------------------------------------------


def _clips(tmp_path, n=6, dur=5.0):
    out = []
    for i in range(n):
        p = tmp_path / f"c{i}.mp4"
        p.write_bytes(b"")
        out.append({"path": str(p), "duration": dur, "score": float(i)})
    return out


def test_plan_beat_montage_on_grid(tmp_path):
    clips = _clips(tmp_path)
    plan = bs.plan_beat_montage(clips, GRID, density="标准", start=0.5, duration=18.0, fps=30)
    shots = plan["shots"]
    beat_set = set(GRID["beats"])
    # 时间轴连续, 无缝, 帧对齐
    t = 0
    for sh in shots:
        ac = sh["add_clip"]
        assert ac["target_start"] == t
        t += ac["duration"]
        assert ac["duration"] > 0
        assert abs(ac["target_start"] * 30 / 1e6 - round(ac["target_start"] * 30 / 1e6)) < 1e-3
        assert ac["source_start"] + ac["duration"] <= 5.0 * 1e6
    assert t == plan["music"]["duration_us"] == int(18.0 * 1e6)
    # 切点都在拍上 (误差 <= 2 帧)
    assert plan["stats"]["max_cut_error_frames"] <= 2
    for c in plan["cuts"][1:-1]:
        assert min(abs(c - b) for b in beat_set) < 1 / 30 + 1e-6
    # 低能段 4 拍一切 (2s), 高能段每拍 (0.5s), 中段 2 拍 (1s)
    lows = [sh["duration"] for sh in shots if sh["level"] == "low"]
    highs = [sh["duration"] for sh in shots if sh["level"] == "high"]
    mids = [sh["duration"] for sh in shots if sh["level"] == "mid"]
    assert max(highs) <= 0.51 and np.median(lows) == pytest.approx(2.0, abs=0.04)
    assert np.median(mids) == pytest.approx(1.0, abs=0.04)
    # 低能段对齐小节: 切点落在强拍
    for sh in shots:
        if sh["level"] == "low" and sh["index"] > 0:
            assert sh["on_downbeat"]
    # 相邻镜头不重复素材
    for a, b in zip(shots[:-1], shots[1:]):
        assert a["path"] != b["path"]
    # 开头镜头 = 最高分素材
    assert shots[0]["score"] == 5.0
    # 重音: 只在高能段强拍, 不超过场景上限
    assert 0 < len(plan["accents"]) <= 4
    for a in plan["accents"]:
        sh = shots[a["shot"]]
        assert sh["on_downbeat"] and sh["level"] == "high"


def test_plan_high_score_on_important_slots(tmp_path):
    clips = _clips(tmp_path, n=12)
    plan = bs.plan_beat_montage(
        clips, GRID, density=2, start=0.5, duration=18.0, energy_speedup=False
    )
    shots = plan["shots"]
    first_use = {}
    for sh in sorted(shots, key=lambda s: -s["importance"]):
        first_use.setdefault(sh["clip_id"], sh["importance"])
    # 最高分的素材放在最重要的槽位上
    top = sorted(shots, key=lambda s: -s["importance"])[0]
    assert top["score"] == 11.0
    climax_scores = [sh["score"] for sh in shots if sh["is_climax"] and sh["on_downbeat"]]
    low_scores = [sh["score"] for sh in shots if sh["level"] == "low" and not sh["on_downbeat"]]
    assert np.mean(climax_scores) > np.mean(low_scores)


def test_plan_start_climax_and_short_clips(tmp_path):
    clips = [
        {"path": str(tmp_path / "a.mp4"), "duration": 0.8},
        {"path": str(tmp_path / "b.mp4"), "duration": 0.8},
    ]
    plan = bs.plan_beat_montage(clips, GRID, density="舒缓", start="climax", duration=6.0)
    assert plan["music"]["source_start"] == 6.5
    for sh in plan["shots"]:
        assert sh["duration"] <= 0.75 + 1e-6  # 镜头不超过素材可用长度
        assert sh["source_start"] + sh["duration"] <= 0.8


def test_plan_highlight_and_image(tmp_path):
    img = tmp_path / "p.jpg"
    img.write_bytes(b"")
    clips = [
        {"path": str(tmp_path / "v.mp4"), "duration": 10.0, "highlight": 6.0, "score": 9},
        {"path": str(img)},
    ]
    plan = bs.plan_beat_montage(clips, GRID, density="标准", start=6.5, duration=4.0)
    first_v = next(sh for sh in plan["shots"] if sh["path"].endswith("v.mp4"))
    assert first_v["source_start"] <= 6.0 <= first_v["source_start"] + first_v["duration"]
    assert any(sh["path"].endswith("p.jpg") and sh["source_start"] == 0 for sh in plan["shots"])


def test_plan_errors(tmp_path):
    with pytest.raises(bs.BeatPlanError):
        bs.plan_beat_montage([], GRID)
    with pytest.raises(bs.BeatPlanError):
        bs.plan_beat_montage(_clips(tmp_path), GRID, density="乱切")


def test_plan_from_detected_beats(tmp_path, no_librosa):
    wav = tmp_path / "click.wav"
    synth_click_track(wav, bpm=120.0)
    res = bd.detect_beats(str(wav), backend="numpy", use_cache=False)
    plan = bs.plan_beat_montage(_clips(tmp_path), res, density="标准", start="climax", duration=8.0)
    assert plan["music"]["path"] == os.path.abspath(str(wav))
    assert plan["stats"]["max_cut_error_frames"] <= 2
    assert all(sh["is_climax"] for sh in plan["shots"][:-1])
    # 高潮段 (high) 每拍切
    assert np.median([sh["duration"] for sh in plan["shots"]]) == pytest.approx(0.5, abs=0.04)


def test_snap_cli(tmp_path, capsys):
    beats = tmp_path / "b.json"
    beats.write_text(json.dumps(GRID), encoding="utf-8")
    rc = bs._main(["snap", "--beats", str(beats), "--cuts", "1.08,2.3", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["data"]
    assert [c["time"] for c in data["cuts"]] == [1.0, 2.3]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="需要 ffmpeg 合成测试视频")
def test_build_draft(tmp_path, no_librosa):
    clips = []
    for i, color in enumerate(("red", "blue", "green")):
        p = tmp_path / f"v{i}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=320x240:d=4:r=30",
                "-pix_fmt",
                "yuv420p",
                str(p),
            ],
            check=True,
        )
        clips.append({"path": str(p), "duration": 4.0, "score": i})
    wav = tmp_path / "song.wav"
    synth_click_track(wav, bpm=120.0, dur=20.0)
    res = bd.detect_beats(str(wav), backend="numpy", use_cache=False)
    plan = bs.plan_beat_montage(clips, res, density="标准", start="climax", duration=6.0)
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    saved = bs.build_draft(plan, "BeatTest", drafts_root=str(drafts), width=320, height=240)
    with open(os.path.join(saved["draft_path"], "draft_info.json"), encoding="utf-8") as f:
        content = json.load(f)
    tracks = {t["type"]: t for t in content["tracks"]}
    vsegs = tracks["video"]["segments"]
    assert len(vsegs) == len(plan["shots"])
    for seg, sh in zip(vsegs, plan["shots"]):
        assert seg["target_timerange"]["start"] == sh["add_clip"]["target_start"]
        assert seg["target_timerange"]["duration"] == sh["add_clip"]["duration"]
        assert seg["source_timerange"]["start"] == sh["add_clip"]["source_start"]
        assert seg["volume"] == 0.0
    aseg = tracks["audio"]["segments"][0]
    assert aseg["source_timerange"]["start"] == plan["music"]["source_start_us"]
    assert aseg["target_timerange"]["duration"] == plan["music"]["duration_us"]
    accented = [vsegs[a["shot"]] for a in plan["accents"]]
    assert all(s.get("common_keyframes") for s in accented)
