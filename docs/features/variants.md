# 多版本输出：15 / 30 / 60 秒 × 画幅（T16，分组 content）

## 用途

`scripts/variants.py`：输入一份剧本（`script_schema` 格式）或已经编译好的分镜表，按段落优先级出 15 / 30 / 60 秒（可自定义）的删减计划，再给每个版本 × 每个画幅生成编译产物：

- 变体剧本：只保留选中的段落，时间写成源视频秒；
- 分镜表：主轨 keep、字幕、运镜和各元素都裁到选中的区间内；
- 装配命令：`storyboard_builder.py <分镜表>`。本脚本不装配，也不碰剪映草稿目录。

## 删减规则

- 段落优先级（数字越小越重要），按下面顺序取第一个能用的：
  1. `priority`：0–5 的整数；
  2. `tags`：`must` / `hook` / `cta` = 0，`core` = 2，`optional` = 4，`cut_first` = 5；
  3. 按 `purpose` 推断：钩子 / CTA = 0，点题 = 1，总结 / 金句 = 2，干货 / 要点 = 3，铺垫 / 过渡 / 彩蛋 = 4；
  4. 按元素推断：带 `cta` / `end_card` 的段落 = 0，带 `hero` 的 = 1；第一段 = 0。
- 指定某个版本：`"keep_in": ["15s"]` 表示该版本必须保留，`"drop_in": [60]` 表示该版本必须去掉。
- 先放优先级为 0 的必留段，再按优先级往里加（同级按原顺序），总长不超过 目标时长 × (1 + 容差)，容差默认 10%。
- 放不下整段时，有转写的段落按**句子边界**截短：保留前面的句子，句尾多留 0.25 秒呼吸。截短后至少要有 3 秒或原段的一半，否则不放。必留段加起来就超长时，先截长的、不是开头的段落。
- 选中的段落按原时间顺序排列，相邻区间合并，少一个剪点。
- 编译用 `script_compiler.Compiler` 的子类 `VariantCompiler`，只改了 `keep_ranges`（和版本区间取交集）。所以最后一段延长、CTA 的 `last`、剪点的假景别拉回等规则，都按这个版本自己的剪点算。

## CLI

```bash
python scripts/variants.py script.json --targets 15,30,60 --formats vertical,horizontal [--style tech] \
       [--tolerance 0.1] [-o variants/] [--plan-only] [--json]
python scripts/variants.py storyboard.json --targets 15,30    # 已编译的分镜表: 只删减, 不能换画幅
```

画幅可以写 `vertical` / `horizontal` / `portrait_3x4` / `square`，也可以写别名：`竖屏` / `横屏` / `小红书` / `方形` / `9:16` / `16:9` / `3:4` / `1:1`。

## Python API

```python
from variants import make_variants, plan_variant, units_from_script, restrict_storyboard
res = make_variants("script.json", targets=[15, 30, 60], formats=["vertical", "horizontal"], out_dir="variants")
```

## 输出

文件（默认放在 `<输入目录>/variants/`）：

- `<名>_15s.script.json`：变体剧本，带 `"variant": {"target", "label", "ranges", "from"}`；
- `<名>_15s_<画幅>[_<风格>].storyboard.json`：分镜表，`compiled_from.variant` 记录版本信息。草稿名加后缀：`<草稿名>_15s`，换画幅时变成 `<草稿名>_15s_horizontal`，不会覆盖原草稿。

返回 / `--json` 的 `data`：

```json
{
 "source": "/abs/script.json", "kind": "script", "full_duration": 60.8,
 "formats": ["vertical", "horizontal"], "out_dir": "/abs/variants",
 "variants": [{
   "target": 15.0, "label": "15s", "duration": 15.15, "budget": 16.5,
   "beats": [{"id": "S01", "range": [0.0, 7.6], "priority": 0, "purpose": "钩子", "trimmed": false},
             {"id": "S07", "range": [49.4, 56.95], "priority": 0, "purpose": "CTA", "trimmed": true}],
   "dropped": ["S02", "S03", "S04", "S05", "S06"],
   "ranges": [[0.0, 7.6], [49.4, 56.95]],
   "warnings": [],
   "script": "/abs/variants/script_15s.script.json",
   "builds": [{"format": "vertical", "style": null, "storyboard": "...", "draft_name": "人字篇V1_15s",
               "output_duration": 15.15, "build_cmd": "python .../storyboard_builder.py \"...\"",
               "compile_args": {"script": "...", "format": "vertical", "style": null}, "notes": []}]
 }, {"target": 90.0, "label": "90s", "skipped": "原片只有 60.8s", "builds": []}]
}
```

`warnings` 会提示以下情况：必留段就超出上限、内容不到目标时长的 60%、某个重要段落放不下又没有转写可以截短。

## 限制

- 截短只能按句子边界截，需要剧本配 `analysis.json`（或者分镜表带 `subtitles.lines`）。没有转写的段落只能整段保留或整段去掉。
- 截短总是保留段落前面的句子。如果 CTA 段的关键句在后面（比如「关注我」放在最后），要么给 CTA 段单独拆一个短段落，要么调 `priority`。
- 输入分镜表时不能换画幅（坐标已经按原画幅排好），要换画幅请传剧本。
- 输入已生成的剪映草稿（只有 draft_content.json、没有分镜表）还不支持。本 skill 的草稿都是由分镜表装配出来的，请对分镜表跑。
- 如果分镜表里 `main.transitions` 是按剪点顺序写的列表，剪点数量变了以后对不上，会退化成第一个转场用于所有剪点。

## 给主线的集成建议

- **SKILL.md Agent Quick Routing**：`| 出 15/30/60 秒版本 / 横竖屏多版本 / 一稿多发 | scripts/variants.py script.json --targets 15,30,60 --formats vertical,horizontal → 逐个 storyboard_builder.py |`
- **rules**：挂到 `rules/guided.md` 第 ⑤ 步之后（「要不要出短版本 / 横屏版本？」作为一道选择题），`rules/cli.md` 加 CLI 说明。
- **script_schema.md**：beats 字段登记 `priority` / `tags` / `keep_in` / `drop_in` / `trim`（false 表示不允许截短）。
- **Mixin / requirements**：无。只调用 `script_compiler` 的公开内容（`Compiler`、`Transcript`、`resolve_time`、`load_profile`、`ScriptError`），没有修改它。
