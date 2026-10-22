"""
采集剪映 11.x 里套用过的字幕模板 / 文字模板为预设 (data/template_presets/)。

用法: 在剪映里任一草稿中给文字套用模板 (等剪映自动保存), 然后运行
    python sync_jy_templates.py [--list]
只读取剪映草稿目录中的明文 template.json, 不修改任何草稿。
"""

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

from utils.template_presets import PRESET_DIR, harvest_presets, presets_from_local_packages  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="采集剪映字幕模板/文字模板预设")
    ap.add_argument("--list", action="store_true", help="只列出已有预设")
    ap.add_argument("--from-packages", action="store_true",
                    help="同时为本地已下载模板包、还没有预设的字幕模板生成预设 (配合 jy_wrapper.py warmup)")
    args = ap.parse_args()
    if not args.list:
        found = harvest_presets()
        if not found:
            print("ℹ️ 没找到套用过模板的草稿: 请先在剪映里给文字套用字幕模板/文字模板。")
        if args.from_packages:
            made = presets_from_local_packages("subtitle_template")
            print(f"由模板包生成 {len(made)} 个字幕模板预设")
    for path in sorted(glob.glob(os.path.join(PRESET_DIR, "*.json"))):
        with open(path, "r", encoding="utf-8") as f:
            p = json.load(f)
        print(f"  {p['name']}\t{p['resource_id']}\t{p['kind']}\t子文字 {len(p['texts'])} 段")


if __name__ == "__main__":
    main()
