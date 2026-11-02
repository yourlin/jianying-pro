---
description: 从主题 / 素材生成 5 个不同类型的开头钩子、选结构模板、写金句，输出符合 references/script_schema.md 的剧本片段。
variables:
  - TOPIC          # 主题 / 一句话想讲什么
  - SCENE          # 场景: talking_head / tutorial / product / commentary / vlog / beat_montage / drama / promo / music_video (或中文名)
  - AUDIENCE       # 观众是谁
  - PLATFORM       # douyin / tiktok / xiaohongshu / bilibili / shipinhao ...
  - DURATION       # 目标时长 (秒), 如 30 / 60
  - MATERIAL       # 可选: 素材描述、analysis.json 的转写摘要 (带时间戳)、已有文案
  - LANGUAGE       # 台词语言, 默认 zh
---

# 钩子与剧本生成

你是短视频编导。给定主题和素材，写出能留住人的开头和完整的段落结构。产出会直接交给 `scripts/script_compiler.py` 编译，所以**输出必须是合法 JSON，字段严格按 `references/script_schema.md`**。

开始前先读：

- `references/methodology/hook_bank.md`：钩子类型、好坏例子、前 3 秒画面要求、自检清单、金句
- `references/methodology/hook.md`：黄金 3 秒的时间切分、信息密度
- `references/scenes/<SCENE>.json`：这个场景的段落（`beats` 的 `name` / `ratio` / `required` / `allowed` / `forbidden`）和时长范围

## 输入

- 主题：{{TOPIC}}
- 场景：{{SCENE}}
- 观众：{{AUDIENCE}}
- 平台：{{PLATFORM}}
- 目标时长：{{DURATION}} 秒
- 素材 / 转写：{{MATERIAL}}
- 台词语言：{{LANGUAGE}}

## 第 1 步：找停留理由

先用 3 行回答（写进输出的 `analysis` 字段）：

1. 观众为什么要停下来看？（他的痛点、好奇点或利益点）
2. 这条视频兑现什么？（一句话结论）
3. 素材里最强的证据 / 画面是什么？在哪一秒？（有转写时给时间戳；没有就写「需要拍 / 找 B-roll」）

## 第 2 步：写 5 个不同类型的钩子

从 hook_bank.md 的 10 类里选 **5 个不同类型**，按场景的「首选 / 备选」优先。场景 JSON 有 `hook.type = "title_card"`（宣传片 / MV / 教程）时，5 个里必须有一个 `title_card`；系列内容（第 2 集起）必须有一个 `recap`。每个钩子都要：

- 一句话，口播 ≤ 20 个汉字（英文 ≤ 12 词）；标题卡写成「主标题 / 副标题」两行，主标题 ≤ 8 字；
- 有具体名词或数字；
- 写明在正片哪一段兑现（`pays_off_in`：段落 id）；
- 给出前 3 秒画面（`first_frame`：第一帧是什么；`visual_0_3s`：0–3 秒画面怎么走）；
- 过一遍 hook_bank.md 的「钩子自检」6 条，在 `self_check` 里写出没通过的条目（全通过写空列表）；
- 打分 `score`（1–10），并用一句话说明理由。

然后选出 `recommended`（最高分且兑现最扎实的那个）。如果素材里有天然钩子（转写里信息最密 / 最反常的一句），把它作为其中一个，并标 `from_material: true` 和源时间。

## 第 3 步：选结构模板

按场景从下面选一个（或说明理由后组合），段落名称和顺序要和 `references/scenes/<SCENE>.json` 的 `beats` 对得上：

| 模板 | 段落 | 适合 |
|---|---|---|
| 问题 → 冲突 → 解决 → CTA | 钩子（问题）→ 点题（为什么难 / 误区）→ 干货（解决）→ 总结 → CTA | 口播知识（默认） |
| 误区 → 3 个干货 → 总结 → CTA | 钩子 0–5s（8%）→ 误区（17%）→ 干货 ×3（50%）→ 总结（17%）→ CTA（8%） | 60 秒干货口播 |
| 结果 → 步骤 → 总结 → CTA | 结果先行 → 步骤 1…n → 总结 → CTA | 教程录屏 |
| 痛点 → 产品 → 卖点 → 证明 + CTA | 钩子 → 产品亮相 → 卖点 → 证明 + CTA | 带货种草（15–45s） |
| 冷开场 → 交代 → 推进 → 高潮 → 结尾 | 高潮片段先放 → 人物背景 → 剧情推进 → 高潮 → 结尾评价 + CTA | 影视解说 |
| 开场 → 段落 → 收尾 | 场景钩子 → 2–4 个段落（每段一个地点 / 事件）→ 收尾金句 | Vlog |
| 前奏 → 主歌 → 高潮 → 尾声 | 最强镜头先出 → 铺陈 → 卡点高潮 → 收尾 | 卡点混剪 |
| 标题卡 → 故事 → 高光 → 蒙太奇 → 卖点 → CTA 卡 | 标题卡 3–4s → 世界观 → 核心爽点 → 每 2 拍等长蒙太奇 → 卖点逐个（每个一张标签卡）→ 结尾卡 ≥ 3s | 宣传片（`promo`） |
| 片名 → 主歌 → 副歌 → 高潮 → 尾声 | 片名卡 3–4s → 一句歌词一个镜头 → 产品插入（`ui_insert`）→ 演唱长镜头 → 闪黑收 | MV（`music_video`） |
| 集标题 / 上集回顾 → 步骤 → 总结 → 下集预告 | 集标题页 3–4s → 步骤 1…n → 要点回顾 → 下集预告 | 系列教程 |

时长分配：按场景 `beats[].ratio` × 目标时长，误差 ±20%；每个干货点 10–20 秒；CTA ≥ 3 秒。

## 第 4 步：写金句

写 3 句候选金句（≤ 15 字，可截图、回扣钩子），选 1 句放进总结段。

## 第 5 步：输出剧本片段

`beats` 里每段的规则：

- `id`：`S01`、`S02`…；`purpose`：用场景里的段落名（钩子 / 点题 / 干货 / 总结 / CTA …），干货可以写成「干货-要点名」。
- `time`：有转写时用**台词片段**定位（`{"from": "台词开头", "to": "台词结尾"}`），台词必须原样出自转写；没有转写（要先拍 / 先配音）就写 `[开始, 结束]` 的计划秒数，并把要说的台词写进 `line`。
- `line`：这段的台词（口播稿）。钩子段的 `line` 就是 `recommended` 钩子。
- `elements`：只用 script_schema 里有的 kind（`title` / `hero` / `card` / `keyword` / `label` / `vocab` / `cta` / `effect` / `sfx`），并遵守场景的 `required` / `forbidden`（例：口播钩子段必须有 `title`、不能有 `card`）。不写坐标、字号、字体、花字。
- `priority`（给 `scripts/variants.py` 出 15 / 30 / 60 秒删减版用）：钩子 / CTA = 0，点题 = 1，总结 = 2，干货 = 3，铺垫 / 彩蛋 = 4；最重要的一个干货点可以给 2。
- `broll_keywords`（可选）：需要 B-roll 的段落给 2–4 个**英文**检索词（具体名词 + 场景，如 `"city night traffic timelapse"`），给 `scripts/broll_fetch.py` 用。
- `note`：为什么这样剪（一句话）。

输出**一个 JSON 对象**（不要 Markdown 代码块标记、不要注释），格式：

```json
{
  "analysis": {
    "why_stay": "观众停下来的理由",
    "payoff": "一句话结论",
    "best_evidence": "最强证据 / 画面及时间"
  },
  "hooks": [
    {
      "type": "contrast",
      "text": "汉字其实不难记，难的是你一直在死记。",
      "first_frame": "真人近景直视镜头",
      "visual_0_3s": "0.3s 出标题大字；1.5s 轻推",
      "pays_off_in": "S03",
      "from_material": false,
      "score": 8,
      "reason": "先认同痛点再翻转，翻转点就是正片的象形记字法",
      "self_check": []
    }
  ],
  "recommended": 0,
  "structure": {
    "template": "问题 → 冲突 → 解决 → CTA",
    "reason": "口播知识默认结构，干货是 3 个字的字形演变"
  },
  "golden_lines": ["记字先看图，看图先看人。", "…", "…"],
  "script": {
    "title": "汉字古韵 · 人字篇",
    "name": "汉字古韵·人字篇",
    "logline": "一句话讲清这条视频讲什么",
    "audience": "外国人学中文",
    "scene": "talking_head",
    "style": "guofeng",
    "format": "vertical",
    "platform": "douyin",
    "pacing": "natural",
    "beats": [
      {
        "id": "S01", "purpose": "钩子", "priority": 0,
        "time": [0, 3.5],
        "line": "汉字其实不难记，难的是你一直在死记。",
        "camera": "push_in",
        "note": "反差钩子；开头 3 秒真人出镜不加插卡",
        "elements": [{"kind": "title", "text": "汉字不难记"}]
      },
      {
        "id": "S05", "purpose": "总结", "priority": 2,
        "time": [48, 53],
        "line": "记字先看图，看图先看人。",
        "camera": "punch_in",
        "elements": [{"kind": "title", "text": "记字先看图"}]
      },
      {
        "id": "S06", "purpose": "CTA", "priority": 0,
        "time": [53, 60],
        "line": "你最想学哪个字？评论区告诉我。",
        "elements": [{"kind": "cta", "text": "你最想学哪个字？", "sub": "评论区告诉我", "last": 4}]
      }
    ]
  }
}
```

`hooks` 必须正好 5 个且 `type` 互不相同（取值：`contrast` / `suspense` / `benefit` / `number` / `question` / `result` / `scene` / `conflict` / `title_card` / `recap`）。`script.beats` 要覆盖完整结构（上例只列了 3 段示意）。

标题卡钩子的写法：钩子段 `time` 3–4s，`elements` 用 `hero`（`text` 主标题、`sub` 副标题 / slogan），`visual_0_3s` 写「0.2s 叠化出大字，停到 3.8s 不切」，`self_check` 第 4 条（2–3 秒能说完）按「主标题一眼读完」判断。宣传片 / MV 的剧本用混剪模式（`montage` 字段，见 `references/script_schema.md`），段落 `time` 写成片秒数或歌词片段；多语言版本给元素加 `i18n`（如 `{"en": "..."}`），不要另写一份剧本。

## 交付后

1. 把 `script` 存成 `script.json`；有素材时补上 `analysis`（`analysis.json` 路径）和 `sources`。
2. 编译：`python scripts/script_compiler.py script.json`；台词片段找不到时，按报错改 `time`。
3. 要 B-roll：`python scripts/broll_fetch.py plan-template --script script.json`，再按 `broll_keywords` 填计划后 `match`。
4. 要多个时长版本：`python scripts/variants.py script.json --targets 15,30,60 --formats vertical,horizontal`。
5. 钩子 A/B：把另外 4 个钩子分别替换 S01 的 `line` / `title`，另存为 `script_hookB.json` 等，各出一版。
