"""
剪映 11.x 素材面板索引 (Panel Assets)

新版剪映 (11.x) 会把素材面板的接口响应缓存在本地 SQLite 中:
    <User Data>/Cache/ressdk_db/<hash>/rp.db  (表 http_cache)
其中包含转场/特效/动画/字体/音色等素材的 resource_id、md5、VIP 标记等信息。

本模块负责:
1. 只读提取这些缓存, 生成 data/panel_*.csv 索引 (由 scripts/sync_jy_panel.py 调用);
2. 加载索引, 并在内置枚举找不到时, 把面板素材动态注册为 pyJianYingDraft 枚举成员,
   使其可以直接用于 add_transition / add_effect / add_animation 等底层 API。
"""

import csv
import glob
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import tempfile
from typing import Dict, Iterable, List, Optional, Tuple

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(SKILL_ROOT, "data")

# effect_type -> (kind, csv 文件名)
EFFECT_TYPE_KINDS: Dict[int, Tuple[str, str]] = {
    19: ("transition", "panel_transitions.csv"),
    12: ("filter", "panel_filters.csv"),
    7: ("video_effect", "panel_video_effects.csv"),
    8: ("character_effect", "panel_character_effects.csv"),
    13: ("video_animation", "panel_video_animations.csv"),
    18: ("text_animation", "panel_text_animations.csv"),
    10: ("font", "panel_fonts.csv"),
    11: ("tts_voice", "panel_tts_voices.csv"),
    26: ("audio_effect", "panel_audio_effects.csv"),
    1: ("flower_text", "panel_flower_texts.csv"),
    6: ("text_template", "panel_text_templates.csv"),
    48: ("subtitle_template", "panel_subtitle_templates.csv"),
    2: ("sticker", "panel_stickers.csv"),
    5: ("stock_video", "panel_stock_videos.csv"),
    9: ("stock_image", "panel_stock_images.csv"),
    3: ("sound_effect", "panel_sound_effects.csv"),
    97: ("mask", "panel_masks.csv"),
    28: ("mask_stroke", "panel_mask_strokes.csv"),
    36: ("blend_mode", "panel_blend_modes.csv"),
    64: ("smart_relight", "panel_smart_relights.csv"),
    20: ("beauty_reshape", "panel_beauty.csv"),   # 美型 (瘦脸/大眼/...)
    21: ("beauty_skin", "panel_beauty.csv"),      # 美颜 (磨皮/美白/...)
    22: ("beauty_makeup", "panel_beauty.csv"),    # 美妆 (套装/口红/腮红/...)
    23: ("beauty_body", "panel_beauty.csv"),      # 智能美体
    24: ("beauty_manual", "panel_beauty.csv"),    # 手动美颜 / 手动美体
    148: ("beauty_preset", "panel_beauty.csv"),   # 一键美颜套装
    35: ("sticker_animation", "panel_sticker_animations.csv"),  # 贴纸入场/出场/循环动画
    27: ("canvas_style", "panel_canvas_styles.csv"),   # 背景填充 → 画布样式
    92: ("text_texture", "panel_text_textures.csv"),  # 文字填充 → 纹理
    37: ("curve_speed", "panel_curve_speeds.csv"),
    61: ("keyframe_curve", "panel_keyframe_curves.csv"),
    29: ("text_glow", "panel_text_glows.csv"),
    34: ("text_bubble", "panel_text_bubbles.csv"),
    49: ("text_bubble", "panel_text_bubbles.csv"),  # 旧版 (Loki) 气泡库; 34 的条目通过 loki_resource_id 指向它
}

# 面板分类名 -> 动画子类型
ANIM_SUB_TYPES = {"入场": "intro", "出场": "outro", "组合": "group", "循环": "loop", "字幕动画": "caption"}

CSV_FIELDS = [
    "name",
    "kind",
    "sub_type",
    "resource_id",
    "effect_id",
    "md5",
    "is_vip",
    "duration_s",
    "is_overlap",
    "categories",
    "params",
    "voice_type",
    "cover_url",
    "preview_url",
]
COVER_FIELDS = ("cover_url", "preview_url")

PANEL_CSV_FILES = sorted({v[1] for v in EFFECT_TYPE_KINDS.values()})


# ---------------------------------------------------------------------------
# 1. 提取 (Extraction)
# ---------------------------------------------------------------------------


class PanelAssetUnavailable(Exception):
    """面板素材暂不可用 (资源未下载到本地, 或该类型不支持自动添加)。"""


def find_local_effect_resource(md5: str, user_data_dirs: Optional[Iterable[str]] = None):
    """在 Cache/effect/<effect_id>/<md5>/ 中查找已下载资源, 返回 (effect_id, 目录路径) 或 None。"""
    if not md5:
        return None
    for ud in user_data_dirs or get_jianying_user_data_dirs():
        root = os.path.join(ud, "Cache", "effect")
        if not os.path.isdir(root):
            continue
        for eid in os.listdir(root):
            path = os.path.join(root, eid, md5)
            if os.path.isdir(path):
                return eid, path
    return None


_LOCAL_MD5_INDEX: Dict[str, str] = {}


def _local_md5_index() -> Dict[str, str]:
    """md5 -> 本地资源目录, 覆盖 Cache/effect/<id>/<md5>/ 与 Cache/artistEffect/<id>/<md5>/。"""
    if _LOCAL_MD5_INDEX:
        return _LOCAL_MD5_INDEX
    for ud in get_jianying_user_data_dirs():
        for sub in ("effect", "artistEffect"):
            root = os.path.join(ud, "Cache", sub)
            if not os.path.isdir(root):
                continue
            for eid in os.listdir(root):
                d = os.path.join(root, eid)
                if not os.path.isdir(d):
                    continue
                for md5 in os.listdir(d):
                    if len(md5) == 32 and os.path.isdir(os.path.join(d, md5)):
                        _LOCAL_MD5_INDEX.setdefault(md5, os.path.join(d, md5))
    return _LOCAL_MD5_INDEX


def _md5_candidates_by_resource(data_dir: Optional[str] = None) -> Dict[str, List[str]]:
    """resource_id -> [md5...], 汇总面板索引 (11.x 版本) 与内置枚举 (旧版本)。"""
    import enum

    import pyJianYingDraft as draft

    out: Dict[str, List[str]] = {}
    for fname in PANEL_CSV_FILES:
        for r in load_panel_rows(fname, data_dir):
            if r.get("resource_id") and r.get("md5"):
                out.setdefault(r["resource_id"], []).append(r["md5"])
    for attr in dir(draft):
        enum_cls = getattr(draft, attr)
        if isinstance(enum_cls, type) and issubclass(enum_cls, enum.Enum):
            for m in enum_cls.__members__.values():
                v = m.value
                rid, md5 = getattr(v, "resource_id", None), getattr(v, "md5", None)
                if rid and md5:
                    out.setdefault(str(rid), []).append(md5)
    return out


def fill_local_resource_paths(content: dict, data_dir: Optional[str] = None) -> int:
    """
    为草稿中引用的动画/转场/滤镜/特效等素材补上剪映本地资源路径。
    实测 (11.6): 动画缺少 path 时效果正常, 但属性面板不显示选中状态。
    """
    local = _local_md5_index()
    if not local:
        return 0
    cands = _md5_candidates_by_resource(data_dir)
    filled = 0

    def fix(item: dict):
        nonlocal filled
        rid = str(item.get("resource_id") or "")
        if not rid or item.get("path") not in (None, "", "C:", "D:"):
            return
        for md5 in cands.get(rid, []):
            if md5 in local:
                item["path"] = local[md5]
                filled += 1
                return

    mats = content.get("materials", {})
    for group in mats.get("material_animations", []):
        for a in group.get("animations", []):
            fix(a)
    for key in ("transitions", "video_effects", "effects", "audio_effects"):
        for item in mats.get(key, []):
            if isinstance(item, dict):
                fix(item)
    return filled


def _svg_aspect_ratio(resource_dir: str) -> Optional[float]:
    svg = os.path.join(resource_dir, "material.svg")
    if not os.path.exists(svg):
        return None
    with open(svg, "r", encoding="utf-8", errors="ignore") as f:
        head = f.read(4000)
    m = re.search(r'viewBox="\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)', head)
    if not m or float(m.group(2)) == 0:
        return None
    return round(float(m.group(1)) / float(m.group(2)), 4)


def get_jianying_user_data_dirs() -> List[str]:
    """返回可能存在的剪映 User Data 目录 (含 CapCut)。"""
    home = os.path.expanduser("~")
    candidates: List[str] = []
    if sys.platform == "darwin":
        candidates += [
            os.path.join(home, "Movies", "JianyingPro", "User Data"),
            os.path.join(home, "Movies", "CapCut", "User Data"),
            os.path.join(
                home, "Library", "Containers", "com.lemon.lvpro", "Data", "Movies",
                "JianyingPro", "User Data",
            ),
        ]
    else:
        local_app_data = os.getenv("LOCALAPPDATA", "")
        if local_app_data:
            candidates += [
                os.path.join(local_app_data, "JianyingPro", "User Data"),
                os.path.join(local_app_data, "CapCut", "User Data"),
            ]
    return [p for p in candidates if os.path.isdir(p)]


def find_jianying_app_resource(*parts: str) -> Optional[str]:
    """已安装剪映的内置资源路径 (安装包 Resources/ 下), 如 ("MixMode", "MixMode.json")。找不到返回 None。"""
    if sys.platform == "darwin":
        roots = ["/Applications/VideoFusion-macOS.app/Contents/Resources",
                 "/Applications/JianyingPro.app/Contents/Resources"]
    else:
        local = os.getenv("LOCALAPPDATA", "")
        roots = sorted(glob.glob(os.path.join(local, "JianyingPro", "Apps", "*", "Resources")), reverse=True)
        roots += sorted(glob.glob("C:/Program Files/JianyingPro/*/Resources"), reverse=True)
    for root in roots:
        path = os.path.join(root, *parts)
        if os.path.exists(path):
            return path
    return None


def find_panel_dbs(user_data_dirs: Optional[Iterable[str]] = None) -> List[str]:
    """找到所有 ressdk rp.db, 按修改时间倒序 (新的优先)。"""
    dbs: List[str] = []
    for ud in user_data_dirs or get_jianying_user_data_dirs():
        root = os.path.join(ud, "Cache", "ressdk_db")
        if not os.path.isdir(root):
            continue
        for sub in os.listdir(root):
            db = os.path.join(root, sub, "rp.db")
            if os.path.isfile(db):
                dbs.append(db)

    def _mtime(p: str) -> float:
        wal = p + "-wal"
        return max(os.path.getmtime(p), os.path.getmtime(wal) if os.path.exists(wal) else 0)

    return sorted(dbs, key=_mtime, reverse=True)


def _iter_http_cache(db_path: str):
    """复制 db(+wal/shm) 到临时目录后只读查询, 避免触碰剪映正在使用的文件。"""
    tmp = tempfile.mkdtemp(prefix="jy_panel_")
    try:
        for suffix in ("", "-wal", "-shm"):
            src = db_path + suffix
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tmp, "rp.db" + suffix))
        con = sqlite3.connect(os.path.join(tmp, "rp.db"))
        try:
            rows = con.execute("SELECT url, response_body FROM http_cache").fetchall()
        except sqlite3.DatabaseError:
            rows = []
        finally:
            con.close()
        for url, body in rows:
            yield url, body
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _load_json(text: str) -> dict:
    try:
        obj = json.loads(text or "{}")
        return obj if isinstance(obj, dict) else {}
    except (TypeError, ValueError):
        return {}


def _is_vip(common_attr: dict) -> bool:
    biz = _load_json((common_attr.get("business_info") or {}).get("json_str"))
    if "is_vip" in biz:
        return bool(biz["is_vip"])
    return bool(_load_json(common_attr.get("extra")).get("is_vip"))


def _build_row(wrapper: dict, category_names: List[str]) -> Optional[dict]:
    ca = wrapper.get("common_attr") or {}
    kind_info = EFFECT_TYPE_KINDS.get(ca.get("effect_type"))
    if not kind_info or not ca.get("title"):
        return None
    kind = kind_info[0]
    resource_id = str(ca.get("third_resource_id_str") or "")
    if resource_id in ("", "0"):
        resource_id = str(ca.get("id") or ca.get("effect_id") or "")
    if not resource_id:
        return None

    sdk_extra = _load_json(ca.get("sdk_extra"))
    extra = _load_json(ca.get("extra"))
    row = {
        "name": ca["title"].strip(),
        "kind": kind,
        "sub_type": "",
        "resource_id": resource_id,
        "effect_id": resource_id,
        "md5": ca.get("md5") or "",
        "is_vip": "1" if _is_vip(ca) else "0",
        "duration_s": "",
        "is_overlap": "",
        "categories": "|".join(sorted({c for c in category_names if c})),
        "params": "",
        "voice_type": "",
    }
    # 面板封面: static_img 为静态 PNG, small 多为动态 GIF (剪映面板展示给所有用户的缩略图)
    cover = ca.get("cover_url") or wrapper.get("cover_url") or {}
    if isinstance(cover, dict):
        small = cover.get("small") or ""
        row["cover_url"] = cover.get("static_img") or small
        row["preview_url"] = small if small and small != row["cover_url"] else ""

    if kind == "sticker_animation":  # 草稿里 resource_id = 面板项 id, third_resource_id = third_resource_id_str
        row["resource_id"] = row["effect_id"] = str(ca.get("id") or resource_id)
        row["params"] = json.dumps({"third_resource_id": resource_id})
    if kind in ("video_animation", "text_animation", "sticker_animation"):
        for c in category_names:
            if c in ANIM_SUB_TYPES:
                row["sub_type"] = ANIM_SUB_TYPES[c]
                break
        dur = (sdk_extra.get("setting") or {}).get("animation_duration")
        if dur:
            row["duration_s"] = str(dur)
    elif kind == "transition":
        tr = sdk_extra.get("transition") or {}
        row["duration_s"] = str(tr.get("defaultDura", 0.5))
        row["is_overlap"] = "1" if tr.get("isOverlap", True) else "0"
    elif kind in ("video_effect", "character_effect"):
        params = (sdk_extra.get("setting") or {}).get("effect_adjust_params") or []
        row["params"] = json.dumps(
            [
                {
                    "name": p.get("effect_key"),
                    "default": p.get("default", 0),
                    "min": p.get("min", 0),
                    "max": p.get("max", 1),
                }
                for p in params
                if p.get("effect_key")
            ],
            ensure_ascii=False,
        )
        if extra.get("effect_duration"):
            row["duration_s"] = str(extra["effect_duration"] / 1000)
    elif kind == "stock_video":
        ms = (wrapper.get("video") or {}).get("duration_ms")
        if ms:
            row["duration_s"] = str(round(float(ms) / 1000, 3))
    elif kind == "sound_effect":
        ms = (wrapper.get("audio_effect") or {}).get("duration_ms")
        if ms:
            row["duration_s"] = str(round(ms / 1000, 3))
    elif kind == "mask_stroke":
        # resource_type 如 mask-stroke-glow; 参数: size/alpha/distance/texture/horizontal_shift/vertical_shift
        setting = sdk_extra.get("setting") or {}
        row["sub_type"] = extra.get("resource_type", "")
        row["params"] = json.dumps({
            "params": {p["effect_key"].replace("effects_adjust_", ""): p.get("default", 0)
                       for p in setting.get("effect_adjust_params") or [] if p.get("effect_key")},
            "color": setting.get("matting_stroke_adjust_color"),
        }, ensure_ascii=False)
    elif kind.startswith("beauty_"):
        # 草稿里 resource_id = 面板项 id, third_resource_id = third_resource_id_str (剪映 11.7 实测)
        row["resource_id"] = row["effect_id"] = str(ca.get("id") or resource_id)
        info = {k: extra[k] for k in ("loki_resource_id", "effect_type", "slider_minValue", "slider_maxValue",
                                      "slider_defaultValue", "vip_name", "intensity_key") if k in extra}
        info["third_resource_id"] = resource_id
        try:  # 美妆互斥组, 如套装 = 眉毛/眼影/.../口红 共 11 个部位
            groups = json.loads(extra["exclusion_group"]) if isinstance(extra.get("exclusion_group"), str) else None
        except ValueError:
            groups = None
        if groups:
            info["exclusion_group"] = groups
        items = (_load_json(extra.get("beautify")).get("items") or []) if extra.get("beautify") else []
        if items and isinstance(items[0], dict):  # 美妆浓度: tag + 默认值
            info["beautify_tag"], info["beautify_default"] = items[0].get("tag"), items[0].get("value")
        row["params"] = json.dumps(info, ensure_ascii=False)
    elif kind == "curve_speed":
        row["params"] = json.dumps(_load_json(extra.get("speed_points")).get("speed_points") or [])
    elif kind == "keyframe_curve":
        row["params"] = json.dumps(_load_json(extra.get("keyframeGraph")).get("keyframeGraph") or [])
    elif kind == "text_glow":
        row["params"] = json.dumps({p["effect_key"].replace("bloom_adjust_", ""): p.get("default", 0)
                                    for p in (sdk_extra.get("setting") or {}).get("bloom_adjust_params") or []
                                    if p.get("effect_key")})
    elif kind == "text_bubble":
        row["sub_type"] = "loki" if ca.get("effect_type") == 49 else ""
        if extra.get("loki_resource_id"):
            row["params"] = json.dumps({"loki_resource_id": str(extra["loki_resource_id"])})
    elif kind == "mask":
        # 形状类型: line/mirror/circle/rectangle/heart/pentagram/heterosexual(异形)/custom/contour/text
        row["sub_type"] = extra.get("complex_video_mask", "")
    elif kind == "tts_voice":
        tone = _load_json(extra.get("tonetype"))
        row["voice_type"] = tone.get("voice_type") or (wrapper.get("timbre") or {}).get(
            "voice_type", ""
        )
    return row


def extract_panel_rows(db_paths: Iterable[str]) -> Dict[str, List[dict]]:
    """解析 http_cache, 返回 {csv文件名: [row, ...]} (按 kind+sub_type+resource_id 去重)。"""
    merged: Dict[Tuple[str, str, str], dict] = {}
    pairs = [(url, body) for db in db_paths for url, body in _iter_http_cache(db)]
    bodies = [body for _url, body in pairs]
    # 美颜美体: 面板分组的 key 只出现在请求 URL 里 (..._category_id_<hash>_<key>_jianyingpro...), 草稿的 category_id 用它
    beauty_keys: Dict[str, str] = {}
    for url, body in pairs:
        m = re.search(r"category_id_[0-9A-Fa-f]+_([a-z0-9_\-]+?)_jianyingpro", url or "")
        if not m or m.group(1).startswith("auto-beauty") or '"effect_type"' not in body:
            continue
        for item in re.finditer(r'"common_attr":\{"id":"(\d+)"', body):
            beauty_keys.setdefault(item.group(1), m.group(1))

    # 第一遍: 汇总所有响应中的分类 id -> 名称 (分类列表与素材列表常在不同的缓存响应里)
    global_cats: Dict[str, str] = {}

    def _collect(o):
        if isinstance(o, dict):
            if "category_id" in o and o.get("category_name"):
                global_cats[str(o["category_id"])] = o["category_name"]
            for v in o.values():
                _collect(v)
        elif isinstance(o, list):
            for v in o:
                _collect(v)

    for body in bodies:
        if '"category_name"' in body:
            _collect(_load_json(body))

    for body in bodies:
        data = _load_json(body).get("data")
        if not isinstance(data, dict):
            continue
        cat_names = {
            str(c.get("category_id")): c.get("category_name", "")
            for c in data.get("categories") or []
            if isinstance(c, dict)
        }
        blocks: List[Tuple[str, dict]] = []
        for cid, blk in (data.get("category_resources") or {}).items():
            if isinstance(blk, dict):
                blocks.append((cat_names.get(str(cid), ""), blk))
        if isinstance(data.get("effect_item_list"), list):
            blocks.append(("", data))

        for cat_name, blk in blocks:
            for wrapper in blk.get("effect_item_list") or []:
                if not isinstance(wrapper, dict):
                    continue
                names = [cat_name] if cat_name else []
                for cid in (wrapper.get("common_attr") or {}).get("category_ids") or []:
                    if str(cid) in global_cats:
                        names.append(global_cats[str(cid)])
                row = _build_row(wrapper, names)
                if not row:
                    continue
                key = (row["kind"], row["sub_type"], row["resource_id"])
                old = merged.get(key)
                if old is None:
                    merged[key] = row
                    continue
                for k in COVER_FIELDS:
                    if row.get(k) and not old.get(k):
                        old[k] = row[k]
                if row["categories"]:
                    cats = set(filter(None, old["categories"].split("|")))
                    cats.update(row["categories"].split("|"))
                    old["categories"] = "|".join(sorted(cats))

    for (kind, _sub, rid), row in merged.items():
        if kind.startswith("beauty_") and rid in beauty_keys:
            info = json.loads(row.get("params") or "{}")
            info["category_key"] = beauty_keys[rid]
            row["params"] = json.dumps(info, ensure_ascii=False)

    # 动画: 同一 resource_id 若已有明确子类型, 丢弃子类型为空的重复项
    typed = {(k[0], k[2]) for k in merged if k[1]}
    out: Dict[str, List[dict]] = {}
    for (kind, sub, rid), row in merged.items():
        if not sub and kind in ("video_animation", "text_animation", "sticker_animation") and (kind, rid) in typed:
            continue
        fname = next(v[1] for v in EFFECT_TYPE_KINDS.values() if v[0] == kind)
        out.setdefault(fname, []).append(row)
    for rows in out.values():
        rows.sort(key=lambda r: (r["sub_type"], r["is_vip"], r["name"]))
    return out


def write_panel_csvs(rows_by_file: Dict[str, List[dict]], data_dir: Optional[str] = None) -> Dict[str, int]:
    data_dir = data_dir or DATA_DIR
    os.makedirs(data_dir, exist_ok=True)
    counts: Dict[str, int] = {}
    for fname, rows in rows_by_file.items():
        path = os.path.join(data_dir, fname)
        # 增量合并: 保留旧索引中已有、但本次缓存里没有的条目 (剪映可能清理旧缓存)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", newline="") as f:
                old = list(csv.DictReader(line for line in f if not line.startswith("#")))
            fresh = {(r["kind"], r["sub_type"], r["resource_id"]) for r in rows}
            old_by_key = {(o.get("kind"), o.get("sub_type"), o.get("resource_id")): o for o in old}
            for r in rows:  # 本次缓存没带封面时沿用旧索引里的
                o = old_by_key.get((r["kind"], r["sub_type"], r["resource_id"])) or {}
                for k in COVER_FIELDS:
                    if not r.get(k) and o.get(k):
                        r[k] = o[k]
            kept = [{k: o.get(k, "") or "" for k in CSV_FIELDS} for o in old
                    if (o.get("kind"), o.get("sub_type"), o.get("resource_id")) not in fresh]
            rows = rows + kept
            rows.sort(key=lambda r: (r["sub_type"], r["is_vip"], r["name"]))
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(f"# JianYing 11.x Panel Assets ({fname[6:-4]}) - generated by sync_jy_panel.py\n")
            f.write("# is_vip=1 means JianYing membership is required to export drafts using it.\n")
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        counts[fname] = len(rows)
    _CATALOG_CACHE.clear()
    _VIP_INDEX.clear()
    return counts


SYNC_STAMP = ".panel_sync_stamp"


def _panel_cache_mtime(dbs: Optional[Iterable[str]] = None) -> float:
    return max((os.path.getmtime(d) for d in (dbs or find_panel_dbs()) if os.path.exists(d)), default=0.0)


def write_sync_stamp(dbs: Optional[Iterable[str]] = None, data_dir: Optional[str] = None) -> None:
    """记录本次同步时剪映面板缓存的修改时间。"""
    with open(os.path.join(data_dir or DATA_DIR, SYNC_STAMP), "w", encoding="utf-8") as f:
        f.write(str(_panel_cache_mtime(dbs)))


def panel_index_stale(data_dir: Optional[str] = None) -> bool:
    """剪映面板缓存在上次同步后又有更新 (用户在剪映里浏览过面板) 时返回 True。"""
    latest = _panel_cache_mtime()
    if not latest:
        return False
    try:
        with open(os.path.join(data_dir or DATA_DIR, SYNC_STAMP), encoding="utf-8") as f:
            return latest > float(f.read().strip() or 0)
    except (OSError, ValueError):
        return True


def ensure_panel_fresh(verbose: bool = True) -> bool:
    """缓存有更新时自动同步面板索引 (只读剪映缓存)。设置 JY_AUTO_SYNC=0 可关闭。返回是否同步了。"""
    if os.getenv("JY_AUTO_SYNC", "1") == "0" or not panel_index_stale():
        return False
    import subprocess

    if verbose:
        print("ℹ️ 剪映面板缓存有更新, 自动同步素材索引 (约 15 秒) ...", flush=True)
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sync_jy_panel.py")
    ok = subprocess.call([sys.executable, script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    _CATALOG_CACHE.clear()
    _VIP_INDEX.clear()
    if verbose and not ok:
        print("⚠️ 自动同步失败, 可手动运行 jy_wrapper.py sync --only panel")
    return ok


def cover_expired(url: str, now: Optional[float] = None) -> bool:
    """签名封面链接的 x-expires 已过期 (或 1 天内过期)。"""
    from urllib.parse import parse_qs, urlparse

    exp = parse_qs(urlparse(url).query).get("x-expires", [""])[0]
    return exp.isdigit() and int(exp) < (now or time.time()) + 86400


# ---------------------------------------------------------------------------
# 2. 加载与查询 (Catalog)
# ---------------------------------------------------------------------------

_CATALOG_CACHE: Dict[str, List[dict]] = {}


def load_panel_rows(fname: str, data_dir: Optional[str] = None) -> List[dict]:
    data_dir = data_dir or DATA_DIR
    cache_key = os.path.join(data_dir, fname)
    if cache_key in _CATALOG_CACHE:
        return _CATALOG_CACHE[cache_key]
    rows: List[dict] = []
    if os.path.exists(cache_key):
        with open(cache_key, "r", encoding="utf-8", newline="") as f:
            lines = [line for line in f if not line.startswith("#")]
        rows = list(csv.DictReader(lines))
    _CATALOG_CACHE[cache_key] = rows
    return rows


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-·]+", "", (s or "")).lower()


def find_panel_asset(
    fname: str, name: str, sub_type: Optional[str] = None, data_dir: Optional[str] = None
) -> Optional[dict]:
    """按名称精确匹配 (忽略空格/下划线/大小写), 其次匹配 resource_id。"""
    if not name:
        return None
    target = _norm(str(name))
    rows = [
        r for r in load_panel_rows(fname, data_dir) if not sub_type or r.get("sub_type") == sub_type
    ]
    for r in rows:
        if _norm(r["name"]) == target:
            return r
    for r in rows:
        if str(name) == r.get("resource_id"):
            return r
    return None


def find_tts_voice(speaker_id: str, data_dir: Optional[str] = None) -> Optional[dict]:
    data_dir = data_dir or DATA_DIR
    for r in load_panel_rows("panel_tts_voices.csv", data_dir):
        if r.get("voice_type") == speaker_id:
            return r
    return None


# ---------------------------------------------------------------------------
# 3. 动态注册枚举 (Enum registration)
# ---------------------------------------------------------------------------


def _member_key(enum_cls, name: str) -> str:
    key = re.sub(r"\W", "_", name.strip())
    if not key or key[0].isdigit():
        key = "_" + key
    base, i = key, 2
    while key in enum_cls.__members__ or key in enum_cls.__dict__:
        key = f"{base}_{i}"
        i += 1
    return key


def _extend_enum(enum_cls, name: str, value):
    """向已定义的 Enum 追加成员, 使 isinstance 校验与 __members__ 查询均可用。"""
    for member in enum_cls.__members__.values():
        if getattr(member.value, "resource_id", None) == value.resource_id:
            return member
    key = _member_key(enum_cls, name)
    member = object.__new__(enum_cls)
    member._name_ = key
    member._value_ = value
    member._sort_order_ = len(enum_cls._member_names_)
    type.__setattr__(enum_cls, key, member)
    enum_cls._member_map_[key] = member
    enum_cls._member_names_.append(key)
    try:
        enum_cls._value2member_map_[value] = member
    except TypeError:
        pass
    return member


def _enum_targets(draft) -> Dict[object, Tuple[str, Optional[str]]]:
    return {
        draft.TransitionType: ("panel_transitions.csv", None),
        draft.VideoSceneEffectType: ("panel_video_effects.csv", None),
        draft.VideoCharacterEffectType: ("panel_character_effects.csv", None),
        draft.IntroType: ("panel_video_animations.csv", "intro"),
        draft.OutroType: ("panel_video_animations.csv", "outro"),
        draft.GroupAnimationType: ("panel_video_animations.csv", "group"),
        draft.TextIntro: ("panel_text_animations.csv", "intro"),
        draft.TextOutro: ("panel_text_animations.csv", "outro"),
        draft.TextLoopAnim: ("panel_text_animations.csv", "loop"),
        draft.FontType: ("panel_fonts.csv", None),
        draft.MaskType: ("panel_masks.csv", None),
        draft.FilterType: ("panel_filters.csv", None),
    }


def _row_to_meta(enum_cls, row: dict, draft):
    from pyJianYingDraft.metadata.effect_meta import (
        AnimationMeta,
        EffectMeta,
        EffectParam,
        MaskMeta,
        TransitionMeta,
    )

    is_vip = row.get("is_vip") == "1"
    rid, eid, md5 = row["resource_id"], row.get("effect_id") or row["resource_id"], row.get("md5", "")
    if enum_cls is draft.TransitionType:
        return TransitionMeta(
            row["name"], is_vip, rid, eid, md5,
            float(row.get("duration_s") or 0.5), row.get("is_overlap", "1") != "0",
        )
    if enum_cls is draft.MaskType:
        if row.get("sub_type") in ("custom", "contour", "text"):
            raise PanelAssetUnavailable(
                f"蒙版「{row['name']}」需要手绘路径或文字内容，暂不支持自动添加，请在剪映中手动设置。"
            )
        local = find_local_effect_resource(md5)
        if not local:
            raise PanelAssetUnavailable(
                f"蒙版「{row['name']}」的资源还没下载到本地。请在剪映的蒙版面板中点一次「{row['name']}」，然后重试。"
            )
        meta = MaskMeta(row["name"], row.get("sub_type") or "heterosexual", rid, local[0], md5,
                        _svg_aspect_ratio(local[1]) or 1.0)
        meta.path = local[1].rstrip("/") + "/"
        meta.is_old_version = False
        meta.is_vip = is_vip
        return meta
    if enum_cls in (draft.VideoSceneEffectType, draft.VideoCharacterEffectType, draft.FontType,
                    draft.FilterType):
        params = []
        for p in json.loads(row.get("params") or "[]"):
            params.append(EffectParam(p["name"], float(p["default"]), float(p["min"]), float(p["max"])))
        return EffectMeta(row["name"], is_vip, rid, eid, md5, params)
    # 动画: 组合动画默认铺满片段, 时长仅作占位
    return AnimationMeta(row["name"], is_vip, float(row.get("duration_s") or 0.5), rid, eid, md5)


def resolve_panel_member(enum_cls, name: str, draft=None, data_dir: Optional[str] = None):
    """在面板索引中查找 name, 找到则注册为 enum_cls 的成员并返回, 否则返回 None。"""
    data_dir = data_dir or DATA_DIR
    if draft is None:
        import pyJianYingDraft as draft  # noqa: F811
    target = _enum_targets(draft).get(enum_cls)
    if not target:
        return None
    row = find_panel_asset(target[0], name, target[1], data_dir)
    if not row:
        return None
    return _extend_enum(enum_cls, row["name"], _row_to_meta(enum_cls, row, draft))


_VIP_INDEX: Dict[str, Dict[str, str]] = {}


def _vip_index(data_dir: Optional[str] = None) -> Dict[str, str]:
    """resource_id -> 名称, 汇总内置枚举与面板索引中的 VIP 素材。"""
    data_dir = data_dir or DATA_DIR
    if data_dir in _VIP_INDEX:
        return _VIP_INDEX[data_dir]
    import enum

    import pyJianYingDraft as draft

    index: Dict[str, str] = {}
    for attr in dir(draft):
        enum_cls = getattr(draft, attr)
        if isinstance(enum_cls, type) and issubclass(enum_cls, enum.Enum):
            for member in enum_cls.__members__.values():
                if is_vip_member(member) and hasattr(member.value, "resource_id"):
                    index[str(member.value.resource_id)] = member_display_name(member)
    for fname in PANEL_CSV_FILES:
        for r in load_panel_rows(fname, data_dir):
            if r.get("is_vip") == "1" and r.get("resource_id"):
                index.setdefault(r["resource_id"], r["name"])
    _VIP_INDEX[data_dir] = index
    return index


def collect_vip_usage(content, data_dir: Optional[str] = None) -> List[dict]:
    """扫描草稿 JSON (dict 或 str) 中所有 resource_id, 返回命中的 VIP 素材。"""
    data_dir = data_dir or DATA_DIR
    if isinstance(content, str):
        content = _load_json(content)
    index = _vip_index(data_dir)
    found: List[dict] = []
    seen = set()

    def walk(o):
        if isinstance(o, dict):
            rid = o.get("resource_id")
            if rid and str(rid) in index and (str(rid), o.get("id")) not in seen:
                seen.add((str(rid), o.get("id")))
                found.append({"name": index[str(rid)], "resource_id": str(rid), "type": o.get("type", "")})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(content)
    return found


def is_vip_member(member) -> bool:
    return bool(getattr(getattr(member, "value", None), "is_vip", False))


def member_display_name(member) -> str:
    v = getattr(member, "value", None)
    return getattr(v, "name", None) or getattr(v, "title", None) or getattr(member, "name", "")
