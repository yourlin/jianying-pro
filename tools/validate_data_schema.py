import csv
import os
from typing import Dict, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")

# filename -> required column groups (any one column in each group is acceptable)
SCHEMA: Dict[str, List[Tuple[str, ...]]] = {
    "cloud_music_library.csv": [("music_id",), ("title",), ("duration_s",), ("categories",)],
    "cloud_sound_effects.csv": [("effect_id",), ("title",), ("duration_s",), ("categories",)],
    "cloud_video_assets.csv": [("id",), ("name",)],
    "cloud_text_styles.csv": [("style_id", "id"), ("name_hint", "name"), ("categories",)],
    "filters.csv": [("identifier",)],
    "text_animations.csv": [("identifier",)],
    "transitions.csv": [("identifier",)],
    "video_intro_animations.csv": [("identifier",)],
    "video_outro_animations.csv": [("identifier",)],
    "video_scene_effects.csv": [("identifier",)],
    "tts_speakers.csv": [("speaker_id",), ("name_hint", "display_name"), ("categories",)],
}

# 剪映 11.x 面板索引 (由 scripts/sync_jy_panel.py 生成, 可选)
OPTIONAL_SCHEMA: Dict[str, List[Tuple[str, ...]]] = {
    name: [("name",), ("resource_id",), ("is_vip",)]
    for name in (
        "panel_transitions.csv", "panel_filters.csv", "panel_video_effects.csv", "panel_character_effects.csv", "panel_video_animations.csv",
        "panel_text_animations.csv", "panel_fonts.csv", "panel_tts_voices.csv",
        "panel_audio_effects.csv", "panel_flower_texts.csv", "panel_text_templates.csv", "panel_subtitle_templates.csv",
        "panel_stickers.csv", "panel_stock_videos.csv", "panel_stock_images.csv", "panel_sound_effects.csv", "panel_masks.csv", "panel_mask_strokes.csv",
        "panel_blend_modes.csv",
    )
}


def _read_header(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8", newline="") as f:
        lines = [line for line in f if not line.startswith("#")]
    if not lines:
        return []
    reader = csv.reader(lines)
    return next(reader, [])


def validate() -> Tuple[int, List[str]]:
    errors: List[str] = []
    if not os.path.exists(DATA_DIR):
        return 1, [f"data directory missing: {DATA_DIR}"]

    for filename, required_groups in list(SCHEMA.items()) + list(OPTIONAL_SCHEMA.items()):
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            if filename not in OPTIONAL_SCHEMA:
                errors.append(f"missing file: {filename}")
            continue
        header = _read_header(path)
        if not header:
            errors.append(f"empty file or invalid csv header: {filename}")
            continue
        missing: List[str] = []
        for group in required_groups:
            if not any(candidate in header for candidate in group):
                missing.append("/".join(group))
        if missing:
            errors.append(f"{filename} missing required columns: {', '.join(missing)}")

    return (1, errors) if errors else (0, [])


def main() -> int:
    code, errors = validate()
    if code != 0:
        print("Data schema validation failed:")
        for e in errors:
            print(f" - {e}")
        return 1
    print("Data schema validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
