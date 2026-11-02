# T17 数据回流 `scripts/retention_feedback.py`

## 用途

导入平台的完播率 / 留存曲线 CSV，找出**陡降点**（掉人点），再与 T8 `quality_review.py` 的 report.json 对齐，看那一刻对应的镜头、字幕、旁白，以及是否在死区里、附近有哪些审片问题。据此输出**归因**和对 `references/methodology/*.md` 的**修改建议**。工具只输出建议，不修改任何文件。

## 用法

```bash
python scripts/quality_review.py --draft "草稿名" --video 导出.mp4 --out /tmp/qr/草稿名
python scripts/retention_feedback.py --csv 留存.csv --report /tmp/qr/草稿名/report.json [--out res.json] [--json]
python scripts/retention_feedback.py --csv 留存.csv --duration 58      # 没有报告: 只找掉人点
# --top 5 (最多报告几个掉人点)   --min-rate 1.5 (陡降最低速度, 百分点/秒)
```

```python
from retention_feedback import analyze, load_curve, find_drops
res = analyze("留存.csv", report_dict, duration=None, top=5, min_rate=1.5)
```

### CSV 兼容性

- 编码：UTF-8（含 BOM）、GBK / GB18030、UTF-16；分隔符可以是逗号、Tab 或分号；可以没有表头（第 1 列为时间，第 2 列为留存）。
- 时间列（按关键词匹配）：`时间点 / 时间 / 播放时长 / 播放进度 / 进度 / 秒 / time / second / position / progress`。支持的取值格式：`5`、`5.5`、`0:05`、`00:00:05`、`5s`、`5秒`、`1分5秒`、`10%`。百分比进度需要 `--duration` 或报告里的时长。
- 留存列：`留存率 / 观众留存 / 完播率 / 在看人数占比 / 观看人数占比 / retention / percent`。支持 `85.3%`、`85.3`、`0.853`（小于等于 1 时按小数处理）。
- 人数列（视频号常见）：`观看人数 / 在看人数 / viewers / count`，会按最大值换算成百分比。

## 算法

1. 先把曲线插值到 0.5s 网格，用 1s 中心差分算出每秒流失速度 rate（百分点/秒）。
2. 基线取 3s 以后 rate 的中位数，代表正常衰减。
3. rate > max(min_rate, 基线 × 2.5 + 0.5) 的连续区间算一次陡降。按掉的总量取前 top 个。
4. 对齐时用观众反应延迟窗口：从掉人起点往前看 2s。死区只有在掉人前已经持续 ≥ 1s 时才算原因；审片问题必须在峰值之前就已出现。
5. 归因：
   - 审片问题按类型映射到原因（钩子、镜头太长、色调跳变、BGM 压人声、声音断档、结尾）；
   - 另有几条独立判断：死区；前 3 秒掉人归为「开头没拦住」；掉人时已经很久没换画面，归为「镜头太长」；节奏能量低于全片中位数的 60%，归为「节奏低谷」；掉人前出现「总结 / 最后」等词，归为「提前给出讲完了的信号」；
   - 都没匹配上时，归为「内容本身」，建议人工复看。
6. 规则建议：按原因汇总各个掉人点，在对应的方法论文件里找到现有条文（返回文件名、行号、原文），再给出建议改法、证据（哪个掉人点、掉了多少）和置信度。证据越多、掉得越多，置信度越高。比如死区会给出「观众在死区持续 X 秒时开始离开」，并建议把 `quality_review --dead-zone` 设为 X−0.5。

## 输出（`--json` 时为 `data`）

```json
{
  "tool": "retention_feedback",
  "curve": {"time_col": "时间点", "value_col": "观众留存率", "kind": "rate", "baseline_rate_pp_s": 0.6, "drop_threshold_pp_s": 2.0},
  "metrics": {"retention_3s": 81.2, "retention_5s": 79.4, "retention_50pct": 70.1, "completion": 55.0, "avg_view_pct": 72.3},
  "drops": [{"index": 1, "start": 11.5, "end": 13.5, "peak": 12.5, "drop_pp": 12.0, "peak_rate_pp_s": 11.4, "vs_baseline": 19.0,
             "context": {"shot": {...}, "since_last_cut": 6.5, "on_screen_text": [...], "speech_before": [...],
                         "dead_zones": [{"start", "end", "dur", "tolerated"}], "issues": [...], "energy": 0.1},
             "attribution": [{"cause": "dead_zone", "label": "死区 (画面和信息都停住了)", "confidence": 0.85, "evidence": "..."}]}],
  "suggestions": [{"cause": "dead_zone", "target": {"file": "references/methodology/hook.md", "line": 34, "text": "- 每 3–5 秒一个信息点。..."},
                   "proposal": "...", "evidence": ["#2 12.5s 掉 12.0pp"], "lost_pp": 12.0, "confidence": 0.74,
                   "param": {"quality_review --dead-zone": 6.0}}],
  "notes": []
}
```

退出码：0 表示分析完成，2 表示输入错误（CSV 读不出、认不出列、报告不是 quality_review 生成的）。

## 限制

- 一次只分析一条视频。跨视频的规律需要调用方多跑几次，再汇总 `suggestions`；单条视频的建议置信度最高 0.9，只能当参考。
- 平台导出的曲线精度通常是 1s 或更粗，掉人点时间误差约 ±0.5s。0.5s 以内的闪帧这类问题对不准。
- 归因是相关性，不是因果：同一时刻可能有多个原因，按置信度排序列出。
- 平台没有公开的导出接口。CSV 需要用户从创作者后台手动下载，本工具不联网。

## 给主线的集成建议

- **SKILL.md Agent Quick Routing**：`发布后看数据 / 完播率 / 留存曲线 / 哪里掉人 → python scripts/retention_feedback.py --csv 留存.csv --report <quality_review 的 report.json>`
- **rules**：在 `rules/review.md` 末尾新增「发布后数据回流」小节，说明：跑完 retention_feedback 后，把 `suggestions` 拿给用户确认，确认后再由人或主线去修改 `references/methodology/*.md`，工具不会自动改。也可以挂到 `rules/guided.md` 的最后一步。
- **Mixin**：不需要。
- **requirements.txt**：不用改（只用到 numpy 和标准库 csv）。
