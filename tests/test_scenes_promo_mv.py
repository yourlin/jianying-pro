"""宣传片 / MV 场景、卡点的 cut_on 开关、教程等待段、标题卡钩子 (fixtures 在 tests/fixtures/scenes)。"""

import copy
import json
import os
import sys

import numpy as np
import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import beat_snap as bs  # noqa: E402
import script_compiler as sc  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "scenes")
FPS = 30


def _script(name: str) -> dict:
    with open(os.path.join(FIX, f"{name}.script.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def _compile(script: dict, **kw) -> dict:
    return sc.Compiler(copy.deepcopy(script), FIX, **kw).compile()


def _lyric_bounds() -> list:
    with open(os.path.join(FIX, "lyrics_mv.json"), "r", encoding="utf-8") as f:
        lines = bs.load_lyrics(json.load(f)["lyrics"])
    return bs.lyric_boundaries(lines)


def _texts(sb: dict) -> list:
    return [t for sh in sb["shots"] for t in sh.get("texts") or []]


# ---------------------------------------------------------------------------
# 场景文件
# ---------------------------------------------------------------------------


def test_scene_aliases_and_files():
    assert sc.load_profile("scenes", "宣传片")["id"] == "promo"
    for alias in ("MV", "音乐视频"):
        assert sc.load_profile("scenes", alias)["id"] == "music_video"
    assert sc.load_profile("scenes", "beat_montage")["pacing"]["cut_on"] == "beat"
    assert sc.load_profile("scenes", "music_video")["pacing"]["cut_on"] == "lyric"
    for sid in ("promo", "music_video"):
        scene = sc.load_profile("scenes", sid)
        assert os.path.exists(os.path.join(ROOT, "references", "scenes", scene["doc"]))
        assert abs(sum(b["ratio"] for b in scene["beats"]) - 1.0) < 0.02


# ---------------------------------------------------------------------------
# 宣传片
# ---------------------------------------------------------------------------


def test_promo_compiles_with_structure():
    sb = _compile(_script("promo"))
    plan = sb["montage"]
    assert sb["compiled_from"]["scene"] == "promo" and plan["cut_on"] == "beat"
    assert sb["main"]["montage"] is True and sb["main"]["keep"] == [[0.0, 50.0]]
    shots = plan["shots"]
    durs = [s["duration"] for s in shots]
    # 镜头 1.0–3.5s 为主, 中位约 1.6–2.1s (标题卡 / 片尾卡是有意的长镜头)
    inner = durs[1:-1]
    assert 1.0 - 1e-6 <= float(np.median(inner)) <= 2.2
    assert min(inner) >= 1.0 - 1e-6
    # 标题卡 3–4s 不切; 片尾卡 >= 3s
    assert 3.0 <= shots[0]["duration"] <= 4.0
    assert shots[-1]["duration"] >= 3.0
    # 蒙太奇段: 每 2 拍一刀, 同样的帧数 (最后一个镜头吸收和下一段拍点之间的几帧误差)
    beat = 60 / 115
    mont = [round(s["duration"] * FPS) for s in shots if s["section"] == "S04"]
    assert len(mont) >= 5
    assert len(set(mont[:-1])) == 1 and mont[0] == round(2 * beat * FPS)
    assert abs(mont[-1] - mont[0]) <= 3
    # 转场: 硬切 >= 70%, 叠化只在开头 / 结尾, 章节 1 次闪黑
    tr = sb["main"]["transitions"]
    assert len(tr) == len(shots) - 1
    assert sum(1 for t in tr if not t) / len(tr) >= 0.7
    assert tr[0]["name"] == "叠化" and tr[-1]["name"] == "叠化"
    assert [t["name"] for t in tr[1:-1] if t] == ["闪黑"]
    # 字幕: 不逐句出; 卖点是左下角标签卡, 3–5s
    assert "subtitles" not in sb
    labels = [t for t in _texts(sb) if t["track"] == "CardLabel"]
    assert len(labels) == 6
    for t in labels:
        assert t["x"] < -0.3 and t["y"] < 0 and t["align"] == 0
        assert 3.0 - 1e-6 <= t["dur"] <= 5.0 + 1e-6
    subs = [t for t in _texts(sb) if t["track"] == "CardLabelSub"]
    assert subs and all(s["y"] < labels[0]["y"] for s in subs)  # 副标题在主标题下方, 不重叠
    # 色调: 按段落检查
    assert sb["review"]["continuity"] == "per_section"
    assert len(sb["review"]["sections"]) == len(sb["shots"])
    # 段落指定素材: 标题卡 / 片尾卡用自己的底图, 其他段落不借用
    assert shots[0]["path"].endswith("title_bg.mp4") and shots[-1]["path"].endswith("end_bg.mp4")
    assert not any(s["path"].endswith(("title_bg.mp4", "end_bg.mp4")) for s in shots[1:-1])
    assert not [n for n in sb["notes"] if n.startswith("⚠️")]


def test_promo_languages_share_timeline():
    zh = _compile(_script("promo"))
    en = _compile(_script("promo"), lang="en")
    assert en["montage"]["cuts"] == zh["montage"]["cuts"]
    assert en["main"]["transitions"] == zh["main"]["transitions"]
    assert [sh["time"] for sh in en["shots"]] == [sh["time"] for sh in zh["shots"]]
    contents = [t["content"] for t in _texts(en)]
    assert "STARFALL" in contents and "Play now" in contents
    assert not any("一" <= ch <= "鿿" for c in contents for ch in c)
    assert en["compiled_from"]["lang"] == "en"


def test_promo_compile_script_file(tmp_path):
    import shutil

    for f in os.listdir(FIX):
        shutil.copy(os.path.join(FIX, f), tmp_path / f)
    out = sc.compile_script(str(tmp_path / "promo.script.json"), lang="en")
    assert out.endswith("_en.json")
    with open(out, "r", encoding="utf-8") as f:
        sb = json.load(f)
    assert sb["project"]["name"].endswith("_en") and sb["montage"]["shots"]


# ---------------------------------------------------------------------------
# MV: 按歌词行切
# ---------------------------------------------------------------------------


def test_music_video_cuts_on_lyric_lines():
    sb = _compile(_script("music_video"))
    plan = sb["montage"]
    assert plan["cut_on"] == "lyric" and plan["scene"] == "music_video"
    bounds = _lyric_bounds()
    shots = plan["shots"]
    assert plan["stats"]["lyric_cuts"] >= 6
    for sh in shots[1:]:
        assert sh["cut_kind"] in ("lyric", "section", "split")
        if sh["cut_kind"] != "split":  # 切点落在歌词行边界上 (误差 <= 1 帧)
            assert min(abs(sh["target_start"] - b) for b in bounds) <= 1 / FPS + 1e-6
    # 标题卡 3.3s 不切
    assert shots[0]["duration"] == pytest.approx(3.3, abs=1 / FPS)
    # 太短的歌词行 (< 1s) 不单独成镜头
    assert min(sh["duration"] for sh in shots) >= 1.0 - 1e-6
    # 高潮段允许长镜头 (超过非高潮段上限 4s), 非高潮段不超过 4s
    climax = [sh for sh in shots if sh["target_start"] >= 26.0]
    assert max(sh["duration"] for sh in climax) > 4.0
    assert all(
        sh["duration"] <= 4.0 + 1e-6 for sh in shots if sh["target_start"] + sh["duration"] <= 26.0
    )
    # 推拉只在进出产品 UI; 没有强拍重音
    cams = sb["main"]["camera"]
    assert [c["move"] for c in cams] == ["push_in", "pull_out"]
    assert all(20.0 - 1e-6 <= c["at"] <= 24.7 for c in cams)
    assert plan["accents"] == []
    # 歌词字幕一句一条, 统一调色提示
    assert len(sb["subtitles"]["lines"]) == 16
    assert sb["review"]["continuity"] == "global"


def test_music_video_section_by_lyric_anchor():
    sb = _compile(_script("music_video"))
    s02 = next(sh for sh in sb["shots"] if sh["id"] == "S02")
    assert s02["time"][0] == pytest.approx(3.3, abs=1 / FPS)


def test_music_video_skips_camera_without_ui_insert():
    script = _script("music_video")
    script["beats"][3]["camera"] = "push_in"  # 高潮段不是 UI 插入: 不推拉
    sb = _compile(script)
    assert all(c["at"] < 24.7 for c in sb["main"]["camera"])
    assert any("只在进出产品 UI" in n for n in sb["notes"])


# ---------------------------------------------------------------------------
# beat_montage / beat_snap 的 cut_on 开关
# ---------------------------------------------------------------------------


def _beats_mv():
    with open(os.path.join(FIX, "beats_mv.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def _clips(tmp_path, n=6, dur=12.0):
    out = []
    for i in range(n):
        p = tmp_path / f"c{i}.mp4"
        p.write_bytes(b"")
        out.append({"path": str(p), "duration": dur, "score": float(i)})
    return out


def test_beat_montage_cut_on_switch(tmp_path):
    lyrics = os.path.join(FIX, "lyrics_mv.json")
    by_beat = bs.plan_beat_montage(_clips(tmp_path), _beats_mv(), start=3.3, duration=20.0)
    assert by_beat["cut_on"] == "beat" and by_beat["scene"] == "beat_montage"
    assert {sh["cut_kind"] for sh in by_beat["shots"][1:]} <= {"beat", "split"}
    by_lyric = bs.plan_beat_montage(
        _clips(tmp_path), _beats_mv(), start=3.3, duration=20.0, cut_on="lyric", lyrics=lyrics
    )
    bounds = [b - 3.3 for b in _lyric_bounds()]
    assert by_lyric["cut_on"] == "lyric"
    for sh in by_lyric["shots"][1:]:
        if sh["cut_kind"] == "lyric":
            assert min(abs(sh["target_start"] - b) for b in bounds) <= 1 / FPS + 1e-6
    assert by_lyric["stats"]["lyric_cuts"] >= 6
    assert len(by_lyric["shots"]) < len(by_beat["shots"])  # 按句切比卡拍慢
    with pytest.raises(bs.BeatPlanError):
        bs.plan_beat_montage(_clips(tmp_path), _beats_mv(), cut_on="lyric")
    with pytest.raises(bs.BeatPlanError):
        bs.plan_beat_montage(_clips(tmp_path), _beats_mv(), cut_on="word")


def test_lyric_long_gap_split_on_beat(tmp_path):
    lines = [{"start": 0.5, "end": 2.0, "text": "a"}, {"start": 12.0, "end": 14.0, "text": "b"}]
    plan = bs.plan_beat_montage(
        _clips(tmp_path), _beats_mv(), start=0.0, duration=16.0, cut_on="lyric", lyrics=lines
    )
    beats = _beats_mv()["beats"]
    splits = [sh for sh in plan["shots"] if sh["cut_kind"] == "split"]
    assert splits  # 0.5–12s 没有歌词: 在拍点上补切, 镜头不超过 4s (非高潮)
    for sh in splits:
        assert min(abs(sh["target_start"] - b) for b in beats) <= 1 / FPS + 1e-6
    assert max(sh["duration"] for sh in plan["shots"]) <= 4.0 + 1e-6


def test_beat_snap_cli_cut_on_lyric(tmp_path, capsys):
    clips = tmp_path / "clips.json"
    clips.write_text(json.dumps(_clips(tmp_path)), encoding="utf-8")
    rc = bs._main(
        [
            "plan",
            "--beats",
            os.path.join(FIX, "beats_mv.json"),
            "--clips",
            str(clips),
            "--scene",
            "music_video",
            "--lyrics",
            os.path.join(FIX, "lyrics_mv.json"),
            "--duration",
            "20",
            "--json",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["data"]["cut_on"] == "lyric"


# ---------------------------------------------------------------------------
# 教程: 长等待加速 / 跳剪, 底部深色字幕条, 默认 BGM
# ---------------------------------------------------------------------------


def test_tutorial_long_wait_jump_cut():
    sb = _compile(_script("tutorial"))
    keep = sb["main"]["keep"]
    # 10–22s 无旁白 12s (> 5s): 只留头尾各 1s; 3–5s 的 2s 停顿照常剪气口
    assert [11.0, 21.0] not in keep
    assert any(a <= 10.5 and b == pytest.approx(11.0) for a, b in keep)
    assert any(a == pytest.approx(21.0) for a, _b in keep)
    removed = sum(b - a for a, b in zip([k[1] for k in keep[:-1]], [k[0] for k in keep[1:]]))
    assert removed >= 10.0
    assert any("等待段" in n and "跳剪" in n for n in sb["notes"])
    assert "speed" not in sb["main"]


def test_tutorial_long_wait_speedup():
    script = _script("tutorial")
    script["waits_mode"] = "加速"
    sb = _compile(script)
    sp = sb["main"]["speed"]
    assert len(sp) == 1 and sp[0]["range"] == [11.0, 21.0]
    assert 2.0 <= sp[0]["speed"] <= 8.0
    assert sp[0]["range"] in sb["main"]["keep"]  # 加速段单独成一段 keep


def test_tutorial_explicit_waits_on_silent_recording(tmp_path):
    analysis = tmp_path / "a.json"
    analysis.write_text(json.dumps({"video": {"duration": 180.0}}), encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"dead_zones": [{"start": 40.0, "end": 58.0},
                                                 {"start": 70.0, "end": 73.0}]}), encoding="utf-8")  # fmt: skip
    script = _script("tutorial")
    script.update(analysis=str(analysis), waits=str(report))
    script["beats"][-1]["time"] = [170, 180]
    sb = sc.Compiler(script, str(tmp_path)).compile()
    assert sb["main"]["keep"] == [[0.0, 41.0], [57.0, 180.0]]  # 3s 的不算等待


def test_tutorial_subtitle_bar_and_bgm():
    sb = _compile(_script("tutorial"))
    prim = sb["subtitles"]["primary"]
    assert prim["y"] == pytest.approx(-0.85) and "anchor_bottom" not in prim
    assert prim["background"]["color"] == "#000000" and prim["border"] is False
    assert sb["audio"]["bgm"]["volume"] == pytest.approx(0.12)
    script = _script("tutorial")
    script["bgm"] = False
    assert _compile(script)["audio"] == {}
    v = _compile(_script("tutorial"), fmt="vertical")  # 竖屏不套横屏的 -0.85
    assert v["subtitles"]["primary"].get("y") != pytest.approx(-0.85)


# ---------------------------------------------------------------------------
# 标题卡钩子: 0 秒大标题停 3–4s 不切, 不算违规
# ---------------------------------------------------------------------------


def _timeline(sb: dict, cuts: list) -> dict:
    total = sb["main"]["keep"][-1][1]
    texts = [{"start": float(t["start"]), "end": float(t["start"]) + float(t["dur"]),
              "text": t["content"]} for t in _texts(sb)]  # fmt: skip
    for ln in (sb.get("subtitles") or {}).get("lines", []):
        texts.append({"start": ln["start"], "end": ln["end"], "text": ln["text"]})
    return {
        "duration": total, "cuts": cuts, "changes": [], "texts": texts, "texts_known": True,
        "overlays": [], "speech_segments": [], "speech_known": False, "speech_text": [],
    }  # fmt: skip


def _static_feat(total: float):
    times = np.arange(0, total, 0.2)
    lab = np.zeros((len(times), 3))
    lab[:, 0] = 30.0  # 压暗的实机画面, 不是黑场
    return {"times": times, "lab": lab, "luma_std": np.full(len(times), 0.12),
            "diff": np.zeros(len(times))}  # fmt: skip


@pytest.mark.parametrize("name", ["promo", "music_video"])
def test_title_card_hook_not_flagged(name):
    import quality_review as qr

    sb = _compile(_script(name))
    cuts = [s["target_start"] for s in sb["montage"]["shots"][1:]]
    assert cuts[0] >= 3.0  # 前 3 秒一刀不切
    tl = _timeline(sb, cuts)
    issues = []
    qr.check_hook(tl, _static_feat(tl["duration"]), None, issues)
    assert issues == []
    assert not [n for n in sb["notes"] if "标题卡" in n]
    assert sb["review"]["hook"]["type"] == "title_card"
    assert sb["review"]["intentional_holds"][0]["why"] == "标题卡"


def test_tutorial_episode_title_hook_not_flagged():
    import quality_review as qr

    sb = _compile(_script("tutorial"))
    keep = sb["main"]["keep"]
    tl = _timeline(sb, [])
    tl["duration"] = sum(b - a for a, b in keep)
    issues = []
    qr.check_hook(tl, _static_feat(tl["duration"]), None, issues)
    assert issues == []
    first = sb["shots"][0]["texts"][0]
    assert first["role"] == "title" and first["start"] <= 0.3
    assert not [n for n in sb["notes"] if "标题卡" in n]
