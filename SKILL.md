---
name: jianying-pro
description: 剪映 (JianYing) AI自动化剪辑的高级封装 API (JyWrapper)，提供开箱即用的 Python 接口，支持录屏、素材导入、字幕生成、Web 动效合成及项目导出。全面适配 MacOS (Apple Silicon/Intel) 与 Windows，支持 v5.9+ (draft_info.json) 架构并适配剪映 11.x（素材面板索引、VIP 素材、画中画/分屏/关键帧等多轨增强、本地 AI 智能字幕、字幕模板/文字模板、剪映素材与模板同步）、工程自修复、智能配音字幕及录屏变焦。
---

# JianYing Pro Skill

Use this skill when the user wants to automate video editing, generate drafts, or manipulate media assets in JianYing Pro.

Agent execution playbook: [docs/agent-playbook.md](docs/agent-playbook.md)
Capability TODO list (剪映 11.x 能力补全): [docs/TODO.md](docs/TODO.md)
Minimal command SOP: [docs/minimal-command-sop.md](docs/minimal-command-sop.md)
Natural language usage guide: [usage.md](usage.md)
Draft inspector CLI:
`python <SKILL_ROOT>/scripts/draft_inspector.py list --limit 20`
`python <SKILL_ROOT>/scripts/draft_inspector.py summary --name "DraftName"`
`python <SKILL_ROOT>/scripts/draft_inspector.py show --name "DraftName" --kind content --json`
For generic editing requests, always follow the "Quick Edit Runtime Template" and "Acceptance Checklist" in that playbook.

## 🚨 重要开发原则 (CRITICAL DEVELOPER RULES)
1.  **脚本位置**：**禁止在 Skill 内部目录创建剪辑脚本**。所有的剪辑逻辑实现代码（`.py` 脚本）必须存放在用户当前项目的**根目录**（或子目录，如 `scripts/`），以保持 Skill 库的纯净和可移植性。
2.  **版本与架构**：
    - **双平台适配**：已全面支持 MacOS (路径探测/录屏) 与 Windows。
    - **Auto-healing**：支持 v5.9+ (`draft_info.json`)。若草稿损坏或版本冲突，使用 `overwrite=True` 初始化 `JyProject` 可触发自动修复。
3.  **挑视觉素材前先看封面**：选字幕模板、文字模板、花字、贴纸、特效、转场、动画、字体这类**看得见的素材**时，不能只凭名称决定。先运行 `python <SKILL_ROOT>/scripts/preview_assets.py "<关键词>" -c <分类> -l 12`（动画类加 `--frames 3`），用 Read 查看生成的对比图，再按编号选；需要时把对比图给用户看，让用户挑。候选太多时，先按名称和分类筛到 12 个以内。
4.  **生成草稿后必须审片**：`project.save()` 之后、告知用户"完成"之前，必须跑
    `python <SKILL_ROOT>/scripts/layout_preview.py --draft "<草稿名>" --auto`，
    并**用 Read 查看输出的对比图**。轨道数/片段数这类结构校验**不能**替代审片——
    它们全部通过时画面仍可能是错的（图卡 1 帧闪过、字幕超出安全区、竖屏被转横屏）。
    退出码 1 就改参数重新生成，不要直接交付。详见 [rules/review.md](rules/review.md)。
    **审片器通过不代表画面正确**：插卡页、结束卡、新字体 / 花字 / 蒙版的镜头，交付前必须在剪映里截图核对（重新生成前先关掉剪映编辑器，否则剪映关闭时会用旧版覆盖草稿）。版面规则见 `references/methodology/layout.md`。
    macOS 上还可以用 `python <SKILL_ROOT>/scripts/jy_preview.py "<草稿名>" --at <秒>`（或 `p.preview(at=[...])`）在剪映里打开草稿并截取播放器画面，查看剪映的实际渲染效果（剪映需停在首页；见 `rules/jianying-11.md` 第 9 节）。
5.  **配乐选择**：
    - **简单演示使用默认音乐**。实际项目，应优先检索并推荐 `data/cloud_music_library.csv` 中的相关曲目，或根据视频主题（如“科技”、“温暖”）进行关键词过滤。
    - 询问用户：“我发现了几首符合主题的云端音乐，要不要试试？（如：`Illuminate` - 科技感）”。

##  规则指南 (Rules)

Read the individual rule files for specific tasks and constraints:

- [rules/setup.md](rules/setup.md) - **Mandatory** initialization code for all scripts.
- [rules/core.md](rules/core.md) - Core operations: Saving, Exporting, and Draft management.
- [rules/cli.md](rules/cli.md) - CLI contracts and machine-readable output conventions.
- [rules/review.md](rules/review.md) - **Mandatory**: 审片与版式自检 (安全区/字号/闪帧/声音冲突)。
- [rules/guided.md](rules/guided.md) - **引导模式**: 用选择题引导用户定场景、风格、平台、字幕、节奏和素材，再出分镜、装配、审片。
- [rules/media.md](rules/media.md) - Importing assets & **AI Video Analysis Optimization (30m/360p)**.
- [rules/text.md](rules/text.md) - Adding Subtitles, Text, and Captions.
- [rules/keyframes.md](rules/keyframes.md) - **Advanced**: Adding Keyframe animations.
- [rules/effects.md](rules/effects.md) - Searching for and applying Filters, Effects, and Transitions.
- [rules/recording.md](rules/recording.md) - **New**: Screen Recording & Smart Zoom automation.
- [rules/web-vfx.md](rules/web-vfx.md) - Advanced: Web-to-Video generation.
- [rules/generative.md](rules/generative.md) - Chain of Thought for generative editing.
- [rules/audio-voice.md](rules/audio-voice.md) - **New**: TTS Voiceover & BGM sourcing.
- [rules/quality.md](rules/quality.md) - **New**: 高质量短片工具链（口播精剪、最佳 take、卡点、ducking、音效、统一调色、动态图形、B-roll、质量审片、封面、多版本、数据回流）。
- [rules/jianying-11.md](rules/jianying-11.md) - **New (11.x)**: Panel asset sync, VIP assets, multi-track (PiP/split/keyframes/masks), AI auto subtitles.

## 🎯 Agent Quick Routing

- **审片 / 版式自检（生成草稿后必做）**：`rules/review.md` -> `scripts/layout_preview.py --draft "<草稿名>" --auto`，然后 Read 对比图
- **引导模式 / 导演流程**（用户说"引导模式""帮我规划怎么剪""不知道剪成什么样""这个剪辑有问题帮我重做""要有编排感""帮我导演"，或给了素材但没说清要什么时）：**读 `rules/guided.md` 并照做**。流程：`scripts/analyze_footage.py` 读素材 → AskUserQuestion 引导用户选场景（`references/scenes/`）、风格（`references/styles/`）、平台、字幕、节奏 → 出封面对比图让用户挑素材 → 按 `references/script_schema.md` 写剧本（只写内容），用 `scripts/script_compiler.py` 结合场景 / 风格 / 画幅规范（`references/scenes|styles|formats/*.json`）编译成分镜表并给用户确认 → 装配 → 审片 + 剪映截图给用户确认。通用剪辑方法论在 `references/methodology/`
- 云端视频 + 云端音乐：`rules/media.md` + `rules/audio-voice.md` -> `examples/cloud_video_music_tts_demo.py`
- 智能配音与字幕 (Script-to-Video)：`rules/text.md` + `rules/audio-voice.md` -> 核心 API `add_narrated_subtitles`
- 旁白与字幕对齐：`rules/text.md` + `rules/audio-voice.md` -> `examples/cloud_video_music_tts_demo.py`
- 录屏与智能变焦：`rules/recording.md` -> `tools/recording/recorder.py`
- 批量导出/无头导出：`rules/core.md` + `rules/cli.md` -> `examples/robust_auto_export.py`
- 影视解说生成：`rules/generative.md` -> `scripts/movie_commentary_builder.py`
- 剪映 11.x 新素材 / VIP 素材：`rules/jianying-11.md` -> 先运行 `scripts/sync_jy_panel.py`，再用 `asset_search.py`（`--free-only` / `--vip-only`）
- 画中画 / 分屏 / 关键帧 / 蒙版：`rules/jianying-11.md` -> `add_overlay` / `add_split_screen` / `add_keyframes` / `add_mask_simple`
- AI 智能字幕（本地语音识别）：`rules/jianying-11.md` -> `add_auto_subtitles`
- **同步剪映素材 / 模板**（用户说"同步剪映素材""更新素材索引""我在剪映里套用了新模板""刷新模板"等时）：运行 `python <SKILL_ROOT>/scripts/jy_wrapper.py sync`（全部同步：面板索引、音乐音效、字幕/文字模板预设），或只同步一部分：`--only templates` / `--only panel` / `--only music`。整个过程只读剪映缓存和草稿，不会改动剪映的数据。剪映升级后或用户在剪映里用过新素材后，都应该先同步一次。
- **高质量短片工具链**（总表见 `rules/quality.md`，每个工具的细节见 `docs/features/`）：
  - 口播精剪：用户说"删口癖""去嗯啊""说错重来""重复句""剪气口"时，先跑 `analyze_footage.py` 拿到转写，再用 `scripts/speech_cleanup.py analysis.json [--draft 名称]`
  - 最佳 take：用户说"同一句说了多遍""挑最好的一遍""多机位选 take"时，用 `scripts/best_take.py analysis.json [--script 剧本] [--draft 名称]`
  - 素材打分 / 废片检测：`scripts/shot_quality.py <视频> --json`
  - 卡点 / 踩点 / 按鼓点混剪：先 `scripts/beat_detect.py song.mp3`，再 `scripts/beat_snap.py plan --audio song.mp3 --clips DIR --start climax [--build 名称]`
  - BGM 让位人声 / 铺音效：ducking 用 `p.auto_duck()` 或 `scripts/core/mix_ops.py --draft 名称`；音效用 `scripts/sfx_designer.py plan --draft 名称 --write`
  - 统一调色：`scripts/color_match.py suggest --ref ref.jpg --shots ...`；草稿内用 `p.match_color(...)`
  - 图表 / 数字滚动 / 圈注 / 地图路线：`scripts/motion_graphics.py render <模板> --data json --out x.mov`；草稿内用 `p.add_motion_graphic(...)`
  - 照着参考视频剪 / 拆解爆款：`scripts/reference_analyzer.py ref.mp4 --name X --install`，然后 Read `contact_sheet.jpg`
  - 找 B-roll / 生成视频：`scripts/broll_fetch.py`（Pexels / Pixabay）、`scripts/genvideo_providers.py`（可灵 / mock）
  - 宣传片 / MV / 按歌词切：场景用 `promo` / `music_video`，剧本里写 `montage` 字段后用 `script_compiler.py` 编译，或者用 `beat_snap.py plan --scene music_video --lyrics 歌词.json`；多语言版本加 `--lang en`，时间线不变，只换文字
  - 个人偏好：编译时会自动套用 `~/.config/jianying-pro/style_overrides.json`（从用户手剪草稿学到的字幕、标题、调色、配乐习惯），编译报告里会列出改了哪些项；`--no-user-prefs` 或 `JY_USER_PREFS=off` 关闭。重新学习用 `scripts/style_profile.py` 和 `scripts/edit_diff.py learn`，见 `docs/features/user_prefs.md`
  - 写钩子和剧本：`prompts/hook_writer.md` + `references/methodology/hook_bank.md`
  - 片子好不好看 / 会不会划走：版式审片清零后跑 `scripts/quality_review.py --draft "<名>"`，然后 Read `viewer_sheet.png`
  - 封面 / 标题：`scripts/cover_maker.py`；15 / 30 / 60 秒版本和横竖屏版本：`scripts/variants.py`
  - 发布后看完播率和掉人点：`scripts/retention_feedback.py --csv 留存.csv --report report.json`
- 素材封面预览（挑选前看效果）：`scripts/preview_assets.py`。常用写法：`"科技" -c panel_text_templates`、`--ready`（只看可直接用的模板）、`--ids <resource_id...>`、`--frames 3`（动图抽帧）。对比图保存在 `~/.cache/jianying-pro/previews/`
- 字幕模板 / 文字模板：`rules/jianying-11.md` -> `add_subtitle_template` / `add_text_template`（或在字幕 API 上传 `subtitle_template=`；新模板先在剪映里套用一次，再运行 `scripts/sync_jy_templates.py`）。字幕模板全部、文字模板 2,268 个已有预设；用到还没有预设的模板时，macOS 上会自动按需预热这一个（约 30 秒，剪映需停在首页），下载不到时报错并推荐同分类候选（要看封面再选），见 `rules/jianying-11.md` 第 8 节

## 📖 经典示例 (Examples)

Refer to these for complete workflows:
- [examples/my_first_vlog.py](examples/my_first_vlog.py) - A complete vlog creation demo with background music and animated text.
- [examples/simple_clip_demo.py](examples/simple_clip_demo.py) - Quick-start tutorial for basic cutting and track management.
- [examples/compound_clip_demo.py](examples/compound_clip_demo.py) - **New**: Professional nested project (Compound Clip) automation.
- [examples/cloud_video_music_tts_demo.py](examples/cloud_video_music_tts_demo.py) - Cloud video + cloud BGM + TTS/subtitle alignment.
- [examples/web_to_video_intro_demo.py](examples/web_to_video_intro_demo.py) - Web-to-Video intro demo (HTML animation -> timeline clip).
- [examples/robust_auto_export.py](examples/robust_auto_export.py) - Stable export workflow and failure handling.
- [examples/auto_exposure_align_demo.py](examples/auto_exposure_align_demo.py) - CV-assisted exposure alignment workflow.
- [examples/video_transcribe_and_match.py](examples/video_transcribe_and_match.py) - **Advanced**: AI-driven workflow (Transcribe Video -> Match B-Roll via AI semantics -> Assemble Draft).

## 🧠 提示词与集成工具 (Prompts & Integrated Tools)

Use these templates and scripts for complex tasks:
- **Asset Search**: Find filters, transitions, and animations by Chinese/English name:
  ```bash
  python <SKILL_ROOT>/scripts/asset_search.py "复古" -c filters
  ```
- **Movie Commentary Builder**: Generate 60s commentary videos from a storyboard JSON:
  ```bash
  python <SKILL_ROOT>/scripts/movie_commentary_builder.py --video "video.mp4" --json "storyboard.json"
  ```
- **Sync Native Assets**: Import your favorited/played BGM/Styles from JianYing App to the Skill:
  ```bash
  python <SKILL_ROOT>/scripts/sync_jy_assets.py
  # Index cloud materials from your existing drafts
  python <SKILL_ROOT>/scripts/build_cloud_music_library.py
  python <SKILL_ROOT>/scripts/build_cloud_text_styles_library.py
  ```
- **README to Tutorial**: Convert a project's README.md into a full installation tutorial video script:
  - Read prompt: `prompts/readme_to_tutorial.md`
  - Inject content into `{{README_CONTENT}}` variable
- **Screen Recorder & Smart Zoom**: Record your screen and auto-apply zoom keyframes:
  ```bash
  python <SKILL_ROOT>/tools/recording/recorder.py
  # Web preview capture (high performance)
  python <SKILL_ROOT>/scripts/web_recorder.py --url "http://localhost:3000" --duration 5
  # Or apply zoom to existing video:
  python <SKILL_ROOT>/scripts/jy_wrapper.py apply-zoom --name "Project" --video "v.mp4" --json "e.json"
  ```
- **Draft Inspector**: Examine draft structure and metadata (v5.9+ support):
  ```bash
  python <SKILL_ROOT>/scripts/draft_inspector.py list --limit 20
  python <SKILL_ROOT>/scripts/draft_inspector.py summary --name "DraftName"
  ```
- **Auto Exporter**: Headless export of a draft to MP4/SRT:
  ```bash
  python <SKILL_ROOT>/scripts/auto_exporter.py "DraftName" "output.mp4" --res 1080 --fps 60
  # For SRT only:
  python <SKILL_ROOT>/scripts/jy_wrapper.py export-srt --name "DraftName"
  ```
  Note: MP4 auto export uses Windows UI Automation. On macOS, generate the draft and export it manually from JianYing.
- **Template Clone & Replacer**: 安全克隆模板并批量替换物料 (防止损坏原模板):
  ```bash
  # 克隆模板生成新项目
  python <SKILL_ROOT>/scripts/jy_wrapper.py clone --template "酒店模板" --name "客户A_副本"
  ```
- **API Validator**: Run a quick diagnostic of your environment:
  ```bash
  python <SKILL_ROOT>/scripts/api_validator.py
  ```

## 🚀 快速开始示例

```python
import os
import sys

# 1. 环境初始化 (必须同步到脚本开头，支持 Win/Mac)
current_dir = os.path.dirname(os.path.abspath(__file__))
env_root = os.getenv("JY_SKILL_ROOT", "").strip()
# 探测 Skill 路径 (支持 Antigravity, Trae, Claude 等)
skill_root = next((p for p in [
    env_root,
    os.path.join(current_dir, ".agents", "skills", "jianying-pro"),
    os.path.join(current_dir, ".agent", "skills", "jianying-pro"),
    os.path.join(current_dir, ".trae", "skills", "jianying-pro"),
    os.path.join(current_dir, ".claude", "skills", "jianying-pro"),
    os.path.join(current_dir, "skills", "jianying-pro"),
    os.path.abspath(".agents/skills/jianying-pro"),
    os.path.abspath(".agent/skills/jianying-pro"),
    os.path.abspath(".trae/skills/jianying-pro"),
    os.path.abspath(".claude/skills/jianying-pro"),
    os.path.abspath("skills/jianying-pro"),
    os.path.dirname(current_dir)
] if p and os.path.exists(os.path.join(p, "scripts", "jy_wrapper.py"))), None)

if not skill_root: raise ImportError("Could not find jianying-pro skill root.")
sys.path.insert(0, os.path.join(skill_root, "scripts"))
from jy_wrapper import JyProject

if __name__ == "__main__":
    # 2. 初始化工程 (支持 v5.9+ 及自修复)
    project = JyProject("New AI Video", overwrite=True)
    assets_dir = os.path.join(skill_root, "assets")

    # 3. 智能配音与字幕 (One-click Script-to-Video)
    project.add_narrated_subtitles(
        text="欢迎使用剪映自动化 Skill。这是一个全面适配 MacOS 的进阶版本。",
        speaker="zh_female_xiaopengyou"
    )

    # 4. 导入额外素材
    project.add_media_safe(os.path.join(assets_dir, "video.mp4"), "0s")
    project.add_media_safe(os.path.join(assets_dir, "audio.mp3"), "0s", track_name="Audio")

    # 5. 添加带动画的标题
    project.add_text_simple("剪映自动化开启", start_time="1s", duration="3s", anim_in="复古打字机")

    project.save()
```

## 🛠️ 初始化与项目规范 (Initialization & Project Rules)

在初始化 `JyProject` 时，请务必根据主视频素材的比例设置分辨率。**默认值为横屏 (1920x1080)**。

### 🚨 脚本存放位置规范
**禁止在 Skill 安装目录下创建你的业务剪辑脚本**。
- **正确做法**：将你的剪辑 Python 脚本放在项目的根目录。
- **原因**：Skill 目录应该只包含工具集源码，便于后续 `git pull` 升级。业务代码混入会导致版本管理混乱。
