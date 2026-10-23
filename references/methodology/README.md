# 剪辑方法论（引导模式通用规则）

场景文件（`../scenes/`）和风格文件（`../styles/`）在这些通用规则的基础上做取舍。数值是默认值：来源给了具体数字的照搬；来源只给原则的标为「经验值」，可以按素材调整，但调整要在分镜 `note` 里写原因。

| 文件 | 内容 |
|---|---|
| [hook.md](hook.md) | 开头钩子、结构与信息密度 |
| [pacing.md](pacing.md) | 节奏：剪气口、跳剪、镜头时长、卡点 |
| [subtitles.md](subtitles.md) | 字幕断句、字号、位置、双语、关键词高亮 |
| [inserts.md](inserts.md) | 插卡 / B-roll / 信息卡、人脸避让 |
| [camera.md](camera.md) | 运镜：拍摄建议与后期关键帧模拟 |
| [transitions_fx.md](transitions_fx.md) | 转场、特效、贴纸、音效 |
| [audio.md](audio.md) | 人声、配乐、响度 |
| [safe_area.md](safe_area.md) | 各平台安全区 |
| [shooting.md](shooting.md) | 各场景怎么拍（拍摄前给用户的建议） |
| [grading.md](grading.md) | 调色：调节顺序和默认值、滤镜强度、常用配方 |
| [shot_grammar.md](shot_grammar.md) | 镜头语言：景别、构图（头顶空间 / 视线空间）、横屏进竖屏、角度、焦点、光线、拍摄覆盖 |
| [staging.md](staging.md) | 机位与调度：轴线、对话场面、动作衔接、运动镜头、节奏、单机位模拟多机位 |
| [edit_grammar.md](edit_grammar.md) | 剪辑原则：六要素、剪点时机、转场含义与时长、跳切、连贯、声音与 J/L cut、常见错误 |
| [jianying_recipes.md](jianying_recipes.md) | 剪映配方：调色、转场、卡点、合成、文字、影集（名称已核对 11.x） |
| [review_rules.md](review_rules.md) | 可自动检查的规则清单（已实现 / 计划） |

## 来源

调研于 2026-10-22，只用于提炼规则，未复制原文。

- **GitHub 剪辑类 Skill / 项目**：
  - douyin-talking-head-editor-h3-director（shenwang2）
  - koubo-edit（lelewaxx）
  - cutcli-cookbook（xuliang2024）
  - video-recap-skills（zenstory-ai）
  - ai-promo-video-maker（liyupi）
  - codex-chatcut-workflow（MapleShaw）
  - talking-head-video-effects（WenbYao）
  - hermes-skills 短视频剪辑指导师（itgoyo）
  - capcut-ai-editor（mrbuslov）
  - claude-capcut-skill（mane23-ai）
  - Easel（ZJU-REAL）
  - video-editing-skill（maxazure）
  - Video-Wrapper-Skills（op7418）
  - pyJianYingDraft（GuanYixuan）
- **拍摄与剪辑理论**：
  - StudioBinder（运镜、跳剪、三分法）
  - 维基百科（Match cut、Cutting on action、30 度规则、J/L cut、景别、三点布光、B-roll、Headroom / Lead room、Whip pan、Ken Burns）
  - TikTok Ads 创意最佳实践
  - CapCut 帮助中心
- **国内创作者经验**：databrush、adsturbo、echohives、cardcrafter、新浪、diantuoyi、绘声绘影、来画、易媒助手、135 编辑器、aimusic-tools、7kamao、太平洋电脑网、B 站专栏 cv17927684 等 19 个公开来源（知乎、抖音创作者中心、剪映学院原文未能读取），见各文件「国内经验」部分。
- **剪映手册**：知乎专栏《剪映从入门到精通：全民视频剪辑实用手册》（用户 2026-10-23 下载的网页）。补充了音量比例、文字设置、转场 / 特效 / 贴纸 / 蒙版数量和时长、关键帧、变速、调色配方、5 个实战案例的节奏、拍摄要求，见各文件「剪映手册」部分和 `grading.md`。文中推荐的书只有一本：《电影剪辑的语法》（*Grammar of the Edit*，Roy Thompson、Christopher J. Bowen 著）。
- **电影书架（用户提供的电子书，2026-10-23）**：《镜头的语法》（汤普森、鲍恩，试读节选）、《电影语言的语法》（阿里洪）、《剪辑的语法》（汤普森、鲍恩）、《剪映视频剪辑从小白到大师（电脑版）》（龙飞）。分别整理进 `shot_grammar.md`、`staging.md`、`edit_grammar.md`、`jianying_recipes.md`，可自动检查的规则汇总在 `review_rules.md`。
