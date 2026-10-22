"""
剪映版本兼容性审计：检查 skill 生成的草稿格式是否落后于本机安装的剪映。

原理（只读取剪映安装包内随软件发布的明文 JS，不涉及加密草稿）：
1. 迁移检查：剪映内置的草稿迁移步骤 (migrateInner, 每步带 version)。
   skill 草稿的 new_version 之后的每一步，都代表一次格式变化；
   若该步涉及 skill 会写入的素材类型，就可能需要适配（例如 125.0.0 的 masks -> common_mask）。
2. 字段检查：剪映的素材数据模型 (common:[字段...])。把 skill 生成草稿中每类素材的字段
   与最相近的模型比对，列出缺失或多余的字段。

用法:
    python tools/audit_jianying_compat.py                  # 生成样例草稿并审计
    python tools/audit_jianying_compat.py --draft <draft_info.json>
    python tools/audit_jianying_compat.py --json
"""

import argparse
import glob
import json
import os
import re
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

APP_JS_CANDIDATES = [
    "/Applications/VideoFusion-macOS.app/Contents/Resources/canvas_agent/app/static/js",
    "/Applications/JianyingPro.app/Contents/Resources/canvas_agent/app/static/js",
]


# 已人工审阅过的迁移步骤 (剪映 11.6.0, 2026-10-20)。新版剪映出现不在此表中的版本时才会被高亮。
REVIEWED_MIGRATIONS = {
    "114.0.0": "数字人 has_real_scene，skill 不涉及",
    "116.0.0": "空迁移",
    "118.0.0": "AI 音乐 ai_music_type，skill 不涉及",
    "122.0.0": "videos.check_flag += 0x1C00000 已适配；美颜 item_effect_type，skill 不涉及",
    "123.0.0": "videos.check_flag += 0x2000000 已适配；抠像描边、AI 人声，skill 不涉及",
    "125.0.0": "masks -> common_mask 已适配",
    "126.0.0": "倒放 video_algorithm，skill 不涉及",
    "127.0.0": "蒙版羽化改为平方存储，已适配",
}


# 剪映打开 skill 草稿后会自动改写的 videos.check_flag (skill 写入值 -> 剪映保存值), 已在剪映内验证不影响使用
KNOWN_CHECK_FLAG_REWRITES = {
    0x780F7FF: "剪映 11.7 (草稿 127~141.0.0) 打开时把 0x3c0f7ff 改写为此值 (+0x4000000, -0x400000); 草稿功能均正常, skill 不跟进",
}
# 已知的标记位 (实测): 加在 skill 写入值上的功能开关
KNOWN_CHECK_FLAG_BITS = {0x80000: "手动美体", 0x20000: "智能打光"}


def _ver(v: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v)[:3])


def find_app_js_dir(explicit: Optional[str] = None) -> Optional[str]:
    cands = [explicit] if explicit else list(APP_JS_CANDIDATES)
    local = os.getenv("LOCALAPPDATA")
    if local:
        cands += glob.glob(os.path.join(local, "JianyingPro", "Apps", "*", "Resources", "canvas_agent", "app", "static", "js"))
    return next((c for c in cands if c and os.path.isdir(c)), None)


def _iter_js(js_dir: str):
    for f in glob.glob(os.path.join(js_dir, "**", "*.js"), recursive=True):
        with open(f, "r", encoding="utf-8", errors="ignore") as fh:
            yield f, fh.read()


def extract_migrations(js_dir: str) -> List[dict]:
    """返回 [{version, keys}]，keys 为迁移代码中涉及的 materials 字段/素材名。"""
    seen, out = set(), []
    for _f, s in _iter_js(js_dir):
        for m in re.finditer(r'\.version="(\d+\.\d+\.\d+)"', s):
            mi = s.find("migrateInner=function", m.end())
            if mi < 0 or mi - m.end() > 400:
                continue
            nxt = s.find('.version="', mi)
            body = s[mi: nxt if 0 < nxt < mi + 8000 else mi + 8000]
            key = (m.group(1), body[:200])
            if key in seen:
                continue
            seen.add(key)
            touched = sorted(set(re.findall(r"materials(?:\.|,\s*\w+=\w+\.)([a-z_]+)", body))
                             | set(re.findall(r"\b\w\.(masks|common_mask|adjust_masks|videos|audios|effects|texts|"
                                              r"transitions|digital_humans|canvases|stickers|speeds|"
                                              r"material_animations|audio_effects|filters)\b", body)))
            props = sorted(set(re.findall(r'"(KFType\w+)"', body)))
            noop = bool(re.match(r"migrateInner=function\(\w\)\{return\(0,\w\.\w+\)\(\w\)\}", body))
            out.append({"version": m.group(1), "touches": touched, "keyframes": props[:6],
                        "noop": noop, "snippet": body[:220]})
    out.sort(key=lambda x: _ver(x["version"]))
    return out


def extract_models(js_dir: str) -> Dict[str, List[str]]:
    """模型定义形如: d={common:[字段...],object:{...}}, ... h=function(){... e.type=X.MetaTypeY ...}
    对每个 MetaType 赋值, 取其前方最近的一个字段列表。"""
    models: Dict[str, List[str]] = {}
    list_pat = re.compile(r'\{common:\[((?:"[a-zA-Z0-9_]+",?)+)\](?:,object:\{)?')
    type_pat = re.compile(r"\be\.type=[\w$]+\.[\w$]+\.(MetaType\w+)")
    for _f, s in _iter_js(js_dir):
        lists = [(m.start(), m.end(), m.group(1)) for m in list_pat.finditer(s)]
        if not lists:
            continue
        for t in type_pat.finditer(s):
            prev = [lst for lst in lists if lst[1] <= t.start() and t.start() - lst[1] < 1200]
            if not prev or t.group(1) in models:
                continue
            l_start, l_end, raw = prev[-1]
            fields = re.findall(r'"([a-zA-Z0-9_]+)"', raw)
            obj = s[l_end: l_end + 1500].split("}}},", 1)[0]
            fields += re.findall(r"([a-z0-9_]+):\{type:", obj)
            models[t.group(1)] = sorted(set(fields))
    return models


# 草稿 materials 键 -> 剪映模型 (已确认对应关系); 其余按字段相似度猜测, 结果仅供参考
KNOWN_MODEL_MAP = {
    "transitions": "MetaTypeTransition",
    "speeds": "MetaTypeSpeed",
    "canvases": "MetaTypeCanvasColor",
    "common_mask": "MetaTypeVideoMask",
    "audio_fades": "MetaTypeAudioFade",
    "audio_effects": "MetaTypeAudioEffect",
    "stickers": "MetaTypeSticker",
    "images": "MetaTypeImage",
    "text_templates": "MetaTypeTextTemplate",
    "placeholders": "MetaTypePlaceholderInfo",
    "vocal_separations": "MetaTypeVocalSeparation",
    "audio_track_indexes": "MetaTypeAudioTrackIndex",
}


def build_sample_draft(out_dir: str) -> str:
    """用 skill 的主要 API 生成一个样例草稿，返回 draft_info.json 路径。"""
    from jy_wrapper import JyProject  # noqa: E402

    v = os.path.join(ROOT, "assets", "video.mp4")
    a = os.path.join(ROOT, "assets", "audio.mp3")
    p = JyProject("Compat_Audit_Sample", drafts_root=out_dir, overwrite=True)
    s1 = p.add_media_safe(v, "0s", "3s", "Main")
    s2 = p.add_media_safe(v, "3s", "3s", "Main")
    p.add_media_safe(a, "0s", "3s", "BGM")
    p.add_transition_simple("叠化", s1)
    p.add_filter_simple(s1, "_1980")
    p.add_segment_effect(s2, "模糊")
    p.add_video_animation(s1, "渐显", "intro")
    p.add_ken_burns(s2)
    p.set_background_fill(s1, "blur")
    ov = p.add_overlay(v, "0s", "2s")
    p.add_mask_simple(ov, "圆形")
    p.add_text_simple("audit", "0s", "2s", anim_in="渐显")
    p.add_effect_simple("模糊", "0s", "1s")
    p.save()
    return os.path.join(out_dir, "Compat_Audit_Sample", "draft_info.json")


def audit(draft_path: str, js_dir: str) -> dict:
    with open(draft_path, "r", encoding="utf-8") as f:
        content = json.load(f)
    draft_ver = content.get("new_version", "0.0.0")
    used = {k for k, v in content.get("materials", {}).items() if v}

    migrations = [m for m in extract_migrations(js_dir) if _ver(m["version"]) > _ver(draft_ver)]
    for m in migrations:
        m["reviewed"] = REVIEWED_MIGRATIONS.get(m["version"], "")
        m["relevant"] = bool(set(m["touches"]) & used) and not m["noop"] and not m["reviewed"]

    models = extract_models(js_dir)
    field_report = []
    for key in sorted(used):
        items = content["materials"][key]
        if not isinstance(items, list) or not isinstance(items[0], dict):
            continue
        ours = set(items[0].keys())
        best, score, confirmed = None, 0.0, False
        if KNOWN_MODEL_MAP.get(key) in models:
            best, confirmed = KNOWN_MODEL_MAP[key], True
            fs = set(models[best])
            score = len(ours & fs) / max(1, len(ours | fs))
        else:
            for name, fields in models.items():
                fs = set(fields)
                j = len(ours & fs) / max(1, len(ours | fs))
                if j > score:
                    best, score = name, j
        if best and (confirmed or score >= 0.6):
            fs = set(models[best])
            field_report.append({
                "material": key, "model": best, "similarity": round(score, 2), "confirmed": confirmed,
                "missing": sorted(fs - ours), "extra": sorted(ours - fs),
            })
    return {"draft_version": draft_ver, "materials_used": sorted(used),
            "migrations_after_draft_version": migrations, "fields": field_report,
            "models_known": len(models), "app_written": audit_app_written(content)}


def audit_app_written(skill_content: dict, limit: int = 12) -> dict:
    """对比剪映最近保存的明文草稿 (template.json) 与 skill 写入的 videos.check_flag 和草稿版本。"""
    import collections

    from utils.panel_assets import get_jianying_user_data_dirs

    files = []
    for ud in get_jianying_user_data_dirs():
        root = os.path.join(ud, "Projects", "com.lveditor.draft")
        files += glob.glob(os.path.join(root, "*", "template.json"))
        files += glob.glob(os.path.join(root, "*", "Timelines", "*", "template.json"))
    files = sorted(set(files), key=os.path.getmtime, reverse=True)[:limit]
    skill_flags = {v.get("check_flag") for v in skill_content.get("materials", {}).get("videos", [])}
    skill_base = min(skill_flags) if skill_flags else 0
    seen, versions = collections.Counter(), collections.Counter()
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        versions[(d.get("new_version", ""), (d.get("last_modified_platform") or {}).get("app_version", ""))] += 1
        for v in d.get("materials", {}).get("videos", []):
            flag = v.get("check_flag")
            if isinstance(flag, int):
                for bit in KNOWN_CHECK_FLAG_BITS:  # 去掉已知的功能开关位, 只比较基础值
                    flag &= ~bit
                seen[flag] += 1
    unknown = {hex(k): n for k, n in seen.items() if k != skill_base and k not in KNOWN_CHECK_FLAG_REWRITES}
    return {"skill_check_flag": hex(skill_base), "app_check_flags": {hex(k): n for k, n in seen.items()},
            "unknown_check_flags": unknown, "app_versions": [f"{a} / {b}" for (a, b) in versions],
            "files": len(files)}


def print_report(r: dict) -> None:
    print(f"skill 草稿版本 new_version = {r['draft_version']}")
    print(f"草稿用到的素材类型: {', '.join(r['materials_used'])}\n")
    print("== 1. 之后的剪映迁移步骤（★ = 新的、涉及 skill 用到的素材，需要关注；✓ = 已审阅）")
    shown = set()
    for m in r["migrations_after_draft_version"]:
        if m["reviewed"]:
            if m["version"] not in shown:
                print(f" ✓ {m['version']:<9} {m['reviewed']}")
                shown.add(m["version"])
            continue
        flag = "★" if m["relevant"] else ("·" if not m["noop"] else " ")
        extra = f"  关键帧: {', '.join(m['keyframes'])}" if m["keyframes"] else ""
        print(f" {flag} {m['version']:<9} 涉及: {', '.join(m['touches']) or '-'}{extra}")
    new = [m for m in r["migrations_after_draft_version"] if m["relevant"]]
    print(f"\n   需要关注的新迁移: {len(new)} 个" + ("（全部已审阅）" if not new else ""))
    print(f"\n== 2. 素材字段比对（已识别剪映模型 {r['models_known']} 个，按相似度匹配）")
    for f in r["fields"]:
        how = "已确认对应" if f["confirmed"] else f"按相似度猜测 {f['similarity']}"
        print(f" - {f['material']} -> {f['model']} ({how})")
        if f["missing"]:
            print(f"     缺少: {', '.join(f['missing'])}")
        if f["extra"]:
            print(f"     多出: {', '.join(f['extra'])}")
    a = r.get("app_written") or {}
    if a.get("files"):
        print(f"\n== 3. 剪映实际写入值（最近保存的 {a['files']} 份明文草稿）")
        print(f"   草稿版本 / 剪映版本: {', '.join(a['app_versions'])}")
        print(f"   videos.check_flag: skill 写 {a['skill_check_flag']}; 剪映保存的: {a['app_check_flags']}")
        for k in a["app_check_flags"]:
            note = KNOWN_CHECK_FLAG_REWRITES.get(int(k, 16))
            if note:
                print(f"   ✓ {k}: {note}")
        if a["unknown_check_flags"]:
            print(f"   ★ 新出现的 check_flag: {a['unknown_check_flags']} —— 剪映可能新增了迁移, 请在剪映里验证 skill 草稿")
        else:
            print("   需要关注的新 check_flag: 0 个")


def main() -> int:
    ap = argparse.ArgumentParser(description="剪映版本兼容性审计")
    ap.add_argument("--draft", help="要审计的 draft_info.json (默认自动生成样例草稿)")
    ap.add_argument("--app-js", help="剪映安装包内 canvas_agent/app/static/js 目录")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    js_dir = find_app_js_dir(args.app_js)
    if not js_dir:
        print("未找到剪映安装包内的 canvas_agent JS 目录，请用 --app-js 指定。")
        return 1
    draft_path = args.draft or build_sample_draft(tempfile.mkdtemp(prefix="jy_audit_"))
    report = audit(draft_path, js_dir)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
