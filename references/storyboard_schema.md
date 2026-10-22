# 分镜表格式（storyboard v2）

分镜表是剪辑的**唯一真相来源**：引导模式第 4 步由 Agent 填写、用户确认，第 5 步由 `scripts/storyboard_builder.py` 确定性装配。要改剪辑就改分镜表重跑，不要手写剪辑脚本。

**时间一律用「源视频时间」（秒）**，可以直接抄转写或 `analysis.json` 的时间戳。剪掉气口后，装配器自动换算成成片时间：落在被剪掉区间里的时间会吸附到下一段开头，跨剪点的元素按成片时长计算。

完整示例：`references/storyboard_template.json`（古韵口播，含所有字段）。

## 顶层

```jsonc
{
  "version": 2,
  "project": {
    "name": "草稿名", "canvas": {"width": 1080, "height": 1920},
    "scene": "口播知识",          // references/scenes/ 里的场景
    "style": "guofeng",           // references/styles/ 里的风格
    "platform": "douyin",         // 安全区按平台校验 (douyin / bilibili / xiaohongshu / shipinhao)
    "allow_vip": true
  },
  "sources": {"main": "主视频.mp4", "img1": "图1.jpg"},   // 相对分镜表所在目录
  "main": { ... },          // 主轨: 保留区间 / 转场 / 调色 / 音频处理 / 后期运镜
  "shots": [ ... ],         // 镜头: 每个镜头说清用途, 挂插卡 / 文字 / 特效 / 贴纸 / 音效
  "subtitles": { ... },     // 字幕
  "audio": {"bgm": { ... }}
}
```

## main

| 字段 | 说明 |
|---|---|
| `source` | `sources` 里的键 |
| `keep` | 保留的源区间 `[[开始, 结束], ...]`，省略表示整段。剪气口 = 把气口两侧拆成两段 |
| `transitions` | 剪点转场。`{"name": "水墨", "duration": 0.5}` 用于所有剪点；或列表按剪点顺序，`null` 表示硬切 |
| `volume` | 原声音量（默认 1.0） |
| `background` | 背景填充（主视频缩小、比如 `pip` 小窗时露出的区域，不设就是黑色）：`{"type": "blur", "blur": 0.75}`（画面自身模糊，取值 0.0625 / 0.375 / 0.75 / 1.0）或 `{"type": "color", "color": "#F5F1E8"}` |
| `grade` | 调色：`adjust`（面板名称和数值，如 `{"色温": -8, "饱和度": 5}`）、`filter`（`{"name", "intensity"}`）、`beauty`（如 `{"磨皮": 30}`） |
| `audio` | 原声处理：`denoise`、`vocal_beautify`、`loudness_lufs`（如 -14） |
| `camera` | 后期运镜（关键帧模拟），见下 |

**camera**：`[{"at": 源时间, "dur": 秒, "move": 名称, "amount": 幅度, "ease": 关键帧曲线}]`

| move | 效果 | 默认 amount |
|---|---|---|
| `push_in` | 缓推：缩放 1 → 1+amount | 0.10 |
| `pull_out` | 缓拉：1+amount → 1 | 0.10 |
| `punch_in` | 强调推近：0.15s 推到 1+amount，保持，段末回到 1 | 0.15 |
| `pan_left` / `pan_right` | 放大后横向平移 | 0.10 |
| `tilt_up` / `tilt_down` | 放大后纵向平移 | 0.10 |
| `shake` | 放大 1.06 后短促抖动，配重音或音效 | 0.02 |
| `pip` | 真人小窗：0.3s 缩到 `scale`（默认 0.4）并移到 `pos`（默认 `[0.55, -0.45]` 右下），保持，段末恢复全屏。配合插卡放大使用 | — |

`ease` 取剪映「关键帧曲线」名称：线性、缓入、缓出、缓动、二次方/三次方缓入/缓出/缓动、回弹离开。默认缓动。

## shots

```jsonc
{
  "id": "S03", "time": [9.5, 15.9], "purpose": "论证-溯源",
  "line": "Many Chinese characters started as pictures",   // 台词 (给人看的, 装配器不用)
  "note": "为什么这样剪",                                   // 给人看的
  "overlays": [ ... ], "texts": [ ... ], "effects": [ ... ], "stickers": [ ... ], "sfx": [ ... ]
}
```

**overlays**（插卡 / 画中画 / B-roll）：

| 字段 | 说明 |
|---|---|
| `source`, `start`, `dur` | 素材和源时间。插卡 3 秒以上 |
| `pos` | `[x, y]`，剪映坐标（中心为 0，右 / 上为正，半个画布为 1） |
| `scale` | `[开始, 结束]`，相同表示不缩放 |
| `fit` | `"cover"`：整屏切图，自动换算成铺满画布（`pos` 忽略，`scale` 只用来表示缓推比例，如 `[1, 1.08]`） |
| `motion` | `float_in`（上滑淡入 + 缓推）/ `push`（只缓推）/ `fade`（淡入淡出）/ `none` |
| `shadow` | `true` 或 `{"alpha", "blur", "distance", "angle"}` |
| `mute` | 默认 `true`（覆盖层视频静音） |
| `label` | 插卡标注，字段同 texts，默认跟随插卡时间 |
| `track` | 默认 `Cards` |

**texts**（标题 / 点题大字 / 词汇卡 / CTA）：

| 字段 | 说明 |
|---|---|
| `content`, `start`, `dur` | 文字和源时间 |
| `role` | `title` / `hero` / `label` / `vocab` / `cta`（决定默认轨道，审片器按角色查字号） |
| `x`, `y`, `scale` | 位置和缩放 |
| `size`, `font`, `color`, `bold`, `align` | 字号、字体（面板名称）、颜色 `#RRGGBB`、加粗、对齐（0 左 / 1 中 / 2 右，默认 1） |
| `flower`, `bubble`, `border` | 花字、气泡、描边 `{"color", "width", "alpha"}` |
| `anim_in`, `anim_out`, `anim_loop` | 文字动画名称 |
| `template` | 改用模板：`{"kind": "subtitle" 或 "text", "name": 模板名}`。这时 `content` 可以是字符串或列表（多段文字按顺序填，`null` 保留原文字） |
| `max_line_width`, `wrap_chars` | 行宽和手动折行字数 |

**effects**：`{"name", "start", "dur"}`，氛围特效 3 秒以上。**stickers**：`{"name", "start", "dur", "pos", "scale", "anim_in", "anim_out", "anim_loop"}`。**sfx**：`{"name" 或 "path", "at", "volume", "dur"}`（名称在 `cloud_sound_effects.csv` 里搜）。

## subtitles

```jsonc
{
  "lines": [{"start": 0.0, "end": 4.4, "text": "你觉得汉字很难记吗？", "text2": "Do you think..."}],
  // 或者直接从转写生成: "from_transcript": "analysis.json", "max_chars": 15, "max_dur": 3.2
  "lead": 0.2,                          // 字幕提前出现的秒数
  "primary":   {"size": 9.5, "y": -0.44, "font": "...", "border": true},
  "secondary": {"size": 6.8, "y": -0.58, "wrap_chars": 26}   // text2 的样式 (双语)
}
```

字幕结尾自动不超过下一句开始。也可以在 `primary` 里给 `template` 用字幕模板。

## audio

```jsonc
{"bgm": {"id": "7376298242855946291", "volume": 0.22, "fade_in": 1.2, "fade_out": 2.5}}
```

`id` 是云端曲库 ID（`cloud_music_library.csv`），也可以用 `query`（曲名）或 `path`（本地文件）。

## 装配结果

- 生成草稿后自动跑审片器（`layout_preview.py --auto`），退出码非 0 表示有版式问题。
- 加 `--preview` 会在剪映里截取每个镜头的画面（macOS）。
- 装配报告写在 `<分镜表>.build.json`：每个镜头在成片里的开始时间和时长、运镜、字幕条数，以及警告（找不到的素材、太短的插卡 / 特效、被剪点截断的文字）。
