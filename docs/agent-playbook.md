# Agent Playbook

Task routing matrix for reliable execution.

## 0) Quick Edit Runtime Template (Default for "来个剪辑")

Use this fixed sequence:

1. Environment check (minimal):
   - verify Python works
   - verify draft root exists
2. Asset resolution:
   - use explicit local/cloud assets
   - when the user named a specific asset, use it directly
   - when you have to choose a **visual asset** yourself (template, 花字, bubble, sticker, effect, transition, animation, font, filter, mask, smart relight), follow section 7: never choose by name alone
3. Script assembly:
   - generate one runnable script with deterministic APIs
4. Execute once:
   - run script and capture success/failure output
5. Acceptance check:
   - validate draft and track structure (see checklist below)
6. **Layout review (mandatory, see `rules/review.md`)**:
   - `python scripts/layout_preview.py --draft "<name>" --auto`
   - **Read the generated contact sheet image**, not just the text report
   - exit code 1 -> fix parameters and regenerate; never ship a failing draft

### Acceptance Checklist (Mandatory)

- Draft folder exists under JianYing drafts root
- `project.save()` completed successfully
- At least one `video` track segment exists
- If BGM exists, it must be on `audio` track (not `video`)
- If narration exists, subtitle segments should exist and be time-aligned
- Every visual asset you chose yourself was picked from a cover contact sheet (section 7), and the choice is stated to the user
- `scripts/layout_preview.py --auto` exits 0: no flash frames, no muted-overlay/volume conflicts, no safe-area or font-size violations
- The contact sheet was actually viewed with Read before reporting completion
- Portrait projects (1080x1920): main video material is 1080x1920 in the draft, not 1920x1080 (see `rules/review.md` for the normalizer bug)
- Image overlays have their intended duration, not the clamped 0.04s (see `rules/review.md`)

## 1) Cloud Video + BGM Draft

- Read: `rules/setup.md`, `rules/media.md`, `rules/audio-voice.md`
- Reference: `examples/cloud_video_music_tts_demo.py`
- Required checks:
  - BGM goes to audio track.
  - project saved successfully.

## 2) Narration + Subtitle Alignment

- Read: `rules/text.md`, `rules/audio-voice.md`
- APIs: `add_tts_intelligent`, `add_narrated_subtitles`
- Required checks:
  - subtitle timing aligns with narration.
  - subtitle track exists.

## 3) Recording + Smart Zoom

- Read: `rules/recording.md`
- Execute: `tools/recording/recorder.py`
- Required checks:
  - recording output exists.
  - generated draft can be opened.

## 4) Effects / Transitions

- Read: `rules/effects.md`, `rules/keyframes.md`
- Tools: `scripts/asset_search.py`
- Required checks:
  - selected IDs resolved.
  - no None returns from effect/transition methods.

## 5) Commentary from Long Video

- Read: `rules/generative.md`
- Prompt: `prompts/movie_commentary.md`
- Execute: `scripts/movie_commentary_builder.py`
- Required checks:
  - input media pre-optimized (<=30 min, 360p preferred).
  - storyboard JSON parsed and applied.

## 6) Export and CI-facing Validation

- Read: `rules/core.md`, `rules/cli.md`
- Execute:
  - `scripts/auto_exporter.py`
  - `scripts/api_validator.py --json`
- Required checks:
  - output file exists.
  - CLI return code and JSON contract are valid.

## 7) Choosing Visual Assets (Mandatory when you pick them yourself)

Names such as 「会话01」「标题59」「KkQ1-4」 say nothing about how an asset looks, so always look at the covers first.

1. Shortlist by purpose and name/category (aim for 12–48 candidates):
   `python scripts/asset_search.py "<关键词>" -c <panel_xxx>`. For templates you can add `--ready` (only usable ones).
2. Build a contact sheet and **Read the image**:
   `python scripts/preview_assets.py "<关键词>" -c <panel_xxx> -l 24 --cols 6`
   - animated assets (templates, text/video animations, transitions, effects, smart relight): add `--frames 3`
   - specific candidates: `--ids <resource_id ...>`
3. Pick 1–3 by style, color, and fit with the footage (tone, background, text length). Prefer free assets unless the user is fine with VIP. Avoid assets without covers.
4. Tell the user what you picked and why. For prominent choices (title templates, opening effects), show the contact sheet path and let them confirm.
5. After saving, remind the user that the final look depends on their text and footage, and should be checked in JianYing.

Assets without covers (parameter-only settings such as 调节/HSL/曲线/阴影, or legacy enum entries) are chosen from color/editing knowledge. Audio (music, SFX, TTS voices) cannot be previewed; pick by name/category/duration and ask the user to listen.

