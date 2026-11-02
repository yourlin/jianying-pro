"""口播精剪 (scripts/speech_cleanup.py) 测试: 合成词级转写, 不依赖剪映 / whisper。"""

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "vendor"))

import pytest  # noqa: E402
import speech_cleanup as sc  # noqa: E402
from speech_cleanup import plan_cleanup, remap_time  # noqa: E402

VIDEO = os.path.join(ROOT, "assets", "video.mp4")


class Talk:
    """按「字 + 停顿」拼一段合成口播 (每个字一个 word, 和 faster-whisper 中文输出一致)。"""

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

    def end_seg(self):
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
        self.end_seg()
        return self.segs


def kept_text(plan):
    return "".join(s["text"] for s in plan["transcript"])


def reasons(plan):
    return [it["reason"] for c in plan["cuts"] for it in c["items"]]


# ---------------------------------------------------------------- 口癖


def test_pure_filler_removed_and_pause_kept_short():
    tr = Talk().say("大家好").say("嗯", 0.3).say("今天讲剪辑", 0.4).transcript()
    plan = plan_cleanup(tr)
    assert kept_text(plan) == "大家好今天讲剪辑"
    cut = next(c for c in plan["cuts"] if c["reason"] == "filler")
    # 嗯 前后共 0.7s 停顿 + 0.18s 嗯 → 只留 max_pause 的气口
    kept_gap = plan["keep"][1]["start"] - plan["keep"][0]["end"]
    assert kept_gap == pytest.approx(cut["duration"])
    pause_left = (plan["keep"][0]["end"] - 0.84) + (1.72 - plan["keep"][1]["start"])
    assert pause_left == pytest.approx(0.25, abs=0.01)


def test_demonstrative_nage_is_kept_but_hesitation_nage_removed():
    tr = Talk().say("点击那个按钮。").say("那个", 0.2).say("我们来看下一步", 0.5).transcript()
    plan = plan_cleanup(tr)
    assert kept_text(plan) == "点击那个按钮我们来看下一步"
    items = [it for c in plan["cuts"] for it in c["items"]]
    assert [it["text"] for it in items if it["reason"] == "filler"] == ["那个"]
    assert items[0]["start"] > 1.0  # 删的是第二个「那个」


def test_repeated_nage_keeps_last_and_prolonged_nage_removed():
    tr = Talk().say("打开那个那个设置").transcript()
    assert kept_text(plan_cleanup(tr)) == "打开那个设置"
    tr = Talk().say("我们").say("那个", per=0.45).say("看一下").transcript()
    assert kept_text(plan_cleanup(tr)) == "我们看一下"


def test_sentence_final_particle_kept_initial_a_removed():
    tr = Talk().say("这个效果很好啊。").say("啊", 0.6).say("我们继续", 0.3).transcript()
    plan = plan_cleanup(tr)
    assert kept_text(plan) == "这个效果很好啊我们继续"
    assert reasons(plan) == ["filler"]


def test_keep_prefix_protects_yejiushishuo():
    tr = Talk().say("也就是说，").say("这一步最重要", 0.3).transcript()
    assert kept_text(plan_cleanup(tr)) == "也就是说这一步最重要"
    tr = Talk().say("就是说，").say("这一步最重要", 0.3).transcript()
    assert kept_text(plan_cleanup(tr)) == "这一步最重要"


def test_custom_filler_table_and_disable():
    tr = Talk().say("对吧").say("我们开始", 0.4).transcript()
    assert kept_text(plan_cleanup(tr)) == "对吧我们开始"
    assert kept_text(plan_cleanup(tr, extra_fillers={"对吧": "pause"})) == "我们开始"
    tr = Talk().say("嗯").say("开始", 0.3).transcript()
    assert kept_text(plan_cleanup(tr, remove_fillers=False)) == "嗯开始"
    with pytest.raises(ValueError):
        plan_cleanup(tr, extra_fillers={"嗯": "sometimes"})


def test_english_fillers():
    words = [
        {"w": " So", "s": 0.0, "e": 0.2},
        {"w": " um", "s": 0.5, "e": 0.8},
        {"w": " today", "s": 1.1, "e": 1.4},
        {"w": " we", "s": 1.4, "e": 1.5},
        {"w": " talk", "s": 1.5, "e": 1.8},
    ]
    plan = plan_cleanup([{"start": 0, "end": 1.8, "text": "", "words": words}])
    assert plan["transcript"][0]["text"] == "today we talk"


# ---------------------------------------------------------------- 结巴 / 重来 / 重复


def test_stutter_keeps_last_copy_but_not_reduplication():
    tr = Talk().say("我我我觉得可以").transcript()
    assert kept_text(plan_cleanup(tr)) == "我觉得可以"
    tr = Talk().say("我们，").say("我们来看看", 0.25).transcript()
    assert kept_text(plan_cleanup(tr)) == "我们来看看"  # 「看看」连读不算结巴
    tr = Talk().say("谢谢大家").transcript()
    assert kept_text(plan_cleanup(tr)) == "谢谢大家"


def test_ng_marker_removes_botched_take():
    tr = (
        Talk()
        .say("大家好，")
        .say("今天我们来讲剪辑的，", 0.2)
        .say("不对，", 0.4)
        .say("今天我们来讲调色。", 0.5)
        .transcript()
    )
    plan = plan_cleanup(tr)
    assert kept_text(plan) == "大家好今天我们来讲调色"
    ng = [it for c in plan["cuts"] for it in c["items"] if it["reason"] == "ng"]
    assert ng and ng[0]["text"] == "今天我们来讲剪辑的不对"


def test_ng_marker_mid_clause_restart():
    # 子句切分不准 (前面没停顿) 时, 只删从重说开头那一位置起的内容
    tr = (
        Talk()
        .say("大家好今天我们讲剪辑，")
        .say("重来，", 0.4)
        .say("今天我们讲调色", 0.4)
        .transcript()
    )
    assert kept_text(plan_cleanup(tr)) == "大家好今天我们讲调色"


def test_strong_marker_without_match_drops_previous_clause():
    tr = Talk().say("开场白说得不好，").say("重来，", 0.4).say("欢迎来到频道", 0.4).transcript()
    assert kept_text(plan_cleanup(tr)) == "欢迎来到频道"


def test_weak_marker_in_normal_speech_is_kept():
    tr = Talk().say("这样做对吗？").say("不对。", 0.3).say("因为光线太暗", 0.3).transcript()
    plan = plan_cleanup(tr)
    assert kept_text(plan) == "这样做对吗不对因为光线太暗"
    tr = Talk().say("这样做是不对的").transcript()
    assert kept_text(plan_cleanup(tr)) == "这样做是不对的"


def test_repeated_sentence_keeps_last_take():
    tr = Talk().say("这个功能非常好用，").say("这个功能非常的好用。", 0.6).transcript()
    plan = plan_cleanup(tr)
    assert kept_text(plan) == "这个功能非常的好用"
    assert "repeat" in reasons(plan)
    tr = Talk().say("今天我们，").say("今天我们来聊聊构图", 0.4).transcript()
    assert kept_text(plan_cleanup(tr)) == "今天我们来聊聊构图"


def test_parallel_sentences_are_not_repeats():
    tr = Talk().say("第一步打开软件，").say("第二步打开设置。", 0.3).transcript()
    assert kept_text(plan_cleanup(tr)) == "第一步打开软件第二步打开设置"
    tr = Talk().say("这个很好用，").say("那个也很便宜，", 0.3).transcript()
    assert kept_text(plan_cleanup(tr)) == "这个很好用那个也很便宜"
    tr = Talk().say("这个功能好用，").say("这个功能好用。", 0.3).transcript()
    assert kept_text(plan_cleanup(tr, remove_repeats=False)) == "这个功能好用这个功能好用"


# ---------------------------------------------------------------- 气口 / 区间


def test_long_pauses_compressed_short_pauses_kept():
    tr = Talk().say("第一句话").say("第二句话", 1.5).say("第三句话", 0.2).transcript()
    plan = plan_cleanup(tr, max_pause=0.3, pad=0.05)
    assert len(plan["keep"]) == 2  # 0.2s 的停顿不动
    gap = plan["cuts"][1]
    assert gap["reason"] == "pause"
    assert gap["duration"] == pytest.approx(1.5 - 0.3, abs=0.01)
    plan = plan_cleanup(tr, compress_pauses=False)
    assert [c["reason"] for c in plan["cuts"]] == ["edge"]


def test_keep_timeline_and_remap():
    tr = Talk().say("开始").say("嗯", 0.5).say("正文内容", 0.5).say("结束", 2.0).transcript()
    plan = plan_cleanup(tr, duration=8.0)
    keep = plan["keep"]
    t = 0.0
    for k in keep:
        assert k["target_start"] == pytest.approx(t, abs=1e-3)
        assert k["end"] > k["start"]
        t += k["duration"]
    # 保留区间和剪掉区间正好拼满原视频
    spans = sorted(
        [(k["start"], k["end"]) for k in keep] + [(c["start"], c["end"]) for c in plan["cuts"]]
    )
    assert spans[0][0] == 0.0 and spans[-1][1] == pytest.approx(8.0)
    for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
        assert a1 == pytest.approx(b0, abs=1e-3)
    assert plan["stats"]["kept"] == pytest.approx(t, abs=2e-3)
    assert plan["stats"]["kept"] + plan["stats"]["removed"] == pytest.approx(8.0)
    # 时间映射
    assert remap_time(plan, keep[0]["start"]) == 0.0
    mid = (keep[1]["start"] + keep[1]["end"]) / 2
    assert remap_time(plan, mid) == pytest.approx(keep[1]["target_start"] + mid - keep[1]["start"])
    嗯 = next(it for c in plan["cuts"] for it in c["items"])
    assert remap_time(plan, (嗯["start"] + 嗯["end"]) / 2) is None
    # 精剪后的转写: 词时间单调且落在新时间线内
    ws = [w for s in plan["transcript"] for w in s["words"]]
    assert [w["w"] for w in ws] == list("开始正文内容结束")
    assert all(a["s"] <= b["s"] for a, b in zip(ws, ws[1:]))
    assert ws[-1]["e"] <= t + 1e-3


def test_pad_protects_word_edges():
    tr = Talk().say("前面").say("后面", 1.0).transcript()
    plan = plan_cleanup(tr, max_pause=0.1, pad=0.05)
    w = tr[0]["words"]
    assert plan["keep"][0]["end"] >= w[1]["e"] + 0.05 - 1e-3
    assert plan["keep"][1]["start"] <= w[2]["s"] - 0.05 + 1e-3


def test_silences_trim_stretched_word():
    # whisper 把「好」拖进了后面 1s 的静音; ffmpeg 静音段把它修正回来
    words = [
        {"w": "你", "s": 0.0, "e": 0.2},
        {"w": "好", "s": 0.2, "e": 1.4},
        {"w": "朋", "s": 1.6, "e": 1.8},
        {"w": "友", "s": 1.8, "e": 2.0},
    ]
    analysis = {
        "source": "/tmp/x.mp4",
        "video": {"duration": 2.2},
        "silences": [{"start": 0.45, "end": 1.55, "duration": 1.1}],
        "transcript": [{"start": 0, "end": 2.0, "text": "你好朋友", "words": words}],
    }
    plan = plan_cleanup(analysis)
    assert plan["source"] == "/tmp/x.mp4" and plan["duration"] == 2.2
    assert len(plan["keep"]) == 2
    assert plan["keep"][0]["end"] < 0.75
    assert plan_cleanup(analysis, silences=[])["stats"]["pieces"] == 1


def test_input_formats_and_warnings():
    whisper_style = {
        "segments": [
            {
                "start": 0,
                "end": 1,
                "text": "嗯好",
                "words": [
                    {"word": "嗯", "start": 0, "end": 0.3},
                    {"word": "好", "start": 0.6, "end": 0.8},
                ],
            }
        ]
    }
    assert kept_text(plan_cleanup(whisper_style)) == "好"
    no_words = [{"start": 0.0, "end": 1.0, "text": "大家好"}]
    plan = plan_cleanup(no_words)
    assert plan["warnings"] and kept_text(plan) == "大家好"
    with pytest.raises(ValueError):
        plan_cleanup({"video": {"duration": 3}, "transcript": None})
    with pytest.raises(TypeError):
        plan_cleanup(no_words, bogus=1)


# ---------------------------------------------------------------- CLI / 草稿


def _analysis_file(tmp_path, video=None):
    tr = Talk().say("大家好").say("嗯", 0.3).say("今天讲剪辑", 0.4).transcript()
    data = {"source": video, "video": {"duration": 3.5}, "silences": [], "transcript": tr}
    p = tmp_path / "analysis.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_cli_json_output(tmp_path, capsys):
    p = _analysis_file(tmp_path)
    out = tmp_path / "plan.json"
    assert sc._main([str(p), "-o", str(out), "--json", "--max-pause", "0.2"]) == 0
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert res["ok"] and res["code"] == "ok"
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert plan["settings"]["max_pause"] == 0.2
    assert "filler" in res["data"]["stats"]["by_reason"]
    # 自定义口癖表 + 关闭检测
    assert sc._main([str(p), "-o", str(out), "--fillers", "讲:always", "--json"]) == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert kept_text(plan) == "大家好嗯今天剪辑"


def test_cli_errors(tmp_path, capsys):
    assert sc._main([str(tmp_path / "missing.json"), "--json"]) == 1
    res = json.loads(capsys.readouterr().out.strip())
    assert not res["ok"] and res["code"] == "not_found"
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert sc._main([str(bad), "--json"]) == 1
    assert json.loads(capsys.readouterr().out.strip())["code"] == "invalid_input"
    p = _analysis_file(tmp_path)
    assert sc._main([str(p), "--draft", "X", "--json"]) == 1  # 没有源视频


@pytest.fixture()
def drafts_root():
    root = tempfile.mkdtemp(prefix="jy_speech_")
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="需要 ffprobe")
def test_build_draft_from_plan(drafts_root, tmp_path):
    p = _analysis_file(tmp_path, VIDEO)
    plan = plan_cleanup(json.loads(p.read_text(encoding="utf-8")))
    res = sc.build_cleanup_draft(plan, VIDEO, "SpeechCut", drafts_root=drafts_root, punch_in=1.25)
    assert res["segments"] == len(plan["keep"]) == 2
    with open(os.path.join(drafts_root, "SpeechCut", "draft_info.json"), encoding="utf-8") as f:
        content = json.load(f)
    segs = [s for t in content["tracks"] if t["type"] == "video" for s in t["segments"]]
    assert len(segs) == 2
    for seg, k in zip(segs, plan["keep"]):
        # 写入时对齐到整帧 (30fps): 起点差不超过半帧, 时长差不超过 1 帧
        assert abs(seg["source_timerange"]["start"] - round(k["start"] * 1e6)) <= 1e6 / 30 / 2 + 1
        assert abs(seg["source_timerange"]["duration"] - round(k["duration"] * 1e6)) <= 1e6 / 30 + 1
    # 首尾相接, 第二段做了假景别放大
    assert segs[1]["target_timerange"]["start"] == (
        segs[0]["target_timerange"]["start"] + segs[0]["target_timerange"]["duration"]
    )
    assert segs[0]["clip"]["scale"]["x"] == 1.0 and segs[1]["clip"]["scale"]["x"] == 1.25


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="需要 ffprobe")
def test_mixin_plans_on_the_fly(drafts_root):
    from core.speech_cut_ops import SpeechCutOpsMixin
    from jy_wrapper import JyProject

    class P(JyProject, SpeechCutOpsMixin):
        pass

    proj = P("SpeechMixin", drafts_root=drafts_root, overwrite=True)
    tr = Talk().say("你好").say("嗯", 0.3).say("世界", 0.3).transcript()
    segs = proj.add_speech_cleanup(VIDEO, tr, target_start="1s", max_pause=0.2)
    assert len(segs) == 2
    assert segs[0].target_timerange.start == 1_000_000
    with pytest.raises(ValueError):
        proj.add_speech_cleanup(VIDEO, tr, punch_in=0.5)


def test_stats_attribute_every_removed_second():
    tr = (
        Talk()
        .say("大家好")
        .say("嗯", 0.3)
        .say("今天我们来讲剪辑的，", 0.4)
        .say("不对，", 0.4)
        .say("今天我们来讲调色。", 0.5)
        .transcript()
    )
    plan = plan_cleanup(tr, duration=8.0)
    br = plan["stats"]["by_reason"]
    assert sum(v["seconds"] for v in br.values()) == pytest.approx(
        plan["stats"]["removed"], abs=0.01
    )
    assert br["filler"]["count"] == 1 and br["ng"]["count"] == 1
    assert {"filler", "ng"} <= {r for c in plan["cuts"] for r in c["reasons"]}
