# 高质量短片工具链

`layout_preview.py` 只保证版面正确。这里的工具负责让片子好看、好听、留得住人：读懂素材、剪顺口播、踩准节拍、混好声音、统一色调、审节奏，发布后再拿数据回来修规则。

每个工具的完整参数、输出格式和限制见 `docs/features/<名字>.md`。所有 CLI 都支持 `--json`，输出约定见 `rules/cli.md`。

## 在引导流程里的位置（`rules/guided.md`）

| 步骤 | 工具 | 作用 |
|---|---|---|
| ① 读素材 | `shot_quality.py` | 给镜头打分：模糊、抖动、曝光、人脸 / 眼神，挑出废片 |
| ① 读素材 | `reference_analyzer.py` | 用户给了参考视频（"照着这条剪"）时，反推出 scene / style JSON |
| ② 精剪 | `speech_cleanup.py` | 口播去口癖、去重复句、去 NG、压缩气口 |
| ② 精剪 | `best_take.py` | 同一句说了多遍时选最好的一遍，结果可以代替 cleanup 计划 |
| ③ 选素材 | `broll_fetch.py` / `genvideo_providers.py` | 按旁白找 B-roll（Pexels / Pixabay），或用文生视频生成 |
| ④ 写剧本 | `prompts/hook_writer.md` + `methodology/hook_bank.md` | 多个钩子版本、结构模板、金句 |
| ⑤ 装配 | `beat_detect.py` + `beat_snap.py` | 节拍检测，切点吸附到鼓点 / 按节拍生成卡点混剪 |
| ⑤ 装配 | `mix_ops.py`（`p.auto_duck()`）+ `sfx_designer.py` | 人声段 BGM 自动压低；转场、文字、强调处铺音效 |
| ⑤ 装配 | `color_match.py`（`p.match_color()`） | 按参考帧统一所有镜头色调 |
| ⑤ 装配 | `motion_graphics.py`（`p.add_motion_graphic()`） | 图表、数字滚动、圈注、进度条、地图路线 |
| ⑥ 审片 | `quality_review.py` | 钩子、节奏、死区、色调跳变、配乐冲突、"观众视角"联系表 |
| ⑦ 交付 | `cover_maker.py` / `variants.py` | 封面候选 + 标题；15 / 30 / 60 秒版本和横竖屏版本 |
| 发布后 | `retention_feedback.py` | 完播率曲线 → 掉人点归因 → 规则修改建议 |
| 学习 | `style_profile.py` / `edit_diff.py` | 从用户手剪草稿学风格，对比 skill 原版和用户改过的版本学修改规律；结果写到 `~/.config/jianying-pro/learned/`，经用户确认后写入 `style_overrides.json` |

## 常用命令

```bash
# 素材打分 / 参考视频
python <SKILL_ROOT>/scripts/shot_quality.py <视频> [--scenes analysis.json] --json
python <SKILL_ROOT>/scripts/reference_analyzer.py ref.mp4 --name X --install   # 然后 Read contact_sheet.jpg

# 口播精剪 / 最佳 take（先跑 analyze_footage.py 拿词级转写）
python <SKILL_ROOT>/scripts/speech_cleanup.py analysis.json [--draft 名称] [--max-pause 0.25] [--punch-in 1.25]
python <SKILL_ROOT>/scripts/best_take.py analysis.json [--script script.json] [--draft 名称]

# 卡点
python <SKILL_ROOT>/scripts/beat_detect.py song.mp3 -o beats.json
python <SKILL_ROOT>/scripts/beat_snap.py plan --audio song.mp3 --clips <素材目录> --start climax [--build 名称]
python <SKILL_ROOT>/scripts/beat_snap.py snap --beats beats.json --cuts "1.2,3.4"

# 混音 / 音效
python <SKILL_ROOT>/scripts/core/mix_ops.py --draft 名称 [--analysis analysis.json]
python <SKILL_ROOT>/scripts/sfx_designer.py rules
python <SKILL_ROOT>/scripts/sfx_designer.py plan --draft 名称 [--analysis analysis.json --keywords 重点] [--beats beats.json] --write

# 调色 / 动态图形
python <SKILL_ROOT>/scripts/color_match.py suggest --ref ref.jpg --shots a.mp4 b.mp4
python <SKILL_ROOT>/scripts/motion_graphics.py render <模板> --data data.json --out x.mov

# B-roll / 生成视频（需要 PEXELS_API_KEY / PIXABAY_API_KEY / 可灵密钥）
python <SKILL_ROOT>/scripts/broll_fetch.py plan-template --analysis analysis.json --format vertical -o broll_plan.json
#   → Agent 给每句填英文 keywords，然后:
python <SKILL_ROOT>/scripts/broll_fetch.py match broll_plan.json --format vertical --download
python <SKILL_ROOT>/scripts/genvideo_providers.py generate --provider kling --prompt "..." --duration 5 --aspect 9:16

# 质量审片 / 交付
python <SKILL_ROOT>/scripts/quality_review.py --draft "<草稿名>" [--video 导出.mp4] [--analysis analysis.json]
python <SKILL_ROOT>/scripts/cover_maker.py <视频> -o <目录> --prompt-only            # 拿到写标题的提示
python <SKILL_ROOT>/scripts/cover_maker.py <视频> -o <目录> --titles 标题1 标题2 --format vertical
python <SKILL_ROOT>/scripts/variants.py script.json --targets 15,30,60 --formats vertical,horizontal

# 发布后
python <SKILL_ROOT>/scripts/retention_feedback.py --csv 留存.csv --report report.json
```

## 规则

1. **质量审片在版式审片之后跑**：`layout_preview.py` 清零后再跑 `quality_review.py`，并用 Read 看 `viewer_sheet.png`，站在观众角度判断哪里会划走。退出码 1 就按 `issues[].suggestion` 改。草稿模式是伪渲染，看不到花字和转场；要更准就导出成片后加 `--video`。
2. **精剪先于装配**：口播素材先跑 `speech_cleanup.py`（或 `best_take.py`），再用它输出的 `transcript`（已经换算到精剪后的时间线）做字幕和剧本定位。
3. **有人声又有 BGM 就做 ducking**：用 `auto_duck()`，替代 `rules/audio-voice.md` 里固定 0.6 音量的做法。
4. **音效要克制**：`sfx_designer` 会限制密度。包装强度选"克制"时只铺转场音效。
5. **调色幅度要在剪映里核对**：`color_match` 的面板参数来自近似模型，方向可靠，幅度要在剪映里截图核对；要精确匹配就用 LUT 模式。
6. **个人偏好不进仓库**：学到的习惯只写进 `~/.config/jianying-pro/`，不改 `references/styles` 等公共规范。
7. **数据回流和学习得到的建议，都要先给用户确认**，确认后再改 `references/methodology`。

## 已知限制（交付前要知道）

- 节拍检测按固定速度处理，变速歌曲的拍点只是近似；没有鼓的歌，强拍可能判错。
- 口播精剪和最佳 take 只看转写文本，whisper 没转出来的"嗯"剪不到（可以在 `analyze_footage --prompt` 里写上"嗯、呃、那个"）。修辞性重复会被当成重说，只留一遍，必要时加 `--no-repeats`。
- `auto_duck` 的音量关键帧按素材时间写；BGM 从中间截取时，关键帧位置要在剪映里实测一次（见 `docs/features/audio_mix_sfx.md`）。
- 人脸检测要用 `opencv-python` 4.x，5.x 没有 Haar 级联，人脸检测会跳过。
- 可灵适配只用 mock 测过；即梦、Veo 只留了接口。`motion_graphics` 渲染需要 playwright。
