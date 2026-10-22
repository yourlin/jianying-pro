---
name: review
description: 生成草稿后的强制审片流程 (版式自检 / 时间线结构检查)。
metadata:
  tags: review, qa, layout, safe-area, audit, 审片
---

# 审片与版式自检（强制）

**`project.save()` 之后、告知用户"完成"之前，必须跑一次审片器。** 轨道数、片段数这类结构校验**不能**替代审片——它们全部通过时画面仍可能是错的。

```bash
python <SKILL_ROOT>/scripts/layout_preview.py --draft "<草稿名>" --auto
```

然后**必须用 Read 查看输出的对比图**（`sheet.jpg`），不要只看文字报告。
退出码 0 = 通过，1 = 有问题。有问题就改参数重新生成，不要直接交付。

快速迭代时可只采样几个关键时刻：

```bash
python <SKILL_ROOT>/scripts/layout_preview.py --draft "<草稿名>" --at 1.5 6.5 12 32
python <SKILL_ROOT>/scripts/layout_preview.py --draft "<草稿名>" --auto --report-only   # 只要报告
```

## 工作原理

从草稿 JSON（优先 `draft_info.skill.json`，其次剪映的明文 `template.json`）读取时间线，
用 ffmpeg 抽主视频与覆盖层的帧，按草稿里的 `transform` / `scale` / 关键帧 / 字号把文字和图卡
合成到画面上，叠加安全区与平台 UI 参考线。不需要打开剪映，也不需要导出。

## 两层检查

**1. 时间线结构检查**（不依赖采样时刻，必跑）

| 标签 | 含义 |
|---|---|
| `[闪帧]` | 覆盖层/文字时长 < 0.4s，画面上等于没出现 |
| `[特效过短]` | 氛围特效 < 3s，观众看不出意图 |
| `[越界]` | 片段超出成片时长 |
| `[声音冲突]` | 覆盖层视频未静音，会和旁白/BGM 打架 |

**2. 版式审片**（按采样时刻）

| 标签 | 含义 |
|---|---|
| `[字号]` | 字高低于可读下限（主字幕 48px / 副字幕 38px @1920） |
| `[安全区]` | 文字超出四边留白 |
| `[平台UI]` | 文字或图卡进入顶部状态区 / 底部文案区 / 右侧按钮区 |
| `[重叠]` | 两条文字轨在画面上打架 |
| `[插卡]` | 横版素材放进竖屏后宽度占比过小 |

## 已知的两个静默 bug（审片器就是为抓它们而写的；2026-10-22 已在 skill 里修复，审片器仍会检查）

1. **图片片段被 clamp 成 0.04s**（已修复：图片按请求时长放置）：图片素材的物理时长只有 40000us，而 `_calculate_duration`
   取 `min(请求时长, 物理时长)`，所以 `add_media_safe` / `add_overlay` 传 `duration="6s"`
   的图片会变成 1 帧闪过。时间线上看着正常，成片里图片根本看不见。加完图片片段后强制写回：

   ```python
   d = int(dur_s * 1_000_000)
   seg.target_timerange.duration = d
   if seg.source_timerange is not None:
       seg.source_timerange.start = 0
       seg.source_timerange.duration = d
   for mat in project.script.materials.videos:
       if mat.material_id == seg.material_id and mat.duration < d:
           mat.duration = d
   ```

2. **竖屏视频被转成横屏**（已修复：标准化转码保持原方向和比例）：`utils/media_normalizer.py` 以 `width % 16 != 0` 作触发条件
   （1080 宽的竖屏必中），而输出 profile 硬编码 `scale=1920:1080 + pad`，把 1080×1920
   转成横屏加黑边。绕过办法：先自行转码一份规范竖屏文件放到
   `<源目录>/__jycache__/<文件名>.__jy_norm__.mp4`（mtime 要新于源文件），标准化器会直接复用缓存。

## 抓不出的（仍需人工终检）

- 花字 / 字幕模板的真实渲染外观（剪映按 ID 云端渲染，草稿里只有 ID）
- 动画、转场的动态过程（只采样静帧）
- 音量平衡、BGM 与旁白的听感
- 画面美感与构图取舍——这个要靠 Read 对比图自己判断

## 配套文件

- 风格规范（硬约束）：`references/styles/guofeng.md`（古韵/国风竖屏）
- 分镜表模板：`references/storyboard_template.json`

生成"有编排感"的成片时，先按分镜表模板规划镜头（每个镜头写明 `purpose` 和插卡的
`why_here`），再按风格规范选素材，最后装配 + 审片。不要跳过分镜表直接写剪辑代码。
