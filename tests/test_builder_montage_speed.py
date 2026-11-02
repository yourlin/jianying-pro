"""装配器: 混剪 (main.montage) / 变速 (main.speed) / 字幕背景 / 多语言草稿名。

示例剧本在 tests/fixtures/scenes; 素材 (纯色视频 / 正弦波音乐) 用 ffmpeg 现场生成, 草稿写到临时 drafts_root。
"""

import copy
import json
import os
import shutil
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "vendor"))

import edit_diff as ed  # noqa: E402
import script_compiler as sc  # noqa: E402
import storyboard_builder as sbb  # noqa: E402
from utils.frame_align import (  # noqa: E402
    frames_to_us,
    snap_source_range,
    snap_us,
    transition_duration,
    us_to_frames,
)

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "scenes")
FPS = 30
FRAME = 1_000_000 / FPS
US = 1_000_000

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="需要 ffmpeg / ffprobe",
)

CLIPS = {  # 示例剧本用到的素材 -> 时长 (秒)
    **{
        c: 12
        for c in (
            "title_bg story_a story_b ult stage_1 stage_2 stage_3 stage_4 ui_shop ui_talent end_bg "
            "band_wide singer_a singer_b choir product_ui"
        ).split()
    },
    "drums": 6,
    "sax": 6,
}


def _ff(*args):
    import subprocess

    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("需要 ffmpeg / ffprobe")
    d = tmp_path_factory.mktemp("scenes")
    for f in os.listdir(FIX):
        shutil.copy(os.path.join(FIX, f), d / f)
    (d / "clips").mkdir()
    for name, dur in CLIPS.items():
        _ff("-f", "lavfi", "-i", f"color=c=gray:s=160x90:r=30:d={dur}", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", str(d / "clips" / f"{name}.mp4"))  # fmt: skip
    _ff("-f", "lavfi", "-i", "sine=f=440:d=55", str(d / "bgm.mp3"))
    shutil.copy(d / "bgm.mp3", d / "song.mp3")
    _ff("-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=40", "-f", "lavfi", "-i",
        "sine=f=300:d=40", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(d / "recording.mp4"))  # fmt: skip
    return str(d)


@pytest.fixture()
def drafts_root(tmp_path, monkeypatch):
    import jy_wrapper

    root = str(tmp_path / "drafts")
    os.makedirs(root)

    class TmpProject(jy_wrapper.JyProject):  # 只写临时 drafts_root, 不碰真实剪映草稿目录
        def __init__(self, name, **kw):
            kw.pop("drafts_root", None)
            super().__init__(name, drafts_root=root, **kw)

    monkeypatch.setattr(jy_wrapper, "JyProject", TmpProject)
    monkeypatch.setenv("JY_USER_PREFS", "off")  # 个人偏好不影响测试
    monkeypatch.setenv("JY_SKIP_EDITOR_CHECK", "1")
    return root


def _script(media, name: str) -> dict:
    with open(os.path.join(media, f"{name}.script.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def _build(media, drafts_root, sb: dict):
    res = sbb.StoryboardBuilder(sb, media).build()
    with open(
        os.path.join(drafts_root, sb["project"]["name"], "draft_info.json"), encoding="utf-8"
    ) as f:
        return res, json.load(f)


def _track(content, name):
    return next(t for t in content["tracks"] if t.get("name") == name)["segments"]


def _end(s):
    return s["target_timerange"]["start"] + s["target_timerange"]["duration"]


def simulate_jianying(content: dict) -> dict:
    """模拟剪映打开草稿时的规整 (同 test_frame_align.simulate_jianying, 另外处理变速):
    片段时长取整帧, 同轨首尾相接逐段吸附; 变速片段的时长 = 截取时长 / 倍速 取整帧; 截取范围落在帧边界;
    重叠转场按 transition_duration 截短。"""
    d = copy.deepcopy(content)
    fps = float(d.get("fps") or 30)
    trans = {m["id"]: m for m in d["materials"].get("transitions", [])}
    for track in d["tracks"]:
        prev_ef = None
        for s in sorted(track["segments"], key=lambda x: x["target_timerange"]["start"]):
            tt, st = s["target_timerange"], s.get("source_timerange")
            speed = float(s.get("speed") or 1.0)
            sf = us_to_frames(tt["start"], fps)
            if prev_ef is not None and abs(sf - prev_ef) <= 1:  # 主轨吸附: 贴上一段终点
                sf = prev_ef
            if st and speed != 1.0:
                ef = sf + us_to_frames(st["duration"] / speed, fps)
                st["start"], st["duration"] = snap_source_range(
                    st["start"], us_to_frames(st["duration"], fps), fps
                )
            else:
                ef = sf + us_to_frames(tt["duration"], fps)
                if st:
                    st["start"], st["duration"] = snap_source_range(st["start"], ef - sf, fps)
            tt["start"] = frames_to_us(sf, fps)
            tt["duration"] = frames_to_us(ef, fps) - tt["start"]
            prev_ef = ef
        segs = track["segments"]
        for i, s in enumerate(segs):
            for r in s.get("extra_material_refs", []):
                m = trans.get(r)
                if m and m.get("is_overlap"):
                    nxt = segs[i + 1]["target_timerange"]["duration"] if i + 1 < len(segs) else None
                    m["duration"] = transition_duration(
                        m["duration"], s["target_timerange"]["duration"], nxt, fps
                    )
    return d


def _assert_no_alignment_noise(content):
    sim = simulate_jianying(content)
    res = ed.diff_drafts(content, sim)
    assert res["modified"] is False, res["changes"]
    assert res["noise"].get("frame_snap", 0) == 0
    assert res["noise"].get("transition_auto", 0) == 0
    for t in content["tracks"]:  # 逐微秒不变 (剪映规整的不动点)
        for s in t["segments"]:
            for v in (s["target_timerange"]["start"], _end(s)):
                assert snap_us(v) == v


def _assert_montage(sb, content, res):
    plan = sb["montage"]
    shots = plan["shots"]
    main = _track(content, "Main")
    # 镜头数 / 首尾相接 / 切点和计划一致 (1 帧以内)
    assert len(main) == len(shots) and res["report"]["montage"]["shots"] == len(shots)
    for a, b in zip(main, main[1:]):
        assert _end(a) == b["target_timerange"]["start"]
    for seg, cut in zip(main, plan["cuts"][:-1]):
        assert abs(seg["target_timerange"]["start"] - cut * US) <= FRAME + 1
    assert abs(_end(main[-1]) - plan["cuts"][-1] * US) <= FRAME + 1
    assert abs(res["report"]["total"] - plan["total_duration"]) <= 1 / FPS
    # 素材静音, 截取起点和计划一致
    for seg, sh in zip(main, shots):
        assert seg["volume"] == 0.0
        assert abs(seg["source_timerange"]["start"] - sh["add_clip"]["source_start"]) <= FRAME
    # 音乐轨: 一段, 从 0 开始铺满成片
    music = [s for t in content["tracks"] if t["type"] == "audio" for s in t["segments"]]
    assert len(music) == 1
    assert music[0]["target_timerange"]["start"] == 0
    assert abs(music[0]["target_timerange"]["duration"] - plan["total_duration"] * US) <= FRAME + 1
    assert abs(music[0]["source_timerange"]["start"] - plan["music"]["source_start_us"]) <= FRAME
    # 转场: 按剪点挂在剪点前一个镜头上
    trans = {m["id"]: m for m in content["materials"]["transitions"]}
    want = [(i, t["name"]) for i, t in enumerate(sb["main"]["transitions"]) if t]
    got = [
        (i, trans[r]["name"])
        for i, s in enumerate(main)
        for r in s.get("extra_material_refs", [])
        if r in trans
    ]
    assert got == want
    return main


@needs_ffmpeg
def test_promo_montage_build(media, drafts_root):
    sb = sc.Compiler(_script(media, "promo"), media).compile()
    res, content = _build(media, drafts_root, sb)
    _assert_montage(sb, content, res)
    # 文字照常叠加: 标题卡 / 标签卡 / 结尾卡
    names = {t.get("name") for t in content["tracks"] if t["type"] == "text"}
    assert {"Hero", "CardLabel"} <= names
    labels = _track(content, "CardLabel")
    assert len(labels) == len([t for sh in sb["shots"] for t in sh.get("texts") or []
                               if t["track"] == "CardLabel"])  # fmt: skip
    assert not [w for w in res["report"]["warnings"] if "镜头" in w or "转场" in w]
    _assert_no_alignment_noise(content)


@needs_ffmpeg
def test_music_video_montage_build(media, drafts_root):
    sb = sc.Compiler(_script(media, "music_video"), media).compile()
    res, content = _build(media, drafts_root, sb)
    main = _assert_montage(sb, content, res)
    # 推拉: 进出产品 UI 的两个镜头上有缩放关键帧, 其他镜头没有
    zoomed = [
        i
        for i, s in enumerate(main)
        if any(k["property_type"] == "KFTypeScaleX" for k in s.get("common_keyframes") or [])
    ]
    cams = sb["main"]["camera"]
    want = sorted({next(i for i, s in enumerate(main) if s["target_timerange"]["start"] <= c["at"] * US
                         < _end(s)) for c in cams})  # fmt: skip
    assert zoomed == want and len(want) == 2
    # 歌词字幕一句一条
    assert res["report"]["subtitles"] == len(sb["subtitles"]["lines"])
    _assert_no_alignment_noise(content)


@needs_ffmpeg
def test_promo_english_build_has_suffix(media, drafts_root):
    out = sc.compile_script(os.path.join(media, "promo.script.json"), lang="en")
    res = sbb.build(out, review=False)
    name = res["draft"]
    assert name.endswith("_en")
    assert os.path.exists(os.path.join(drafts_root, name, "draft_info.json"))
    with open(os.path.join(drafts_root, name, "draft_info.json"), encoding="utf-8") as f:
        content = json.load(f)
    texts = [json.loads(m["content"])["text"] for m in content["materials"]["texts"]]
    assert "STARFALL" in texts and not any("一" <= ch <= "鿿" for t in texts for ch in t)
    with open(out, "r", encoding="utf-8") as f:
        sb = json.load(f)
    assert (
        len(_track(content, "Main"))
        == len(sb["montage"]["shots"])
        == res["report"]["montage"]["shots"]
    )


def _tutorial_speed_sb(media) -> dict:
    script = _script(media, "tutorial")
    script["waits_mode"] = "加速"
    sb = sc.Compiler(script, media).compile()
    sb["audio"] = {}  # 默认 BGM 要从云端下载, 测试里不要
    return sb


@needs_ffmpeg
def test_tutorial_speedup_build(media, drafts_root):
    sb = _tutorial_speed_sb(media)
    (sa, sb_), sp = sb["main"]["speed"][0]["range"], sb["main"]["speed"][0]["speed"]
    res, content = _build(media, drafts_root, sb)
    main = _track(content, "Main")
    keep = sb["main"]["keep"]
    assert len(main) == len(keep)
    for a, b in zip(main, main[1:]):
        assert _end(a) == b["target_timerange"]["start"]
    k = keep.index([sa, sb_])
    fast = main[k]
    assert fast["speed"] == pytest.approx(sp)
    assert abs(fast["source_timerange"]["duration"] - (sb_ - sa) * US) <= FRAME
    assert abs(fast["target_timerange"]["duration"] - (sb_ - sa) / sp * US) <= FRAME / 2 + 1

    # 理想成片时间: 原速段按源时长, 变速段按 (b-a)/speed
    def out(src):
        t = 0.0
        for a, b in keep:
            f = sp if [a, b] == [sa, sb_] else 1.0
            if src <= b:
                return t + max(0.0, src - a) / f
            t += (b - a) / f
        return t

    for seg, (a, _b) in zip(main, keep):
        assert abs(seg["target_timerange"]["start"] - out(a) * US) <= FRAME
    assert abs(res["report"]["total"] - out(keep[-1][1])) <= 1 / FPS
    assert res["report"]["speed"][0]["speed"] == pytest.approx(sp)
    # 字幕按源时间定位, 换算到成片时要考虑变速
    subs = _track(content, "SubMain")
    lines = sb["subtitles"]["lines"]
    lead = float(sb["subtitles"].get("lead", 0))
    assert len(subs) == len(lines)
    for seg, ln in zip(subs, lines):
        assert abs(seg["target_timerange"]["start"] - out(max(0, ln["start"] - lead)) * US) <= FRAME
    after = [ln for ln in lines if ln["start"] - lead >= sb_]
    assert after  # 加速段后面的字幕整体前移了 (b-a)(1 - 1/speed)
    cta = _track(content, "CTA")[0]
    cta_src = next(t for sh in sb["shots"] for t in sh["texts"] if t["track"] == "CTA")["start"]
    assert abs(cta["target_timerange"]["start"] - out(cta_src) * US) <= FRAME
    # 统一转场不加在变速边界 (源视频连续, 不是剪点)
    trans = {m["id"] for m in content["materials"].get("transitions", [])}
    with_t = [i for i, s in enumerate(main) if set(s.get("extra_material_refs", [])) & trans]
    joins = {i for i, (x, y) in enumerate(zip(keep, keep[1:])) if abs(x[1] - y[0]) < 1e-3}
    assert with_t and not set(with_t) & joins
    _assert_no_alignment_noise(content)


@needs_ffmpeg
def test_subtitle_background_written(media, drafts_root):
    sb = _tutorial_speed_sb(media)
    bg = sb["subtitles"]["primary"]["background"]
    _res, content = _build(media, drafts_root, sb)
    subs = _track(content, "SubMain")
    mats = {m["id"]: m for m in content["materials"]["texts"]}
    for s in subs:
        m = mats[s["material_id"]]
        assert m["check_flag"] & 16
        assert m["background_color"] == bg["color"]
        assert m["background_alpha"] == pytest.approx(bg["alpha"])
        assert m["background_round_radius"] == pytest.approx(bg["round_radius"])
        assert m["background_style"] == bg["style"]
    others = [m for m in mats.values() if m["id"] not in {s["material_id"] for s in subs}]
    assert others and not any(m["check_flag"] & 16 for m in others)


def test_text_background_spec():
    bg = sbb.text_background({"color": "#11223380", "round_radius": 0.3, "style": 2})
    j = bg.export_json()
    assert j["background_color"] == "#112233" and j["background_alpha"] == pytest.approx(0x80 / 255)
    assert j["background_style"] == 2 and j["background_round_radius"] == 0.3
    j = sbb.text_background(True).export_json()
    assert j["background_color"] == "#000000" and j["background_alpha"] == 0.6


def test_speed_keep_and_timeline():
    keep, speeds, bad = sbb.speed_keep(
        [[0, 3.3], [4.7, 11.0], [11.0, 21.0], [21.0, 40.0]],
        [{"range": [11.0, 21.0], "speed": 5.0}, {"range": [50, 60], "speed": 2}],
    )
    assert speeds == [1.0, 1.0, 5.0, 1.0] and len(bad) == 1
    keep2, speeds2, _ = sbb.speed_keep(
        [[0, 30]], [{"range": [10, 20], "speed": 4}]
    )  # 区间在 keep 里面: 拆开
    assert keep2 == [(0, 10), (10, 20), (20, 30)] and speeds2 == [1.0, 4.0, 1.0]
    tl = sbb.Timeline(keep, speeds)
    assert tl.total == pytest.approx(3.3 + 6.3 + 2.0 + 19.0)
    assert tl.to_out(16.0) == pytest.approx(9.6 + 1.0)
    assert tl.to_out(22.0) == pytest.approx(11.6 + 1.0)
    s, d = tl.span(20.0, 2.0)  # 跨变速边界: 0.2s (加速段) + 1s
    assert s == pytest.approx(11.4) and d == pytest.approx(1.2)
    assert sbb.Timeline([(0, 37.55), (38.05, 86.4)]).to_out(40) == pytest.approx(39.5)


@needs_ffmpeg
def test_retime_ripples_in_frames(tmp_path):
    from jy_wrapper import JyProject

    video = os.path.join(ROOT, "assets", "video.mp4")
    p = JyProject("Retime", drafts_root=str(tmp_path), overwrite=True)
    a = p.add_media_safe(video, 0, 3_000_000, "Main")
    b = p.add_media_safe(video, 3_000_000, 1_000_000, "Main", source_start=500_000)
    c = p.add_media_safe(video, 4_000_000, 1_000_000, "Main", source_start=1_500_000)
    p.set_speed(a, 3.1)  # 3s / 3.1 = 0.9677s -> 29 帧 = 966666us
    assert a.target_timerange.duration == frames_to_us(29)
    for x, y in ((a, b), (b, c)):  # 后面的片段按帧平移, 仍然首尾相接
        assert x.target_timerange.start + x.target_timerange.duration == y.target_timerange.start
    for s in (a, b, c):
        for v in (s.target_timerange.start, s.target_timerange.start + s.target_timerange.duration):
            assert snap_us(v) == v
    assert c.target_timerange.start == frames_to_us(59)  # 120 帧 - 61 帧
    assert c.target_timerange.duration == frames_to_us(89) - frames_to_us(59)
