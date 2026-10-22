# API Reference

This document is the canonical API index for this skill.

## Python Wrapper (`JyProject`)

Bootstrap:

```python
import os
import sys
skill_root = os.getenv("JY_SKILL_ROOT", r"<SKILL_ROOT>")
sys.path.insert(0, os.path.join(skill_root, "scripts"))
from jy_wrapper import JyProject
```

### Constructor

```python
JyProject(project_name: str, width: int = 1920, height: int = 1080, drafts_root: str | None = None, overwrite: bool = True)
```

### Core Lifecycle

- `save()`: persist and patch draft content.
- `get_track_duration(track_name: str) -> int`: timeline end in microseconds.

### Media APIs

- `add_media_safe(media_path, start_time=None, duration=None, track_name=None, source_start=0, **kwargs)`
- `add_audio_safe(media_path, start_time=None, duration=None, track_name="AudioTrack", **kwargs)`
- `add_clip(media_path, source_start, duration, target_start=None, track_name="VideoTrack", **kwargs)`
- `add_cloud_media(query, start_time=None, duration=None, track_name=None)`
- `add_cloud_music(query, start_time=None, duration=None, name=None, duration_s=None)`

### Text / Voice APIs

- `add_text_simple(text, start_time=None, duration="3s", track_name="Subtitles", **kwargs)`
- `add_tts_intelligent(text, speaker="zh_male_huoli", start_time=None, track_name="AudioTrack")`
- `add_narrated_subtitles(text, speaker="zh_female_xiaopengyou", start_time=None, track_name="Subtitles")`

`add_text_simple(..., **kwargs)` supports `style`, `border`, `clip_settings`, `font`, `background`, `shadow`.
Use `clip_settings=draft.ClipSettings(transform_y=-0.8)` for subtitle bottom position.
Do not pass `transform_y` directly as a top-level arg.

### VFX / Transition APIs

- `add_effect_simple(effect_name, start_time=None, duration="3s", track_name="EffectTrack")`
- `add_transition_simple(transition_name, video_segment=None, duration="1s", track_name=None)`
- `add_web_asset_safe(html_path, start_time=None, duration="5s", track_name="WebVfxTrack", output_dir=None)`

### Multi-track APIs (11.x)

- `add_overlay(media_path, start_time, duration, track_name="Overlay", position="center", scale=0.4, alpha=1.0, rotation=0.0)`
- `add_split_screen(media_paths, start_time=0, duration=None, layout="horizontal")`
- `add_video_animation(segment, name, kind="intro"|"outro"|"group", duration=None)`
- `add_segment_effect(segment, effect_name, params=None)`
- `add_filter_simple(segment, filter_name, intensity=100)`
- `add_mask_simple(segment, mask_name="圆形", **mask_kwargs)`
- `add_keyframes(segment, prop, [(offset, value), ...], curve_type="Line")`
- `add_ken_burns(segment, zoom_from=1.0, zoom_to=1.15, pan=(0, 0))`
- `set_background_fill(segment, "blur"|"color", blur=0.0625, color="#00000000")`

### AI / VIP APIs (11.x)

- `add_auto_subtitles(media_path, start_time=0, model="small", language="zh", max_chars=18)` (requires `faster-whisper`)
- `get_vip_usage()` -> `[{name, resource_id, type}]`; `save()` also returns `vip_assets`
- Constructor `allow_vip=False` (or env `JY_ALLOW_VIP=0`) skips VIP assets during name resolution
- `scripts/sync_jy_panel.py [--json]` refreshes `data/panel_*.csv` from the local JianYing 11.x cache

## Minimal End-to-End Example

```python
import os
import sys

skill_root = r"<SKILL_ROOT>"
sys.path.insert(0, os.path.join(skill_root, "scripts"))
from jy_wrapper import JyProject

project = JyProject("API_Demo", overwrite=True)
project.add_media_safe(os.path.join(skill_root, "assets", "video.mp4"), "0s", "5s", "VideoTrack")
bgm = project.add_cloud_media("7546546694282676275", "0s", "8s", "BGM_Track")
if bgm:
    bgm.volume = 0.6
project.add_narrated_subtitles("这是一个 API 文档示例。", start_time="0.5s")
project.save()
```

## CLI Contract

Common `--json` output:

```json
{
  "ok": true,
  "code": "ok",
  "reason": "",
  "data": {}
}
```

Exit codes:

- `0`: success
- `1`: runtime/infra failure
- `2`: invalid input or strict precondition failure

Key scripts:

- `scripts/api_validator.py`
- `scripts/asset_search.py`
- `scripts/build_cloud_music_library.py`
- `scripts/auto_exporter.py`
- `scripts/draft_inspector.py`

### Draft Inspector CLI

```bash
python <SKILL_ROOT>/scripts/draft_inspector.py list --limit 20
python <SKILL_ROOT>/scripts/draft_inspector.py summary --name "DraftName"
python <SKILL_ROOT>/scripts/draft_inspector.py show --name "DraftName" --kind content --json
```
