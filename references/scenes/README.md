# 场景规范

引导模式第 ② 步选场景，第 ④ 步按场景的**结构模板**写分镜。每个场景文件都包含：适用素材、建议时长、结构模板（各段用途和时长占比）、节奏与包装要点、引导问题（第二轮问用户的）。

| 场景 | 文件 | 典型素材 |
|---|---|---|
| 口播知识 | [talking_head.md](talking_head.md) | 单人对镜头讲解，单机位 |
| Vlog | [vlog.md](vlog.md) | 多段生活片段 + 少量口播 |
| 教程录屏 | [tutorial.md](tutorial.md) | 屏幕录制 / 操作演示 + 讲解 |
| 带货种草 | [product.md](product.md) | 产品特写、使用效果、口播卖点 |
| 影视解说 | [commentary.md](commentary.md) | 影视原片 + 旁白 |
| 卡点混剪 | [beat_montage.md](beat_montage.md) | 多段素材 + 节奏感强的音乐 |
| 宣传片 | [promo.md](promo.md) | 游戏 / 产品实机画面 + BGM，没有旁白（横屏） |
| 音乐视频（MV） | [music_video.md](music_video.md) | 一首歌 + 演唱 / 乐队 / 产品画面，按歌词行切 |
| 剧情短片 | [drama.md](drama.md) | 多场景、有人物对话的故事 |

通用规则见 `../methodology/`，视觉规范见 `../styles/`。

宣传片、MV、卡点混剪这类「多段素材 + 音乐」的片子，剧本用混剪模式（`montage` 字段，见 `../script_schema.md`）：编译器调用 `scripts/beat_snap.py` 按拍或按歌词行规划镜头。
