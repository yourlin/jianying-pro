"""
构建音效索引 data/sfx_library.csv: 把剪映云端音效 (data/cloud_sound_effects.csv) 和本地音效目录
按「用途」分类成 whoosh / pop / click / hit / riser / ding / typing / camera ... , 供 sfx_designer.py 按事件挑音效。

剪映云端音效可以按 effect_id 引用: CloudManager.download_asset(effect_id) 下载到本地缓存后作为普通音频放进草稿
(会员音效 is_vip=1 不下载, 标记为不可用)。本地音效 (自己的 wav/mp3 目录) 直接用文件路径。

用法:
    python scripts/build_sfx_library.py                          # 由云端音效索引重建 data/sfx_library.csv
    python scripts/build_sfx_library.py --local-dir ~/SFX        # 同时收录本地音效目录 (按文件名分类)
    python scripts/build_sfx_library.py --json                   # 机器可读输出 (各分类数量)
    python scripts/build_sfx_library.py --validate               # 只校验现有索引的表头 / 取值
"""

import argparse
import csv
import os
import re
import subprocess
import sys
from collections import Counter
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.cli_protocol import emit_result, make_result  # noqa: E402

DATA_DIR = os.path.join(ROOT, "data")
CLOUD_SFX = os.path.join(DATA_DIR, "cloud_sound_effects.csv")
LIBRARY = os.path.join(DATA_DIR, "sfx_library.csv")

FIELDS = ["id", "name", "category", "duration_s", "source", "is_vip", "available", "path", "tags"]
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".aiff", ".aif"}

# 分类 -> 中文说明 (也用于 --stats 输出)
CATEGORIES: Dict[str, str] = {
    "whoosh": "嗖 / 呼 / 唰: 转场、快速移动 (含 swoosh / swish)",
    "pop": "啵 / 弹出 / 气泡: 文字、贴纸、元素出现",
    "click": "鼠标 / 按键 / 响指: 点击、切换、列表项",
    "hit": "砰 / 咚 / 重音 / 撞击: 强调、开场、卡点重拍",
    "riser": "渐升 / 蓄力: 揭晓、高潮前、结尾前铺垫",
    "ding": "叮 / 提示音: 要点、完成、结尾",
    "typing": "打字 / 键盘: 打字机文字、搜索框",
    "camera": "快门 / 拍照: 定格、拍照卡点、截图",
    "boom": "爆炸 / 低频冲击: 大反转、震撼开场",
    "glitch": "电流 / 雪花 / 故障: 故障转场、科技感",
    "page": "翻书 / 翻页: 章节切换、图文翻页",
    "cash": "收银 / 硬币 / 数钱: 价格、收益、数字",
    "error": "错误 / 哔: 错误示范、打叉、消音",
    "magic": "仙尘 / 闪烁 / 星闪: 变身、亮点、美化",
    "laugh": "笑声: 综艺效果",
    "applause": "欢呼 / 鼓掌: 成就、结尾",
    "countdown": "倒计时 / 时钟: 倒数、紧张感",
}

# 关键词规则, 按顺序匹配 (先匹配到的分类生效; 越具体越靠前)
KEYWORD_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("typing", ("打字", "键盘", "typing", "keyboard", "typewriter", "空格键")),
    ("camera", ("拍照", "快门", "拍立得", "宝丽来", "对焦", "camera", "shutter", "胶卷")),
    ("countdown", ("倒计时", "倒数", "countdown", "嘀嗒", "滴答", "时钟", "秒钟", "tick")),
    ("cash", ("收银", "硬币", "数钱", "点钞", "cash", "coin")),
    ("page", ("翻书", "翻页", "page", "paper")),
    ("glitch", ("电流", "雪花", "没信号", "干扰", "故障", "glitch", "static")),
    ("error", ("错误", "error", "屏蔽脏话", "哔", "beep", "wrong")),
    ("riser", ("渐升", "渐进", "上升", "蓄力", "riser", "rise", "uplifter", "build")),
    ("boom", ("爆炸", "boom", "explosion", "低音波", "炮")),
    ("click", ("鼠标", "点击", "click", "响指", "snap", "按钮", "按键", "订书机")),
    ("applause", ("鼓掌", "欢呼", "applause", "cheering", "victory")),
    ("laugh", ("笑声", "laugh", "哈哈")),
    (
        "whoosh",
        (
            "嗖",
            "呼",
            "唰",
            "咻",
            "woosh",
            "whoosh",
            "swoosh",
            "swish",
            "风声",
            "旋风",
            "划过",
            "穿梭",
            "快速移动",
            "转场音",
            "换镜头",
            "闪回",
        ),
    ),
    ("ding", ("叮", "ding", "提示音", "提示声", "铃", "bell", "chime", "叮咚", "滴，", "嘟咚")),
    ("pop", ("啵", "弹出", "气泡", "pop", "bubble", "弹跳", "弹簧", "出现音效", "弹窗", "泡泡")),
    (
        "hit",
        (
            "砰",
            "咚",
            "重音",
            "重击",
            "撞击",
            "碰撞",
            "拳",
            "打击",
            "hit",
            "impact",
            "punch",
            "嘭",
            "噔",
            "哐",
            "鼓",
            "gakeen",
            "木鱼",
        ),
    ),
    ("magic", ("仙尘", "闪烁", "星闪", "魔法", "bling", "sparkle", "magic", "仙女")),
]


# 名称含这些词的不是短音效 (背景乐 / 环境底噪 / 动物声), 不收录
EXCLUDE_WORDS = ("bgm", "配乐", "背景音乐", "背景音", "呼噜", "呼吸", "叹气", "marimba")


def _strip_catalog_suffix(title: str) -> str:
    """去掉剪映标题里的版权库编号, 如 'Gentle pop(1040887)' -> 'Gentle pop'。"""
    return re.sub(r"\(\d{5,}\)\s*$", "", title or "").strip()


def classify(name: str, tags: str = "") -> str:
    """按名称 (以及剪映分类标签) 推断音效用途分类; 推断不出返回空字符串。"""
    text = _strip_catalog_suffix(name).lower()
    tag_set = set((tags or "").split("|"))
    if "BGM" in tag_set or any(w in text for w in EXCLUDE_WORDS):
        return ""
    for cat, words in KEYWORD_RULES:
        if any(w.lower() in text for w in words):
            return cat
    # 名称没线索时, 用剪映分类兜底
    if "笑声" in tag_set:
        return "laugh"
    if "提示音" in tag_set:
        return "ding"
    return ""


def read_csv_rows(path: str) -> List[dict]:
    """读取带 '#' 注释头的 CSV (data/ 目录约定)。"""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        lines = [line for line in f if not line.startswith("#")]
    return list(csv.DictReader(lines))


def _fmt_dur(v) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return ""


def cloud_entries(path: str = CLOUD_SFX) -> List[dict]:
    """剪映云端音效 -> 索引行。只收录能分类的条目; is_vip 为空 (未知) 视为不可用。"""
    out = []
    for r in read_csv_rows(path):
        eid = (r.get("effect_id") or "").strip()
        title = (r.get("title") or "").strip()
        if not eid or not title:
            continue
        tags = r.get("categories") or ""
        cat = classify(title, tags)
        if not cat:
            continue
        vip = (r.get("is_vip") or "").strip()
        out.append(
            {
                "id": eid,
                "name": _strip_catalog_suffix(title),
                "category": cat,
                "duration_s": _fmt_dur(r.get("duration_s")),
                "source": "jianying_cloud",
                "is_vip": vip,
                "available": "1" if vip == "0" else "0",
                "path": "",
                "tags": tags,
            }
        )
    return out


def probe_duration(path: str) -> Optional[float]:
    """读音频时长: 先用 soundfile (wav/flac/ogg), 再用 ffprobe; 都不行返回 None。"""
    try:
        import soundfile as sf  # 可选依赖

        info = sf.info(path)
        if info.frames and info.samplerate:
            return round(info.frames / info.samplerate, 3)
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        return round(float(out), 3) if out else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def local_entries(folder: str) -> List[dict]:
    """
    扫描本地音效目录 (递归)。分类优先用子目录名 (如 SFX/whoosh/a.wav), 其次按文件名关键词推断;
    推断不出的归到 'misc' (sfx_designer 不会自动挑它, 但可以按 id 指定)。
    """
    folder = os.path.abspath(os.path.expanduser(folder))
    out = []
    for dirpath, _dirs, files in os.walk(folder):
        for fn in sorted(files):
            if os.path.splitext(fn)[1].lower() not in AUDIO_EXTS:
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, folder)
            parts = [p.lower() for p in rel.replace("\\", "/").split("/")[:-1]]
            cat = next((p for p in reversed(parts) if p in CATEGORIES), "")
            stem = os.path.splitext(fn)[0]
            cat = cat or classify(stem) or classify(" ".join(parts)) or "misc"
            dur = probe_duration(path)
            out.append(
                {
                    "id": "local:" + rel.replace("\\", "/"),
                    "name": stem,
                    "category": cat,
                    "duration_s": _fmt_dur(dur) if dur else "",
                    "source": "local",
                    "is_vip": "0",
                    "available": "1",
                    "path": path,
                    "tags": "/".join(parts),
                }
            )
    return out


def write_library(rows: Iterable[dict], path: str = LIBRARY) -> int:
    rows = sorted(
        rows,
        key=lambda r: (
            r["category"],
            r["source"] != "local",
            r["available"] != "1",
            float(r["duration_s"] or 99),
            r["id"],
        ),
    )
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(
            "# JianYing SFX Library (按用途分类的音效索引) - generated by scripts/build_sfx_library.py\n"
        )
        f.write(
            "# source=jianying_cloud: 用 id (effect_id) 经 CloudManager 下载; source=local: 直接用 path。\n"
        )
        f.write("# available=1 可直接使用 (免费或本地); is_vip=1 需剪映会员, skill 不下载。\n")
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    return len(rows)


def load_library(path: str = LIBRARY, local_dirs: Iterable[str] = ()) -> List[dict]:
    """读取音效索引 (可附加本地目录, 不写文件)。duration_s 转成 float (未知为 None)。"""
    rows = read_csv_rows(path)
    for d in local_dirs or ():
        rows.extend(local_entries(d))
    out = []
    for r in rows:
        r = dict(r)
        try:
            r["duration_s"] = float(r.get("duration_s") or "nan")
            if r["duration_s"] != r["duration_s"]:
                r["duration_s"] = None
        except ValueError:
            r["duration_s"] = None
        out.append(r)
    return out


def validate_library(path: str = LIBRARY) -> List[str]:
    """校验索引: 表头齐全、分类合法、id 唯一、available/is_vip 取值、本地文件存在。返回错误列表。"""
    if not os.path.exists(path):
        return [f"missing file: {path}"]
    with open(path, "r", encoding="utf-8", newline="") as f:
        lines = [line for line in f if not line.startswith("#")]
    reader = csv.DictReader(lines)
    errors = []
    missing = [c for c in FIELDS if c not in (reader.fieldnames or [])]
    if missing:
        return [f"sfx_library.csv missing required columns: {', '.join(missing)}"]
    seen = set()
    for i, r in enumerate(reader, start=2):
        if r["id"] in seen:
            errors.append(f"line {i}: duplicate id {r['id']}")
        seen.add(r["id"])
        if r["category"] not in CATEGORIES and r["category"] != "misc":
            errors.append(f"line {i}: unknown category {r['category']}")
        if r["available"] not in ("0", "1"):
            errors.append(f"line {i}: available must be 0/1")
        if r["source"] not in ("jianying_cloud", "local"):
            errors.append(f"line {i}: unknown source {r['source']}")
        if r["source"] == "local" and not os.path.exists(r["path"]):
            errors.append(f"line {i}: local file missing {r['path']}")
    return errors


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="构建按用途分类的音效索引 data/sfx_library.csv")
    ap.add_argument("--cloud", default=CLOUD_SFX, help="剪映云端音效索引 (sync_jy_music.py 生成)")
    ap.add_argument("--local-dir", action="append", default=[], help="本地音效目录, 可重复")
    ap.add_argument("-o", "--output", default=LIBRARY)
    ap.add_argument("--validate", action="store_true", help="只校验现有索引, 不重建")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.validate:
        errors = validate_library(args.output)
        res = make_result(
            not errors,
            "ok" if not errors else "invalid",
            "; ".join(errors[:20]),
            {"errors": errors},
        )
        if args.json:
            emit_result(res, True)
        else:
            print(
                "✅ 音效索引校验通过"
                if not errors
                else "❌ 音效索引校验失败:\n - " + "\n - ".join(errors)
            )
        return 0 if not errors else 1

    rows = cloud_entries(args.cloud)
    for d in args.local_dir:
        rows.extend(local_entries(d))
    n = write_library(rows, args.output)
    counts = Counter(r["category"] for r in rows)
    usable = Counter(r["category"] for r in rows if r["available"] == "1")
    data = {
        "output": os.path.abspath(args.output),
        "total": n,
        "categories": {c: {"total": counts[c], "available": usable[c]} for c in sorted(counts)},
    }
    if args.json:
        emit_result(make_result(True, "ok", "", data), True)
        return 0
    print(f"✅ 已写入 {n} 条音效 -> {args.output}")
    for c in sorted(counts, key=lambda k: -counts[k]):
        print(f"  {c:<10} {counts[c]:>4} 条 (可用 {usable[c]:>3})  {CATEGORIES.get(c, '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
