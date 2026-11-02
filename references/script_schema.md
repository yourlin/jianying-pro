# 剧本格式（script.json）

每个剪辑项目一份剧本，只写**讲什么、每段出现什么**，不写坐标、字号、字体、花字。这些由三层规范决定：

| 层 | 位置 | 决定什么 |
|---|---|---|
| 场景 | `references/scenes/<id>.json` | 段落结构、节奏阈值（剪气口）、各元素的时长约束、插卡策略、允许的运镜、字幕和音频默认值、检查规则 |
| 风格 | `references/styles/<id>.json` | 各角色（标题 / 点题字 / 标注 / 词卡 / CTA / 字幕）的字体、花字、字号、动画；剪点转场、结尾特效、调色、小窗背景、配乐、音效、运镜幅度 |
| 画幅 | `references/formats/<id>.json` | 画布、安全区、位置槽（标题带 / 主区域 / 下巴下方 / 字幕位）、真人小窗、插卡大小、字幕字号下限 |

编译：

```bash
python scripts/script_compiler.py script.json -o storyboard.json [--build] [--preview]
python scripts/script_compiler.py script.json --format horizontal --style tech --build   # 同一剧本换画幅 / 风格
python scripts/script_compiler.py script.json --lang en                                   # 多语言版本: 只换文字, 时间线不变
```

编译器还会读素材分析（`analysis.json`）：
- 用转写里的台词定位时间；
- 用人脸位置让词卡避开下巴、插卡避开脸；
- 按场景的节奏阈值找出要剪的停顿。

换画幅或风格时，草稿名会自动加后缀，不会覆盖原草稿。编译出的分镜表带 `compiled_from` 标记；编译器不会覆盖手写的分镜表。

## 字段

```jsonc
{
  "title": "汉字古韵 · 人字篇",           // 剧本标题
  "name": "汉字古韵·人字篇V6",            // 生成的草稿名
  "logline": "一句话讲清这条视频讲什么",
  "audience": "外国人学中文",
  "scene": "talking_head",               // 或中文：口播知识 / vlog / 教程录屏 / 带货种草 / 影视解说 / 卡点混剪 / 剧情短片 / 宣传片 / MV（音乐视频）
  "style": "guofeng",                    // 国风 / 科技 / 清新 / 综艺 / 商务
  "format": "vertical",                  // 竖屏 / 横屏 / 小红书 / 方形（或 9:16 / 16:9 / 3:4 / 1:1）
  "platform": "tiktok",
  "pacing": "natural",                   // compact 紧凑 / natural 自然 / none 不剪（阈值见场景规范）
  "insert_mode": "pip",                  // 插卡方式：pip 真人小窗 / cover 整屏切图 / overlay 叠加（按人脸空白区放）
  "analysis": "analysis.json",
  "sources": {"main": "主视频.mp4", "img1": "图1.jpg"},
  "subtitles": {"lang": "en", "fixes": {"识别错的词": "正确写法"}, "exclude": [[开始, 结束]]},   // mode: "none" 表示不加
  "bgm": {"id": "..."},                  // 省略则用风格默认配乐；false 表示不要配乐
  "waits": [[40, 58]],                   // 可选：等待段（源视频秒）；也可以是 JSON 路径（读 waits 或 dead_zones，如 quality_review 的 report.json）
  "waits_mode": "跳剪",                  // 可选：跳剪 / 加速 / 保留（覆盖场景 pacing.wait.mode）
  "lang": "en",                          // 可选：输出哪种语言的文字（也可以编译时 --lang）
  "translations": {"en": {"原文": "译文"}},  // 可选：多语言对照表；元素也可以自带 i18n
  "montage": { ... },                    // 可选：混剪模式（宣传片 / MV / 卡点），见下
  "beats": [ ... ]
}
```

**等待段**（场景有 `pacing.wait` 时，目前是教程录屏）：超过 `wait.max_s`（5s）的等待，`cut` 跳剪只留头尾各 1s，`speedup` 中间加速到约 2s（2–8 倍，写进分镜表 `main.speed`）。没给 `waits` 时，用转写里超过 5s 的无旁白空档。

## montage（混剪模式）

宣传片、MV、卡点混剪这类「多段素材 + 一首音乐」的片子，没有主视频。写了 `montage` 后：

- 编译器调用 `scripts/beat_snap.py` 规划镜头（按拍切或按歌词行切），结果放在分镜表顶层 `montage`（和 `beat_snap.py plan` 的输出同一格式），`main` 标 `"montage": true`、`keep` 为 `[[0, 总时长]]`；
- **段落 `time` 用成片时间**（秒），或用歌词片段定位（`{"from": "歌词", "to": "歌词"}`）；段落开头会吸到最近的拍点 / 歌词边界，分镜表里的段落时间是吸附后的实际切点；
- 歌词同时生成歌词字幕（场景 `subtitles.default = "lyrics"` 时）。

```jsonc
"montage": {
  "music": "bgm.mp3",                    // 音乐文件
  "beats": "bgm.beats.json",             // 可选：beat_detect.py 的结果；省略则现场检测
  "clips": "clips/",                     // 素材目录，或 [{path, duration?, score?, highlight?}]
  "lyrics": "lyrics.json",               // 按歌词切时必填：[{start, end, text}]（音乐时间），或带 transcript 的 JSON
  "cut_on": "lyric",                     // 可选：beat 按拍 / lyric 按歌词行（默认取场景 pacing.cut_on）
  "start": 0,                            // 可选：音乐起点（秒），或 "climax"
  "duration": 50,                        // 可选：成片时长；默认到最后一段结尾
  "density": "标准"                      // 可选：舒缓 / 标准 / 快闪（场景 pacing.cut_every_beats）
}
```

## beats（段落）

```jsonc
{
  "id": "S03", "purpose": "溯源",
  "time": {"from": "Many Chinese characters", "to": "everyday life"},   // 用台词定位；也可以写 [开始, 结束]（源视频秒）
  "camera": "push_in",                   // 段落开头的运镜；也可以写列表 [{"move", "time", "dur"}]
  "note": "为什么这样剪",
  "elements": [ ... ]
}
```

混剪模式下段落还可以写（不写就取场景里同名段落的默认值，按 `section` 或 `purpose` 匹配场景 `beats[].name`）：

| 字段 | 说明 |
|---|---|
| `section` | 对应场景段落名（如「蒙太奇」）；省略时用 `purpose` |
| `cut_every_beats` | 段内每几拍切一次；`0` 表示整段一个镜头（标题卡 / 片尾卡） |
| `equal_length` | 段内镜头同样的帧数（宣传片蒙太奇段） |
| `clips` | 这一段只用这几个素材（路径或 `sources` 的键）；整段不切的段落专用的素材，其他段落不会借用 |
| `transition` | 这一段开头的转场：`opening` / `ending` / `chapter`（取场景 `transitions` 的定义）、转场名，或 `null` 硬切 |
| `ui_insert` | 这一段是产品 UI 插入：进出各加一次快速推近 / 拉远（MV 只在这里推拉） |

## elements（元素）

| kind | 字段 | 编译结果 |
|---|---|---|
| `title` | `text`，可选 `time` | 标题带上的标题；默认从段落开始后 0.3s 出现，时长按场景上限 |
| `hero` | `text`、`sub` | 点题大字 + 下方小字；`insert_mode` 为 pip 时，真人同时缩成小窗 |
| `card` | `asset`、`label`，可选 `time` / `mode` / `sfx: false` | 插卡：按素材方向选大小；按 `mode` 用小窗、整屏切图或叠加；自动配标注和风格的插卡音效 |
| `keyword` / `label` | `text`、`time` | 关键词或标注；风格的花字不适合亮背景时，自动改成白字黑描边 |
| `vocab` | `items: [{word, top, main, bottom}]` | 词卡：按 `word` 的词级时间戳卡点；上 / 中 / 下三行放在下巴下方；这一段不显示底部字幕 |
| `cta` | `text`、`sub`、`last`（最后几秒） | 互动卡；`insert_mode` 为 pip 时，最后 `last` 秒真人缩成小窗 |
| `end_card` | `tag`、`title`、`words`、`sub`、`last`（最后几秒，默认 6） | 结束卡：底卡 + 系列标签 + 大标题 + 词卡 + 小字；宣传片的 CTA 结尾卡至少停 3s |
| `effect` | `name`（默认 `ending`，即风格的结尾特效） | 段落末尾的特效，时长不少于风格规定 |
| `sfx` | `name`（`accent` / `card`，或具体音效名）、`time` | 音效 |

多语言：任何带文字的元素都可以加 `i18n`（如 `{"en": "..."}`），`sub` / `label` / `title` / `tag` 对应 `sub_i18n` 等；没写时查剧本 `translations`。换语言后副标题和主标题相同时（原版副标题就是译文），副标题不再重复出现。

时间写法（所有 `time` 都适用）：`[开始, 结束]`、`"台词片段"`，或 `{"from": "台词", "to": "台词"}`。台词按词匹配，不区分大小写和标点。

## 和分镜表的关系

剧本 → 编译 → 分镜表（`storyboard_schema.md`）→ 装配。一般只改剧本。需要精细调整某个镜头时，可以先编译出分镜表，复制一份另起名字，再手改，然后直接用 `storyboard_builder.py` 装配。
