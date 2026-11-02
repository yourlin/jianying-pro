# 最佳 take 挑选（T7）：`scripts/best_take.py`

## 用途

同一句话说了好几遍（起句重来、说错重来、整句重录），或者多个机位、多次拍摄录下了同一段内容时，给每一遍综合打分，选出最好的一遍，说明理由，并且直接输出精剪计划：落选的几遍剪掉，最佳那遍里的口癖和气口照常清理。输出格式和 `speech_cleanup` 的 `cleanup_plan.json` 一样，可以直接替换使用。

### 怎么判断是同一句的几遍

1. **切句**：沿用 `speech_cleanup` 的单元和子句切分。遇到以下情况切开：句末标点、换了一个转写分句、停顿 ≥ `sentence_gap`（0.7s）、单独成句的 NG 标记（不对 / 重来…）、新子句的开头和当前句的开头一样（同一句内部的起句重来）。
2. **归组**：
   - **有剧本时**：每句和剧本里的每个目标句比较（整句相似度，或者这句是目标句的开头，即没说完的一遍），归到最像的那句，相似度要 ≥ `script_match`（0.5）。剧本里对不上的句子，再按下面「没有剧本」的方法互相归组。
   - **没有剧本时**：整句相似度 ≥ `similarity`（0.75），或者短的一遍是长的一遍的开头，就算同一句。同一个源里只往回比较最近 `lookback`（3）组，最多相隔 `window`（60s）。来自不同源（多机位）的句子不受这个限制。只差在数字上的排比句（第一步 / 第二步）不归为一组。
3. **NG 标记**：强标记（重来、说错了…）一律剪掉。弱标记（不对、错了…）只有前后两句属于同一组时才剪；问句后面的「不对。」是正常回答，保留。被剪的标记前面那一遍记为「被打断」。

### 打分（每项 0–100，缺失的维度不参与，剩下的权重重新归一）

| 维度 | 权重 | 计算 |
|---|---|---|
| `fluency` 流畅度 | 0.35 | `100 − 12×口癖 − 10×结巴 − 5×卡顿 − 8×长停顿`，再按 8:2 混入语速分。口癖和结巴用 `speech_cleanup.plan_cleanup` 检测。卡顿指非标点处停顿 ≥ 0.35s，长停顿指 ≥ 0.8s。语速正常区间：中文 3.5–6.2 字/秒，英文 1.8–3.6 词/秒，每超出 1 个单位扣 30 分。计算语速时不算口癖和停顿的时间 |
| `complete` 完整度 | 0.20 | 和参考句的字覆盖率。参考句是剧本目标句；没有剧本时用这一组里最长的一遍。被 NG 标记打断的减半 |
| `script` 剧本相似度 | 0.20 | 去掉口癖后的文本和目标句的相似度（`difflib`）。只在有剧本时计算 |
| `visual` 画面 | 0.15 | `shot_quality.score_segments` 的段分（清晰、稳定、曝光） |
| `face` 正对镜头 | 0.10 | 段内各采样点 `shot_quality` 人脸分的均值，没检测到脸记 0。人脸检测器不可用时不参与 |

覆盖率低于 0.9（没说完）时，总分再乘 `0.6 + 0.4×覆盖率`。每组最后一遍加 `recency`（2 分），用来打破平局：其他条件差不多时，通常最后一遍是说对的那遍。

## 用法

### CLI

```bash
# 单个素材 (analysis.json 来自 analyze_footage.py; 有 source 视频时自动做画面打分)
python <SKILL_ROOT>/scripts/best_take.py analysis.json                    # → takes.json + best_take_plan.json
python <SKILL_ROOT>/scripts/best_take.py analysis.json --script lines.txt --json
python <SKILL_ROOT>/scripts/best_take.py transcript.json --video talk.mp4 --no-cleanup
python <SKILL_ROOT>/scripts/best_take.py analysis.json --weights "fluency=0.5,face=0" --no-visual

# 多机位 / 多次拍摄: 每个文件一个 analysis.json, 跨文件归组
python <SKILL_ROOT>/scripts/best_take.py camA.json camB.json --script script.json

# 直接生成剪映草稿 (单源默认按精剪计划; --sequence 按剧本顺序逐句拼接, 多源时总是这样)
python <SKILL_ROOT>/scripts/best_take.py analysis.json --draft "最佳take" --punch-in 1.25
```

其他参数：`--similarity`、`--every`（画面采样间隔，默认 0.5s）、`--max-pause`（透传给 `plan_cleanup`）、`--plan-out`、`-o`、`--drafts-root`、`--track`。

剧本格式：`.txt` 文件（按行和句末标点切句）、JSON 字符串列表、`{"lines": [...]}`，或者 `script.json`（读 `beats[*].line / say / narration / voiceover / text`）。

错误码（`--json`）：`not_found`（文件或源视频不存在）、`invalid_input`（JSON 坏了、没有转写、剧本为空、多输入时用了 `--video`）、`empty_plan`。

### Python

```python
from best_take import select_takes, build_plan, load_script, build_best_take_draft

sel = select_takes(analysis_dict)                                   # 或者传转写列表
sel = select_takes(analysis_dict, ["大家好。", "今天讲三个技巧。"], visual=False)
sel = select_takes([{"analysis": a1, "video": "camA.mp4"}, a2], script_json,
                   weights={"face": 0.2}, cleanup_kwargs={"max_pause": 0.3}, similarity=0.8)
sel["plan"]                  # 单源: speech_cleanup 格式的计划, 可以直接给 add_speech_cleanup 用
build_best_take_draft(sel, "最佳take", drafts_root=None, use_plan=True)

# Mixin (尚未注册到 JyProject)
from jy_wrapper import JyProject
from core.take_ops import BestTakeOpsMixin
class P(JyProject, BestTakeOpsMixin): ...
p = P("最佳take")
p.add_best_takes(sel)                         # 单源走 plan.keep; use_plan=False / 多源走 sequence
p.save()
```

`select_takes(..., **overrides)` 可以覆盖 `DEFAULTS` 里的任何阈值：`similarity`、`script_match`、`sentence_gap`、`window`、`lookback`、`hesitation`、`long_pause`、`recency`、`every`。传未知参数会抛 `TypeError`。

## 输出格式（`takes.json`）

```json
{
 "sources": ["/abs/talk.mp4"],
 "settings": {"similarity": 0.75, "...": "...", "weights": {"fluency": 0.35, "...": 0}, "visual": true, "cleanup": true},
 "script": ["大家好。", "今天讲剪辑的三个技巧。"],
 "groups": [{
   "id": 2, "target": "今天讲剪辑的三个技巧。", "target_index": 1, "best": 3,
   "reason": "选第3遍 (7.68–10.02s, 98 分): 没有明显问题。落选: 第1遍 90 分 (1 个口癖; 流畅度更差); 第2遍 54 分 (没说完 (覆盖 31%); 完整度更差)",
   "takes": [{"take": 1, "source": "/abs/talk.mp4", "start": 1.64, "end": 4.96, "text": "…", "total": 90.1,
              "scores": {"fluency": 90.4, "complete": 100.0, "script": 96.0, "visual": 82.3, "face": 75.0},
              "metrics": {"fillers": 1, "stutters": 0, "hesitations": 0, "long_pauses": 0, "rate": 4.6,
                          "rate_unit": "字/秒", "rate_range": [3.5, 6.2], "rate_score": 100.0, "fluency": 90.4,
                          "coverage": 1.0, "visual_issues": [], "facing_share": 0.8},
              "notes": ["1 个口癖"], "aborted": null, "chosen": false}]
 }],
 "missing": [{"index": 2, "text": "记得点赞关注。"}],
 "rejected": [{"source": "…", "start": 1.64, "end": 4.96, "text": "…", "group": 2, "kind": "take | ng_marker", "detail": "…"}],
 "sequence": [{"source": "…", "start": 0.3, "end": 1.0, "text": "大家好", "group": null, "target_index": 0}],
 "plan": {"keep": [], "cuts": [], "transcript": [], "stats": {}, "...": "speech_cleanup 格式"},
 "plans": null,
 "stats": {"sentences": 6, "groups": 1, "rejected_takes": 2, "ng_markers": 0, "rejected_seconds": 4.04},
 "warnings": []
}
```

- `groups` 只列出多于一遍的组。有剧本时，只说了一遍但对上剧本的句子也会列出，方便看剧本相似度。
- `plan` 和 `cleanup_plan.json` 结构一样，多了 `cuts[*].reason == "take"`、`items[*].reason == "take"`、`stats.by_reason.take`，`settings.best_take = true`。含落选 take 的剪切区间里，空档全部记到 `take` 名下；`by_reason` 各项秒数之和仍然等于 `removed`。
- `plan` 按源视频的时间顺序剪辑。`sequence` 是最终的句子顺序：有剧本时按剧本顺序，没有剧本、多个源时按每组第一次出现的顺序。多机位或需要按剧本重排时，用 `sequence`。
- 多个源时 `plan = null`，`plans` 是 `{源: 计划}`，每个源各自去掉自己落选的 take。

## 限制

- 文本判断全部基于转写和 `difflib` 字符相似度，不懂同义改写。没有剧本时，像「今天讲视频剪辑的技巧 / 今天讲视频调色的技巧」这样只改了几个字的相邻句，会被当成同一句的两遍；这种情况给剧本，或者调高 `--similarity`。
- 修辞性重复（故意说两遍）会被当成重拍，只留一遍。
- 一遍里把剧本的两句连着说完，只会归到其中一句。
- 流畅度只看转写：whisper 吞掉的「嗯」检测不到。没有词级时间戳的句子，卡顿和语速都不准。没有声学分析（音量、气息、情绪）。
- 画面和人脸打分依赖 opencv 和源视频。opencv 5.x 没有 Haar 级联，`face` 维度会自动不参与（见 `shot_quality.md`）。只有多于一遍的组才做画面打分。
- 多机位同步拍摄时，各机位的转写要分别做（每个文件一个 analysis.json），时间轴不做对齐；选出来的是某个机位里的那一遍。
- `--draft` 的 `sequence` 模式每句前后各留 0.08s，句内的口癖不会剪掉。需要句内精剪时，用单源的 plan 模式。

## 给主线的集成建议（T18）

- **SKILL.md 路由**：`同一句说了多遍 / 挑最好的一遍 / 多机位选 take / NG 重拍 → scripts/best_take.py analysis.json [--script 剧本] [--draft 名称]（输出的 best_take_plan.json 可代替 speech_cleanup 的 cleanup_plan.json）`
- **rules**：挂到 `rules/cli.md`，在「口播精剪」那一节后面加一节「最佳 take」。引导流程里：analyze → （素材有重拍时）best_take → 字幕。有重拍时，用 best_take 代替 speech_cleanup，因为 plan 已经包含了口癖和气口清理。
- **Mixin 注册**：`scripts/core/take_ops.py:BestTakeOpsMixin` 加到 `JyProject` 的基类列表里，放在 `MediaOpsMixin` 之后。它内部会调用 `SpeechCutOpsMixin.add_speech_cleanup`，所以两个 Mixin 一起注册。注册后，`build_best_take_draft` 里的临时子类可以去掉。
- **requirements**：不需要改。语音部分只用标准库；画面部分复用 `shot_quality`（opencv-python、numpy，requirements 里已经有），缺失时降级并给出警告。
- **复用关系**：依赖 `speech_cleanup` 的 `load_transcript`、`_build_units`、`_split_clauses`、`_strip_ng`、`_same_start`、`_ratio`、`_numeric_diff`、`plan_cleanup` 和 NG 标记表。改这些私有函数的签名时，要同步检查 `best_take.py`。
