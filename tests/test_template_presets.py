import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "vendor"))

from utils.template_presets import extract_presets, instantiate_preset  # noqa: E402


def _content():
    """剪映 11.x 套用字幕模板后的草稿结构 (精简自真实 template.json)。"""
    text = {"id": "T1", "group_id": "caption_template_1", "content": json.dumps({
        "styles": [{"range": [0, 3], "size": 12, "effectStyle": {"id": "F1", "path": ""}}], "text": "旧文字"},
        ensure_ascii=False),
        "words": {"text": ["旧", "文", "字"], "start_time": [0, 1, 2], "end_time": [1, 2, 3]}}
    tpl = {"id": "TPL", "resource_id": "7416724149906705683", "name": "向下弹出-方块粗体黄",
           "text_template_resource_type": "subtitle_template", "type": "text_template_subtitle", "path": "",
           "merge_content": "旧文字", "resources": [{"panel": "flower", "resource_id": "F1", "path": ""}], "material_text_ranges": [{"location": 0, "length": 9}],
           "origin_word_info": {"text": "旧文字", "words": [], "start_time": 0, "end_time": 2000},
           "text_info_resources": [{"id": "I1", "text_material_id": "T1", "extra_material_refs": ["E1", "A1"],
                                    "attach_info": {"duration": 2000000}, "word_index": [0, 3]}]}
    return {
        "materials": {
            "text_templates": [tpl], "texts": [text],
            "effects": [{"id": "E1", "type": "text_effect", "resource_id": "F1"}],
            "material_animations": [{"id": "A1", "animations": [{"duration": 2000000, "resource_id": "X"}]}],
            "videos": [{"id": "V1"}],
        },
        "tracks": [{"type": "text", "segments": [{
            "material_id": "TPL", "extra_material_refs": ["A1", "E1"],
            "target_timerange": {"start": 0, "duration": 2000000}, "clip": {"transform": {"x": 0, "y": -0.73}}}]}],
    }


def test_extract_and_instantiate():
    presets = extract_presets(_content(), source="t")
    assert len(presets) == 1
    p = presets[0]
    assert p["kind"] == "subtitle_template"
    assert set(p["extras"]) == {"effects", "material_animations"}

    inst = instantiate_preset(p, "新的字幕", 4000000)
    mats = inst["materials"]
    tpl, text = mats["text_templates"][0], mats["texts"][0]
    all_ids = {m["id"] for items in mats.values() for m in items}
    # 全部换新 ID, 引用关系保持
    assert not all_ids & {"TPL", "T1", "E1", "A1"}
    assert inst["material_id"] == tpl["id"]
    assert set(inst["extra_material_refs"]) <= all_ids
    info = tpl["text_info_resources"][0]
    assert info["text_material_id"] == text["id"] and set(info["extra_material_refs"]) <= all_ids
    # 文字/时长替换
    c = json.loads(text["content"])
    assert c["text"] == "新的字幕" and c["styles"][0]["range"] == [0, 4]
    assert text["words"]["text"] == list("新的字幕") and text["words"]["end_time"][-1] == 4000
    assert tpl["merge_content"] == "新的字幕"
    assert tpl["material_text_ranges"][0]["length"] == len("新的字幕".encode("utf-8"))
    assert info["attach_info"]["duration"] == 4000000 and info["word_index"] == [0, 4]
    assert mats["material_animations"][0]["animations"][0]["duration"] == 4000000
    assert text["group_id"] != "caption_template_1"
    # 两次实例化互不冲突
    inst2 = instantiate_preset(p, "x", 1000000)
    assert inst2["material_id"] != inst["material_id"]


def test_skeleton_without_resources_is_skipped():
    """skill 写入但剪映未重新套用的骨架 (无依赖资源) 不能被采集成预设。"""
    content = _content()
    content["materials"]["text_templates"][0]["resources"] = []
    assert extract_presets(content) == []


def test_parse_rich_text_from_package():
    from utils.template_presets import parse_rich_text

    st = parse_rich_text('<effectStyle id="111" path=""><size=12><color=(1,0.9,0.2,1)><font id="222" path="">[字幕]</font>'
                         '</color></size></effectStyle>')
    assert st["size"] == 12.0 and st["effectStyle"]["id"] == "111" and st["font"]["id"] == "222"
    assert st["fill"]["content"]["solid"]["color"] == [1.0, 0.9, 0.2]
