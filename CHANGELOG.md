# Changelog

## Unreleased
- Methodology from the Zhihu handbook 《剪映从入门到精通：全民视频剪辑实用手册》:
  - new `methodology/grading.md` (adjust order and defaults, filter strengths by genre, teal-orange / HSL / wheel recipes, mood-based grading);
  - audio golden ratios, text settings, transition / effect / sticker / mask counts and durations, keyframe and speed tips, the pacing of 5 worked cases, and shooting rules.
  - New scene **剧情短片** (`scenes/drama.md` / `drama.json`).
  - The talking-head scene gains "a visual change at least every 30 s" and "1 s black card between chapters".
- **Scripts and three-layer specs**: each project now has a content-only script (`script.json`, `references/script_schema.md`). `scripts/script_compiler.py` compiles it, together with the specs and the footage analysis, into a storyboard.
  - Machine-readable specs: scenes (`references/scenes/*.json`: beats, pacing, element limits, insert policy, camera whitelist, subtitle/audio defaults, guided questions, checks), styles (`references/styles/*.json`: per-role font/flower/size/animation, transitions, ending effect, grade, PiP background, BGM, SFX, camera amounts) and formats (`references/formats/*.json`: vertical / horizontal / 3:4 / square canvas, safe areas, slots, PiP, card sizes, subtitle sizes).
  - Times are anchored to transcript phrases; vocab cards sync to word timestamps and avoid the chin; cuts come from the scene's pacing threshold; jump cuts are covered by a fake shot-size change.
  - The same script can be recompiled with `--format` / `--style`. Verified by rebuilding V5 from a script as V6 (same structure; the reviewer passes).
- **Guided mode** (`rules/guided.md`): asks the user, through multiple-choice questions, to choose the scene, style, platform, subtitles, pacing and assets, then writes a storyboard for confirmation, assembles it, and reviews the result with them.
  - `scripts/analyze_footage.py`: footage analysis — geometry, silences, scene cuts, faster-whisper transcript with word timestamps, **face position plus free zones for inserts** (OpenCV), and asset orientation.
  - `scripts/storyboard_builder.py`: deterministic assembler for storyboard v2 (`references/storyboard_schema.md`). Supports source-time cuts, transitions, grading, audio processing, camera moves (push / pull / punch / pan / tilt / shake / **pip**), cover-fit inserts, labels, texts and templates, stickers, effects, SFX, subtitles split from the transcript, BGM and background fill. It runs the reviewer afterwards, and with `--preview` captures each shot in JianYing.
  - Specs: 6 scenes (`references/scenes/`); 5 styles (`references/styles/`, new: tech / fresh / variety / business), whose whitelists were checked against the panel indexes; methodology (`references/methodology/`: hook, pacing, subtitles, inserts and face avoidance, camera, transitions and effects, audio, safe areas, shooting). The methodology draws on GitHub editing skills, film-editing references and Chinese creator articles.
- Fixes:
  - **Keyframes on trimmed segments**: JianYing measures keyframe `time_offset` from the start of the *material*, not the segment. `add_keyframes` now converts segment-relative times (source start + offset × speed), and the reviewer reads keyframes the same way. Found while capturing V5 in JianYing: the PiP keyframes were shifted by the trim offsets.
  - Images are no longer clamped to 0.04 s.
  - The video normalizer keeps portrait orientation; it used to force 1920x1080 with padding.
  - Keyframe easing now works on uniform scale.
  - The `TextStyle` default left alignment is avoided in the builder.
- **On-demand warm-up** (macOS, on by default): when `add_subtitle_template` / `add_text_template` meets a template with no preset, the skill warms up just that one in JianYing (about 30 s), generates the preset and continues. If the template can no longer be downloaded, it raises `PanelAssetUnavailable` with same-category candidates that have presets, plus the `preview_assets.py` command to compare their covers (candidates are picked by category only). Turn it off with `JyProject.auto_warmup = False`.
- **Text templates from packages**: `warmup --kind text` and `sync_jy_templates.py --from-packages` now generate text-template presets as well (304 free). The generated presets include stickers (position/size/timing/animations), per-element timing, loop durations and default texts. Checked against 19 JianYing-applied presets, and 17 were verified in-app by screenshot against the panel covers. Sticker animations that cannot be downloaded are dropped, because JianYing hides the whole sticker otherwise. Templates with shape elements are skipped.
- Multi-text templates: `add_text_template([...], template)` fills each text in order (`None` keeps the template text); `template_texts()` lists the defaults.
- **Fully automatic warm-up** (`warmup --auto`, macOS): opens each warm-up draft in JianYing, waits, returns home, moves the used draft to the Trash, then generates presets. `--min-id` skips old templates.
- `jy_preview`: verifies the playhead after seeking (reads the timecode via accessibility and corrects dropped keys), scrolls the home list to find drafts, and returns home through the menu.
- **Text backgrounds** in package-generated presets (subtitle and text templates): `text_params.canvas*` maps to `background_*` (color, alpha, rounded corners, size, offset, texture fill from the package's `image/`), and `check_flag` bit 16 turns the background on (JianYing's own presets: 63 with a background, 47 without). Matches 25 of 26 JianYing-applied presets; verified in-app. All 3,855 package presets were regenerated.
- VIP text templates: the newest 2,200 were warmed up automatically (1,964 usable in total).
- `jy_preview`: picks whichever warm-up draft is visible first (drafts created in the same second have no stable order), retries a click that doesn't open the draft, waits before discarding a used draft (JianYing otherwise recreates an empty stub folder), and seeks twice for the first capture.
- Harvest skips templates the skill itself wrote into a draft (unchanged material IDs), so generated presets are never re-imported as "real" ones.

## v0.1.0 - 2026-10-22
First release of **jianying-pro**, originally adapted from [luoluoluo22/jianying-editor-skill](https://github.com/luoluoluo22/jianying-editor-skill) (MIT) and since largely rewritten. Earlier development releases (1.x) are listed under "Earlier releases" below.
- Renamed the skill, its directories and cache (`~/.cache/jianying-pro`).
- **In-app preview capture** (macOS, `scripts/jy_preview.py`, `JyProject.preview(at=[...])`): opens the draft in JianYing through accessibility (clicks the draft cover on the home page and switches tabs to refresh the list), seeks with Home / Shift+→ / →, captures only the JianYing window (`screencapture -l`), crops it to the player canvas, then returns to the home page with Cmd+W. It refuses to run while the editor is open so JianYing cannot overwrite a freshly saved draft. A small Swift helper (`tools/macos/jy_macos.swift`) lists windows and sends CGEvent clicks/keys. Verified on JianYing 11.7.
- Preset generation from packages scans the cache once per run instead of once per template (2,205 package dirs: minutes → about 1 s).
- **Batch subtitle-template warm-up**: `jy_wrapper.py warmup` builds a draft of ID-only template segments. Opening it makes JianYing download the template packages, and `sync_jy_templates.py --from-packages` then generates presets from them (`preset_from_package`). Checked against 16 genuine presets: dependencies, size, color, font, 花字, animations and spacing all match. JianYing-applied presets always take precedence. Verified in-app (`JY116_PackagePreset_Test`). `--batches N` (0 = all remaining) generates several warm-up drafts at once; `--free-only` and `--vip-only` restrict which templates are included. In batches of 50, after waiting about 1 minute, most packages finish downloading; anything that fails is picked up again by the next batch.

## Earlier releases

### v1.12.0 - 2026-10-21
- **Sticker animations**: `add_sticker(..., anim_in=, anim_out=, anim_loop=)` and `add_sticker_animation()`. The index `panel_sticker_animations.csv` has 33 entries (effect_type 35, which was previously mistaken for text loop animations). Text line spacing now matches JianYing 11.7 (panel 10 -> 0.5), and bold also writes `bold_width`. Letter spacing, alignment, border, background, shadow and background blur were checked against in-app samples and already matched. Text textures and canvas styles are indexed only (not in the desktop panel). Verified in-app (`JY116_Props_Test`).
- **Audio processing** (`core/audio_ops.py`): `set_volume_db`, `set_loudness_normalize` (loudness measured with ffmpeg ebur128, within ~0.1 of JianYing's own value), `set_audio_denoise`, `set_vocal_beautify` and `set_vocal_separation`. Verified in-app (`JY116_Audio_Test`): vocal beautify and separation (人声 = choice 2, 背景声 = choice 1) are computed by JianYing when the draft is opened.
- Cover downloads respect the CDN's anti-scraping limits: browser-like UA (the default `python-requests` UA gets 403), a global rate limit of about 4 requests/s, 3 workers, and backoff retries on 403/429/5xx/timeouts. Before this, some covers hung until timeout.
- Beauty/body index `panel_beauty.csv` (239 items: 美型/美颜/美妆/智能美体/手动美颜美体/一键套装). `resource_id` is the panel item id and `third_resource_id` comes from the item's `third_resource_id_str`, matching JianYing drafts. `set_beauty` now covers all of them (美颜/美型/美妆/智能美体, 216 items) using the field layout JianYing uses when it expands a one-click preset. Each item also gets its real panel group key (`skin_management`/`face_shape`/`facial_features`...) and `intensity_key`. Makeup items get their intensity key, a strength value (`face_adjust_params`) and an `exclusion_group`. When names clash, the 美颜 item wins over the 美体 one. Verified in-app (`JY116_Beauty_Test4`): JianYing runs face detection in the background after opening, then applies everything.
- **Speed and motion** (`core/motion_ops.py`): `set_speed(seg, x, change_pitch=)` (later clips ripple), `set_curve_speed` (7 presets), keyframe easing curves via `add_keyframes(..., ease=)` / `set_keyframe_ease` (11 presets, bezier handles computed exactly as JianYing does), and `add_text_glow` (text glow; sets text check_flag bit 64). New indexes: `panel_curve_speeds.csv`, `panel_keyframe_curves.csv`, `panel_text_glows.csv`. Verified in-app (`JY116_Motion_Test`); retimed durations snap to whole frames, as JianYing does.
- **Template production**: `JyProject.from_template()` (the docs already described it, but it didn't exist before), `replace_material_by_name()` (slot replacement by material name: case-insensitive, extension optional) and `reconnect_all_assets()`. Imported media now keep the original file name as the material name (the copy inside the draft still has an md5 file name), so slots can be found by name and the JianYing timeline shows real names.
- `add_local_adjust(seg, mask, **adjust)`: local color adjustment inside a mask; the full picture is kept, via a masked copy on an upper track. Verified in-app (`JY116_LocalAdjust_Test`).
- `preview_assets.py --prefetch <csv> [--animated]`; all subtitle-template static covers are cached.
- `audit_jianying_compat.py` section 3 compares the skill's `videos.check_flag` with the values JianYing actually saves. JianYing 11.7 rewrites 0x3c0f7ff to 0x780f7ff on open, which was verified harmless.
- Docs: known limitations reorganized (not possible / auto-processed / needs manual trigger); `docs/agent-playbook.md` now requires picking visual assets from cover contact sheets.
- **Text bubbles (文字气泡)**: `add_text_simple(..., bubble=)` and `add_text_bubble(seg, name)`. The bubble library (335 bubbles) is indexed as `panel_text_bubbles.csv`, with covers. Bubbles are written as JianYing 11.7 does (`text_shape`, category `bubble`, local resource path). Verified in-app (`JY116_Bubble_Test`): bubbles not yet downloaded are fetched by ID, and bubbles combine with 花字.

### v1.11.0 - 2026-10-21
- **AI quality and smart color** (`core/quality_ops.py`): `set_one_click_quality`, `set_ultra_hd`, `set_denoise`, `set_frame_interpolation`, `set_super_resolution`, `set_deflicker` and `set_motion_blur` (these stack in `video_algorithm`), plus `set_smart_relight`, `set_color_correct` and `set_color_clone`. JianYing processes them when the draft is opened (verified with flags only, `JY116_AIQuality_Flags_Test`); smart relight needs check_flag bit 0x20000. Verified in-app (`JY116_AIQuality_Test`). Smart relight presets (18, now indexed as `panel_smart_relights.csv`) are built from the preset package's `relit_default.json`, matching JianYing drafts field by field; color clone supports 汐蓝/透亮/食色/秋枫.
- **Stabilization, motion blur and beauty**: `set_stabilize`, `set_motion_blur` and `set_beauty` (磨皮/美白). JianYing processes all of these when the draft is opened; verified in-app with flags only (`JY116_AI_Flags_Test`). Also `set_manual_slim` (手动美体), which needs check_flag bit 0x80000 on the video material. `set_smart_matting` writes the flag, but JianYing only processes it after the user unticks and re-ticks it; `save()` lists those clips. All verified in-app (`JY116_Beauty_Test`).
- **Blend modes and mask strokes**: `set_blend_mode(seg, "滤色", opacity=)` (10 modes, resolved from the installed app's `MixMode/MixMode.json`) and `add_mask_stroke(seg, "发光", color=, size=, ...)` (7 styles; written to the video material's `video_mask_stroke`, and the segment flag `enable_mask_stroke` is set). Panel sync now records each mask stroke's `resource_type` and its default parameters. Verified in-app (`JY116_BlendStroke_Test`).
- **Picture settings** (`core/picture_ops.py`, matching JianYing 11.7 drafts field by field): `add_mask_shadow`, `add_video_stroke` (outline stroke: 单层/发光/手绘/虚线描边), `add_video_shadow`, `set_corner_pin` (变形) and `set_chroma_key` (色度抠图). Slider arguments now take the values shown in JianYing's panel (0–100; angles in degrees), and `add_mask_stroke` uses the same convention. Settings stored on the video material get a per-segment material copy. Verified in-app (`JY116_Picture_Test1`). Real draft samples are in `references/jy11_samples/`.
- **Adjust panel** (`core/adjust_ops.py`): `set_adjust` (all 16 basic sliders, using panel values; matches the in-app sample within 0.02), `set_hsl`, `set_curves`, `set_color_wheels` and `set_smart_color`. Verified in-app (`JY116_Adjust_Test`), including 智能调色, which JianYing computes from the flag alone. `set_lut(seg, cube, strength=, skin_protect=)` imports the .cube into JianYing's LUT library the same way the app does (verified in-app, `JY116_LUT_Test`; the draft needs the absolute .cube path).
- **JianYing-native drafts are readable**: JianYing writes a plaintext `template.json` when a draft is closed. Inspect, clone and export-srt now use the newer of this file and the skill copy. Cloning a native draft copies its relative-path media (from the draft's `textReading/` and from `Cache/onlineMaterial` / `Cache/music`) into the new draft. `export-srt` resolves subtitles that use subtitle/text templates.
- **Asset cover previews**: panel sync now stores each asset's cover image (`cover_url`, a static PNG) and animated preview (`preview_url`, a GIF) in `panel_*.csv`; 86–100% of assets have one. New `scripts/preview_assets.py` builds a numbered contact sheet of candidate covers (`--frames 3` samples frames from animated previews), so assets can be compared visually before choosing. Adds the Pillow dependency. `asset_search.py` and `preview_assets.py` auto-sync the panel index when JianYing's panel cache has changed since the last sync (`JY_AUTO_SYNC=0` / `--no-sync` to disable). Expired signed cover links are re-resolved by ID via the asset API (not possible for subtitle templates).
- Template batch warm-up: harvest skips skeletons that JianYing did not re-apply (empty `resources`); fills names by ID; derives the kind from `type`; when several presets share a name, warns and uses the newest. Batch warm-up itself is still a TODO (`rules/jianying-11.md` section 8).

### v1.10.0 - 2026-10-20
- **Subtitle templates and text templates (字幕模板 / 文字模板)**: new `add_subtitle_template(text, template, ...)` and `add_text_template(...)`, plus `subtitle_template=` / `text_template=` on `add_text_simple`, `add_auto_subtitles` and `add_narrated_subtitles`. Verified in-app on JianYing 11.7.0-beta2: 向下弹出-方块粗体黄 (short and long text), 口播-小马奔腾童趣字幕 (per-character animation), and the text template 我的天呐.
  - Templates are rendered by the script inside the template package, and the draft fields differ per template. So templates are not hand-assembled: the skill harvests the materials JianYing itself wrote (into the plaintext `template.json` next to each draft) into `data/template_presets/<resource_id>.json`, then clones them with new IDs and replaces the text, per-character timing and duration.
  - New `scripts/sync_jy_templates.py`: harvests every template applied in any JianYing draft (16 on first run). Read-only. It also runs automatically the first time an unharvested template is used.
- New `jy_wrapper.py sync [--only panel|music|templates]`: one command that syncs panel indexes, the music, sound-effect and stock-footage libraries, and template presets.
- `asset_search.py`: template results are marked "✅可直接用" (usable now) or "⏳需在剪映套用一次" (apply once in JianYing first). New `--ready` lists only templates usable now.
- Docs: 花字 was wrongly listed as index-only; it has been usable since v1.9.0.

### v1.9.0 - 2026-10-20
- **Masks on JianYing 11.x**: masks were silently ignored by JianYing 11.x (shown as plain rectangles). Masks are now written to `materials.common_mask` with the 11.x fields (`category`, `constant_material_id`, `is_old_version`, `path`, `expansion`, ...).
- **New shape masks**: 24 panel masks indexed into `panel_masks.csv` (圆角瓶盖, 饼干, 八边形, 六边形, 爆炸, 花朵, ...). `add_mask_simple` resolves the downloaded resource from `Cache/effect/<id>/<md5>/` and computes the aspect ratio from its SVG; prompts to click the mask once in JianYing when not yet downloaded. Mask outline styles moved to `panel_mask_strokes.csv`.
- New `tools/audit_jianying_compat.py`: lists JianYing draft migrations newer than the skill's draft version and diffs generated material fields against JianYing's data models (read from the app's plaintext JS).
- Mask `feather` now matches the JianYing 11.x UI value (stored as `(feather/100)**2`, per migration 127.0.0; verified 20/50/80 in-app).
- Video materials now carry the `check_flag` bits JianYing adds in migrations 122.0.0/123.0.0 (`0x1C00000 | 0x2000000`); verified playback, adjustments, matting and beauty in-app.
- `add_auto_subtitles(prompt=...)`: keywords are passed as faster-whisper `hotwords` with a Chinese `initial_prompt`; stray spaces between CJK characters are removed. Verified end-to-end with faster-whisper 1.2.1 (requires `av<16`).
- New `scripts/sync_jy_music.py`: incremental music and sound-effect library sync from the 11.x cache (7038 songs / 280 sound effects on first run). Collection names inferred from browse order (pages are grouped by fetch time) and persisted per page hash; `--assign` corrects them. VIP songs and sound effects are indexed but never downloaded outside JianYing.
- Filters from the 11.x panel (`panel_filters.csv`, 1231 incl. 433 new names) with category names; panel sync now resolves category IDs across all cached responses.
- Audio: `add_audio_safe(volume=, fade_in=, fade_out=)`, `add_audio_effect` (变声/场景音); fixed `add_keyframes(audio_seg, "volume", ...)`. Verified in-app.
- 花字: `add_text_simple(flower=...)`, `add_flower_effect`, and `flower=` on `add_narrated_subtitles` (2418 indexed). Fixed text materials being snapshotted at placement time (花字/font/style changes made afterwards were silently dropped). Verified in-app.
- Panel indexes are now merged incrementally (entries evicted from JianYing's cache are kept); subtitle templates (1577) and text templates (9454) indexed for search.
- Stock footage library (素材库): videos and images indexed from the panel cache and merged into `cloud_video_assets.csv` (6845 entries); downloads resolve fresh URLs with the matching effect_type, and image content types are accepted.
- Save now fills the local resource `path` for animations, transitions, filters, effects and audio effects already downloaded by JianYing; without it JianYing 11.6 applies the effect but its property panel shows nothing selected. Verified in-app.
- Text loop animations: `duration` is now one cycle (default = the animation's own default, override via `anim_loop_duration`); previously one cycle spanned the whole clip, making motion barely visible. Verified in-app.
- Color keyframes (brightness/contrast/saturation) now locate the installed JianYing's `DefaultAdjustBundle` (macOS app bundle / Windows install) instead of a hardcoded Windows 5.9 path. Split screen (2/3/4) and position/rotation/color keyframes verified in-app.
- Transitions: 1804 indexed from the 11.x panel (1377 new names); built-in, VIP and new transitions plus custom durations verified in-app. `add_transition_simple` warns when the incoming clip has no source handle for an overlapping transition (shown as hatched freeze frames in JianYing).
- Stickers: new `add_sticker(name, start, duration, position=, scale=, rotation=)` (410 indexed). Verified in-app.
- Character effects (人物特效, panel `face-prop`): 326 indexed; new `add_character_effect(seg, name)`. Face-attached effects need a visible face. Verified in-app.
- Video scene effects: 2360 indexed (1361 new names).
- Draft plaintext copy (`draft_info.skill.json`) written on every save; inspector/clone/export-srt fall back to it for drafts JianYing has encrypted. Loading an encrypted draft no longer silently recreates (wipes) it; continuing requires `continue_from_skill_copy=True` and backs up JianYing's version. New CLI: `jy_wrapper.py clone` and `jy_wrapper.py export-srt`.
- Panel sync appends newly seen free TTS voices to `tts_speakers.csv`.
- Workflows: `smart_zoomer.py` import fixed (it could not run), click markers now rendered (it called a non-existent `add_sticker_at`), new `jy_wrapper.py apply-zoom` CLI; `smart_rough_cut.py` no longer requires Gemini (`--extract-frames` for an AI assistant to pick highlights, `--clips` to build the draft) and its text style args are applied; `movie_commentary_builder.py` subtitle mask is positioned correctly. Web VFX recording verified with Playwright 1.52.
- Basic masks prefer the locally downloaded 11.x version (the legacy 爱心 mask does not render in 11.6); `rect_width`/`round_corner` work with the 11.x rectangle.

### v1.8.0 - 2026-10-19
- **JianYing 11.x Panel Assets**:
  - New `scripts/sync_jy_panel.py`: read-only extraction of JianYing 11.x panel caches (`Cache/ressdk_db/*/rp.db`) into `data/panel_*.csv` (transitions, scene effects, video/text animations, fonts, TTS voices, audio effects, flower texts, text templates, stickers, sound effects, masks, blend modes).
  - Name resolution falls back to the panel index and registers new assets as real `pyJianYingDraft` enum members, so all existing APIs accept 11.x asset names.
- **VIP Assets**:
  - Fixed `data/*.csv` marking every asset as `Free`; VIP flags now come from enum metadata (`tools/refresh_enum_vip_flags.py`).
  - `asset_search.py` shows a VIP column and supports `--free-only` / `--vip-only`; it also searches the `panel_*` indexes.
  - `save()` reports VIP assets used (`vip_assets`); `allow_vip=False` / `JY_ALLOW_VIP=0` restricts resolution to free assets.
  - New 11.x VIP TTS voices are indexed but not synthesized outside JianYing (use in-app 文本朗读); 34 new free voices added to `tts_speakers.csv`.
- **Multi-track**: `add_overlay`, `add_split_screen`, `add_video_animation`, `add_segment_effect`, `add_filter_simple`, `add_mask_simple`, `add_keyframes`, `add_ken_burns`, `set_background_fill`.
- **AI**: `add_auto_subtitles` (local faster-whisper ASR -> subtitle track + SRT).
- **Fixes**: `add_effect_simple` now registers the effect material (effect-track segments previously referenced a missing material); `add_text_simple(font="名称")` accepts font names.
 
### v1.7.0 - 2026-09-11
- **Draft Self-containment & Media Missing Fix** (Contributed by @shaozheliu, #23):
  - Fixed JianYing Pro 5.9+ "media missing" error caused by empty `local_material_id` and external transient file cleanup.
  - Generates stable, non-empty `local_material_id` based on filename stem for `VideoMaterial` and `AudioMaterial`.
  - Automatically stages imported assets into the draft's internal directory to ensure self-contained project bundles.
  - Removed dummy cloud music fallbacks (`cloud_music_xxx.mp3`); aborts cleanly on download failure to prevent corrupted drafts.
- **macOS Compatibility & Resilient Media Probing** (Contributed by @twodogegg, #20):
  - Added primary detection for modern macOS JianYing draft root and `.agents` skill installations.
  - Added `ffprobe` fallback probing when `pymediainfo` or `libmediainfo` is unavailable.
  - Added video geometry and codec normalization for odd dimensions or incompatible streams.
  - Added safe platform detection for auto exporter with clear unsupported notices on non-Windows environments.
  - Expanded unit test coverage with 19 test cases.

### v1.6.0 - 2026-04-19
- **Core Enhancements**:
  - **Intelligent TTS (Narrated Subtitles)**: Unified `add_narrated_subtitles` API for one-click script-to-video workflow.
  - **Auto-healing System**: Modernized draft generator to support `draft_info.json` (v5.9+) and automatic repair of corrupted projects.
- **MacOS Compatibility**:
  - Full path resolution for Apple Silicon and Intel Macs.
  - Integrated `avfoundation` for high-performance screen recording on macOS.
- **Ecosystem Tools**:
  - `build_cloud_music_library.py`: Automated scanning of local drafts to index used cloud assets.
  - `web_recorder.py`: Pro-grade recording engine for web-based VFX assets.
- **Bug Fixes & API Polish**:
  - Fixed `pyJianYingDraft` export issues for `Transition`, `Filter`, and `Mask`.
  - Refactored `VfxOpsMixin` to use correct segment-level API calls.
  - Improved error handling for cloud music fallbacks.

### v1.5.0 - 2026-03-04
- Security hardening:
  - sanitized draft project names and blocked path traversal/out-of-root delete.
  - restored TLS verification for SAMI TTS by default.
  - added cloud download URL/header/size guards.
- API/CLI standardization:
  - unified machine-readable `--json` output for key scripts.
  - added strict mode for validator (`--strict`).
  - centralized runtime config (`scripts/utils/config.py`).
- Quality engineering:
  - expanded unit tests for security guards.
  - added repo hygiene and data schema checks.
  - added CI lint/format/test/schema pipeline.
- Repo organization:
  - removed tracked runtime artifacts and cache binaries.
  - added compatibility wrappers and common logger utility.
