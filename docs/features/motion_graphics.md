# 动态图形模板库 (T12) — `scripts/motion_graphics.py` + `assets/motion_graphics/`

## 用途

做数据类 / 讲解类短片时常用的动效，不用每次手写 HTML：传 JSON 数据，渲染成带透明背景的视频叠加到时间线上。

| 模板 | 效果 | data 主要字段 |
|---|---|---|
| `bar_chart` | 柱状图增长（竖屏 / 超过 10 项自动横向柱） | `items:[{label,value,color}]`, `title`, `unit`, `max`, `orientation: auto/vertical/horizontal`, `decimals`, `show_values` |
| `line_chart` | 折线图从左到右揭开，点依次弹出，末端数值跟随 | `labels:[...]`, `series:[{name,values,color}]` (1~5 条), `title`, `unit`, `min`, `max`, `decimals` |
| `counter` | 数字滚动（千分位、前后缀、小数、结束时轻弹） | `to` (必填), `from`, `prefix`, `suffix`, `decimals`, `label`, `separator` |
| `callout` | 圈注 / 箭头 / 方框 / 下划线，手绘感描边 + 文字标签 | `marks:[{type: circle/arrow/box/underline, x, y, r, w, h, from:[x,y], text, color, at}]`（坐标为画面比例 0~1） |
| `progress_bar` | 进度条 / 环形进度（可多条） | `value` + `label`，或 `items:[{label,value(0~100)}]`；`style: bar/ring`, `show_percent` |
| `map_route` | 地图路线：虚线底轨 + 实线生长 + 移动光点 + 途经点标注 | `points:[{x,y,label}]`（地图区比例 0~1），可选 `path`（0~1000 坐标 SVG path，沿它画路线）、`outline`（区域轮廓 SVG path）、`smooth`、`marker` |
| `list_reveal` | 列表逐条滑入（编号徽标 / 圆点，可带副标题） | `items:["文字" 或 {text, sub, color}]` (1~12 条), `title`, `numbered`, `highlight_last` |

所有模板可带 `title`。通用选项：画幅 `aspect`（landscape 1920x1080 / portrait 1080x1920 / square 1080x1080）或
`size="宽x高"`；`background`（`transparent` / 颜色 / `auto`）；`duration`（总时长，默认按模板与数据量估算）；
`hold`（动画结束后静止秒数，默认 1）；`theme`（`primary` / `accent` / `text` / `muted` / `track` / `label_bg` /
`font` / `palette`）。纯色背景会自动选深/浅文字色；透明背景给内容加柔和投影，叠在亮画面上也看得清。
竖屏字号放大 1.25 倍（手机观看）。

模板是纯 SVG + 原生 JS，不依赖 CDN，离线可渲染；所有文字走 `textContent`，配置 JSON 内联时转义 `</`。

## CLI

```bash
python scripts/motion_graphics.py list
python scripts/motion_graphics.py html bar_chart --data data.json --out chart.html   # 浏览器打开即预览
python scripts/motion_graphics.py render counter --data '{"to":12800,"suffix":"+","label":"累计用户"}' \
       --out n.mov --aspect portrait                                                  # ProRes 4444 透明
python scripts/motion_graphics.py render map_route --data route.json --out r.mp4 --background "#101820"
python scripts/motion_graphics.py render list_reveal --data '{"items":["a","b"]}' --out l.webm --theme '{"primary":"#FF5A36"}' --json
```

- `--mode frames`（默认）：playwright 打开页面，逐帧调用 `window.__mgSeek(t)` 截图（`omit_background` 保留透明），
  PNG 流经 stdin 交给 ffmpeg：`.mov` = ProRes 4444（透明），`.webm` = VP9 yuva420p（透明），`.mp4` = H.264（不透明）。
  帧时间精确，不受机器卡顿影响。
- `--mode record`：走现有 `web_recorder.record_web_animation`（实时录屏，固定 1920x1080，不透明），
  页面自动播放并按 rules/web-vfx.md 协议在结束时设 `window.animationFinished = true`。
- `--background auto`（默认）：`.mov/.webm` 透明，`.mp4` 黑底（可配合 `set_blend_mode(seg, "滤色")` 叠加）。
- `--json` 输出 `{ok, code, reason, data}`；错误码 `invalid_input`（退出码 2，消息以字段路径开头，如
  `items[2].value: 必须是数字`）、`missing_dependency`（3，缺 playwright / ffmpeg，附安装命令）、`render_failed`（1）。

## Python API

```python
import motion_graphics as mg
html = mg.render_html("bar_chart", data, aspect="portrait", background="transparent")
mg.write_html("counter", {"to": 99}, "x.html")
info = mg.render_video("line_chart", data, "out.mov", fps=30, hold=1.5, theme={"primary": "#FF5A36"})
# info = {"output", "html", "width", "height", "duration", "fps", "transparent", "mode"}
mg.build_config(...)    # 只做校验 + 生成注入页面的 MG_CONFIG
```

Mixin（`scripts/core/motion_graphics_ops.py`，`MotionGraphicsOpsMixin`）：

```python
seg = p.add_motion_graphic("bar_chart", {"items": [...]}, start_time="2s", duration="5s")   # 默认透明 .mov
seg = p.add_motion_graphic("counter", {"to": 10000}, "8s", background="#101820", fmt="mp4")
```

画幅默认跟草稿画布一致。透明 .mov 直接复制进草稿目录并入轨（**绕过 `add_media_safe` 的 H.264 规范化**，
否则透明通道会丢）；不透明输出仍走 `add_media_safe`。

## 新增模板

在 `assets/motion_graphics/<名字>.html` 写一个页面：保留 `window.MG_CONFIG = /*MG_CONFIG*/null;` 和
`/*MG_RUNTIME*/` 两个占位符，调用 `MG.run(function setup(cfg) { ...; return function update(t) {...}; })`，
`update(t)` 必须是 t 的纯函数（逐帧渲染会乱序调用）。运行时 `_runtime.js` 提供 `MG.el / MG.text / MG.pill /
MG.fit / MG.title / MG.prog / MG.ease / MG.fmt / MG.color`、`MG.W/H/U/pad/A/portrait/theme/data`。
然后在 `motion_graphics.py` 的 `TEMPLATES` 里登记说明、校验函数和默认时长。

## 限制

- 渲染需要 `playwright` + `playwright install chromium` 和 ffmpeg；本仓库测试只测 HTML 生成与参数校验，不实际录制。
- 剪映对 ProRes 4444 透明 .mov 的支持取决于版本；若导入后透明区域变黑，改用纯黑底 mp4 + 混合模式「滤色」。
- 帧模式 30fps 下每秒约 30 次截图，1080p 一段 6 秒的动效渲染约 10~30 秒。
- 地图路线是示意图（简化 SVG 坐标），不做真实地理投影；需要真实轮廓时把 SVG path 换算到 0~1000 坐标传 `outline`。
- 文字长度有上限（标签 30 字 / 列表 40 字），超宽时自动缩小字号，不自动换行。

## 给主线的集成建议

- SKILL.md Agent Quick Routing：`数据图表 / 数字滚动 / 箭头圈注 / 进度条 / 地图路线 / 列表动画 → scripts/motion_graphics.py render <模板>（草稿内用 p.add_motion_graphic）`
- rules：挂到 `rules/web-vfx.md`（新增「现成模板」一节：模板表 + 逐帧模式 `__mgSeek` 协议 + 透明 .mov 说明），
  并在其中说明优先用模板、模板不够再手写 HTML。
- 注册 Mixin：`JyProject` 继承列表加 `MotionGraphicsOpsMixin`（`from core.motion_graphics_ops import MotionGraphicsOpsMixin`）。
- 可以考虑让 `add_web_asset_safe` 复用 `motion_graphics._record_frames`，以支持竖屏尺寸与透明背景（目前固定 1920x1080）。
- requirements.txt：无改动（playwright 已在列表；ffmpeg 为系统依赖）。
