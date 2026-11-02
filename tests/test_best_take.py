"""最佳 take 挑选 (scripts/best_take.py) 测试: 合成词级转写 + 合成视频, 不依赖剪映 / whisper。"""

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "vendor"))

import best_take as bt  # noqa: E402
import pytest  # noqa: E402

VIDEO = os.path.join(ROOT, "assets", "video.mp4")


class Talk:
    """按「字 + 停顿」拼合成口播, 每个字一个 word; end() 结束一个转写分句。"""

    def __init__(self, t0=0.3, per=0.18):
        self.t = t0
        self.per = per
        self.segs = []
        self.words = []

    def say(self, text, pause_before=0.0, per=None):
        self.t += pause_before
        for c in text:
            if c in "，。？！,.?!":
                self.words.append({"w": c, "s": round(self.t, 3), "e": round(self.t, 3)})
                continue
            d = per or self.per
            self.words.append({"w": c, "s": round(self.t, 3), "e": round(self.t + d, 3)})
            self.t += d
        return self

    def end(self):
        if self.words:
            w = self.words
            self.segs.append(
                {
                    "start": w[0]["s"],
                    "end": w[-1]["e"],
                    "text": "".join(x["w"] for x in w),
                    "words": w,
                }
            )
        self.words = []
        return self

    def transcript(self):
        self.end()
        return self.segs


def plan_text(plan):
    return "".join(s["text"] for s in plan["transcript"])


def three_takes():
    return (
        Talk()
        .say("大家好。")
        .end()
        .say("今天我们讲讲", 0.8)
        .say("嗯", 0.3)
        .say("剪辑的三个技巧。", 0.5)
        .end()
        .say("今天我们", 1.0)
        .end()
        .say("今天我们讲讲剪辑的三个技巧。", 1.0)
        .end()
        .say("第一个是节奏。", 0.8)
        .transcript()
    )


def test_picks_clean_complete_take_and_plan_drops_others():
    sel = bt.select_takes(three_takes(), visual=False)
    assert len(sel["groups"]) == 1
    g = sel["groups"][0]
    assert len(g["takes"]) == 3 and g["best"] == 3
    t1, t2, t3 = g["takes"]
    assert t1["metrics"]["fillers"] == 1 and "1 个口癖" in t1["notes"]
    assert t2["metrics"]["coverage"] < 0.5 and any("没说完" in n for n in t2["notes"])
    assert t3["chosen"] and not t1["chosen"]
    assert "选第3遍" in g["reason"] and "口癖" in g["reason"]
    plan = sel["plan"]
    assert plan_text(plan) == "大家好今天我们讲讲剪辑的三个技巧第一个是节奏"
    br = plan["stats"]["by_reason"]
    assert br["take"]["count"] == 2
    assert sum(v["seconds"] for v in br.values()) == pytest.approx(
        plan["stats"]["removed"], abs=0.01
    )
    # 落选 take 不在 keep 里, 最佳 take 在
    for r in sel["rejected"]:
        assert not any(k["start"] < r["end"] and k["end"] > r["start"] for k in plan["keep"])
    assert any(k["start"] <= t3["start"] and k["end"] >= t3["end"] for k in plan["keep"])
    assert {"take"} <= {r for c in plan["cuts"] for r in c["reasons"]}


def test_earlier_take_wins_when_later_is_worse():
    tr = (
        Talk()
        .say("这个功能可以一键导出视频。")
        .end()
        .say("这个功能", 1.2)
        .say("嗯", 0.4)
        .say("可以", 0.4)
        .say("呃", 0.4)
        .say("一键", 0.2)
        .say("导出视频。", 0.5)
        .end()
        .say("下一个话题。", 1.0)
        .transcript()
    )
    sel = bt.select_takes(tr, visual=False)
    g = sel["groups"][0]
    assert g["best"] == 1
    assert g["takes"][1]["metrics"]["fillers"] == 2
    assert g["takes"][1]["metrics"]["hesitations"] + g["takes"][1]["metrics"]["fillers"] >= 2
    assert plan_text(sel["plan"]) == "这个功能可以一键导出视频下一个话题"


def test_recency_breaks_ties():
    tr = Talk().say("这是一句话。").end().say("这是一句话。", 1.0).transcript()
    sel = bt.select_takes(tr, visual=False)
    assert sel["groups"][0]["best"] == 2
    sel = bt.select_takes(tr, visual=False, recency=0.0)
    assert sel["groups"][0]["best"] == 2  # 完全相同时也取后一遍 (平局按顺序)


def test_parallel_numbered_sentences_are_not_takes():
    tr = (
        Talk()
        .say("第一步打开软件。")
        .end()
        .say("第二步打开软件。", 0.8)
        .end()
        .say("我们来说说剪辑。", 0.8)
        .end()
        .say("我们来说说调色。", 0.8)
        .transcript()
    )
    sel = bt.select_takes(tr, visual=False)
    assert sel["groups"] == [] and sel["rejected"] == []
    assert plan_text(sel["plan"]) == "第一步打开软件第二步打开软件我们来说说剪辑我们来说说调色"


def test_script_prefers_on_script_take_and_reports_missing():
    script = "大家好我是小林。\n今天讲剪辑的三个技巧。\n记得点赞关注。"
    tr = (
        Talk()
        .say("大家好我是小林。")
        .end()
        .say("今天讲剪辑的四个小技巧。", 1.0)
        .end()
        .say("今天讲剪辑的三个技巧。", 1.0)
        .end()
        .say("今天讲讲剪辑的几个技巧。", 1.0)
        .transcript()
    )
    sel = bt.select_takes(tr, script, visual=False)
    g = next(g for g in sel["groups"] if g["target_index"] == 1)
    assert len(g["takes"]) == 3 and g["best"] == 2
    assert g["takes"][1]["scores"]["script"] == 100.0
    assert g["takes"][0]["scores"]["script"] < 100
    assert [m["index"] for m in sel["missing"]] == [2]
    assert [s["target_index"] for s in sel["sequence"]] == [0, 1]
    assert plan_text(sel["plan"]) == "大家好我是小林今天讲剪辑的三个技巧"


def test_ng_marker_is_cut_and_marks_take_aborted():
    tr = (
        Talk()
        .say("我们先看第一个案例，")
        .say("重来。", 0.4)
        .end()
        .say("我们先看第一个案例。", 0.8)
        .end()
        .say("这样对吗？", 0.8)
        .end()
        .say("不对。", 0.4)
        .transcript()
    )
    sel = bt.select_takes(tr, visual=False)
    g = sel["groups"][0]
    assert g["best"] == 2 and g["takes"][0]["aborted"] == "重来"
    kinds = sorted(r["kind"] for r in sel["rejected"])
    assert kinds == ["ng_marker", "take"]
    # 问句后的「不对」是正常回答, 不剪
    assert plan_text(sel["plan"]) == "我们先看第一个案例这样对吗不对"


def test_load_script_formats(tmp_path):
    assert bt.load_script("第一句。第二句！\n第三句") == ["第一句。", "第二句！", "第三句"]
    assert bt.load_script(["a b c", "第二句"]) == ["a b c", "第二句"]
    assert bt.load_script({"lines": ["一", "二"]}) == ["一", "二"]
    sj = {"beats": [{"id": "S1", "line": "开场白。"}, {"id": "S2", "say": "正文。"}, {"id": "S3"}]}
    assert bt.load_script(sj) == ["开场白。", "正文。"]
    p = tmp_path / "s.json"
    p.write_text(json.dumps(sj, ensure_ascii=False), encoding="utf-8")
    assert bt.read_script(str(p)) == ["开场白。", "正文。"]


def test_weights_and_unknown_args():
    tr = three_takes()
    with pytest.raises(TypeError):
        bt.select_takes(tr, visual=False, bogus=1)
    with pytest.raises(ValueError):
        bt.select_takes(tr, visual=False, weights={"bogus": 1})
    with pytest.raises(ValueError):
        bt.select_takes([], visual=False)
    sel = bt.select_takes(tr, visual=False, weights={"complete": 0})
    assert sel["settings"]["weights"]["complete"] == 0


def test_no_cleanup_keeps_fillers_in_best_take():
    tr = (
        Talk()
        .say("今天讲剪辑")
        .end()
        .say("今天", 1.0)
        .say("嗯", 0.3)
        .say("讲剪辑技巧。", 0.3)
        .transcript()
    )
    sel = bt.select_takes(tr, visual=False, cleanup=False)
    assert sel["groups"][0]["best"] == 2
    assert "嗯" in plan_text(sel["plan"])
    sel = bt.select_takes(tr, visual=False)
    assert "嗯" not in plan_text(sel["plan"])


def test_multi_source_groups_across_files():
    a = Talk().say("欢迎来到我的频道。").end().say("今天讲调色。", 0.8).transcript()
    b = (
        Talk()
        .say("欢迎", 0.2)
        .say("嗯", 0.4)
        .say("来到", 0.4)
        .say("呃", 0.4)
        .say("我的频道。", 0.3)
        .transcript()
    )
    srcs = [
        {"analysis": {"transcript": b, "source": "/tmp/camB.mp4"}},
        {"analysis": {"transcript": a, "source": "/tmp/camA.mp4"}},
    ]
    sel = bt.select_takes(srcs, visual=False)
    assert sel["plan"] is None and set(sel["plans"]) == {"/tmp/camA.mp4", "/tmp/camB.mp4"}
    g = sel["groups"][0]
    assert g["takes"][g["best"] - 1]["source"] == "/tmp/camA.mp4"
    assert [s["source"] for s in sel["sequence"]] == ["/tmp/camA.mp4", "/tmp/camA.mp4"]
    assert sel["plans"]["/tmp/camB.mp4"]["keep"] == []


# ---------------------------------------------------------------- 画面打分


def _write_video(path, parts, w=480, h=270, fps=25):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(0)
    tex = (rng.random((h, w)) * 255).astype(np.uint8)
    tex = cv2.cvtColor(cv2.GaussianBlur(tex, (0, 0), 1.0), cv2.COLOR_GRAY2BGR)
    vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    if not vw.isOpened():
        pytest.skip("cv2.VideoWriter 不可用")
    for dur, kind in parts:
        fr = tex if kind == "sharp" else cv2.GaussianBlur(tex, (0, 0), 7)
        for _ in range(int(dur * fps)):
            vw.write(np.ascontiguousarray(fr))
    vw.release()
    return path


def test_visual_quality_decides_between_equal_takes(tmp_path):
    video = _write_video(str(tmp_path / "v.mp4"), [(4, "sharp"), (4, "blur")])
    tr = (
        Talk(t0=0.5)
        .say("这一段讲的是构图。")
        .end()
        .say("这一段讲的是构图。", 2.4)  # 第二遍落在模糊段 (4.3s 起)
        .transcript()
    )
    assert tr[1]["start"] > 4.0
    sel = bt.select_takes({"transcript": tr, "source": video, "video": {"duration": 8.0}})
    g = sel["groups"][0]
    assert sel["settings"]["visual"] is True
    sharp, blur = g["takes"]
    assert sharp["scores"]["visual"] > blur["scores"]["visual"]
    assert g["best"] == 1, g["reason"]
    assert any("画面" in n for n in blur["notes"])
    # 不做画面打分时同分, 取后一遍
    assert bt.select_takes(tr, visual=False)["groups"][0]["best"] == 2


def test_missing_video_warns():
    tr = Talk().say("同一句话。").end().say("同一句话。", 1.0).transcript()
    sel = bt.select_takes({"transcript": tr, "source": "/nonexistent/x.mp4"})
    assert any("找不到源视频" in w for w in sel["warnings"])
    assert sel["groups"][0]["takes"][0]["scores"]["visual"] is None


# ---------------------------------------------------------------- CLI / 草稿


def _analysis_file(tmp_path, source=None):
    data = {"transcript": three_takes(), "silences": [], "video": {"duration": 13.0}}
    if source:
        data["source"] = source
    p = tmp_path / "analysis.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_cli_json(tmp_path, capsys):
    p = _analysis_file(tmp_path)
    script = tmp_path / "s.txt"
    script.write_text("大家好。\n今天我们讲讲剪辑的三个技巧。\n第一个是节奏。", encoding="utf-8")
    assert bt._main([str(p), "--script", str(script), "--no-visual", "--json"]) == 0
    res = json.loads(capsys.readouterr().out.strip())
    assert res["ok"] and res["code"] == "ok"
    data = res["data"]
    assert data["stats"]["rejected_takes"] == 2 and data["missing"] == []
    sel = json.loads(open(data["takes_path"], encoding="utf-8").read())
    plan = json.loads(open(data["plan_path"], encoding="utf-8").read())
    assert plan_text(plan) == "大家好今天我们讲讲剪辑的三个技巧第一个是节奏"
    assert plan["keep"] == sel["plan"]["keep"]
    # 文本输出
    assert bt._main([str(p), "--no-visual", "-o", str(tmp_path / "t.json")]) == 0
    assert "选第3遍" in capsys.readouterr().out


def test_cli_errors(tmp_path, capsys):
    assert bt._main([str(tmp_path / "missing.json"), "--json"]) == 1
    assert json.loads(capsys.readouterr().out.strip())["code"] == "not_found"
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert bt._main([str(bad), "--json"]) == 1
    assert json.loads(capsys.readouterr().out.strip())["code"] == "invalid_input"
    p = _analysis_file(tmp_path)
    assert bt._main([str(p), str(p), "--video", "x.mp4", "--json"]) == 1
    assert json.loads(capsys.readouterr().out.strip())["code"] == "invalid_input"
    assert bt._main([str(p), "--no-visual", "--draft", "X", "--json"]) == 1  # 没有源视频
    assert json.loads(capsys.readouterr().out.strip())["code"] == "not_found"


@pytest.fixture()
def drafts_root():
    root = tempfile.mkdtemp(prefix="jy_take_")
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _short_takes():
    # assets/video.mp4 只有 5s
    return (
        Talk(t0=0.2, per=0.12)
        .say("你好世界。")
        .end()
        .say("你好", 0.5)
        .say("嗯", 0.3)
        .say("世界。", 0.3)
        .end()
        .say("再见。", 0.5)
        .transcript()
    )


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="需要 ffprobe")
def test_build_draft_from_plan_and_sequence(drafts_root):
    an = {"transcript": _short_takes(), "source": VIDEO, "video": {"duration": 5.0}}
    sel = bt.select_takes(an, visual=False)
    assert sel["groups"][0]["best"] == 1
    res = bt.build_best_take_draft(sel, "BestTake", drafts_root=drafts_root)
    assert res["segments"] == len(sel["plan"]["keep"])
    with open(os.path.join(drafts_root, "BestTake", "draft_info.json"), encoding="utf-8") as f:
        content = json.load(f)
    segs = [s for t in content["tracks"] if t["type"] == "video" for s in t["segments"]]
    for seg, k in zip(segs, sel["plan"]["keep"]):
        # 写入时对齐到整帧 (30fps), 和请求值相差不超过半帧
        assert abs(seg["source_timerange"]["start"] - round(k["start"] * 1e6)) <= 1e6 / 30 / 2 + 1

    res = bt.build_best_take_draft(sel, "BestTakeSeq", drafts_root=drafts_root, use_plan=False)
    assert res["segments"] == len(sel["sequence"]) == 2


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="需要 ffprobe")
def test_mixin(drafts_root):
    from core.take_ops import BestTakeOpsMixin
    from jy_wrapper import JyProject

    class P(JyProject, BestTakeOpsMixin):
        pass

    proj = P("TakeMixin", drafts_root=drafts_root, overwrite=True)
    an = {"transcript": _short_takes(), "source": VIDEO, "video": {"duration": 5.0}}
    sel = bt.select_takes(an, visual=False)
    segs = proj.add_best_takes(sel, use_plan=False, punch_in=1.2)
    assert len(segs) == 2
    assert segs[1].clip_settings.scale_x == 1.2
    with pytest.raises(ValueError):
        proj.add_best_takes(sel, punch_in=0.5)
