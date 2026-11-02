import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import edit_diff as ed  # noqa: E402

FRAME = 1_000_000 / 30


def _text_content(text, size=8.0, color=(1.0, 1.0, 1.0)):
    style = {
        "fill": {"content": {"solid": {"color": list(color)}}},
        "size": size,
        "range": [0, len(text)],
    }
    return json.dumps({"styles": [style], "text": text}, ensure_ascii=False)


def _seg(sid, mid, start, dur, src=None, refs=(), y=0.0, volume=1.0, kfs=None):
    s = {
        "id": sid,
        "material_id": mid,
        "target_timerange": {"start": start, "duration": dur},
        "source_timerange": {"start": src, "duration": dur} if src is not None else None,
        "speed": 1.0,
        "volume": volume,
        "visible": True,
        "clip": {"alpha": 1.0, "scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0.0, "y": y}},
        "extra_material_refs": list(refs),
        "common_keyframes": kfs or [],
    }
    return s


def make_draft():
    """skill 版: 主轨 3 段视频 (段间转场), 1 条 BGM (淡入淡出), 2 条字幕。"""
    mats = {
        "videos": [
            {"id": f"v{i}", "path": f"/abs/draft/materials/clip{i}.mp4", "type": "video"}
            for i in range(3)
        ],
        "audios": [{"id": "a0", "path": "/abs/music/bgm.mp3", "name": "bgm"}],
        "texts": [
            {"id": "t0", "content": _text_content("第一句字幕", 8.0)},
            {"id": "t1", "content": _text_content("第二句字幕", 8.0)},
        ],
        "transitions": [
            {
                "id": "tr0",
                "name": "叠化",
                "effect_id": "322577",
                "duration": 600000,
                "is_overlap": True,
            },
            {
                "id": "tr1",
                "name": "叠化",
                "effect_id": "322577",
                "duration": 500000,
                "is_overlap": True,
            },
        ],
        "audio_fades": [{"id": "f0", "fade_in_duration": 200000, "fade_out_duration": 1000000}],
        "speeds": [{"id": f"sp{i}", "speed": 1.0} for i in range(4)],
    }
    video = [
        _seg("s0", "v0", 0, 3_000_000, src=1_000_000, refs=["sp0", "tr0"]),
        _seg("s1", "v1", 3_000_000, 3_000_000, src=0, refs=["sp1", "tr1"]),
        _seg("s2", "v2", 6_000_000, 3_000_000, src=500_000, refs=["sp2"]),
    ]
    audio = [_seg("sa", "a0", 0, 9_000_000, src=0, refs=["sp3", "f0"], volume=0.3)]
    text = [
        _seg("st0", "t0", 0, 2_000_000, y=-0.8),
        _seg("st1", "t1", 3_000_000, 2_000_000, y=-0.8),
    ]
    return {
        "canvas_config": {"width": 1080, "height": 1920, "ratio": "original"},
        "fps": 30,
        "duration": 9_000_000,
        "materials": mats,
        "tracks": [
            {"id": "T0", "type": "video", "name": "Main", "segments": video},
            {"id": "T1", "type": "audio", "name": "BGM", "segments": audio},
            {"id": "T2", "type": "text", "name": "Sub", "segments": text},
        ],
    }


def add_jianying_noise(d):
    """模拟剪映打开草稿: 改写所有 id、补默认附属素材、改相对路径、时间吸附到整帧、补默认字段。"""
    d = copy.deepcopy(d)
    idmap = {}
    for kind, lst in d["materials"].items():
        for m in lst:
            new = f"JY-{kind}-{m['id']}".upper()
            idmap[m["id"]] = new
            m["id"] = new
            if m.get("path"):
                m["path"] = "materials/" + os.path.basename(m["path"])
    d["materials"]["canvases"] = []
    d["materials"]["vocal_separations"] = []
    d["materials"]["loudnesses"] = []
    for ti, t in enumerate(d["tracks"]):
        t["id"] = f"NEWTRACK{ti}"
        t["attribute"] = 0
        for si, s in enumerate(t["segments"]):
            s["id"] = f"NEWSEG{ti}{si}"
            s["material_id"] = idmap[s["material_id"]]
            s["extra_material_refs"] = [idmap[r] for r in s["extra_material_refs"]]
            for kind, extra in (
                ("canvases", {"type": "canvas_color", "color": ""}),
                ("vocal_separations", {"choice": 0}),
                ("loudnesses", {"enable": False, "target_loudness": 0}),
            ):
                mid = f"AUTO-{kind}-{ti}{si}"
                d["materials"][kind].append({"id": mid, **extra})
                s["extra_material_refs"].append(mid)
            s["enable_adjust"] = False
            s["responsive_layout"] = {"enable": False}
            s["render_timerange"] = {"start": 0, "duration": 0}
            tt = s["target_timerange"]
            tt["start"] = int(round(round(tt["start"] / FRAME) * FRAME)) + 1  # 整帧 + 1us 误差
            tt["duration"] = int(round(round(tt["duration"] / FRAME) * FRAME))
    return d


def write(tmp_path, name, d):
    p = tmp_path / name
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return str(p)


def by_kind(res):
    out = {}
    for c in res["changes"]:
        out.setdefault(c["kind"], []).append(c)
    return out


def test_noise_only_is_no_change(tmp_path):
    base = make_draft()
    edited = add_jianying_noise(base)
    res = ed.diff_files(write(tmp_path, "a.json", base), write(tmp_path, "b.json", edited))
    assert res["modified"] is False, res["changes"]
    assert res["alignment"]["matched"] == 6
    assert res["noise"]["id_rewritten"] == 6
    assert res["noise"]["path_rewritten"] == 4
    assert res["noise"]["auto_materials_added"]["canvases"] == 6


def test_sparse_defaults_are_not_changes():
    """剪映 Timelines 备份里省略默认字段 (start=0、clip 缺省、volume 缺省)。"""
    base = make_draft()
    sparse = copy.deepcopy(base)
    for t in sparse["tracks"]:
        for s in t["segments"]:
            if s["target_timerange"]["start"] == 0:
                del s["target_timerange"]["start"]
            if s["volume"] == 1.0:
                del s["volume"]
            s["clip"] = {"transform": s["clip"]["transform"]}
            del s["speed"], s["visible"]
    assert ed.diff_drafts(base, sparse)["modified"] is False


def test_cut_trim_and_duration_detected():
    base = make_draft()
    edited = add_jianying_noise(base)
    seg = edited["tracks"][0]["segments"][1]
    seg["source_timerange"]["start"] += 200_000  # 入点推后 200ms
    seg["source_timerange"]["duration"] -= 200_000
    seg["target_timerange"]["duration"] -= 200_000
    for s in edited["tracks"][0]["segments"][2:]:
        s["target_timerange"]["start"] -= 200_000  # 后面片段整体前移 (吸附), 不应重复报移动
    k = by_kind(ed.diff_drafts(base, edited))
    assert k["trim_in"][0]["delta_ms"] == pytest.approx(200, abs=1)
    assert k["duration"][0]["delta_ms"] == pytest.approx(-200, abs=40)
    assert "moved" not in k
    assert "trim_out" not in k


def test_text_size_position_color_content():
    base = make_draft()
    edited = add_jianying_noise(base)
    mats = edited["materials"]["texts"]
    mats[0]["content"] = _text_content("第一句字幕", 10.0, (1.0, 0.85, 0.0))
    mats[1]["content"] = _text_content("第二句改了", 8.0)
    edited["tracks"][2]["segments"][0]["clip"]["transform"]["y"] = -0.6
    k = by_kind(ed.diff_drafts(base, edited))
    assert k["size"][0]["before"] == 8.0 and k["size"][0]["after"] == 10.0
    assert k["color"][0]["after"] == "#FFD900"
    assert k["position"][0]["after"] == -0.6
    assert k["content"][0]["after"] == "第二句改了"  # 文字改了仍和原片段对齐, 不算删除 + 新增
    assert "deleted" not in k and "added" not in k


def test_delete_add_reorder():
    base = make_draft()
    edited = add_jianying_noise(base)
    segs = edited["tracks"][0]["segments"]
    # 交换前两段的素材顺序 (保持时间轴位置)
    segs[0]["material_id"], segs[1]["material_id"] = segs[1]["material_id"], segs[0]["material_id"]
    segs[0]["source_timerange"]["start"], segs[1]["source_timerange"]["start"] = 0, 1_000_000
    del edited["tracks"][2]["segments"][1]  # 删掉第二条字幕
    edited["materials"]["texts"].append({"id": "NEWT", "content": _text_content("新加的", 8.0)})
    edited["tracks"][2]["segments"].append(_seg("x", "NEWT", 7_000_000, 1_000_000, y=-0.8))
    k = by_kind(ed.diff_drafts(base, edited))
    assert len(k["deleted"]) == 1 and k["deleted"][0]["segment"]["material"] == "第二句字幕"
    assert len(k["added"]) == 1
    assert k["reordered"]


def test_transition_fx_audio_keyframes():
    base = make_draft()
    edited = add_jianying_noise(base)
    trs = edited["materials"]["transitions"]
    trs[0]["name"], trs[0]["effect_id"] = "闪白", "999"  # 换转场
    seg2 = edited["tracks"][0]["segments"][1]
    seg2["extra_material_refs"] = [r for r in seg2["extra_material_refs"] if "TRANSITIONS" not in r]
    edited["tracks"][1]["segments"][0]["volume"] = 0.15
    edited["materials"]["audio_fades"][0]["fade_out_duration"] = 2_000_000
    edited["tracks"][0]["segments"][2]["common_keyframes"] = [
        {"property_type": "KFTypeScaleX", "keyframe_list": [{"time_offset": 0, "values": [1.0]},
                                                             {"time_offset": 1_000_000, "values": [1.2]}]}
    ]  # fmt: skip
    edited["canvas_config"]["width"], edited["canvas_config"]["height"] = 1920, 1080
    k = by_kind(ed.diff_drafts(base, edited))
    assert k["transition_replaced"][0]["after"] == "闪白"
    assert k["transition_removed"]
    assert k["volume"][0]["before"] == 0.3 and k["volume"][0]["after"] == 0.15
    assert k["fade_out"][0]["delta_ms"] == pytest.approx(1000)
    assert k["keyframe_added"][0]["field"] == "KFTypeScaleX"
    assert k["resize"][0]["after"] == "1920x1080"


def test_transition_auto_clamp_is_noise():
    """剪映把重叠转场取偶数帧: 500ms → 466.7ms; 超过相邻片段一半时截短。"""
    base = make_draft()
    base["tracks"][0]["segments"][1]["target_timerange"]["duration"] = 900_000
    base["tracks"][0]["segments"][1]["source_timerange"]["duration"] = 900_000
    base["tracks"][0]["segments"][2]["target_timerange"]["start"] = 3_900_000
    edited = add_jianying_noise(base)
    for m in edited["materials"]["transitions"]:
        # tr0 600ms 不超过一半 (取偶数帧仍 600ms); tr1 所在片段 900ms → 上限 450ms → 偶数帧 12 帧 = 400ms
        m["duration"] = 600000 if m["id"].endswith("TR0") else 400000
    res = ed.diff_drafts(base, edited)
    assert res["modified"] is False, res["changes"]
    assert res["noise"]["transition_auto"] == 1
    # 用户真改了转场时长 (变长) 要报出来
    edited["materials"]["transitions"][0]["duration"] = 1_000_000
    assert by_kind(ed.diff_drafts(base, edited))["transition_duration"]


def test_learn_patterns_and_markdown(tmp_path):
    base = make_draft()
    diffs = []
    for i, shift in enumerate((100_000, 140_000)):
        edited = add_jianying_noise(base)
        edited["tracks"][0]["segments"][0]["source_timerange"]["start"] -= shift
        edited["materials"]["texts"][0]["content"] = _text_content("第一句字幕", 10.0)
        diffs.append({**ed.diff_drafts(base, edited), "draft": f"d{i}"})
    rules = {r["key"]: r for r in ed.learn_patterns(diffs)}
    assert rules["trim_in"]["mean_delta_ms"] == pytest.approx(-120, abs=1)
    assert "入点提前 120ms" in rules["trim_in"]["rule"]
    assert "8.0 → 10.0" in rules["size"]["rule"]
    assert "styles" in rules["size"]["target"]
    md = ed.render_markdown([], list(rules.values()), [], ed.suggestions(list(rules.values()), []))
    assert "对 skill 的改进建议" in md and "入点提前" in md


def test_draft_dir_versions_and_learn_readonly(tmp_path, monkeypatch):
    """草稿目录: skill 副本 + 明文 / 加密备份 + Timelines/<id>/template.json; learn 只写输出目录。"""
    root = tmp_path / "drafts"
    d = root / "测试草稿"
    (d / ".backup" / "TL").mkdir(parents=True)
    (d / "Timelines" / "TL").mkdir(parents=True)
    base = make_draft()
    write(d, "draft_info.skill.json", base)
    write(d / ".backup", "20261101101725_abc.load.bak", base)
    (d / ".backup" / "20261101101745_abc.close.bak").write_text("ZW5jcnlwdGVk", encoding="utf-8")
    edited = add_jianying_noise(base)
    edited["materials"]["texts"][0]["content"] = _text_content("第一句字幕", 12.0)
    write(d / "Timelines" / "TL", "template.json", edited)
    monkeypatch.setattr(ed, "DRAFT_ROOTS", [str(root)])
    before = {p: os.path.getmtime(p) for p in map(str, d.rglob("*"))}
    out = tmp_path / "learned"
    res = ed.learn(["测试草稿"], str(out))
    assert res["modified"] == {"测试草稿": True}
    payload = json.loads((out / "edit_diffs.json").read_text(encoding="utf-8"))
    dr = payload["drafts"][0]
    assert dr["encrypted_backups"] == [os.path.join(".backup", "20261101101745_abc.close.bak")]
    assert {c["kind"] for c in dr["comparisons"]} == {"main", "backup→final", "skill→backup"}
    assert "8.0 → 12.0" in (out / "edit_diffs.md").read_text(encoding="utf-8")
    assert before == {p: os.path.getmtime(p) for p in map(str, d.rglob("*"))}  # 草稿目录未被改动
    with pytest.raises(ed.InputError):
        ed.learn(["测试草稿"], str(root / "out"))  # 输出目录不能在草稿目录里


def test_cli_diff_json(tmp_path, capsys):
    base = make_draft()
    a = write(tmp_path, "a.json", base)
    b = write(tmp_path, "b.json", add_jianying_noise(base))
    assert ed.main(["--json", "diff", a, b]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["data"]["modified"] is False
    (tmp_path / "enc.json").write_text("ZW5j", encoding="utf-8")
    assert ed.main(["diff", a, str(tmp_path / "enc.json")]) == 2
