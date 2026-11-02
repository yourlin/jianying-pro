# 参考视频结构反推（T9，分组 reference）

## 用途

`scripts/reference_analyzer.py` 读一条参考短视频（mp4 / mov），反推出它的剪辑结构，生成可以「照着这条剪」的 scene / style 草案：

- **镜头序列**：每个镜头的起止时间、时长、运镜（static / push_in / pull_out / punch_in / pan_left / pan_right / tilt_up / tilt_down / shake）、运动强度、亮度、饱和度、平均色。镜头时长分布给出 p10 / p25 / 中位 / p75 / p90、CV 和分桶直方图。
- **转场粗分类**：`hard` 硬切、`dip_black` 闪黑 / 黑场、`dip_white` 闪白、`dissolve` 叠化、`push` / `pull` 快速推拉。每类都映射到剪映的转场名（叠化 / 闪黑 / 闪白 / 推近 / 拉远）。片头淡入、片尾淡出单独放在 `edges` 里，不算镜头之间的转场。
- **切点与 BGM 节拍的对齐程度**：用 `beat_detect.detect_beats` 分析参考视频的音轨，统计切点落在拍点 / 强拍 / 起音点上的比例，再和随机切点的期望命中率比较，给出标签：强卡点 / 部分卡点 / 不卡点。同时估算「每几拍切一刀」，以及高 / 中 / 低能量段各自的切速。
- **字幕出现节奏**：在顶部 / 中部 / 底部三个横带里检测文字，给出每个横带的文字占比、出现时段、换字时刻、换字间隔中位数和画面位置（`transform_y`），并推断字幕类型：`full` 全程字幕 / `labels` 标签或花字 / `titles` 只有标题 / `none` 无字。OCR 可选（`--ocr`），按需加载。
- **钩子段（前 3s）**：切点数、首镜头时长、相对全片的运动强度和亮度、各横带的文字覆盖率、开头是否黑场、首个声音出现的时间。据此打上钩子类型（快切蒙太奇 / 大字标题 / 字幕口播直入 / 强动作开场 / 转场冲击 / 单镜头平铺），并按 `references/methodology/hook.md` 检查问题，例如「开头黑场超过 0.3s」。
- **整体节奏标签**：快闪 / 快节奏 / 中等 / 舒缓 / 长镜头。附加标签有「卡点」、「越剪越快」 / 「前快后慢」、「长短错落」。
- **结构分段**：开场钩子 → 主体（有音乐高潮时拆出「高潮」段；没有时按镜头时长变化拆成快切段 / 铺陈段）→ 收尾。所有分段边界都对齐到切点。
- **scene / style 草案**：与 `references/scenes/*.json`、`references/styles/*.json` 结构相同。以最接近的内置场景和风格为底板，再用实测值覆盖。
- **缩略图联系表**：`contact_sheet.jpg` 放每个镜头的中间帧，每张标注序号、起点、时长、入场转场、运镜。`hook_strip.jpg` 是前 3 秒每 0.5s 一帧。调用方 agent 可以 Read 这两张图，补充机器判断不了的内容（景别、人物、题材）。

## 用法

### CLI

```bash
python scripts/reference_analyzer.py ref.mp4                       # 输出到 ref.reference/
python scripts/reference_analyzer.py ref.mp4 -o out/ --name 爆款A --scene auto --style auto
python scripts/reference_analyzer.py ref.mp4 --beats ref.beats.json # 复用已有节拍
python scripts/reference_analyzer.py ref.mp4 --no-beats --no-sheet --json
python scripts/reference_analyzer.py ref.mp4 --ocr                  # 需要 rapidocr_onnxruntime 或 tesseract
python scripts/reference_analyzer.py ref.mp4 --name 爆款A --install # 草案写进 references/, script_compiler 直接可用
```

参数说明：`--scene` / `--style` 指定底板（默认按分析结果推断）；`--threshold` 是 ffmpeg scene 阈值（默认 0.3）；`--fps` 是分析帧率（默认取源帧率但不超过 30，视频超过 90s 时用 15）；`--backend auto|librosa|numpy` 选节拍检测后端。`--json` 遵守 `{ok, code, reason, data}`，失败时 `code=reference_analyze_failed`。

### Python

```python
from reference_analyzer import analyze_reference
rep = analyze_reference("ref.mp4", "out/", name="爆款A", backend="auto")
rep["shot_stats"]["median"], rep["beat"]["label"], rep["hook"]["types"], rep["rhythm"]["label"]
rep["files"]  # report / scene / style / contact_sheet / hook_strip
```

### 「照着这条剪」

1. 跑 `reference_analyzer.py ref.mp4 --name 爆款A --install`。
2. 剧本里写 `"scene": "ref_爆款a"`，并用 `--style ref_爆款a` 编译，或者把草案里的值手动合并到现有场景。草案 id 由 `--name` 经 slug 处理得到：非 ASCII 字符会被替换掉，所以中文名建议同时带一个英文标识，比如 `--name hot_a`。
3. 卡点混剪：`beat_snap.py plan --density <rep.beat.cut_every_beats>`，用参考的切速来切。
4. 看 `contact_sheet.jpg`，补充景别、题材等判断。

## 输出格式

`reference.json`（节选）：

```json
{"schema": 1, "source": "/abs/ref.mp4",
 "video": {"width": 360, "height": 640, "fps": 30.0, "duration": 12.0, "has_audio": true, "analysis_fps": 30.0},
 "shots": [{"index": 0, "start": 0.0, "end": 1.0, "duration": 1.0, "thumb_t": 0.5, "transition_in": null,
            "motion": 0.1, "camera": {"move": "static", "zoom_per_s": 0.0, "pan_per_s": 0.0, "tilt_per_s": 0.0, "jitter": 0.0},
            "luma": 0.35, "saturation": 0.8, "color": "#C82828"}],
 "shot_stats": {"count": 9, "mean": 1.33, "median": 1.485, "p10": 1.0, "p25": 1.0, "p75": 1.5, "p90": 1.52,
                "min": 1.0, "max": 1.52, "cv": 0.18, "histogram": {"<0.5s": 0, "0.5-1s": 0, "1-2s": 9}},
 "cuts": [1.0, 2.0, 3.0, 4.517, 6.015, 7.5, 9.017, 10.5],
 "transitions": [{"t": 4.517, "type": "fade", "style": "dip_black", "label": "闪黑 / 黑场", "jy_transition": "闪黑",
                  "duration": 0.567, "window": [4.233, 4.8]},
                 {"t": 7.5, "type": "zoom", "style": "push", "jy_transition": "推近", "zoom_rate_per_s": 1.32}],
 "transition_counts": {"hard": 4, "dip_black": 1, "dissolve": 1, "push": 1, "dip_white": 1},
 "dominant_transition": "dip_black", "hard_cut_ratio": 0.5, "edges": [],
 "beat": {"tempo": 120.06, "beat_interval": 0.4998, "label": "强卡点", "aligned_ratio": 1.0, "chance_ratio": 0.28,
          "alignment_score": 1.0, "downbeat_ratio": 0.5, "onset_ratio": 1.0, "tolerance_s": 0.07, "mean_offset_ms": 4.0,
          "cut_every_beats": 3, "cuts_per_s_by_energy": {"mid": 0.67}, "climax": null, "offsets_s": [0.0]},
 "text": {"kind": "full", "main_band": "bottom", "top_title": false,
          "bands": {"bottom": {"present_ratio": 1.0, "segments": [[0.0, 12.0]], "events": [{"t": 1.4, "at_cut": false}],
                               "median_interval": 1.4, "per_min": 45.0, "y": 0.8, "transform_y": -0.6}}},
 "motion": {"avg": 0.11, "level": "low", "curve_step_s": 0.5, "curve": [0.0], "camera_moves": {"static": 8, "pan_right": 1}},
 "hook": {"window": [0, 3], "cuts": 2, "first_shot_duration": 1.0, "motion_vs_overall": 0.3,
          "text_coverage": {"top": 0, "middle": 0.67, "bottom": 1.0}, "opening_fade": null,
          "types": ["大字标题", "字幕口播直入"], "issues": []},
 "rhythm": {"pace": "快节奏", "label": "卡点快节奏", "tags": ["卡点", "快节奏"], "trend": "均匀", "shots_per_min": 45.0},
 "color": {"mean_rgb": "#7F7F7F", "luma": 0.5, "saturation": 0.6, "contrast": 0.1, "warmth": 0.05,
           "palette": [{"color": "#C82828", "share": 0.08}]},
 "structure": [{"name": "开场钩子", "ratio": 0.25, "purpose": "钩子: 大字标题、字幕口播直入", "required": ["title"],
                "allowed": [], "forbidden": [], "camera": null, "note": "参考 0.0–3.0s: 3 个镜头, 中位 1.00s (≈ 每 2 拍切)",
                "span": [0.0, 3.0], "avg_shot": 1.0}],
 "scene_guess": "beat_montage", "style_guess": "variety",
 "drafts": {"id": "ref_synthetic", "scene_base": "beat_montage", "style_base": "variety"},
 "files": {"report": "...", "scene": "...", "style": "...", "contact_sheet": "...", "hook_strip": "..."},
 "notes": ["节拍: beat_detect (numpy)"]}
```

**scene 草案**：保留底板场景的全部字段，以下字段用实测值覆盖或新增：

- `id` / `name` / `derived_from`
- `duration`：参考时长 ±20%
- `pacing.avg_shot`（取 [p25, p75]）、`pacing.median_shot`、`pacing.pattern`；卡点视频再加 `pacing.cut_every_beats.参考`
- `beats`：上面的结构分段，字段与内置场景相同，另有 `span` / `avg_shot`
- `camera.allowed` / `camera.per_minute`：取参考里实际出现的运镜
- `subtitles.default`（`full` 对应 true，`labels` 对应 "labels"，其余为 false），以及 `max_dur`、`reference_transform_y`
- `audio.reference_tempo`
- `transitions`：统计信息
- `checks`：追加「镜头中位时长 ≈ …」、「前 3 秒 N 个切点」、「切点卡拍」、「以硬切为主」
- `reference`：摘要

**style 草案**：保留底板风格的全部字段，以下字段用实测值覆盖或新增：

- `cut_transition`：硬切占比 ≥ 70% 时为 null，否则取主导转场及其实测时长
- `chapter_transition`：第二种转场
- `grade.adjust`：由均值亮度 / 对比度 / 饱和度 / 冷暖换算出亮度、对比度、饱和度、色温，滤镜沿用底板白名单
- `palette`：主色
- `camera.amount` / `camera.punch`：由实测推拉速度得到
- `subtitle_position`
- `reference.color`

## 算法要点

- 解码：ffmpeg 输出 320 宽的 rgb24 帧，流式处理。逐帧只保留 80 宽灰度小图和统计量（亮度分位数、饱和度、48 维直方图、三条横带的文字分数和边缘签名）。
- 硬切：候选来自 `analyze_footage.detect_scenes`（ffmpeg scene）加上自己的帧差尖峰。校验条件是「这一帧的帧差 ≥ 前后 3 帧中位数的 2.5 倍」，这样可以排除快速推拉、渐变造成的连续大帧差。
- 闪黑 / 闪白：判定用 p90 < 0.1（黑）或 p10 > 0.9（白），因为叠在画面上的字幕不影响分位数。再向两侧扩展到亮度单调变化的帧，得到整个淡出加淡入的窗口。
- 叠化：先找两端帧差最大的窗口，再检验中间帧能否写成 `(1-α)·首 + α·尾`：要求残差 < 20%、α 单调且没有跳变、两端直方图差 ≥ 0.15，并且两端之间不是单纯的平移关系。这几条用来排除镜头内的平移 / 运动。
- 快速推拉：在切点前后 0.3s 内逐帧估计中心缩放（±12% 网格加抛物线细化）。任意一侧中位速度 ≥ 35%/s 且方向一致，就判为推 / 拉。
- 运镜：镜头内每 0.2s 取一对帧。先用相位相关估平移，抵消平移后再估缩放，最后取逐对速度的中位数。有静止字幕的横带会先填成均值，否则相位相关会锁定在不动的字上。
- 卡拍：容差 = max(70 ms, 2 帧)。随机切点的命中率 = 2·容差 / 拍间隔，`alignment_score = (命中率 - 随机命中率) / (1 - 随机命中率)`。
- 字幕：在若干连续行内统计强水平梯度（|Δx| > 60）的最大平均密度，减去全帧行密度的中位数（用来去掉纹理背景）。横带边缘签名（12×80）的归一化 L1 变化 > 0.18 记为一次换字。

合成测试视频（9 个镜头、8 个已知切点，覆盖 4 种转场加推近和平移，并带字幕和 120 BPM 点击音轨）的结果：切点误差 ≤ 17 ms，转场类型全部正确，卡拍 100%，字幕换字间隔 1.4s 准确测出，整段分析约 3s。

## 限制

- 转场分类是粗分类。擦除、滑动、故障、遮罩类转场多半会被识别成硬切，或者归入最接近的一类。极短的叠化（< 0.15s）也会被当成硬切。
- 字幕检测是启发式的。画面里本身就有大量横向高对比纹理（键盘、栅栏、文字背景板）时可能误报。会动的花字、竖排文字识别不准。底部有固定 logo 或水印时，会被当成「全程有字」。OCR 是可选功能，默认不开。
- 运镜估计假设变换是全局的（整体缩放加平移）。主体运动大、画面分屏或画中画时会被判为 static 或 shake；纯色、无纹理的镜头估不出运动。
- 节拍分析依赖 beat_detect：BGM 被人声盖住或没有鼓点时，tempo 和卡拍结论不可靠，这时 `beat.label` 可能是「不卡点」或「无法判断」。音轨很安静时会跳过节拍分析，并在 notes 里说明。
- 场景 / 风格推断只看镜头时长、字幕、色彩统计，不理解题材。底板推断错了用 `--scene` / `--style` 指定即可，实测覆盖的字段不受影响。
- 草案 id 只保留 ASCII 字符（`ref_<slug>`）。
- 长视频会按 15 fps 分析，这时 < 0.15s 的闪白可能漏检。分析 60s、30fps 的视频约占 30 MB 内存。

## 给主线的集成建议（T18）

- **SKILL.md Agent Quick Routing** 加一行：
  `| 照着这条剪 / 参考视频 / 拆解爆款结构 / 仿剪 | python scripts/reference_analyzer.py ref.mp4 --name X --install → 看 contact_sheet.jpg → 剧本 scene/style 用 ref_x |`
- **rules**：在 `rules/cli.md` 加「Reference analyzer」一节（支持 `--json`，失败码 `reference_analyze_failed`）。引导模式第 ② 步选场景 / 风格时，如果用户给了参考视频，先跑本工具，用草案作为默认选项。可以在 `references/scenes/README.md` 和 `references/styles/README.md` 补一句：`ref_*.json` 由 reference_analyzer 生成。
- **Mixin**：无。本功能不改草稿，不需要注册 Mixin。
- **requirements.txt**：不需要改，只用到 numpy、Pillow（已有）和 ffmpeg。可选依赖可以加注释：`# Optional: 参考视频字幕 OCR (reference_analyzer --ocr) # rapidocr_onnxruntime`。librosa 仍然是可选依赖，见 beat.md。
- **script_compiler**（本分组未改动）：`load_profile` 直接读 `references/<kind>/<id>.json`，所以用 `--install` 生成的 `ref_*` 草案无需改代码就能用。可以考虑把 `pacing.median_shot`、`subtitles.reference_transform_y` 接进 B-roll 插入间隔和字幕位置的默认值。
- **CHANGELOG**：新增参考视频结构反推（镜头 / 转场 / 卡拍 / 字幕节奏 / 钩子 / 节奏标签，scene 与 style 草案，镜头联系表）。
