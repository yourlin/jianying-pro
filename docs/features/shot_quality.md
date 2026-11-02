# 素材质量打分（T6）`scripts/shot_quality.py`

## 用途

给视频按固定间隔抽帧，对每个采样点和每一段打 0–100 分，并给出问题标签。可以用来：挑最好的镜头或 take（T7）、挑封面帧（T15）、粗剪前剔除废片（模糊、抖动、黑帧、过曝）。

## 测量方法（先把短边降到 360 像素）

| 维度 | 方法 | 子分 |
|---|---|---|
| 清晰度 | 灰度 Laplacian 方差，4x4 分块取最清晰的 4 块的均值 `lap_focus`（避免纯色墙、浅景深背景把整帧拉低）；`lap_var` 是整帧方差 | log10 映射，15 → 0、400 → 100 |
| 抖动 | 从采样点起连续读 4 帧，相邻两帧之间用 `cv2.phaseCorrelate` 求位移。位移均值记为 `motion_px`（运镜速度），位移偏离均值的幅度记为 `shake_px`（抖动）。平稳的推拉摇移不算抖 | `shake_px` 到 4 时为 0；运镜每帧超过 12px 也扣分 |
| 曝光 | 平均亮度、过曝（≥250）/ 欠曝（≤8）像素占比、亮度标准差 | 均值在 85–165 之间不扣分 |
| 人脸 | OpenCV Haar 级联（和 `analyze_footage.py` 同一套）：正脸级联命中表示正对镜头，只有侧脸级联命中表示没正对；在脸的上 60% 里找眼睛；用肤色占比过滤误检（植物、手） | 正脸 100 / 侧脸 55，再乘眼睛、大小、贴边的系数 |
| 转场 | 4 帧之间亮度跳变超过 18，或直方图 Bhattacharyya 距离超过 0.35 | 总分乘 0.6 |

总分 = 清晰 40% + 稳定 25% + 曝光 35%；检测到人脸时按 8:2 再混入人脸分。黑帧的总分不超过 5。`--require-face` 时没有人脸的采样点乘 0.7（口播素材用）。

问题标签：模糊、抖动、运镜过快、过曝、欠曝、黑帧、低对比、转场、无人脸、未正对镜头、看不到眼睛、人脸过小、人脸贴边。某个问题出现在一段 30% 以上的采样点里，就记到这一段的 `issues` 里。

## CLI

```bash
python scripts/shot_quality.py footage.mp4                         # 整段，每 0.5s 一个采样点，打印摘要
python scripts/shot_quality.py footage.mp4 --ranges "0-3,5.5-8" -o quality.json
python scripts/shot_quality.py footage.mp4 --scenes analysis.json --json   # 按 analyze_footage 的镜头切换点分段
python scripts/shot_quality.py talk.mp4 --require-face --every 1
```

`--json` 按 rules/cli.md 的约定输出 `{ok, code, reason, data}`，失败时 `code = "shot_quality_failed"`。

## Python API

```python
from shot_quality import score_video, score_segments, measure, grab_frame, ranges_from_scenes
q = score_video("a.mp4", every_s=0.5, require_face=False)
q = score_segments("a.mp4", [(0, 3), (5.5, 8)], every_s=0.5, min_samples=3)
m = measure([frame0, frame1, frame2, frame3])  # 单个采样点（连续 BGR 帧）
```

## 输出格式（data）

```json
{
 "source": "/abs/a.mp4",
 "video": {"width": 1920, "height": 1080, "fps": 25.0, "frames": 400, "duration": 16.0},
 "every_s": 0.5,
 "face_detector": "haar | unavailable",
 "samples": [{"t": 0.25, "score": 86.2,
   "sharpness": {"lap_var": 43.0, "lap_focus": 139.7, "score": 68.0},
   "stability": {"shake_px": 0.1, "motion_px": 0.2, "score": 97.5},
   "exposure": {"mean": 126.0, "std": 40.1, "over": 0.0, "under": 0.0, "score": 100.0},
   "face": {"count": 1, "box": [0.42, 0.2, 0.58, 0.6], "facing": true, "eyes": 2, "size": 0.4, "skin": 0.97, "score": 100.0},
   "transition": false, "issues": []}],
 "summary": {"score": 85.8, "median": 87.2, "p10": 81.3, "issues": {"抖动": 0.06}, "best_t": 1.5, "worst_t": 9.0},
 "segments": [{"start": 0, "end": 3, "score": 84.0, "min": 80.1, "best_t": 1.5, "worst_t": 0.25, "issues": [], "n": 6}]
}
```

`face` 为 null 表示没检测到人脸或检测器不可用。`eyes = -1` 表示没有眼睛级联，状态未知。`segments` 只在传了 ranges 或 scenes 时出现。

## 性能

1080p、23s 的视频每 0.5s 采样一次，在 M 系列 Mac 上约 1.5s。采样点之间隔得近就顺序 grab，隔得远（超过 3s）就 seek。

## 限制

- **opencv-python 5.x 去掉了 `CascadeClassifier` 和自带的级联文件**（本机 `~/.venvs/jianying-pro` 装的是 5.0），这种环境下人脸检测降级为 `face_detector = "unavailable"`，其他指标照常。requirements.txt 固定的 4.11 自带级联。也可以用环境变量 `JY_HAARCASCADE_DIR` 指定级联目录。`analyze_footage.detect_faces` 在 opencv 5 上会直接抛 AttributeError，建议主线一起处理。
- Haar 级联只认比较正的正脸和侧脸，戴口罩、大角度低头、画面里人脸很小（不到短边的 10%）时检测不到。"表情好不好"只用"正脸 + 眼睛可见"来近似，没有做表情分类。
- 纹理很少的画面（纯色背景、天空）即使对焦准确，清晰度分也偏低。
- 抖动靠全局平移估计，不处理旋转和缩放抖动；画面里有大面积运动主体时，可能会被误判为运镜。

## 给主线的集成建议

- SKILL.md 路由一行：`素材质量 / 挑镜头 / 废片检测（模糊、抖动、曝光、人脸） → python scripts/shot_quality.py <视频> [--scenes analysis.json] --json`
- rules：挂到 `rules/cli.md`（CLI 工具清单），引导模式（guided）第 1 步 analyze_footage 之后加一步可选的"素材打分"。
- Mixin：没有，这是独立 CLI 和函数库，不需要注册 JyProject。
- requirements：不用改（只用 opencv-python、numpy）。建议把 `opencv-python` 固定在 `<5`，否则人脸检测不可用。
