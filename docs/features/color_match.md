# 统一调色 (T13) — `scripts/color_match.py`

## 用途

多机位 / 多场景素材拼在一起时色调不一致（一段偏蓝、一段偏暗、一段发灰）。按一张参考帧或一段参考镜头，
给每个镜头算出剪映「调整」面板的参数（色温 / 色调 / 饱和度 / 亮度 / 对比度 / 高光 / 阴影，取值 -50~50，
和 `set_adjust` 一致），也可以给每段生成 `.cube` LUT 配合 `set_lut`。

## 原理

1. 每个镜头均匀抽 5 帧（避开首尾 10%，缩到最长边 320），统计 Lab 均值/标准差、亮度 p5/p50/p95、
   色度（饱和度）、灰世界白平衡（R/G、B/G）。
2. `simulate_adjust` 是剪映调节的**近似模型**（剪映算法没有公开），见 `MODEL_GAINS`：
   色温 → Lab b，色调 → Lab a，饱和度 → 色度缩放，对比度 → 以 L=50 为轴拉伸，亮度 → 中间调为主的 L 偏移，
   高光 / 阴影 → 按亮度加权的 L 偏移。
3. `fit_adjust` 先算初值，再做阻尼不动点迭代，让模拟后的统计量接近参考；取整并按 `strength` 缩放。
   模拟后差异反而变大时不给参数（只给警告）；参数到 ±50 上限时提示改用 LUT。
4. LUT 模式：Lab 均值/方差迁移（Reinhard），标准差比例限制在 0.5~2 倍以内，避免平坦画面被拉坏。

## CLI

```bash
# 指定参考帧, 多个镜头 (路径 或 路径@开始-结束 秒)
python scripts/color_match.py suggest --ref ref.jpg --shots a.mp4 b.mp4@3.5-8 c.png -o color_match.json

# 读 analyze_footage.py 的 analysis.json, 按 scenes 切镜头, 以第 0 段为参考, 同时生成 LUT 和预览三联图
python scripts/color_match.py suggest --analysis analysis.json --ref-shot 0 --lut-dir luts/ --preview-dir pv/

# 自动选参考 (统计量最接近全体中位数的一段), 保守强度
python scripts/color_match.py suggest --ref auto --shots a.mp4 b.mp4 c.mp4 --strength 0.7 --json

# 单个镜头的统计
python scripts/color_match.py stats clip.mp4@0-3 --json
```

`--json` 输出遵循 `rules/cli.md` 的 `{ok, code, reason, data}`；错误码 `invalid_input`（文件不存在/读不到）、
`missing_dependency`（没装 opencv），退出码 2。

## Python API

```python
import color_match as cm
res = cm.match_shots([{"source": "a.mp4", "start": 0, "end": 3}, {"source": "b.png"}],
                     reference={"source": "ref.jpg"}, strength=1.0, lut_dir=None)
s = cm.suggest_adjust(cm.sample_frames("b.mp4", 2, 5), cm.color_stats([cm.load_image("ref.jpg")]))
preview = cm.simulate_adjust(rgb_float_image, s["adjust"])     # 预览建议效果
table = cm.build_lut(src_stats, ref_stats, size=33); cm.write_cube("m.cube", table, 33)
```

Mixin（`scripts/core/color_match_ops.py`，`ColorMatchOpsMixin`）：

```python
p.match_color("ref.jpg", track_name="Main")                   # 写 set_adjust 面板参数
p.match_color(segs[0], segments=segs[1:], mode="lut")          # 以第一段为参考, 每段生成 LUT 并 set_lut
p.match_color(None, track_name="Main", dry_run=True)           # 自动选参考, 只返回建议
```

`reference` 可以是图片/视频路径、`"路径@开始-结束"`、`{"source","start","end"}`、片段对象或 None（自动）。
片段取素材的 `material_instance.path` 和 `source_timerange`（图片不看时间范围）。差异小于 `min_distance`
（默认 2）的片段不动。

## 输出格式

```json
{
  "model": "jy_adjust_approx_v1",
  "reference": {"label": "ref.jpg", "auto": false, "stats": {"L_mean": 52.1, "...": 0}},
  "shots": [{
    "index": 0, "label": "b.mp4@3.5-8", "source": "/abs/b.mp4", "start": 3.5, "end": 8.0,
    "stats": {"L_mean": 48.0, "L_std": 20.1, "L_p05": 15, "L_p50": 47, "L_p95": 80,
              "a_mean": 1.2, "b_mean": -9.8, "a_std": 8, "b_std": 12, "chroma_mean": 14,
              "chroma_spread": 14.4, "rgb_mean": [0.4, 0.45, 0.55], "wb_rg": 0.9, "wb_bg": 1.2},
    "diff": {"L_mean": -4.1, "b_mean": -12.3, "...": 0, "notes": ["偏暗", "偏冷 (蓝)"]},
    "is_reference": false,
    "adjust": {"色温": 27, "色调": -3, "亮度": 6},
    "params": {"temperature": 27, "tone": -3, "brightness": 6},
    "distance_before": 14.2, "distance_after": 0.9,
    "predicted_stats": {"...": 0},
    "lut": "/abs/luts/match_00_b_mp4_3_5-8.cube", "distance_after_lut": 0.7,
    "preview": "/abs/pv/shot_00.jpg",
    "warnings": []
  }]
}
```

Mixin 返回的每项还有 `segment_id` 和 `applied`（`"adjust"` / `"lut"` / None）。

## 限制

- 面板参数是近似模型换算的：方向可靠，幅度要在剪映里预览后微调；要精确匹配用 LUT 模式。
- 只匹配全局统计量，不分区域、不识别肤色；参考和镜头内容差别很大（室内 vs 户外夜景）时只能部分拉近（会给警告）。
- `mode="lut"` 会调用 `set_lut` → `import_lut`，把 .cube 复制进剪映 User Data 的 LUT 库（跟剪映里手动导入一样）。
- 视频抽帧用 OpenCV `VideoCapture`；个别编码 seek 不准时退回读第一帧。Windows 上中文路径的视频可能打不开
  （图片用 imdecode，没有这个问题）。
- 只依赖 numpy + opencv-python（requirements.txt 已有）。

## 给主线的集成建议

- SKILL.md Agent Quick Routing：`统一调色 / 多段色调不一致 / 按参考帧匹配颜色 → scripts/color_match.py suggest（草稿内用 p.match_color）`
- rules：挂到调色相关规则（`rules/jianying-11.md` 的「调整」一节，或新建 `rules/color.md`）；
  说明先 `match_color(dry_run=True)` 看建议，再应用；LUT 模式会写入剪映 LUT 库。
- 注册 Mixin：`scripts/jy_wrapper.py` 的 `JyProject` 继承列表加 `ColorMatchOpsMixin`
  （`from core.color_match_ops import ColorMatchOpsMixin`，需在 `AdjustOpsMixin` 之后任意位置）。
- 可在 guided 流程里，素材分析（analysis.json）之后加一步 `color_match.py suggest --analysis analysis.json`。
- requirements.txt：无改动。
