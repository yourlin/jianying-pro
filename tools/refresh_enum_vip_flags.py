"""
根据内置枚举元数据 (is_vip) 刷新 data/*.csv 的 description 列 (Free / VIP)。

用法: python tools/refresh_enum_vip_flags.py
"""

import csv
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts", "vendor"))

import pyJianYingDraft as draft  # noqa: E402

CSV_FILES = [
    "filters.csv",
    "transitions.csv",
    "text_animations.csv",
    "video_intro_animations.csv",
    "video_outro_animations.csv",
    "video_scene_effects.csv",
]


def refresh(path: str) -> tuple:
    with open(path, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return 0, 0
    fields = list(rows[0].keys())
    vip = 0
    for row in rows:
        enum_cls = getattr(draft, row.get("enum_type", ""), None)
        member = enum_cls.__members__.get(row["identifier"]) if enum_cls else None
        if member is None:
            continue
        is_vip = bool(getattr(member.value, "is_vip", False))
        row["description"] = "VIP" if is_vip else "Free"
        vip += is_vip
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows), vip


def main() -> int:
    for name in CSV_FILES:
        path = os.path.join(ROOT, "data", name)
        if os.path.exists(path):
            total, vip = refresh(path)
            print(f"{name:<30} {total:>5} rows, VIP {vip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
