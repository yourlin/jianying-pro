"""
从剪映 11.x 的本地缓存增量同步「音乐库」「音效库」「素材库(视频/图片)」索引
(data/cloud_music_library.csv, data/cloud_sound_effects.csv, data/cloud_video_assets.csv)。

剪映会缓存音乐面板的接口响应 (Cache/ressdk_db/*/rp.db 的 http_cache):
- /lv/v1/get_collections        歌单列表 (推荐音乐、国风、纯音乐……)
- /lv/v1/get_collection_songs   歌单内的歌曲分页 (每页 50 首, URL 为哈希, 不含歌单 id)

歌曲分页不带歌单信息, 因此按抓取时间把分页切成「一次连续浏览」(offset 从 50 重新开始即为新歌单),
再按歌单面板顺序推断歌单名。推断结果会打印出来, 可用 --assign 修正。

用法:
    python scripts/sync_jy_music.py                    # 增量同步并打印每段浏览对应的歌单
    python scripts/sync_jy_music.py --dry-run          # 只看推断结果, 不写文件
    python scripts/sync_jy_music.py --assign 17=AI音乐 # 修正第 17 段浏览的歌单名
"""

import argparse
import csv
import json
import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.panel_assets import DATA_DIR, _iter_http_cache, _load_json, find_panel_dbs  # noqa: E402

LIBRARY = os.path.join(DATA_DIR, "cloud_music_library.csv")
STATE = os.path.join(DATA_DIR, ".music_sync_state.json")
FIELDS = ["music_id", "title", "duration_s", "categories", "url", "author", "is_vip"]
UNKNOWN = "未知歌单"


def _response(body: str) -> dict:
    j = _load_json(body)
    inner = j.get("response")
    if isinstance(inner, str) and inner:
        return _load_json(inner)
    return j.get("data") if isinstance(j.get("data"), dict) else {}


def collect_pages(dbs: List[str]):
    collections: List[str] = []
    pages: Dict[str, dict] = {}
    for db in dbs:
        for url, body in _iter_http_cache(db):
            if not url.startswith("/lv/v1/get_collection"):
                continue
            r = _response(body)
            cols = [c.get("name", "") for c in r.get("collections") or []]
            if len(cols) > len(collections):
                collections = cols
            if "songs" in url and isinstance(r.get("songs"), list):
                t = int(_load_json(body).get("systime") or 0)
                old = pages.get(url)
                # 同一歌单+分页的 URL 哈希固定; 以首次抓取时间排序才能还原浏览顺序
                if old is None or t < old["t"]:
                    pages[url] = {"url": url, "t": t, "offset": r.get("next_offset"),
                                  "has_more": r.get("has_more"), "songs": r["songs"]}
    return collections, sorted(pages.values(), key=lambda p: p["t"])


SESSION_GAP_S = 600  # 两页间隔超过 10 分钟视为新的浏览会话


def group_runs(pages: List[dict]) -> List[dict]:
    runs: List[dict] = []
    for p in pages:
        last = runs[-1]["pages"][-1] if runs else None
        new_session = last is None or p["t"] - last["t"] > SESSION_GAP_S
        restart = p["offset"] == 50 or (
            last is not None and p["offset"] is not None and last["offset"] is not None
            and p["offset"] <= last["offset"]
        )
        if new_session or restart or not last.get("has_more", True):
            runs.append({"pages": [], "session_start": new_session})
        runs[-1]["pages"].append(p)
    return runs


def assign_names(runs: List[dict], collections: List[str], state: Dict[str, str],
                 overrides: Dict[int, str]) -> None:
    prev_idx = None
    for i, run in enumerate(runs):
        known = [state[p["url"]] for p in run["pages"] if p["url"] in state]
        if i in overrides:
            run["name"], run["source"] = overrides[i], "手动指定"
        elif known:
            run["name"], run["source"] = known[0], "已记录"
        elif prev_idx is not None and prev_idx + 1 < len(collections):
            # 新会话也接着上一个歌单往下 (若从头重刷, 前面的分页已记录, 会按记录识别)
            src = "推断:接上次继续" if run.get("session_start") else "推断:按面板顺序"
            run["name"], run["source"] = collections[prev_idx + 1], src
        elif prev_idx is None and i == 0 and collections:
            run["name"], run["source"] = collections[0], "推断:会话起点"
        else:
            run["name"], run["source"] = UNKNOWN, "无法推断"
        prev_idx = collections.index(run["name"]) if run["name"] in collections else None


def load_library() -> Dict[str, dict]:
    rows: Dict[str, dict] = {}
    if os.path.exists(LIBRARY):
        with open(LIBRARY, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(line for line in f if not line.startswith("#")):
                if row.get("music_id"):
                    rows[row["music_id"]] = {k: row.get(k, "") or "" for k in FIELDS}
    return rows


def merge(library: Dict[str, dict], runs: List[dict]) -> tuple:
    added = updated = 0
    for run in runs:
        for page in run["pages"]:
            for s in page["songs"]:
                mid = str(s.get("id") or "")
                if not mid:
                    continue
                cats = [run["name"]] if run["name"] != UNKNOWN else []
                cats += [g for g in s.get("genres") or [] if g]
                is_vip = "0" if s.get("paid_type") == "free" else "1"
                row = library.get(mid)
                if row is None:
                    library[mid] = {
                        "music_id": mid, "title": (s.get("title") or "").strip(),
                        "duration_s": str(s.get("duration") or ""), "categories": "|".join(dict.fromkeys(cats)),
                        "url": "", "author": (s.get("author") or "").strip(), "is_vip": is_vip,
                    }
                    added += 1
                else:
                    old = [c for c in row["categories"].split("|") if c]
                    new = list(dict.fromkeys(old + cats))
                    if new != old or not row.get("is_vip") or not row.get("author"):
                        row["categories"] = "|".join(new)
                        row["is_vip"] = row.get("is_vip") or is_vip
                        row["author"] = row.get("author") or (s.get("author") or "").strip()
                        updated += 1
    return added, updated


def save_library(library: Dict[str, dict]) -> None:
    with open(LIBRARY, "w", encoding="utf-8", newline="") as f:
        f.write("# JianYing Cloud Music Library\n")
        f.write("# AI Guidance: Use 'music_id' to reference. categories = 歌单|曲风. is_vip=1 需剪映会员(skill 不下载).\n")
        f.write("# Synced by scripts/sync_jy_music.py (incremental)\n")
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(library.values())


SFX_LIBRARY = os.path.join(DATA_DIR, "cloud_sound_effects.csv")
SFX_FIELDS = ["effect_id", "title", "duration_s", "categories", "is_vip"]


def merge_sound_effects() -> tuple:
    """把面板音效 (panel_sound_effects.csv, 由 sync_jy_panel.py 生成) 增量合并进 cloud_sound_effects.csv。"""
    from utils.panel_assets import extract_panel_rows, write_panel_csvs

    write_panel_csvs({k: v for k, v in extract_panel_rows(find_panel_dbs()).items()
                      if k == "panel_sound_effects.csv"})
    from utils.panel_assets import load_panel_rows

    lib: Dict[str, dict] = {}
    if os.path.exists(SFX_LIBRARY):
        with open(SFX_LIBRARY, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(line for line in f if not line.startswith("#")):
                if row.get("effect_id"):
                    lib[row["effect_id"]] = {k: row.get(k, "") or "" for k in SFX_FIELDS}
    before, updated = len(lib), 0
    for r in load_panel_rows("panel_sound_effects.csv"):
        rid = r["resource_id"]
        cats = [c for c in r.get("categories", "").split("|") if c]
        if rid not in lib:
            lib[rid] = {"effect_id": rid, "title": r["name"], "duration_s": r.get("duration_s", ""),
                        "categories": "|".join(cats), "is_vip": r["is_vip"]}
        else:
            row = lib[rid]
            merged = list(dict.fromkeys([c for c in row["categories"].split("|") if c] + cats))
            if merged != row["categories"].split("|") or not row.get("is_vip"):
                row["categories"] = "|".join(merged)
                row["is_vip"] = row.get("is_vip") or r["is_vip"]
                row["duration_s"] = row.get("duration_s") or r.get("duration_s", "")
                updated += 1
    with open(SFX_LIBRARY, "w", encoding="utf-8", newline="") as f:
        f.write("# JianYing Cloud Sound Effects Library\n")
        f.write("# AI Guidance: Use 'effect_id' to reference. is_vip=1 需剪映会员(skill 不下载).\n")
        f.write("# Synced by scripts/sync_jy_music.py (incremental)\n")
        w = csv.DictWriter(f, fieldnames=SFX_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(lib.values())
    vip = sum(1 for r in lib.values() if r.get("is_vip") == "1")
    return before, len(lib) - before, updated, len(lib), vip


VIDEO_LIBRARY = os.path.join(DATA_DIR, "cloud_video_assets.csv")
VIDEO_FIELDS = ["id", "name", "type", "duration", "url", "categories", "is_vip"]


def merge_stock_assets() -> tuple:
    """把面板素材库 (视频/图片) 增量合并进 cloud_video_assets.csv。"""
    from utils.panel_assets import extract_panel_rows, load_panel_rows, write_panel_csvs

    wanted = {"panel_stock_videos.csv": "video", "panel_stock_images.csv": "image"}
    write_panel_csvs({k: v for k, v in extract_panel_rows(find_panel_dbs()).items() if k in wanted})
    lib: Dict[str, dict] = {}
    if os.path.exists(VIDEO_LIBRARY):
        with open(VIDEO_LIBRARY, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(line for line in f if not line.startswith("#")):
                if row.get("id"):
                    lib[row["id"]] = {k: row.get(k, "") or "" for k in VIDEO_FIELDS}
    before = len(lib)
    for fname, kind in wanted.items():
        for r in load_panel_rows(fname):
            cats = [c for c in r.get("categories", "").split("|") if c]
            old = lib.get(r["resource_id"])
            if old is None:
                lib[r["resource_id"]] = {"id": r["resource_id"], "name": r["name"], "type": kind,
                                         "duration": r.get("duration_s", ""), "url": "",
                                         "categories": "|".join(cats), "is_vip": r["is_vip"]}
            else:
                old["categories"] = "|".join(dict.fromkeys(
                    [c for c in old.get("categories", "").split("|") if c] + cats))
                old["is_vip"] = old.get("is_vip") or r["is_vip"]
    with open(VIDEO_LIBRARY, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=VIDEO_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(lib.values())
    vids = [r for r in lib.values() if r["type"] in ("video", "image")]
    return before, len(lib) - before, len(lib), sum(1 for r in vids if r.get("is_vip") == "1")


def main() -> int:
    ap = argparse.ArgumentParser(description="增量同步剪映音乐库索引")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--assign", action="append", default=[], help="修正歌单名, 如 17=AI音乐")
    args = ap.parse_args()

    dbs = find_panel_dbs()
    if not dbs:
        print("未找到剪映缓存 (Cache/ressdk_db)。")
        return 1
    collections, pages = collect_pages(dbs)
    runs = group_runs(pages)
    state = {}
    if os.path.exists(STATE):
        with open(STATE, "r", encoding="utf-8") as f:
            state = json.load(f)
    overrides = {}
    for a in args.assign:
        idx, _, name = a.partition("=")
        overrides[int(idx)] = name.strip()
    assign_names(runs, collections, state, overrides)

    print(f"歌单面板: {len(collections)} 个; 缓存分页: {len(pages)}; 浏览段: {len(runs)}")
    for i, run in enumerate(runs):
        n = sum(len(p["songs"]) for p in run["pages"])
        titles = " / ".join(s.get("title", "")[:10] for s in run["pages"][0]["songs"][:3])
        print(f"  [{i:>2}] {run['name']:<10} ({run['source']}) {n:>5} 首  例: {titles}")

    library = load_library()
    before = len(library)
    added, updated = merge(library, runs)
    vip = sum(1 for r in library.values() if r.get("is_vip") == "1")
    print(f"\n音乐库: 原有 {before} 首, 新增 {added}, 更新 {updated}, 现共 {len(library)} 首 (VIP {vip})")
    if args.dry_run:
        print("(dry-run, 未写入)")
        return 0
    save_library(library)
    for run in runs:
        for p in run["pages"]:
            state[p["url"]] = run["name"]
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    print(f"[+] 已写入 {LIBRARY}")

    before, added, updated, total, vip = merge_sound_effects()
    print(f"音效库: 原有 {before} 个, 新增 {added}, 更新 {updated}, 现共 {total} 个 (VIP {vip})")
    print(f"[+] 已写入 {SFX_LIBRARY}")

    before, added, total, vip = merge_stock_assets()
    print(f"素材库(视频/图片): 原有 {before} 个, 新增 {added}, 现共 {total} 个 (VIP {vip})")
    print(f"[+] 已写入 {VIDEO_LIBRARY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
