# T8 成片质量审片 `scripts/quality_review.py`

## 用途

`layout_preview.py` 查的是**版面**（安全区、字号、重叠）；`quality_review.py` 查的是**好不好看、留不留得住人**。
它检查以下几类问题，规则取自 `references/methodology/{hook,pacing,edit_grammar,review_rules}.md`：

| 分类 | 检查 | 问题标签 |
|---|---|---|
| 钩子 (hook) | 首帧黑场 / 慢淡入；前 3 秒有无人声、有无文字；0–3s 的切镜数、新字幕数、视觉变化量、信息密度；「大家好 / 今天聊一下」式开场 | `首帧黑场` `慢淡入` `前3秒无人声无文字` `人声进入太晚` `前3秒画面静止` `寒暄式开场` |
| 节奏 (pacing) | 镜头（画面变化）时长分布，对照场景经验区间（口播 3–6s、vlog 2–4s…）；每秒节奏曲线（切镜 / 新字幕 / 人声 / 运动量 → 能量 0–1） | `节奏偏慢` `节奏过碎` `长镜头` `节奏单调` |
| 死区 (dead_zone) | 连续 ≥ N 秒（默认 3.5s）画面无明显变化（4×4 网格锚点差分），且没有新字幕、插卡、切镜。有意停留（分镜表 `intentional_holds`、自动识别的片尾 CTA 卡）覆盖的部分不算 | `死区`（无人声 = 高，有人声 = 中） |
| 连贯 (continuity) | 切点前后 Lab 亮度差 ≥ 25，或相对差 ≥ 35% 且绝对差 ≥ 8；色温 b* 差 ≥ 10，色偏 a* 差 ≥ 12。一侧是黑场 / 短闪白、切点 0.6s 内有闪黑闪白时不算。跳变的切点 ≥ 6 个且占 ≥ 40% 时视为有意的段落配色，合并成一条 low；分镜表声明 `continuity: per_section` 时段与段之间不查 | `亮度跳变` `色温跳变` `色偏跳变` `段落配色切换` |
| 音频 (audio) | 中段静音空洞 ≥ 0.8s；人声段 BGM 比人声低不足 12dB（草稿模式按分轨精确算，视频模式用转写的词间停顿估算底噪）；全片无声 | `静音空洞` `BGM压人声` `全片无声` |
| 结尾 (ending) | 最后一个新信息之后又拖了 ≥ 3s（有意停留 / CTA 卡不算）；片尾静音；BGM 提前结束或没有淡出；最后 20% 有没有 CTA | `结尾拖沓` `片尾静音` `配乐提前结束` `配乐未淡出` `没有CTA` |

评分：每类 100 分，扣分为高 30、中 15、低 5。总分按权重加权：钩子 0.30、死区 0.20、节奏 0.15、音频 0.15、连贯 0.10、结尾 0.10。没评估的类别不计入总分。等级：A ≥ 85，B ≥ 70，C ≥ 55，其余为 D。

## 用法

```bash
# 草稿模式: 用 layout_preview.build_frame 伪渲染画面 (默认 2fps), 音频按草稿轨道分轨 (voice / bgm / sfx) 叠加
python scripts/quality_review.py --draft "草稿名"
python scripts/quality_review.py --draft "草稿名" --drafts-root /path/to/drafts   # 测试 / 非默认目录

# 成片模式: ffmpeg 解码 (默认 10fps, > 600s 的视频自动降到总帧数 ≤ 6000, 最低 4fps), 自动找切镜
python scripts/quality_review.py --video out.mp4 --analysis analysis.json --subtitles out.srt

# 带分镜表 (script_compiler 写的 review 块): 标题卡钩子 / 有意停留 / 按段落查色调
python scripts/quality_review.py --video out.mp4 --storyboard promo.storyboard.json

# 混合: 画面和声音取成片, 字幕 / 切点 / BGM 轨道语义取草稿 (最准)
python scripts/quality_review.py --draft "草稿名" --video 导出.mp4

# 常用参数
--genre 口播知识|vlog|教程录屏|带货种草|影视解说|卡点混剪   (默认 auto)
--dead-zone 3.5   --sample-fps 10   --sheet-mode auto|second|shot   --max-tiles 120
--storyboard storyboard.json   (草稿模式默认找草稿目录里的 *storyboard*.json, 多个时优先 project.name 同名的)
--out DIR (默认 <tmp>/jy_quality_review/<名称>)   --report-only   --fail-on medium   --json
```

Python API：

```python
from quality_review import review
rep = review(draft="草稿名", drafts_root=None, video=None, analysis=None, subtitles=None,
             genre="auto", dead_zone=3.5, out_dir="/tmp/qr", storyboard=None)   # -> dict (同 report.json)
```

退出码与 `layout_preview` 一致：0 表示通过，1 表示存在严重度 ≥ `--fail-on`（默认 medium）的问题。另外 2 表示输入错误（找不到草稿或视频、草稿已加密、缺 ffmpeg）。

## 输出

`<out>/report.json`：

```json
{
  "tool": "quality_review", "version": 1,
  "source": {"mode": "draft+video", "draft": "...", "video": "...", "duration": 42.3, "canvas": [1080, 1920]},
  "genre": "口播知识",
  "review_hints": {"hook_type": "title_card", "holds": [{"start", "end", "why"}], "continuity": "per_section", "sections": [[0, 3.7], ...], "source": "...storyboard.json"},
  "score": {"overall": 72, "grade": "B", "by_category": {"hook": 70, "dead_zone": 85, "pacing": 100, "audio": 70, "continuity": 95, "ending": 85}},
  "hook": {"first_frame_L": 43.1, "cuts_0_3": 1, "texts_0_3": 2, "first_speech": 0.2, "info_density_per_s": 1.33, ...},
  "pacing": {"genre": "...", "target_shot_s": [3, 6], "shots": [{"index", "start", "end", "dur"}], "stats": {...}, "curve": [{"t", "cuts", "new_texts", "events", "speech", "motion", "energy"}]},
  "dead_zones": [{"start", "end", "dur", "speech"}],
  "continuity": {"cuts": [{"t", "dL", "da", "db"}]},
  "audio": {"holes": [...], "bgm_conflicts": [{"start", "end", "bgm_minus_voice_db"}], ...},
  "ending": {"last_info_at", "tail_after_last_info", "tail_silence_s", "has_cta",
             "cta_card": {"start", "end", "dur", "qr_density", "text_score"}, "storyboard_cta": [...]},
  "issues": [{"id": "Q001", "category": "hook", "type": "首帧黑场", "severity": "high", "start": 0, "end": 1.2,
              "message": "...", "suggestion": "...", "rule": "references/methodology/hook.md#黄金 3 秒"}],
  "timeline": {"cuts", "overlays", "texts", "speech", "speech_text"},
  "notes": ["被跳过的检查及原因"],
  "sheet": "<out>/viewer_sheet.png"
}
```

`<out>/viewer_sheet.png` 是「观众视角」联系表：视频 ≤ 120s 时每秒一帧，更长时每镜头一帧，并固定带上 0/1/2/3s 的钩子帧。每帧标注时间戳和镜头号；边框颜色表示该时段最严重的问题（红 = 高，橙 = 中，黄 = 低）；顶部蓝条标出前 3 秒钩子区，底部绿条是节奏能量；帧下方列出问题标签。调用方 agent **必须用 Read 看这张图**，判断观众会在哪里划走。

`timeline` 段是给 T17 `retention_feedback.py` 对齐用的。

## 切镜检测（视频模式）

10fps、96 宽解码（`-skip_loop_filter all` 加速），逐帧算以下几种差：像素差、RGB 直方图、**对比度归一化灰度差**（减均值除标准差，暗场同色调也能比）、4×6 粗网格均值差（构图）、色相直方图差（按色度加权，白底 / 黑白画面不比）。

- **硬切**：满足任一条即可。
  - 归一化差是局部尖峰：≥ 0.30 且 ≥ 前后 1s 中位数 × 4，或 ≥ 0.60 且 ≥ 中位数 × 2；同时粗网格差 ≥ 0.15，用来排除 AI 视频的抖动和细节闪烁。
  - 色相直方图突变 ≥ 0.30，例如游戏换关卡色。
  - 直方图差 ≥ 0.35 且像素差 ≥ 0.10，例如纯色块切换。
  - 变化只落在粗网格的一行、且不满一整行时不算，例如字幕条、下三分之一标签出现或消失。
- **软切**（叠化 / 淡入淡出）：前后各 0.4s 的首尾差大（≥ 0.25），路径「直」（逐帧差累计 ≤ 首尾差 / 0.6），原始灰度粗网格一半以上的格子在变（局部插图淡入不算），两端画面相对静止。
- **闪黑 / 闪白**：进出两刀间隔 ≤ 0.8s，且中间几乎是纯黑或纯白时，合并成一刀。

用户成片基准（`~/.config/jianying-pro/learned/benchmarks.md` Q1）。真值以 reference_analyzer 的切点为基础，对照联系表人工核对：同一个叠化 / 闪白记为一刀，补上了 3 个漏掉的录屏翻页。容差 ±0.25s：

| 视频 | 修复前 召回 / 误检 | 修复后 召回 / 误检 |
|---|---|---|
| 宣传片样本 50.9s 1080p | 13/20 = 65% / 1 | 19/20 = 95% / 1 |
| MV 样本 109.7s | 12/42 = 29% / 1 | 40/42 = 95% / 1 |
| PPT 演示录屏 227.7s 4K | 13/32 = 41% / 1 | 30/32 = 94% / 0 |

## 片尾 CTA 卡（视频模式自动识别）

满足以下全部条件时，认定为 CTA 卡，记进 `ending.cta_card`，并且不报死区和结尾拖沓：

- 最后一个镜头不超过 6s（允许叠化进入）；
- 镜头里的静止部分在 2–5s 之间；
- 静止段中点那一帧（320 宽）满足下面任一条：
  - 某个方块里强边缘像素占比 ≥ 0.25（二维码或密集文字）；
  - `reference_analyzer.text_band_features` 的横带文字分数 ≥ 0.12（居中大字）。

## 分镜表 review 块

`script_compiler` 会在分镜表里写入 `review` 块，格式见 `references/script_schema.md`。quality_review 按下表使用其中的字段：

| 字段 | 作用 |
|---|---|
| `hook.type == "title_card"` | 开头 3–4s 的静止标题卡不报「前3秒画面静止」，`hook.hook_type` 记为 title_card |
| `intentional_holds[].time` | 这些区间（标题卡、CTA 结尾卡）前后各放宽 0.3s，被覆盖的部分不计入死区，也不计入结尾拖沓；`why` 里含 CTA 或「结尾」时视为有 CTA |
| `continuity == "per_section"` + `sections` | 切点离段落边界 ≤ 0.35s 时不查色调跳变，`continuity.cuts[].section_boundary = true`；只查段内的切点 |

分镜表的读取顺序：先用 `--storyboard`；草稿模式下没有传时，再找草稿目录里的 `*storyboard*.json`。文件没有 `review` 块时按通用规则审，并写进 `notes`。

## 限制

- 草稿模式下画面是伪渲染（与 layout_preview 相同）：不含花字、模板、动画、转场的动态过程，所以运动量会偏低。要求精确时请传导出的 `--video`。
- 视频模式没有分轨：必须给 `--analysis`（带词级时间戳的转写）才能估算 BGM 与人声的差值，否则跳过这项并写进 `notes`。屏幕上的文字只能来自 `--subtitles`；烧录字幕的变化会被网格差分当作画面变化。
- 「前 3 秒有无人声」要有转写或草稿人声分轨。只有混音时无法区分人声和音乐，结果会写进 `notes`。
- 色调跳变只比较切点两侧的平均 Lab 值。没有分镜表时，只能靠「跳变占多数 → 段落配色」这条启发式识别有意的风格切换；零星几次有意切换（比如 MV 里的亮色 UI 插入镜头）仍会逐条报出。
- 切镜检测的已知漏检：
  - 同模板、只换文字的深色幻灯片（PPT 218.27s）；
  - 剧烈晃动的特写之间的切换（MV 80.33s）；
  - 0.3s 内的 UI 推近转场（MV 25.0s）；
  - 片头 0.8s 内的叠化。
- 切镜检测的已知误检：静态 UI 里大面积弹窗弹出，例如宣传片商店界面（34.85s），会被算作一刀。
- 视频模式仍然看不到烧录字幕的文字内容（Q3）。字幕条的出现和消失只算画面变化，不算切镜。
- `genre=auto` 新增「教程录屏」：Lab 亮度中位数 ≥ 75、色度 < 8、帧差中位数 < 0.005 时判为教程录屏，对应白底录屏和课件。深色 UI 的录屏仍然会落到 vlog。
- 草稿已被剪映加密且没有明文副本时会退出（退出码 2）。
- 只依赖 numpy、Pillow 和 ffmpeg，没有新增依赖。

## 给主线的集成建议

- **SKILL.md Agent Quick Routing**：`成片好不好看 / 钩子 / 节奏 / 死区 / 会不会划走 → python scripts/quality_review.py --draft "<名>" [--video 导出.mp4]，然后 Read viewer_sheet.png`
- **rules**：在 `rules/review.md` 的「审片与版式自检」后面新增「第 3 层：质量审片」，内容包括：layout_preview 通过后再跑 quality_review，退出码 1 时按 `issues[].suggestion` 修改；把标签表加进去；并在「抓不出的」一节里注明 BGM 听感已部分覆盖（`BGM压人声`）。
- `references/methodology/review_rules.md`：把「相邻片段平均亮度差 > 35%」「片尾配乐结束点」「人声段配乐至少低 12dB」三条从 ⏳ 改为 ✅（`quality_review`）。
- **Mixin**：不需要（纯 CLI 加函数）。
- **requirements.txt**：不用改。
- **CI**：`tests/test_quality_review.py` 里依赖 ffmpeg 的用例在缺 ffmpeg 时会自动 skip。
