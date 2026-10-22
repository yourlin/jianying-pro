"""
从本机剪映 (11.x) 的素材面板缓存刷新素材库索引。

用法:
    python scripts/sync_jy_panel.py            # 扫描本机剪映缓存, 生成 data/panel_*.csv
    python scripts/sync_jy_panel.py --json     # 机器可读输出
    python scripts/sync_jy_panel.py --db /path/to/rp.db

提示: 在剪映里多浏览几个面板 (转场/特效/动画/字体/朗读音色), 缓存会更完整, 再运行本脚本即可。
本脚本只读复制缓存, 不会修改剪映的任何文件。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.cli_protocol import emit_result, make_result  # noqa: E402
from utils.panel_assets import (  # noqa: E402
    DATA_DIR,
    extract_panel_rows,
    find_panel_dbs,
    write_panel_csvs,
    write_sync_stamp,
)


def merge_free_tts_voices(data_dir: str) -> int:
    """把面板中新出现的免费朗读音色增量追加到 tts_speakers.csv (VIP 音色只保留在面板索引中)。"""
    import csv

    from utils.panel_assets import load_panel_rows

    path = os.path.join(data_dir, "tts_speakers.csv")
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    known = {r.get("speaker_id") for r in csv.DictReader(line for line in lines if not line.startswith("#"))}
    new = []
    for r in load_panel_rows("panel_tts_voices.csv", data_dir):
        vt = r.get("voice_type")
        if r.get("is_vip") == "0" and vt and vt not in known:
            known.add(vt)
            cats = [c for c in r.get("categories", "").split("|") if c and c not in ("热门", "最新")][:3]
            new.append((vt, r["name"].replace(",", " "), "|".join(["tts", "11.x新增"] + cats)))
    if new:
        with open(path, "a", encoding="utf-8", newline="") as f:
            if lines and not lines[-1].endswith("\n"):
                f.write("\n")
            csv.writer(f, lineterminator="\n").writerows(new)
    return len(new)


def main() -> int:
    parser = argparse.ArgumentParser(description="刷新剪映 11.x 素材面板索引")
    parser.add_argument("--db", action="append", help="指定 rp.db 路径 (可多次)")
    parser.add_argument("--out", default=DATA_DIR, help="输出目录, 默认 skill 的 data/")
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果")
    args = parser.parse_args()

    dbs = args.db or find_panel_dbs()
    if not dbs:
        emit_result(
            make_result(False, "not_found", "未找到剪映素材缓存 (Cache/ressdk_db/*/rp.db)。请先打开剪映并浏览素材面板。"),
            True,
        )
        return 1

    rows = extract_panel_rows(dbs)
    counts = write_panel_csvs(rows, args.out)
    write_sync_stamp(dbs, args.out)
    vip = {f: sum(1 for r in rs if r["is_vip"] == "1") for f, rs in rows.items()}

    added_voices = merge_free_tts_voices(args.out)

    if args.json:
        emit_result(make_result(True, "ok", "", {"dbs": dbs, "counts": counts, "vip": vip}), True)
    else:
        print(f"[*] 扫描缓存: {len(dbs)} 个")
        for fname in sorted(counts):
            print(f"  {fname:<30} {counts[fname]:>6}  (VIP {vip.get(fname, 0)})")
        print(f"[+] 已写入: {args.out}")
        print(f"[+] 新增免费朗读音色 {added_voices} 个 -> tts_speakers.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
