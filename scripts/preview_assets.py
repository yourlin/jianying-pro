"""
素材封面预览: 把候选素材的剪映面板封面拼成一张带编号的对比图, 供挑选前查看效果。

用法:
    python preview_assets.py "科技 标题" -c panel_text_templates -l 12
    python preview_assets.py --ids 7542199191150431513 7416724149906705683
    python preview_assets.py "震惊" -c panel_text --frames 3      # 动图取 3 帧, 看动画过程
    python preview_assets.py "口播" -c panel_subtitle --ready     # 只看已可直接使用的模板

输出对比图路径与编号对照表; 封面缓存在 ~/.cache/jianying-pro/covers/。
封面是剪映面板展示给所有用户的缩略图 (免费 / VIP 素材都有), 只用于挑选, 不是素材本身。
"""

import argparse
import concurrent.futures as cf
import csv
import io
import os
import sys
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

from asset_search import DATA_DIR, row_is_vip, search_assets  # noqa: E402
from utils.panel_assets import EFFECT_TYPE_KINDS, cover_expired, ensure_panel_fresh  # noqa: E402

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "jianying-pro")
COVER_DIR = os.path.join(CACHE_DIR, "covers")
SHEET_DIR = os.path.join(CACHE_DIR, "previews")
ALLOWED_HOST_SUFFIXES = (".byteimg.com", ".byteeffecttos.com", ".bytednsdoc.com", ".ibytedtos.com")
MAX_BYTES = 8 * 1024 * 1024
TILE = 200
# CDN 有防爬: 默认的 python-requests UA 会被 403, 请求过密时连接会被挂起。用常规 UA + 全局限速 + 退避重试。
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/121.0.0.0 Safari/537.36", "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
MIN_INTERVAL = 0.25  # 两次请求之间至少间隔 (秒), 约每秒 4 个
WORKERS = 3
_rate_lock = threading.Lock()
_last_request = [0.0]
_session = requests.Session()
_session.headers.update(HEADERS)


def _throttle() -> None:
    with _rate_lock:
        wait = _last_request[0] + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.monotonic()


def _download(url: str) -> Optional[bytes]:
    """限速下载一张图; 403/429/5xx/超时退避重试 2 次。"""
    for attempt in range(3):
        _throttle()
        try:
            res = _session.get(url, timeout=(5, 10))
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
            continue
        if res.status_code in (403, 429) or res.status_code >= 500:
            time.sleep(2 * (attempt + 1))
            continue
        if res.status_code != 200 or not res.headers.get("Content-Type", "").startswith("image/"):
            return None
        return res.content if len(res.content) <= MAX_BYTES else None
    return None
FONT_CANDIDATES = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "C:/Windows/Fonts/msyh.ttc",
]


def _allowed(url: str) -> bool:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host.endswith(s) for s in ALLOWED_HOST_SUFFIXES)


def _rows_by_ids(ids: List[str]) -> List[dict]:
    want = {str(i) for i in ids}
    found: Dict[str, dict] = {}
    for f in sorted(os.listdir(DATA_DIR)):
        if not (f.startswith("panel_") and f.endswith(".csv")):
            continue
        with open(os.path.join(DATA_DIR, f), encoding="utf-8") as fh:
            for r in csv.DictReader(line for line in fh if not line.startswith("#")):
                if r.get("resource_id") in want and r["resource_id"] not in found:
                    r["source_file"] = f
                    found[r["resource_id"]] = r
    return [found[i] for i in ids if i in found]


KIND_EFFECT_TYPE = {kind: et for et, (kind, _csv) in EFFECT_TYPE_KINDS.items()}


def refresh_cover_urls(row: dict) -> bool:
    """封面链接过期且剪映缓存里也没有新的时, 按 ID 向剪映素材接口重新取封面地址 (只取缩略图地址)。"""
    from cloud_manager import JY_API_HEADERS, JY_MGET_ITEM_URL

    et = KIND_EFFECT_TYPE.get(row.get("kind", ""))
    if not et:
        return False
    for body in ({"items": [{"id": row["resource_id"], "effect_type": et}]},
                 {"items": [{"id": row["resource_id"], "effect_type": et, "source": 1}]}):
        try:
            res = requests.post(JY_MGET_ITEM_URL, headers=JY_API_HEADERS, json=body, timeout=15)
            items = (res.json().get("data") or {}).get("effect_item_list") or []
        except (requests.RequestException, ValueError):
            continue
        cover = ((items[0].get("common_attr") or {}).get("cover_url") or {}) if items else {}
        if cover.get("static_img") or cover.get("small"):
            small = cover.get("small") or ""
            row["cover_url"] = cover.get("static_img") or small
            row["preview_url"] = small if small != row["cover_url"] else ""
            return True
    return False


def _cover_path(row: dict, animated: bool, url: str) -> str:
    ext = ".gif" if ".gif" in url.split("?")[0] else ".png"
    return os.path.join(COVER_DIR, f"{row.get('kind', 'x')}_{row['resource_id']}{'_anim' if animated else ''}{ext}")


def fetch_cover(row: dict, animated: bool) -> Optional[bytes]:
    url = (row.get("preview_url") if animated else "") or row.get("cover_url") or ""
    if url:
        for ext in (".png", ".gif"):  # 已缓存的封面不受链接过期影响
            cached = os.path.join(COVER_DIR, f"{row.get('kind', 'x')}_{row['resource_id']}{'_anim' if animated else ''}{ext}")
            if os.path.exists(cached):
                with open(cached, "rb") as f:
                    return f.read()
    if (not url or cover_expired(url)) and refresh_cover_urls(row):
        url = (row.get("preview_url") if animated else "") or row.get("cover_url") or ""
    if not url or not _allowed(url):
        return None
    path = _cover_path(row, animated, url)
    data = _download(url)
    if data is None:
        return None
    os.makedirs(COVER_DIR, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return data


def _frames(data: bytes, n: int):
    from PIL import Image, ImageSequence

    img = Image.open(io.BytesIO(data))
    frames = [f.convert("RGBA") for f in ImageSequence.Iterator(img)] or [img.convert("RGBA")]
    if n <= 1 or len(frames) == 1:
        return [frames[len(frames) // 2] if len(frames) > 1 else frames[0]]
    return [frames[min(len(frames) - 1, round(i * (len(frames) - 1) / (n - 1)))] for i in range(n)]


def build_sheet(rows: List[dict], images: List[Optional[bytes]], frames: int, cols: int) -> str:
    from PIL import Image, ImageDraw, ImageFont

    font_path = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
    font = ImageFont.truetype(font_path, 15) if font_path else ImageFont.load_default()
    cell_w, label_h = TILE * frames, 24
    cols = max(1, min(cols, len(rows)))
    nrows = (len(rows) + cols - 1) // cols
    pad = 8
    sheet = Image.new("RGB", (cols * (cell_w + pad) + pad, nrows * (TILE + label_h + pad) + pad), (32, 32, 32))
    draw = ImageDraw.Draw(sheet)
    for i, (row, data) in enumerate(zip(rows, images)):
        x = pad + (i % cols) * (cell_w + pad)
        y = pad + (i // cols) * (TILE + label_h + pad)
        if data:
            try:
                for j, fr in enumerate(_frames(data, frames)):
                    fr.thumbnail((TILE, TILE))
                    bg = Image.new("RGB", (TILE, TILE), (60, 60, 60))  # 透明底的花字/贴纸放在灰底上便于看清
                    bg.paste(fr, ((TILE - fr.width) // 2, (TILE - fr.height) // 2), fr)
                    sheet.paste(bg, (x + j * TILE, y))
            except Exception:
                data = None
        if not data:
            draw.rectangle([x, y, x + cell_w - 1, y + TILE - 1], outline=(90, 90, 90))
            draw.text((x + 10, y + TILE // 2 - 8), "无封面", fill=(160, 160, 160), font=font)
        tag = " VIP" if row_is_vip(row) else ""
        label = f"#{i + 1} {row.get('name', '')}{tag}"
        while draw.textlength(label, font=font) > cell_w - 4 and len(label) > 4:
            label = label[:-2] + "…"
        draw.text((x + 2, y + TILE + 3), label, fill=(255, 210, 90) if tag else (230, 230, 230), font=font)
    os.makedirs(SHEET_DIR, exist_ok=True)
    out = os.path.join(SHEET_DIR, f"preview_{time.strftime('%Y%m%d_%H%M%S')}.png")
    sheet.save(out)
    return out


def prefetch(csv_name: str, animated: bool = False) -> int:
    """预下载封面: 签名链接过期后 (字幕模板无法按 ID 重新取地址) 仍能预览。已缓存的跳过。"""
    from utils.panel_assets import load_panel_rows

    name = csv_name if csv_name.endswith(".csv") else f"{csv_name}.csv"
    rows = [dict(r) for r in load_panel_rows(name) if r.get("cover_url")]
    if not rows:
        print(f"❌ {name} 没有带封面的条目")
        return 1
    jobs = [(r, False) for r in rows] + ([(r, True) for r in rows if r.get("preview_url")] if animated else [])
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        ok = sum(1 for got in ex.map(lambda j: fetch_cover(*j), jobs) if got)
    size = sum(os.path.getsize(os.path.join(COVER_DIR, f)) for f in os.listdir(COVER_DIR)) if os.path.isdir(COVER_DIR) else 0
    print(f"✅ {name}: {ok}/{len(jobs)} 个封面已在本地缓存 ({COVER_DIR}, 共 {size / 2**20:.0f} MB)")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="剪映素材封面预览 (对比图)")
    ap.add_argument("query", nargs="?", default="", help="搜索关键词 (同 asset_search.py)")
    ap.add_argument("-c", "--category", help="限定分类, 如 panel_text_templates / panel_flower / panel_stickers")
    ap.add_argument("-l", "--limit", type=int, default=12)
    ap.add_argument("--ids", nargs="+", help="直接指定 resource_id")
    ap.add_argument("--frames", type=int, default=1, help="动图取几帧 (>1 时用动态预览, 能看出动画过程)")
    ap.add_argument("--cols", type=int, default=0, help="每行几个 (默认自动)")
    ap.add_argument("--ready", action="store_true", help="只看已可直接使用的字幕/文字模板")
    vip = ap.add_mutually_exclusive_group()
    vip.add_argument("--free-only", action="store_true")
    vip.add_argument("--vip-only", action="store_true")
    ap.add_argument("--no-sync", action="store_true", help="不自动检查/同步面板索引")
    ap.add_argument("--prefetch", metavar="CSV", help="预下载某个面板索引的全部封面到本地缓存, 如 panel_subtitle_templates")
    ap.add_argument("--animated", action="store_true", help="配合 --prefetch: 同时下载动图 (体积大很多)")
    args = ap.parse_args()

    if args.prefetch:
        return prefetch(args.prefetch, args.animated)

    if not args.no_sync:
        ensure_panel_fresh()
    if args.ids:
        rows = _rows_by_ids(args.ids)
    else:
        if not args.query and not args.ready:
            ap.error("需要关键词、--ids 或 --ready")
        vf = "free" if args.free_only else "vip" if args.vip_only else "all"
        rows = search_assets(args.query, args.category, args.limit * 3, vf, args.ready)
        rows = [r for r in rows if r.get("source_file", "").startswith("panel_")]
    rows = rows[: args.limit]
    if not rows:
        print("❌ 没有找到带封面的面板素材 (封面来自 panel_*.csv, 先运行 jy_wrapper.py sync --only panel)")
        return 1

    animated = args.frames > 1
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        images = list(ex.map(lambda r: fetch_cover(r, animated) or (fetch_cover(r, False) if animated else None), rows))
    frames = max(1, args.frames)
    cols = args.cols or (3 if frames > 1 else 4)
    out = build_sheet(rows, images, frames, cols)

    print(f"🖼️ 对比图: {out}")
    print("#  | 名称 | 类型 | VIP | 状态 | resource_id")
    for i, r in enumerate(rows, 1):
        state = {"1": "可直接用", "0": "需在剪映套用一次"}.get(r.get("ready", ""), "")
        print(f"{i:<2} | {r.get('name', '')} | {r.get('source_file', '')[6:-4]} | "
              f"{'VIP' if row_is_vip(r) else '免费'} | {state} | {r.get('resource_id', '')}")
    missing = sum(1 for d in images if not d)
    if missing:
        print(f"ℹ️ {missing} 个没有封面 (索引里没有或已过期, 可重新同步面板)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
