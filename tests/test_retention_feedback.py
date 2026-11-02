import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import retention_feedback as rf  # noqa: E402


def synth_curve(duration=30, drops=((1.5, 15), (12.0, 12)), decay=0.6):
    """合成留存曲线: 正常每秒掉 decay pp, 在给定时刻额外陡降 (时刻, pp, 1s 内掉完)。"""
    t = np.arange(0, duration + 0.01, 0.5)
    r = 100 - decay * t
    for at, pp in drops:
        r -= pp * np.clip(t - at, 0, 1)
    return t, r


def write_csv(path, header, rows, encoding="utf-8"):
    with open(path, "w", encoding=encoding, newline="") as f:
        f.write(",".join(header) + "\n")
        for row in rows:
            f.write(",".join(str(x) for x in row) + "\n")


def fake_report(duration=30):
    """最小的 quality_review 报告: 12s 前后是一个 7s 死区, 开头有寒暄。"""
    return {
        "tool": "quality_review",
        "source": {"duration": duration},
        "genre": "口播知识",
        "pacing": {
            "target_shot_s": [3.0, 6.0],
            "shots": [
                {"index": 1, "start": 0.0, "end": 6.0, "dur": 6.0},
                {"index": 2, "start": 6.0, "end": 20.0, "dur": 14.0},
                {"index": 3, "start": 20.0, "end": 30.0, "dur": 10.0},
            ],
            "curve": [{"t": float(i), "energy": 0.1 if 6 <= i < 13 else 0.6} for i in range(30)],
        },
        "dead_zones": [{"start": 6.0, "end": 13.0, "dur": 7.0, "speech": 0.9}],
        "issues": [
            {"id": "Q001", "type": "寒暄式开场", "severity": "medium", "start": 0, "end": 5,
             "category": "hook"},
            {"id": "Q002", "type": "死区", "severity": "medium", "start": 6.0, "end": 13.0,
             "category": "dead_zone"},
        ],  # fmt: skip
        "timeline": {
            "cuts": [6.0, 20.0],
            "texts": [
                {"start": 0, "end": 3, "text": "大家好", "track": "Sub"},
                {"start": 9, "end": 14, "text": "这个方法其实很简单", "track": "Sub"},
            ],
            "speech_text": [{"start": 9.0, "text": "这个方法其实很简单"}],
        },
    }


def test_load_curve_douyin_percent_strings_and_mmss(tmp_path):
    t, r = synth_curve()
    p = tmp_path / "douyin.csv"
    rows = [(f"{int(x // 60):02d}:{x % 60:04.1f}", f"{v:.2f}%") for x, v in zip(t, r)]
    write_csv(p, ["时间点", "观众留存率"], rows)
    c = rf.load_curve(str(p))
    assert c["time_col"] == "时间点" and c["value_col"] == "观众留存率"
    assert c["t"][3] == pytest.approx(1.5) and c["r"][0] == pytest.approx(100)


def test_load_curve_gbk_fraction_and_counts(tmp_path):
    t, r = synth_curve()
    p = tmp_path / "gbk.csv"
    write_csv(p, ["播放时长(秒)", "留存率"], [(x, round(v / 100, 4)) for x, v in zip(t, r)], "gbk")
    c = rf.load_curve(str(p))
    assert max(c["r"]) == pytest.approx(100, abs=0.01)  # 0.x 小数 -> 百分比
    p2 = tmp_path / "wx.csv"  # 视频号: 人数
    write_csv(p2, ["时间", "观看人数"], [(f"{x}秒", int(v * 37)) for x, v in zip(t, r)])
    c2 = rf.load_curve(str(p2))
    assert c2["kind"] == "count" and c2["r"][0] == pytest.approx(100)


def test_load_curve_progress_percent_needs_duration(tmp_path):
    p = tmp_path / "yt.csv"
    write_csv(p, ["Video position (%)", "Audience retention (%)"],
              [(f"{x}%", 100 - x / 2) for x in range(0, 101, 10)])  # fmt: skip
    with pytest.raises(rf.InputError):
        rf.load_curve(str(p))
    c = rf.load_curve(str(p), duration=60)
    assert c["t"][-1] == pytest.approx(60)


def test_load_curve_without_header_and_tab(tmp_path):
    p = tmp_path / "raw.tsv"
    p.write_text("0\t100\n1\t95\n2\t91\n3\t88\n", encoding="utf-8")
    c = rf.load_curve(str(p))
    assert c["t"] == [0, 1, 2, 3] and c["r"][-1] == 88


def test_find_drops_locates_synthetic_cliffs():
    t, r = synth_curve()
    drops, base, thr = rf.find_drops(t, r)
    assert base == pytest.approx(0.6, abs=0.05)
    assert len(drops) == 2
    assert drops[0]["peak"] == pytest.approx(2.0, abs=0.6) and drops[0]["drop_pp"] == pytest.approx(
        15, abs=2.5
    )
    assert drops[1]["peak"] == pytest.approx(12.5, abs=0.6)
    m = rf.curve_metrics(t, r)
    assert m["retention_3s"] == pytest.approx(100 - 1.8 - 15, abs=0.2)
    assert m["completion"] == pytest.approx(100 - 18 - 27, abs=0.2)


def test_analyze_attributes_and_suggests(tmp_path):
    t, r = synth_curve()
    p = tmp_path / "c.csv"
    write_csv(p, ["时间", "留存"], list(zip(t, r)))
    res = rf.analyze(str(p), fake_report())
    d0, d1 = res["drops"]
    assert d0["attribution"][0]["cause"] == "hook"
    causes1 = [a["cause"] for a in d1["attribution"]]
    assert causes1[0] == "dead_zone" and "slow_shot" in causes1
    assert d1["context"]["shot"]["index"] == 2
    assert "这个方法其实很简单" in d1["context"]["on_screen_text"]
    sug = {s["cause"]: s for s in res["suggestions"]}
    assert {"dead_zone", "hook"} <= set(sug)
    dz = sug["dead_zone"]
    assert dz["target"]["file"].endswith("references/methodology/hook.md") and dz["target"].get(
        "line"
    )
    assert dz["param"]["quality_review --dead-zone"] == 6.0
    assert "寒暄式开场" in sug["hook"]["proposal"]


def test_cli_json_and_no_report(tmp_path, capsys):
    t, r = synth_curve(drops=((10.0, 20),))
    p = tmp_path / "c.csv"
    write_csv(p, ["time", "retention"], list(zip(t, r)))
    out = tmp_path / "res.json"
    assert rf.main(["--csv", str(p), "--json", "--out", str(out)]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["ok"] and len(res["data"]["drops"]) == 1 and res["data"]["suggestions"] == []
    assert out.exists()
    rep = tmp_path / "report.json"
    rep.write_text(json.dumps(fake_report()), encoding="utf-8")
    assert rf.main(["--csv", str(p), "--report", str(rep)]) == 0
    assert "规则修改建议" in capsys.readouterr().out


def test_cli_input_errors(tmp_path):
    assert rf.main(["--csv", str(tmp_path / "none.csv")]) == 2
    bad = tmp_path / "bad.csv"
    bad.write_text("名字,值\na,b\nc,d\ne,f\n", encoding="utf-8")
    assert rf.main(["--csv", str(bad)]) == 2


def test_methodology_files_untouched(tmp_path):
    """只输出建议: 跑完 analyze 后方法论文件内容不变。"""
    files = [os.path.join(rf.METHODOLOGY, f) for f in os.listdir(rf.METHODOLOGY)]
    before = {f: open(f, "rb").read() for f in files}
    t, r = synth_curve()
    p = tmp_path / "c.csv"
    write_csv(p, ["时间", "留存"], list(zip(t, r)))
    rf.analyze(str(p), fake_report())
    assert all(open(f, "rb").read() == b for f, b in before.items())
