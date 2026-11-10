# 录屏与智能缩放 (Recording & Smart Zoom)

如果你需要录制屏幕内容并在剪映中自动应用"智能缩放"（根据点击位置自动放大画面），请遵循以下流程。

## 1. 启动录制器
运行内置的图形化录制工具：
```bash
# 默认保存到当前目录
python <SKILL_ROOT>/tools/recording/recorder.py
```

### 录制器功能：
- **画面录制**: 捕获全屏内容。
- **音频录制**: 支持捕获系统声音。
  - **macOS**: 需安装虚拟音频设备（如 BlackHole 或 Soundflower），并在脚本中配置设备索引。
  - **Windows**: 需在脚本中配置正确的 DirectShow 设备 ID。
- **事件捕获**: 实时记录点击（Click）、按键（Keypress）和光标移动（Move）。
- **小圆点模式**: 录制过程中会变为红色小圆点，点击即可停止。

## 2. 自动生成草稿
录制结束后，录制器会弹出选单，选择 **"✨ 自动生成智能草稿"**。

这会后台调用 `scripts/jy_wrapper.py apply-zoom` 命令：
1. **导入视频**: 将刚录制的 MP4 导入剪映。
2. **分析事件**: 读取配套的 `_events.json`。
3. **应用关键帧**: 在每个点击位置点，自动插入"放大-停留-恢复"的缩放关键帧，实现无需手动剪辑的导览视频效果。

## 3. 手动调用智能缩放
如果你已经有了录屏文件和对应的 JSON 事件文件，也可以手动执行：
```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py apply-zoom --name "我的演示项目" --video "recording.mp4" --json "recording_events.json" --scale 150
```

## 注意事项 (Constraints)
- **环境**: 支持 macOS 和 Windows。
  - macOS 使用 `avfoundation` 采集（FFmpeg 内置），音频录制需额外安装 BlackHole 等虚拟音频驱动。
  - Windows 使用 `gdigrab` 桌面采集 + `dshow` 音频。
- **依赖**: 必须安装 `ffmpeg` 以及 Python 库 `pynput` 和 `tkinter`。
  - macOS: Homebrew 安装路径为 `/opt/homebrew/bin/ffmpeg`（`brew install ffmpeg`）。若脚本找不到 ffmpeg，需确保 `/opt/homebrew/bin` 在 PATH 中。
  - Windows: 需将 ffmpeg.exe 所在目录加入系统 PATH。
- **坐标**: 录制器会自动处理不同分辨率下的坐标归一化，确保缩放位置精准。

## 4. 网页 / App 网页版高清录屏（产品演示推荐）

桌面录制器录的是整个屏幕；做产品演示时，更推荐用无头浏览器按脚本录真实页面，可重复、画面稳定：

```python
from web_recorder import record_web_hidpi, smooth_scroll
r = record_web_hidpi(url, "clips/home.mp4", actions, viewport=(432, 768), dpr=2.5, storage_state="state.json")
```

- 用 DevTools 截屏流按**设备像素**采集（432×768 × 2.5 = 1080×1920 竖屏），按帧时间戳合成 30fps MP4。
  不要用 Playwright 的 `record_video` 配 `deviceScaleFactor`：画面只占左上角，或放大后发糊。
- `actions(page, ready)` 里在画面准备好时调用 `ready()`，返回值里的 `ready` 时刻用作剪辑的 `source_start`，跳过加载白屏。
- 移动端模拟下 `mouse.wheel` 不滚动、鼠标拖动会选中文字：滚动用 `smooth_scroll()`；`TOUCH_JS`（自动注入）关闭文字选中并显示触控圆点。
- 完整流程（每个功能一段、先登录存 `storage_state`、抽帧检查）见 `rules/app-promo.md` 第 4 节。
