"""
字幕模板 / 文字模板预设 (剪映 11.x)。

模板由剪映用包内脚本 (Cache/artistEffect/<id>/<md5>/js/template) 渲染, 草稿里只记录模板素材
(materials.text_templates) 及其子文字 (texts)、花字 (effects)、动画 (material_animations) 的引用关系,
字段多且随模板而异。因此不手工拼装, 而是:

1. 采集: 从剪映自己保存的明文 template.json 中, 取出用户在剪映里套用过的模板素材, 存为预设
   (data/template_presets/<resource_id>.json)。
2. 克隆: 生成草稿时复制预设, 换新 ID, 替换文字/逐字时间/时长。
"""

import copy
import glob
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from utils.panel_assets import find_panel_asset, get_jianying_user_data_dirs

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRESET_DIR = os.path.join(SKILL_ROOT, "data", "template_presets")


def _new_id() -> str:
    return str(uuid.uuid4()).upper()


def _draft_json_candidates(user_data_dirs=None) -> List[str]:
    out = []
    for ud in user_data_dirs or get_jianying_user_data_dirs():
        root = os.path.join(ud, "Projects", "com.lveditor.draft")
        out += glob.glob(os.path.join(root, "*", "template.json"))
        out += glob.glob(os.path.join(root, "*", "Timelines", "*", "template.json"))
    return out


def _load_plain_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            if f.read(1) != "{":
                return None
            f.seek(0)
            return json.load(f)
    except (OSError, ValueError):
        return None


def extract_presets(content: dict, source: str = "") -> List[dict]:
    """从一份明文草稿中提取每个模板片段的预设。"""
    mats = content.get("materials", {})
    templates = {m["id"]: m for m in mats.get("text_templates", []) if m.get("id")}
    if not templates:
        return []
    by_id: Dict[str, tuple] = {}
    for key, items in mats.items():
        if key in ("text_templates", "texts") or not isinstance(items, list):
            continue
        for m in items:
            if isinstance(m, dict) and m.get("id"):
                by_id[m["id"]] = (key, m)
    texts = {m["id"]: m for m in mats.get("texts", []) if m.get("id")}

    presets = []
    for track in content.get("tracks", []):
        for seg in track.get("segments", []):
            tpl = templates.get(seg.get("material_id"))
            if not tpl or not tpl.get("resource_id"):
                continue
            # 剪映真正套用过的模板都带依赖资源; 为空说明是 skill 写入、剪映未重新套用的骨架, 不能当预设
            if not tpl.get("resources"):
                continue
            refs = list(seg.get("extra_material_refs", []))
            sub_texts = []
            for info in tpl.get("text_info_resources", []):
                refs += info.get("extra_material_refs", [])
                if info.get("text_material_id") in texts:
                    sub_texts.append(texts[info["text_material_id"]])
            extras: Dict[str, List[dict]] = {}
            seen = set()
            for rid in refs:
                if rid in by_id and rid not in seen:
                    seen.add(rid)
                    key, m = by_id[rid]
                    extras.setdefault(key, []).append(m)
            name = tpl.get("name") or ""
            if not name:  # 剪映"点击重试"后重新套用的模板不带名称, 按 ID 从面板索引补上
                for csv_name in ("panel_subtitle_templates.csv", "panel_text_templates.csv"):
                    row = find_panel_asset(csv_name, str(tpl["resource_id"]))
                    if row:
                        name = row["name"]
                        break
            presets.append({
                "resource_id": str(tpl["resource_id"]),
                "name": name,
                # type 是可靠的 (text_template_subtitle / text_template); 重试套用后的 resource_type 可能不准
                "kind": "subtitle_template" if tpl.get("type") == "text_template_subtitle" else "text_template",
                "source": source,
                "segment": {
                    "duration": seg["target_timerange"]["duration"],
                    "clip": seg.get("clip"),
                    "uniform_scale": seg.get("uniform_scale"),
                    "extra_material_refs": seg.get("extra_material_refs", []),
                },
                "template": tpl,
                "texts": sub_texts,
                "extras": extras,
            })
    return presets


def harvest_presets(user_data_dirs=None, preset_dir: str = PRESET_DIR, verbose: bool = True) -> List[dict]:
    """扫描剪映草稿的明文 template.json, 保存其中套用过的模板预设 (较新的草稿优先)。"""
    files = sorted(_draft_json_candidates(user_data_dirs), key=os.path.getmtime)
    found: Dict[str, dict] = {}
    for path in files:  # 旧 -> 新, 新的覆盖旧的
        content = _load_plain_json(path)
        if not content:
            continue
        draft_dir = os.path.dirname(path)
        if os.path.basename(os.path.dirname(draft_dir)) == "Timelines":
            draft_dir = os.path.dirname(os.path.dirname(draft_dir))
        draft_name = os.path.basename(draft_dir)
        for p in extract_presets(content, source=draft_name):
            found[p["resource_id"]] = p
    os.makedirs(preset_dir, exist_ok=True)
    for rid, p in found.items():
        with open(os.path.join(preset_dir, f"{rid}.json"), "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False, indent=1)
        if verbose:
            print(f"✅ 模板预设: {p['name']} ({rid}, {p['kind']}) <- 草稿「{p['source']}」")
    return list(found.values())


def find_preset(name_or_id: str, preset_dir: str = PRESET_DIR) -> Optional[dict]:
    key = str(name_or_id).strip()
    path = os.path.join(preset_dir, f"{key}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    matches = []
    for path in glob.glob(os.path.join(preset_dir, "*.json")):
        with open(path, "r", encoding="utf-8") as f:
            p = json.load(f)
        if p.get("name") == key:
            matches.append((os.path.getmtime(path), p))
    if not matches:
        return None
    matches.sort(key=lambda x: x[0], reverse=True)
    if len(matches) > 1:
        ids = ", ".join(m[1]["resource_id"] for m in matches)
        print(f"ℹ️ 有 {len(matches)} 个同名模板「{key}」({ids}), 使用最新采集的 {matches[0][1]['resource_id']}; "
              f"可改用 resource_id 指定")
    return matches[0][1]


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _even_words(text: str, dur_ms: int) -> List[dict]:
    chars = [c for c in text]
    if not chars:
        return []
    step = max(1, dur_ms // len(chars))
    return [{"text": c, "start_time": i * step, "end_time": (i + 1) * step} for i, c in enumerate(chars)]


def _relocate(path: str) -> str:
    """预设中的本地资源路径若不存在, 换到当前机器上存在的剪映 User Data 目录。"""
    if not path or os.path.exists(path) or "User Data" not in path:
        return path
    tail = path.split("User Data", 1)[1].lstrip("/\\")
    for ud in get_jianying_user_data_dirs():
        cand = os.path.join(ud, tail)
        if os.path.exists(cand):
            return cand
    return path


def _replace_text_material(mat: dict, text: str, dur_ms: int, group_id: str) -> None:
    content = json.loads(mat.get("content") or "{}")
    styles = content.get("styles") or [{}]
    style = copy.deepcopy(styles[0])  # 多段样式 (关键词高亮等) 只保留首段样式铺满全文
    style["range"] = [0, _utf16_len(text)]
    for k in ("font", "effectStyle"):
        if isinstance(style.get(k), dict) and style[k].get("path"):
            style[k]["path"] = _relocate(style[k]["path"])
    content["styles"] = [style]
    content["text"] = text
    mat["content"] = json.dumps(content, ensure_ascii=False)
    words = mat.get("words")
    if isinstance(words, dict) and words.get("text"):
        ws = _even_words(text, dur_ms)
        mat["words"] = {
            "text": [w["text"] for w in ws],
            "start_time": [w["start_time"] for w in ws],
            "end_time": [w["end_time"] for w in ws],
        }
    if mat.get("group_id"):
        mat["group_id"] = group_id


def instantiate_preset(preset: dict, text: str, duration_us: int) -> Dict[str, Any]:
    """
    克隆预设, 返回 {"material_id", "extra_material_refs", "clip", "materials": {列表名: [素材]}}。
    """
    tpl = copy.deepcopy(preset["template"])
    texts = copy.deepcopy(preset["texts"])
    extras = copy.deepcopy(preset.get("extras", {}))
    orig_dur = max(1, int(preset["segment"]["duration"]))
    dur_ms = duration_us // 1000

    id_map: Dict[str, str] = {tpl["id"]: _new_id()}
    for m in texts:
        id_map[m["id"]] = _new_id()
    for items in extras.values():
        for m in items:
            id_map[m["id"]] = _new_id()

    group_id = f"caption_template_{int(time.time() * 1000)}{uuid.uuid4().int % 1000:03d}"
    for m in texts:
        m["id"] = id_map[m["id"]]
        _replace_text_material(m, text, dur_ms, group_id)
    for items in extras.values():
        for m in items:
            m["id"] = id_map[m["id"]]
            if m.get("path"):
                m["path"] = _relocate(m["path"])
            for a in m.get("animations", []) or []:
                if a.get("duration") == orig_dur:
                    a["duration"] = duration_us
                elif a.get("duration"):
                    a["duration"] = min(a["duration"], duration_us)
                if a.get("path"):
                    a["path"] = _relocate(a["path"])

    tpl["id"] = id_map[tpl["id"]]
    if not tpl.get("effect_id"):  # 剪映"点击重试"套用后会清空 effect_id; 剪映自己的草稿里两者相同
        tpl["effect_id"] = tpl.get("resource_id", "")
    tpl["path"] = _relocate(tpl.get("path", ""))
    for r in tpl.get("resources", []) or []:
        if r.get("path"):
            r["path"] = _relocate(r["path"])
    infos = tpl.get("text_info_resources", []) or []
    for info in infos:
        info["id"] = _new_id()
        info["text_material_id"] = id_map.get(info.get("text_material_id"), info.get("text_material_id"))
        info["extra_material_refs"] = [id_map.get(r, r) for r in info.get("extra_material_refs", [])]
        attach = info.get("attach_info") or {}
        if attach.get("duration"):
            attach["duration"] = min(duration_us, int(attach["duration"] * duration_us / orig_dur))
        if info.get("word_index"):
            info["word_index"] = [0, len(text)]
    if tpl.get("material_text_ranges"):
        blen = len(text.encode("utf-8"))
        tpl["material_text_ranges"] = [
            {**r, "location": i * blen, "length": blen}
            for i, r in enumerate(tpl["material_text_ranges"][: len(infos) or None])
        ]
    if tpl.get("merge_content"):
        tpl["merge_content"] = text * max(1, len(infos))
    for key in ("origin_word_info", "current_word_info"):
        info = tpl.get(key)
        if isinstance(info, dict) and info.get("text"):
            tpl[key] = {**info, "text": text, "start_time": 0, "end_time": dur_ms,
                        "words": _even_words(text, dur_ms), "keyword_ranges": []}

    materials: Dict[str, List[dict]] = {"text_templates": [tpl], "texts": texts}
    for key, items in extras.items():
        materials.setdefault(key, []).extend(items)
    return {
        "material_id": tpl["id"],
        "extra_material_refs": [id_map.get(r, r) for r in preset["segment"].get("extra_material_refs", [])],
        "clip": preset["segment"].get("clip"),
        "materials": materials,
    }


# ---------------------------------------------------------------------------
# 由模板包直接生成预设 (免费字幕模板: 剪映打开预热草稿时只下载、不重新套用, 无法采集)
# ---------------------------------------------------------------------------
import re as _re

_TAG = _re.compile(r"<(effectStyle|font)\s+id=\"([^\"]*)\"[^>]*>|<size=([\d.]+)>|<color=\(([^)]*)\)>")


def parse_rich_text(rich: str) -> dict:
    """模板包 richText (如 <effectStyle id=".."><size=12><color=(1,0.9,0.2,1)><font id="..">[文字]...) -> 草稿样式。"""
    head = rich.split("[", 1)[0]
    style = {"range": [0, 1]}
    for m in _TAG.finditer(head):
        if m.group(1) == "effectStyle":
            style["effectStyle"] = {"id": m.group(2), "path": ""}
        elif m.group(1) == "font":
            style["font"] = {"id": m.group(2), "path": ""}
        elif m.group(3):
            style["size"] = float(m.group(3))
        elif m.group(4):
            rgba = [float(v) for v in m.group(4).split(",")]
            style["fill"] = {"content": {"render_type": "solid", "solid": {"color": rgba[:3]}}}
            style["_alpha"] = rgba[3] if len(rgba) > 3 else 1.0
    style.setdefault("fill", {"content": {"render_type": "solid", "solid": {"color": [1, 1, 1]}}})
    return style


def _hex_rgba(rgb, a=1.0) -> str:
    return "#" + "".join(f"{max(0, min(255, round(float(v) * 255))):02x}" for v in list(rgb) + [a])


def preset_from_package(row: dict, package_dir: str, kind: str = "subtitle_template") -> dict:
    """
    由已下载的模板包 (Cache/artistEffect/<id>/<md5>/) 生成预设, 结构与 extract_presets 采集到的相同。
    子文字的样式/字体/花字/动画来自 content.json, 依赖资源来自 extra.json。
    """
    with open(os.path.join(package_dir, "content.json"), "r", encoding="utf-8") as f:
        content = json.load(f)
    extra = {}
    if os.path.exists(os.path.join(package_dir, "extra.json")):
        with open(os.path.join(package_dir, "extra.json"), "r", encoding="utf-8") as f:
            extra = json.load(f)
    local = _local_md5_by_resource()
    children = [c for c in content.get("children") or [] if c.get("type") == "text"]
    texts, infos, anims, effects, seg_refs = [], [], [], [], []
    for ch in children:
        tp = ch.get("text_params") or {}
        style = parse_rich_text(tp.get("richText", ""))
        alpha = style.pop("_alpha", 1.0)
        for k in ("font", "effectStyle"):
            if k in style:
                style[k]["path"] = local.get(style[k]["id"], "")
        tid = _new_id()
        rgb = style["fill"]["content"]["solid"]["color"]
        texts.append({
            "id": tid, "type": "text", "content": json.dumps({"styles": [style], "text": "A"}, ensure_ascii=False),
            "font_size": 15, "text_size": 30, "line_spacing": tp.get("lineSpacing", 0.02),
            "letter_spacing": tp.get("letterSpacing", 0), "alignment": tp.get("typeSettingAlign", 1),
            "typesetting": tp.get("typeSettingKind", 0), "bold_width": tp.get("boldValue", 0),
            "italic_degree": tp.get("italicDegree", 0), "underline_offset": tp.get("decorationOffset", 0.22),
            "underline_width": tp.get("decorationWidth", 0.05), "global_alpha": tp.get("globalAlpha", 1),
            "line_max_width": 0.82, "line_feed": 1, "force_apply_line_max_width": True, "check_flag": 15,
            "text_color": _hex_rgba(rgb, alpha), "name": ch.get("name", ""), "use_effect_default_color": True,
            "group_id": "caption_template_1", "sub_template_id": -1, "fixed_width": -1, "fixed_height": -1,
            "words": {"text": ["A"], "start_time": [0], "end_time": [1000]},
            "current_words": {"text": [], "start_time": [], "end_time": []},
        })
        refs = []
        if style.get("effectStyle"):
            eid = _new_id()
            effects.append({"id": eid, "type": "text_effect", "resource_id": style["effectStyle"]["id"],
                            "path": style["effectStyle"]["path"], "source_platform": 1, "value": 0,
                            "apply_target_type": 0, "sub_type": "none", "platform": "all", "visible": True})
            refs.append(eid)
        aid = _new_id()
        anims.append({"id": aid, "type": "sticker_animation", "multi_language_current": "none", "animations": [
            {"type": a.get("anim_type") or "caption", "resource_id": a.get("anim_resource_id", ""),
             "path": local.get(a.get("anim_resource_id", ""), ""), "material_type": "sticker", "start": 0,
             "duration": 1, "source_platform": 0, "platform": "all", "name": "", "id": "", "panel": "",
             "category_id": "", "category_name": "", "third_resource_id": "", "request_id": ""}
            for a in ch.get("anims") or [] if a.get("anim_resource_id")]})
        refs.append(aid)
        seg_refs += refs
        pos, sc = ch.get("position") or [0, 0, 0], ch.get("scale") or [1, 1, 1]
        size = ch.get("original_size") or [0, 0]
        infos.append({
            "id": _new_id(), "text_material_id": tid, "extra_material_refs": refs, "clip_type": "", "capital": "",
            "word_index": [], "lyric_keyframes": [], "order_in_layer": ch.get("order_in_layer", 0),
            "attach_info": {"start_time": 0, "duration": 1, "original_size_width": size[0], "original_size_height": size[1],
                            "clip": {"alpha": 1, "rotation": (ch.get("rotation") or [0, 0, 0])[2], "rotation3d": [],
                                     "flip": {"horizontal": False, "vertical": False},
                                     "scale": {"x": sc[0], "y": sc[1]}, "transform": {"x": pos[0], "y": pos[1]}}},
        })
    resources = [{"panel": d.get("type", ""), "resource_id": str(d.get("resource_id", "")),
                  "source_platform": 1 if d.get("source") == 1 else 0,
                  "path": local.get(str(d.get("resource_id", "")), "")}
                 for d in extra.get("depend_resource_list") or []]
    subtitle = kind == "subtitle_template"
    tpl = {
        "id": _new_id(), "type": "text_template_subtitle" if subtitle else "text_template",
        "text_template_resource_type": kind, "resource_id": row["resource_id"], "effect_id": row["resource_id"],
        "name": row["name"], "path": package_dir, "resources": resources, "text_info_resources": infos,
        "source_platform": 1, "category_name": "subtitle-templates" if subtitle else "", "category_id": "",
        "check_flag": 7, "platform": "all", "version": "1.0.0", "preview_time": 0.1, "is_pre_rendered": False,
        "non_text_info_resources": [], "material_text_ranges": [], "merge_content": "", "text_to_audio_ids": [],
        "origin_word_info": {"text": "", "words": [], "start_time": 0, "end_time": 0, "keyword_ranges": []},
        "current_word_info": {"text": "", "words": [], "start_time": 0, "end_time": 0, "keyword_ranges": []},
    }
    return {"resource_id": row["resource_id"], "name": row["name"], "kind": kind, "source": "package",
            "segment": {"duration": 1, "clip": {"alpha": 1, "rotation": 0, "flip": {"horizontal": False, "vertical": False},
                                                 "scale": {"x": 1, "y": 1}, "transform": {"x": 0, "y": -0.73}},
                        "uniform_scale": {"on": True, "value": 1}, "extra_material_refs": seg_refs},
            "template": tpl, "texts": texts, "extras": {"material_animations": anims, "effects": effects}}


_MD5_CACHE: Dict[str, str] = {}


def _local_md5_by_resource(refresh: bool = False) -> Dict[str, str]:
    """resource_id -> 本地资源目录 (Cache/effect 与 Cache/artistEffect 下 <id>/<md5>/ 中第一个)。同一进程内缓存扫描结果。"""
    if _MD5_CACHE and not refresh:
        return _MD5_CACHE
    out = _MD5_CACHE
    out.clear()
    for ud in get_jianying_user_data_dirs():
        for sub in ("artistEffect", "effect"):
            root = os.path.join(ud, "Cache", sub)
            if not os.path.isdir(root):
                continue
            for rid in os.listdir(root):
                d = os.path.join(root, rid)
                if os.path.isdir(d) and rid not in out:
                    md5s = [m for m in os.listdir(d) if len(m) == 32 and os.path.isdir(os.path.join(d, m))]
                    if md5s:
                        out[rid] = os.path.join(d, md5s[0])
    return out


def presets_from_local_packages(kind: str = "subtitle_template", preset_dir: str = PRESET_DIR,
                                verbose: bool = True) -> List[dict]:
    """为本地已下载模板包、但还没有预设的模板生成预设 (剪映真正套用过的预设不会被覆盖)。"""
    csv_name = "panel_subtitle_templates.csv" if kind == "subtitle_template" else "panel_text_templates.csv"
    from utils.panel_assets import load_panel_rows

    made = []
    os.makedirs(preset_dir, exist_ok=True)
    for ud in get_jianying_user_data_dirs():
        root = os.path.join(ud, "Cache", "artistEffect")
        for row in load_panel_rows(csv_name):
            path = os.path.join(preset_dir, f"{row['resource_id']}.json")
            pkg = os.path.join(root, row["resource_id"], row.get("md5", ""))
            if os.path.exists(path) or not row.get("md5") or not os.path.exists(os.path.join(pkg, "content.json")):
                continue
            try:
                preset = preset_from_package(row, pkg, kind)
            except (OSError, ValueError, KeyError) as e:
                if verbose:
                    print(f"⚠️ {row['name']}: 模板包解析失败 ({e})")
                continue
            if not preset["texts"]:
                continue
            with open(path, "w", encoding="utf-8") as f:
                json.dump(preset, f, ensure_ascii=False, indent=1)
            made.append(preset)
            if verbose:
                print(f"✅ 由模板包生成预设: {row['name']} ({row['resource_id']})")
    return made
