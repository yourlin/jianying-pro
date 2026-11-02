# 剧本与钩子生成（T14，分组 content）

## 用途

- **`prompts/hook_writer.md`**：给调用方 Agent 的提示词。输入主题、场景、观众、平台、时长、素材或转写，产出：
  - 停留理由分析；
  - 5 个**不同类型**的钩子，每个都带第一帧画面、0–3 秒画面安排、在哪一段兑现、自检结果、打分和推荐；
  - 按场景选的结构模板（问题 → 冲突 → 解决 → CTA、结果 → 步骤 → 总结 → CTA、痛点 → 产品 → 卖点 → 证明 + CTA……）；
  - 3 句金句候选；
  - 一份符合 `references/script_schema.md` 的剧本片段（`script`）。段落带 `priority`（给 `variants.py` 删减用）和可选的 `broll_keywords`（给 `broll_fetch.py` 检索用）。
- **`references/methodology/hook_bank.md`**：钩子类型库，共 8 类：反差、悬念、利益、数字、提问、结果先行、场景身份、冲突。每类给出句式、好例子和坏例子（带原因）、配什么画面。另有前 3 秒画面要求表、按场景选钩子的表、6 条钩子自检和金句写法。

## 用法

Agent 在引导模式第 ④ 步「写剧本」时：

1. 读 `prompts/hook_writer.md`，把 `{{TOPIC}}`、`{{SCENE}}`、`{{AUDIENCE}}`、`{{PLATFORM}}`、`{{DURATION}}`、`{{MATERIAL}}`、`{{LANGUAGE}}` 换成实际值。`MATERIAL` 可以贴 `analysis.json` 转写的「时间戳 + 文本」摘要。
2. 按提示词产出 JSON，把 5 个钩子给用户挑；用户选定后把 `script` 存成 `script.json`。
3. `python scripts/script_compiler.py script.json`：台词片段找不到时，按报错改 `time`。
4. 可选：`broll_fetch.py plan-template --script script.json` 会自动带入 `broll_keywords`；`variants.py script.json --targets 15,30,60` 出多个时长版本。

## 输出格式

```json
{
  "analysis": {"why_stay": "...", "payoff": "...", "best_evidence": "..."},
  "hooks": [{"type": "contrast|suspense|benefit|number|question|result|scene|conflict", "text": "...",
             "first_frame": "...", "visual_0_3s": "...", "pays_off_in": "S03", "from_material": false,
             "score": 8, "reason": "...", "self_check": []}],
  "recommended": 0,
  "structure": {"template": "问题 → 冲突 → 解决 → CTA", "reason": "..."},
  "golden_lines": ["...", "...", "..."],
  "script": { "...script_schema 字段...", "beats": [{"id", "purpose", "priority", "time", "line", "camera", "note", "elements", "broll_keywords"?}] }
}
```

`priority`、`line`、`broll_keywords` 是剧本里的附加字段。`script_compiler.py` 会忽略它们，现有编译不受影响。

## 限制

- 这是提示词和方法论文档，没有可执行代码，产出质量取决于调用方模型。JSON 合法性要靠 `script_compiler.py` 编译验证；用台词片段写的 `time` 必须原样出自转写。
- 钩子的例子都是中文短视频语境，英文 / TikTok 内容可以沿用这些类型，但例句要另写。
- 广告类内容的绝对化用语（「最」「第一」）只在自检里提醒，没有做违禁词库。

## 给主线的集成建议

- **SKILL.md Agent Quick Routing**：`| 写剧本 / 想开头钩子 / 多个钩子版本 / 金句 | prompts/hook_writer.md + references/methodology/hook_bank.md → script_compiler.py |`
- **rules**：`rules/guided.md` 第 ④ 步「写剧本」引用 `prompts/hook_writer.md`（先出 5 个钩子让用户选，再写全剧本）；`references/methodology/README.md` 的表格加一行 `hook_bank.md`（钩子类型库、好坏例子、前 3 秒画面）；`hook.md` 末尾可以加「类型和例子见 hook_bank.md」。
- **script_schema.md**：在 beats 字段说明里登记附加字段 `priority` / `tags` / `keep_in` / `drop_in`（`variants.py` 用）、`line`、`broll_keywords`（`broll_fetch.py` 用）。
- **Mixin / requirements**：无。
