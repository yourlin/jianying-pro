# 音频：自动 ducking（T4）· 音效库索引（T5a）· 音效设计（T5）

## 用途

- **T4 自动 ducking**（`scripts/core/mix_ops.py` → `MixOpsMixin`）：有人声的地方用音量关键帧把 BGM 压低（默认 -10 dB）。人声开始前 `attack` 秒开始往下压，人声结束后 `release` 秒内恢复。两段人声之间的间隔如果比 `attack + release + hold` 还短，就一直压着不恢复，免得 BGM 一抽一抽。
- **T5a 音效库索引**（`scripts/build_sfx_library.py` → `data/sfx_library.csv`）：把剪映云端音效（`data/cloud_sound_effects.csv`）和本地音效目录按用途分成 17 类：whoosh、pop、click、hit、riser、ding、typing、camera、boom、glitch、page、cash、error、magic、laugh、applause、countdown。背景乐、环境底噪、动物声不收录。
- **T5 音效设计**（`scripts/sfx_designer.py`）：拿时间线事件（转场、文字出现、强调词、卡点、结尾……）查规则表，生成音效铺设计划，并控制密度防止太吵；也可以直接写进草稿的音频轨。

## 剪映云端音效能否按 ID 引用

可以。`CloudManager.download_asset(effect_id)` 先查 `cloud_sound_effects.csv`，再按 ID 现取下载地址，下到本地缓存后当普通音频用 `add_audio_safe` 放进草稿（`add_cloud_media` 也是这样做的）。会员音效（`is_vip=1`）不下载，索引里标成 `available=0`；`is_vip` 为空的也按不可用处理。

## API

```python
from jy_wrapper import JyProject
from core.mix_ops import MixOpsMixin, voice_ranges_from_analysis, duck_envelope

class P(JyProject, MixOpsMixin):  # 主线注册后直接用 JyProject
    pass

p = P("口播成片", overwrite=False)

# 1) 整条 BGM 轨自动 ducking: BGM 轨按名称识别 (bgm / music / 音乐 / 配乐 / 背景),
#    人声轨按名称识别 (voice / tts / 配音 / 人声 / 旁白 / 口播 / 解说 ...), 也可以显式指定
p.auto_duck(bgm_track="BGM", voice_tracks=["配音"], depth_db=-10, attack=0.15, release=0.3, hold=0.3)

# 2) 人声来自 analysis.json (analyze_footage.py): 素材在时间线 T 秒开始、从素材 S 秒截取时 offset = T - S
p.auto_duck(analysis="analysis.json", analysis_source="transcript", analysis_offset=0.0)

# 3) 单个片段, 显式传区间 (秒) 或片段对象
p.duck_bgm(bgm_seg, voice_ranges=[(1.2, 4.8)], voice_segments=[tts_seg], depth_db=-12)
p.duck_bgm(bgm_seg, voice_ranges=[...], dry_run=True)  # 只计算包络, 不写入

# 4) 音效设计
import sfx_designer as sd
events, total = sd.events_from_draft(p)                     # 转场 / 切点 / 文字 / 结尾
events += sd.events_from_analysis("analysis.json", ["免费", "重点"])  # 强调词 + 镜头切换
events += sd.events_from_beats("beats.json")                # 强拍 (downbeats 优先)
plan = sd.design(events, duration=total, voice_ranges=voice_ranges_from_analysis("analysis.json"))
p.apply_sfx_plan(plan, track_name="SFX")                    # 重叠时自动用 SFX_1, SFX_2 ...
p.save()
```

纯函数：`merge_ranges`、`duck_envelope(seg_start, seg_end, ranges, depth_db, attack, release, hold, base_volume)`、`voice_ranges_from_analysis`、`write_volume_keyframes` / `get_volume_keyframes`、`iter_tracks(script)`（导入轨道和新建轨道一起遍历）。

## CLI

```bash
# T4: 给已有草稿做 ducking (--dry-run 只算不存)
python scripts/core/mix_ops.py --draft 草稿名 [--root 草稿目录] [--bgm-track BGM] [--voice-tracks 配音,TTS] \
    [--analysis analysis.json --source transcript --offset 0] [--depth -10 --attack 0.15 --release 0.3] [--json]

# T5a: 重建 / 校验音效索引
python scripts/build_sfx_library.py [--local-dir ~/SFX] [--json]
python scripts/build_sfx_library.py --validate

# T5: 规则表 / 生成计划 / 写入草稿
python scripts/sfx_designer.py rules
python scripts/sfx_designer.py plan --events events.json --duration 30 -o sfx_plan.json
python scripts/sfx_designer.py plan --draft 草稿名 --analysis analysis.json --keywords 免费,重点 \
    --beats beats.json --voice-from-analysis --sfx-dir ~/SFX --write
python scripts/sfx_designer.py apply --plan sfx_plan.json --draft 草稿名 [--track SFX]
```

加 `--json` 时按 `rules/cli.md` 的约定输出：`{"ok", "code", "reason", "data"}`。

## 数据格式

`data/sfx_library.csv`：开头几行是 `#` 注释，跟 `data/` 下其他文件一样。

| 字段 | 说明 |
|---|---|
| id | 剪映 effect_id；本地音效为 `local:<相对路径>` |
| name | 名称（已去掉版权库编号） |
| category | whoosh / pop / click / hit / riser / ding / typing / camera / boom / glitch / page / cash / error / magic / laugh / applause / countdown；本地音效分不出类时为 misc |
| duration_s | 时长（秒）；本地音效用 soundfile / ffprobe 读，读不到为空 |
| source | `jianying_cloud` / `local` |
| is_vip / available | 会员标记；available=1 表示可以直接用 |
| path | 本地文件路径（云端音效为空） |
| tags | 剪映原分类，或本地子目录 |

本地目录的分类规则：先看子目录名（如 `SFX/whoosh/a.wav`），再看文件名关键词。

事件文件写成 `[{"type", "time", ...}]` 或 `{"events": [...], "duration": 30}`。可选字段：`category`（覆盖分类）、`sfx_id`（指定音效）、`priority`、`volume_db`、`offset`、`duration`（typing 这类持续型事件用）。事件名支持中文别名，如 转场、强调词、卡点、结尾、标题、揭晓、拍照、打字。

铺设计划 `plan`：

```json
{"items": [{"time": 3.0, "start": 2.9, "duration": 1.0, "event_type": "transition", "category": "whoosh",
            "sfx_id": "6864839893147715585", "name": "嗖", "source": "jianying_cloud", "path": "",
            "volume_db": -15, "volume": 0.1778, "offset": -0.1, "trimmed": false}],
 "dropped": [{"event": {...}, "reason": "too_close | same_category_too_close | density_limit | during_voice | no_rule | no_sfx_in:... | out_of_timeline"}],
 "stats": {"events": 8, "placed": 6, "dropped": 2, "per_minute": 18.0, "by_category": {...}},
 "settings": {"min_gap": 0.6, "same_gap": 1.5, "max_per_window": 4, "window": 10.0, "duration": 20}}
```

## 规则与密度（默认值）

- 转场配 whoosh，-15 dB，提前 0.1s；强调词配 hit 或 ding，-14 dB；开场钩子配 hit 或 boom，-12 dB。
- 文字出现配 pop，-18 dB；揭晓配 riser，终点对齐事件点；结尾配 ding，终点落在结尾前 0.3s。
- 卡点配 click，-22 dB，人声段内跳过。普通硬切默认不配音效（口播跳切太密）。完整规则见 `sfx_designer.py rules`，可以用 `--rules '{"cut": {"categories": ["whoosh"]}}'` 打开或覆盖，设为 null 则关闭。
- 密度控制：
  - 任意两个音效间隔至少 0.6s；
  - 同一分类间隔至少 1.5s；
  - 每 10s 最多 4 个；
  - 冲突时保留优先级高的事件。
- 选音效：
  - 只选 available=1 的；
  - 时长合适的优先，其次本地优先；
  - 同名音效去重，每类在前 5 个候选里轮换，避免同一个音效反复出现；
  - 超过规则 `max_dur` 的音效会被截断，末尾加淡出。
- 音量参考 `references/methodology/audio.md` 和 `transitions_fx.md`：音效 -18 到 -12 dB，少即是多。

## 限制

- **关键帧时间**：音量关键帧按「素材时间」写（截取起点 + 相对时间 × 速度），跟 `TrackOpsMixin._keyframe_base` 在 11.7 上对视频关键帧的实测一致。但 `track_ops.add_keyframes` 的音频分支目前直接写相对时间。BGM 从素材 0s 开始、速度为 1 时两者一样；从中间截取的 BGM 需要主线在剪映里实测一次，统一两边的写法。
- **关键帧数值**：写的是绝对音量（片段 volume × 衰减），片段的 `volume` 字段不改。
- **导入模式**：vendor pyJianYingDraft 的导入模式（`overwrite=False` 重新加载草稿）在 Python 3.14 下会报 `KeyError: 'fps'`，因为读不到类注解。这也是两个基线旧失败的原因。相关测试在这种情况下自动 skip；CI 用的 Python 3.12 能正常跑（本地用 3.12 验证过 43/43 通过）。
- **云端音效**：写入草稿时要联网下载；下载失败的条目放进 `failed` 返回，不会中断其他条目。
- **事件推断**：`events_from_draft` 只看第一条视频轨的切点、非字幕文字轨，以及时间线结尾。字幕轨的识别方式：轨道名含 字幕 / subtitle / caption，或者有 8 段以上、覆盖超过 40% 时长。
- **时间对齐**：analysis.json 和节拍文件的时间是素材时间，需要用 `--offset` 换算到时间线。
- **分类准确度**：分类靠名称关键词，不听音频，个别条目可能分错。需要特定音效时可以在事件里指定 `sfx_id`。

## 给主线的集成建议

- **注册 Mixin**：在 `scripts/jy_wrapper.py` 里 `from core.mix_ops import MixOpsMixin`，然后把它加到 `JyProject` 的继承列表里（放在 `MediaOpsMixin` 之后即可）。它依赖 `add_audio_safe` 和 `cloud_manager`。
- **SKILL.md 路由**：加一行：「**BGM 让位人声 / 自动 ducking / 铺音效**（用户说"人声时音乐小一点""加音效""转场加嗖的一声"等时）：`project.auto_duck()` 或 `python <SKILL_ROOT>/scripts/core/mix_ops.py --draft 名称`；音效 `python <SKILL_ROOT>/scripts/sfx_designer.py plan --draft 名称 --write`；音效库 `data/sfx_library.csv`（`build_sfx_library.py` 重建）」。
- **rules**：挂到 `rules/audio-voice.md`，加一节「Ducking 与音效设计」，内容是 API、CLI 和默认值。`rules/keyframes.md` 补一句：音量关键帧用 `duck_bgm`，时间按素材时间写。
- **数据校验**：在 `tools/validate_data_schema.py` 的 `OPTIONAL_SCHEMA` 里加 `"sfx_library.csv": [("id",), ("name",), ("category",), ("available",)]`。更严格的校验用 `build_sfx_library.py --validate`，可以放进 CI。
- **同步**：`jy_wrapper.py sync --only music` 跑完后，可以接着跑 `build_sfx_library.py`，让音效索引跟着更新。
- **requirements**：不用改。soundfile 和 ffprobe 是可选的，只用来读本地音效时长，没有时时长为空。
- **其他任务**：T3 卡点吸附 / T2 节拍检测的输出直接喂给 `--beats`（支持 `downbeats` / `beats` / 时间列表）；T8 审片可以复用 `get_volume_keyframes` 检查 BGM 有没有让位人声。
