# 音乐节拍检测 + 卡点吸附（T2 / T3，分组 beat）

## 用途

- **T2 `scripts/beat_detect.py`**：分析 BGM，给出 tempo、拍点、强拍（每小节第 1 拍）、起音点（鼓点），以及段落能量（找副歌 / 高潮段，用来放高光镜头）。
- **T3 `scripts/beat_snap.py`**：
  - `snap_cuts`：把已有的切点（手工剪点、`analyze_footage` 的 scenes）吸附到最近的拍点 / 强拍 / 起音点。
  - `plan_beat_montage`：按节拍生成镜头时长序列。强拍和高潮段放高分镜头，高能段切得快，低能段切得慢；输出可以直接传给 `add_clip`。
  - `build_draft`：把计划写成剪映草稿（视频轨 + BGM，素材静音，强拍加 punch_in）。
- **`scripts/core/beat_ops.py`**：`BeatOpsMixin`（`apply_beat_plan`、`add_music_from`），落地草稿时用。还没有注册到 JyProject。

## 用法

### CLI

```bash
# 节拍检测 (默认写到 <音乐>.beats.json; 结果会缓存)
python scripts/beat_detect.py song.mp3 [-o beats.json] [--backend auto|librosa|numpy] \
       [--beats-per-bar 4] [--start-bpm 120] [--no-cache] [--cache-dir DIR] [--json]

# 切点吸附
python scripts/beat_snap.py snap --beats song.beats.json --cuts "1.02,2.4,3.97" [--tolerance 0.15] [--onsets] [--fps 30]
python scripts/beat_snap.py snap --audio song.mp3 --cuts analysis.json      # 读 analysis.json 的 scenes

# 混剪规划 (+ 可选直接生成草稿)
python scripts/beat_snap.py plan --audio song.mp3 --clips ./clips --density 标准 --start climax --duration 20 \
       -o plan.json [--build 卡点混剪 --drafts-root DIR --size 1080x1920] [--no-speedup] [--json]
```

`--clips` 可以传素材目录，也可以传 JSON：`[{path, duration?, score?, highlight?, source_start?}]`。
没写 `duration` 时用 ffprobe 读取，图片视为无限长。

### Python

```python
from beat_detect import detect_beats
from beat_snap import snap_cuts, plan_beat_montage, build_draft

res = detect_beats("song.mp3")                       # dict, 时间单位为秒
snap_cuts([1.02, 2.4], res, tolerance=0.15, fps=30)  # -> [1.0, 2.4] (2.4 超出容差, 保持原位)

clips = [{"path": "a.mp4", "score": 82, "highlight": 3.2}, {"path": "b.mp4", "score": 60}]
plan = plan_beat_montage(clips, res, density="标准", start="climax", duration=15, fps=30)
for sh in plan["shots"]:
    project.add_clip(**sh["add_clip"])               # 微秒 int, 时间轴从 0 开始无缝衔接
# 或者: build_draft(plan, "卡点混剪", drafts_root=...)

# 用 Mixin (主线注册后可直接 project.apply_beat_plan(plan))
class P(JyProject, BeatOpsMixin): ...
P("卡点混剪", 1080, 1920).apply_beat_plan(plan)
```

shot_quality.py 的结果可以直接映射成 clips：`score = summary.score`、`highlight = summary.best_t`。

## 输出格式

`detect_beats` / beats.json:

```json
{"schema": 1, "audio": "/abs/song.mp3", "backend": "librosa|numpy", "duration": 24.0,
 "tempo": 120.0, "beat_interval": 0.5, "beats_per_bar": 4, "downbeat_phase": 1,
 "beats": [0.49, 0.99], "downbeats": [0.99, 2.99], "onsets": [0.49, 0.99],
 "energy_sections": [{"start": 0.0, "end": 8.99, "energy": 0.0, "loudness_db": -31.2,
                      "level": "low", "bars": 5, "is_climax": false}],
 "climax": {"start": 8.99, "end": 16.99}, "cached": false}
```

`plan_beat_montage`:

```json
{"scene": "beat_montage",
 "music": {"path": "...", "source_start": 8.99, "duration": 15.0, "source_start_us": 8990000,
           "duration_us": 15000000, "volume": 0.9, "tempo": 120.0},
 "fps": 30, "density": "标准", "cut_every_beats": 2, "total_duration": 15.0,
 "shots": [{"index": 0, "path": "...", "clip_id": 0, "source_start": 2.5, "duration": 0.5,
            "target_start": 0.0, "music_time": 8.99, "on_downbeat": true, "level": "high",
            "energy": 1.0, "is_climax": true, "importance": 3.8, "score": 82, "accent": null,
            "add_clip": {"media_path": "...", "source_start": 2500000, "duration": 500000,
                         "target_start": 0, "track_name": "VideoTrack"}}],
 "cuts": [8.99, 9.49], "accents": [{"at": 2.0, "shot": 4, "move": "punch_in", "dur": 0.5}],
 "mute_footage": true,
 "stats": {"shots": 30, "clips_used": 6, "clips_total": 6, "avg_shot": 0.5,
           "max_cut_error_s": 0.011, "max_cut_error_frames": 0.33}}
```

`accents` 和 storyboard 的 `camera` 用同一种结构（`{at, move, dur}`），可以直接并进 `storyboard.main.camera`。

## 算法

- **解码**：用 ffmpeg 解成 22.05 kHz 单声道 PCM。没有 ffmpeg 时，`.wav` 用标准库 `wave` 读（Windows CI 能跑）。
- **numpy 后端**：频谱通量 onset envelope（STFT 1024 / hop 256，取对数幅度的半波整流差分）→ 自相关估 tempo（以 120 BPM 为中心、1 个八度标准差的对数高斯先验，再叠加 2 倍周期的自相关，防止把八分音符当成拍）→ Ellis 2007 动态规划跟踪拍点 → 拍点对齐到峰值并做亚帧插值。
- **librosa 后端**：`beat_track(trim=False)` 加 `onset_detect`。librosa 默认的 trim 会把安静前奏里的拍整段丢掉，所以关掉，首尾裁剪由外层统一做。
- **tempo 修正**：两个后端都用拍间隔中位数重新算 BPM，避免帧量化带来的误差。
- **强拍**：轮流试 `beats_per_bar` 种相位，选「低频 <160 Hz 通量（底鼓）+ 响度 + onset」加权平均最强的一组。
- **段落能量**：按小节算响度（dB），中值平滑，再按动态范围归一化（只用完整小节统计，弱起和余音不计入），分成 low / mid / high 三档，合并连续同档的小节，短于 2 小节的段并入相邻段。高潮段取「能量 × √时长」最大的一段。动态范围 < 3 dB 时整首都是 mid，不标高潮。
- **混剪切速**：以 `references/scenes/beat_montage.json` 的 `cut_every_beats` 为基准（舒缓 4 / 标准 2 / 快闪 1）。high 段切速加倍，low 段减半（跟 beat_montage.md 的「前奏 4 拍、主歌 2 拍、副歌每拍」一致）。切点对齐小节网格（以最近的强拍为 0），再对齐到帧。镜头最短 `avg_shot[0]`（0.3 s），超过素材可用长度的镜头在中间的拍点上再切开。
- **素材分配**：重要度 = 段落能量 + 强拍 0.5 + 高潮 0.3 + 开头钩子 2.0。按重要度从高到低挑素材：没用过的优先，其次分高的，相邻镜头不用同一个素材；有 `highlight` 时，截取窗口让高光落在镜头前 30%。素材被重复使用时，顺着往后取没用过的部分。
- **重音**：只加在 high / 高潮段的强拍上，最多 `effects.max` 个（4），间隔 ≥ 2 s，对应场景 checks 里的「不要每拍都加特效」。

## 精度（合成点击音轨，测试已覆盖）

| 后端 | tempo 误差 | 拍点最大误差 | 强拍 |
|---|---|---|---|
| numpy | < 0.3 BPM（90 / 128 / 150 BPM） | ≈ 14 ms（偏早约 10 ms） | 全对 |
| librosa | < 2 BPM | ≈ 28 ms（偏晚约 22 ms） | 全对 |

以 30 fps 计都在 1 帧以内，满足场景 checks 里的「切点落在节拍上（误差 ≤ 2 帧）」。

## 限制

- tempo 视为恒定（取拍间隔中位数），变速曲、rubato 的 BPM 只是近似值；拍点本身由 DP 跟踪，能容忍小幅漂移。
- 强拍判定依赖底鼓或响度重音。没有鼓的纯钢琴 / 弦乐曲可能错相位，这时可以用 `--beats-per-bar` 或手工指定 beats.json。
- 段落能量只看响度，不做和声 / 结构分析。「副歌」等于响度最高的连续段，渐强型的曲子可能只标出结尾。
- numpy 后端是整首一次性处理。3 分钟的歌约 0.3 s，内存约 60 MB；超长音频（> 30 分钟）建议先截取。
- `build_draft` 不会自动加转场 / 白闪，只加 punch_in 关键帧；白闪等效果留给 storyboard / 主线。
- 缓存的键是绝对路径 + 大小 + mtime + 参数。文件被原地改写但 mtime 不变时会命中旧缓存，可以用 `--no-cache`。

## 给主线的集成建议（T18）

- **SKILL.md Agent Quick Routing** 加一行：
  `| 卡点 / 踩点 / 节拍 / 混剪按鼓点切 | python scripts/beat_detect.py song.mp3 → python scripts/beat_snap.py plan --audio song.mp3 --clips DIR --start climax --build NAME |`
- **rules**：在 `rules/cli.md` 加「Beat tools」一节（两个 CLI 都支持 `--json`，遵守 `{ok, code, reason, data}`）。卡点场景的规则（切速、重音上限）沿用 `references/scenes/beat_montage.md`，把其中「skill 不能自动听歌，选曲后要请用户确认节拍」改为「先跑 beat_detect.py，节拍不准时再请用户确认 / 提供节拍」。
- **Mixin 注册**：在 `scripts/jy_wrapper.py` 的 `JyProject` 基类列表里加 `BeatOpsMixin`（`from core.beat_ops import BeatOpsMixin`）。注册后 `beat_snap.build_draft` 里的临时子类可以删掉，直接用 `JyProject`。
- **script_compiler 接入**（本分组没有改它）。注意 script_compiler 里的 `beats` 是剧本段落，不是音乐拍点。建议：
  1. 剧本 `music` 字段写了音频路径、且场景为 `beat_montage` 时，编译期调用 `detect_beats(music)`，把结果写进 `storyboard["music_beats"]`；
  2. 编译出切点（段落边界 / B-roll 插入点）后，调用 `snap_cuts(cuts, music_beats, tolerance=场景 camera.min_gap_s/2 或 0.15, fps=画幅 fps)`；
  3. 素材是一堆 clips 的纯混剪，直接用 `plan_beat_montage` 产出 `main` 轨的镜头列表，`accents` 并进 `main.camera`（字段结构一致），`music.source_start` 作为 BGM 的截取起点；
  4. 段落 `ratio`（前奏 / 主歌 / 高潮 / 尾声）可以改用 `energy_sections` 的真实边界，代替固定比例。
- **requirements.txt**：不需要改。numpy 已经在里面；librosa 可以作为可选依赖加一行注释：`# Optional: 更稳的节拍检测 (beat_detect.py) # librosa>=0.10`。
- **CHANGELOG**：新增节拍检测 / 卡点吸附 / 节拍混剪规划，以及 `BeatOpsMixin`。
