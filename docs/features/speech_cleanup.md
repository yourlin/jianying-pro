# 口播精剪（T1）：`scripts/speech_cleanup.py`

## 用途

拿词级转写（`analyze_footage.py` 的 `analysis.json`，或单独的转写 JSON）规划口播精剪，给出保留区间和每一刀的原因，也可以直接生成剪映草稿。

| 原因 `reason` | 检测内容 | 规则 |
|---|---|---|
| `filler` | 口癖、填充词 | 词表可配置，分 4 种模式（见下）。「那个按钮」这类指代用法不删 |
| `stutter` | 结巴 / 重复起词：「我我我」「我们，我们」 | 只留最后一遍。连读的叠词（看看、谢谢）不算：要么说了 ≥3 遍，要么两遍之间停顿 ≥ `stutter_gap`(0.1s) |
| `ng` | 说错重来：「不对 / 重来 / 再说一遍…」 | 标记必须单独成句。删掉标记本身，再往回找和后面重说同开头的位置，从那里删起（可以从子句中间开始）。**强标记**（重来、再说一遍、说错了…）找不到同开头的句子时，删紧挨着的上一句；**弱标记**（不对、错了、等一下…）后面必须确实重说了同一句才剪，前一句是问句（「对吗？不对。」）时不剪 |
| `repeat` | 重复句、起句重来 | 相邻句（往后最多看 3 句、12s，中间只隔着 ≤6 字的碎句）相似度 ≥ `similarity`(0.72)，或者前一句（或它的后半截）是后一句的开头：只留最后一遍。只差在数字 / 序号上的排比（第一步… / 第二步…）不删 |
| `pause` | 过长气口 | 不全删，压到 `max_pause`(0.25s)；剪掉口癖后，前后的停顿合起来一起算 |
| `edge` | 片头 / 片尾静音 | 各留 `edge_keep`(0.15s) |

口癖模式：

- `always`：出现就删，比如 嗯 / 呃 / 唔 / um / uh。
- `isolated`：在句首、前后有停顿或拖长音时删，比如 啊 / 额 / 哦 / 哎。句末语气词（「好啊」）保留。
- `pause`：后面有停顿、拖长、连说（那个那个）、后面紧跟另一个口癖，或者整句都是口癖时才删。比如 那个 / 这个 / 就是说 / 然后呢 / 怎么说呢 / you know。
- `strict`：只在前后都有停顿、连说，或者整句都是口癖时删。用于高频连接词：就是 / 然后 / 那么 / 其实 / like / so。
- `DEFAULT_KEEP_PREFIX` 用来防误删，比如「也就是说」「这就是」「先然后」里的这几个词不当口癖处理。

切点会吸附到字边界，前后至少各留 `pad`(0.05s)，防止吞字。另外，词级时间戳经常把字尾拖进后面的静音里，所以会用 analysis 里的 `silences`（ffmpeg silencedetect）修正保留字的首尾。

## 用法

### CLI

```bash
# 1. 先转写 (已有)
python <SKILL_ROOT>/scripts/analyze_footage.py talk.mp4 -o analysis.json

# 2. 规划精剪 → cleanup_plan.json (默认写在 input 同目录)
python <SKILL_ROOT>/scripts/speech_cleanup.py analysis.json
python <SKILL_ROOT>/scripts/speech_cleanup.py analysis.json --max-pause 0.35 --pad 0.06 -o plan.json
python <SKILL_ROOT>/scripts/speech_cleanup.py analysis.json --extra-fillers "对吧:strict,你懂吧" --no-repeats
python <SKILL_ROOT>/scripts/speech_cleanup.py analysis.json --fillers "嗯:always,那个:pause"   # 覆盖整张口癖表
python <SKILL_ROOT>/scripts/speech_cleanup.py analysis.json --filler-file my_fillers.json      # {词: 模式}
python <SKILL_ROOT>/scripts/speech_cleanup.py raw_transcript.json --video talk.mp4 --duration 63.2

# 3. 直接生成剪映草稿 (按 keep 区间 add_clip; --punch-in 让奇数段放大, 做假景别遮跳剪)
python <SKILL_ROOT>/scripts/speech_cleanup.py analysis.json --draft "口播精剪" --punch-in 1.25
python <SKILL_ROOT>/scripts/speech_cleanup.py analysis.json --json      # {ok, code, reason, data}
```

开关：`--no-fillers` / `--no-stutters` / `--no-ng` / `--no-repeats` / `--keep-pauses`。还可以用 `--ng-markers "重来,cut"` 覆盖标记表（自定义标记一律按强标记处理）、`--similarity 0.8` 调相似度、`--drafts-root` / `--track` 指定草稿目录和轨道。

错误码（`--json`）：`not_found`（文件不存在，或生成草稿时找不到源视频）、`invalid_input`（JSON 坏了或没有转写）、`empty_plan`。

### Python

```python
from speech_cleanup import plan_cleanup, remap_time, build_cleanup_draft

plan = plan_cleanup(analysis_dict)                       # 或者传转写列表 [{start,end,text,words:[{w,s,e}]}]
plan = plan_cleanup(segments, silences=sil, duration=63.2, max_pause=0.3, pad=0.05,
                    extra_fillers={"对吧": "strict"}, remove_repeats=False, similarity=0.8)
remap_time(plan, 12.34)       # 源视频时间 → 精剪后时间 (落在剪掉的区间里返回 None)
build_cleanup_draft(plan, "talk.mp4", "口播精剪", drafts_root=None, punch_in=1.25)

# Mixin (尚未注册到 JyProject)
from jy_wrapper import JyProject
from core.speech_cut_ops import SpeechCutOpsMixin
class P(JyProject, SpeechCutOpsMixin): ...
p = P("口播精剪")
segs = p.add_speech_cleanup("talk.mp4", plan)            # 也可以直接传 analysis / 转写, 现场规划
p.save()
```

`plan_cleanup(..., **overrides)` 可以覆盖 `DEFAULTS` 里的任何阈值：`max_pause`、`pad`、`edge_keep`、`min_cut`、`pause_th`、`prolong_th`、`clause_gap`、`stutter_gap`、`similarity`、`retake_window`、`ng_lookback`、`silence_trust`。传未知参数会抛 `TypeError`。

## 输出格式（`cleanup_plan.json`）

```json
{
  "source": "/abs/talk.mp4", "duration": 63.2,
  "settings": {"max_pause": 0.25, "pad": 0.05, "...": "..."},
  "keep": [{"start": 0.35, "end": 1.2, "duration": 0.85, "target_start": 0.0, "text": "大家好"}],
  "cuts": [{"start": 1.2, "end": 1.55, "duration": 0.35, "reason": "filler", "reasons": ["filler", "pause"],
            "seconds": {"filler": 0.18, "pause": 0.17},
            "items": [{"reason": "filler", "text": "嗯", "start": 1.2, "end": 1.38, "detail": "嗯: 填充音"}]}],
  "transcript": [{"start": 0.0, "end": 2.9, "text": "大家好今天讲剪辑", "words": [{"w": "大", "s": 0.05, "e": 0.23}]}],
  "stats": {"original": 63.2, "kept": 51.8, "removed": 11.4, "keep_ratio": 0.82, "pieces": 37,
            "by_reason": {"filler": {"count": 9, "seconds": 2.1}, "pause": {"count": 20, "seconds": 6.3}},
            "items": {"filler": 9, "ng": 1}},
  "warnings": []
}
```

- `keep` 和 `cuts` 按时间排列，正好拼满 `[0, duration]`。`target_start` 是这一段在精剪后时间线上的起点。
- `transcript` 已经映射到精剪后的时间线，格式和 `analyze_footage` 一样，可以直接拿去生成字幕或做后续分析（比如 T7 最佳 take）。
- `by_reason.seconds` 加起来等于 `removed`。某一刀里嵌在别的原因中间的部分，记到覆盖它的最小条目上（比如说错重来的那段里夹着一个「嗯」，这个「嗯」算口癖）。

## 限制

- 全部靠转写文本和时间戳做启发式判断，没有做声学分析。转写漏掉的口癖（whisper 常把「嗯」吞掉）剪不到。可以在 `analyze_footage.py --prompt` 里写上「嗯、呃、那个」，让转写保留填充词。
- 没有词级时间戳的句子按字数均分时长，切点会不准，`warnings` 里会提示。
- 修辞性重复（「很重要，很重要」）会被当作重复句，只留一遍。这类内容多的话用 `--no-repeats`。「这个很重要。这个很重要的原因是…」也可能被当作起句重来。
- `那个` / `这个` 后面跟了停顿时会被删。偶尔会误删真正的指代（「我要那个，……」），可以把它们改成 `strict` 模式。
- 相似度用的是 `difflib`，按字比较，不懂同义改写。
- 草稿生成只放一条视频轨（音视频同一个片段），不做 J/L-cut、B-roll 覆盖、字幕。剪点遮挡只提供 `punch_in` 交替缩放。
- 测试 `test_build_draft_from_plan` / `test_mixin_plans_on_the_fly` 需要 `ffprobe`，没有时跳过。

## 给主线的集成建议（T18）

- **SKILL.md 路由**：`口播精剪 / 删口癖 / 去嗯啊 / 说错重来 / 剪气口 → scripts/speech_cleanup.py analysis.json [--draft 名称]（先 analyze_footage 转写；plan.json 里的 transcript 可直接做字幕）`
- **rules**：挂到 `rules/cli.md`（新增一节「口播精剪」），并在引导流程（analyze → 精剪 → 字幕）里把它放在 `analyze_footage.py` 之后。`references/methodology/pacing.md`「剪气口」可以加一句「自动实现：speech_cleanup.py，默认 max_pause 0.25s / pad 0.05s」。
- **Mixin 注册**：`scripts/core/speech_cut_ops.py:SpeechCutOpsMixin` 加到 `jy_wrapper.JyProject` 的基类列表里（依赖 `add_clip`，放在 `MediaOpsMixin` 之后）。注册后，`build_cleanup_draft` 里的临时子类可以改成直接用 `JyProject`。
- **requirements**：不需要改，纯标准库（difflib / bisect）。草稿生成复用已有依赖。
- **下游**：T7 最佳 take 可以复用 `_retake_kind` / `_same_start` 的「同句多遍」分组，也可以读 `cuts[*].items` 里 `reason in ("repeat", "ng")` 的条目，作为候选 take。
