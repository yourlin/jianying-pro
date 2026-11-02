# 封面与标题（T15）`scripts/cover_maker.py`

## 用途

从视频里挑候选封面帧，排上大字标题，生成 N 张候选封面和一张对比拼图，再输出一份"写标题的提示"。标题文案由调用方 agent 来写，脚本只负责挑帧和排版。

## 流程

1. **挑帧**：调用 `shot_quality.score_video` 打分（也可以用 `--quality` 复用已有结果）。封面分 = 素材分 × 0.8，再加人脸分：正脸 +10、两只眼睛都可见 +5、脸高占画面 15–55% +3、脸在水平居中位置 +2，满分 100。以下帧会被跳过：转场帧、黑帧、`analysis.json` 镜头切换点前后 `--avoid` 秒内的帧。素材里有人脸时，没有脸的帧扣 10 分。候选帧之间至少相隔 `--gap` 秒；凑不够 N 张时放宽间隔再补。
2. **裁切**：`fill` 模式按画幅铺满裁切，裁切中心跟着人脸。`auto` 模式下，如果上、下两个标题区都会压到人脸，而且素材和画幅的比例相差很多（比如横屏素材做竖屏封面），就改用 `fit` 模式：画面放大到画布宽的 1.4 倍，跟着人脸裁掉左右两边，放到标题区下方；空出的地方用同一帧放大、模糊、压暗后铺底。
3. **标题区**：画布和安全区取自 `references/formats/<id>.json`。竖屏还要收进封面中央的 1080×1320（safe_area.md），左右取对称，避免居中的文字进入右侧按钮区。标题区放在安全区的上带或下带（各占安全区高度的 42%），选和人脸（含头发）重叠少的那一个。
4. **排字**：用 `utils/text_metrics.fit` 按真实字宽排版（layout.md）。没有手动换行时先试排一行，一行字高不低于画布短边的 9%（约 97px，对应 safe_area.md 的 ≥ 80pt）就用一行；否则折成两行，两行尽量等长。标题里写 `\n` 可以手动断行。主标题超过 8 个字时报警告。
5. **样式**（候选之间轮流使用）：`yellow_black` 黄字黑边、`white_black` 白字黑边、`black_band` 半透明黑底白字。背景偏亮时加一条从画面边缘往内渐隐的压暗，并记录字色对背景的对比度。

## CLI

```bash
# 1) 先拿写标题的提示（有 analysis.json 时会带上转写节选）
python scripts/cover_maker.py video.mp4 -o out/covers --prompt-only --analysis analysis.json

# 2) agent 写好标题后出封面
python scripts/cover_maker.py video.mp4 -o out/covers --titles "三个技巧" "别再这样剪" --subtitle "新手必看" \
    -n 6 --format vertical --analysis analysis.json --quality quality.json --json

# 标题文件：每行一个，以「副标题:」开头的行作为副标题
python scripts/cover_maker.py video.mp4 -o out/covers --titles-file titles.txt --styles yellow_black,black_band
```

参数：`--format vertical|horizontal|square|portrait_3x4`（也可以传 json 路径），`--font <剪映字体名>`（取自 data/panel_fonts.csv，默认用系统中文字体），`--mode auto|fill|fit`，`--every`，`--gap`，`--avoid`。

## Python API

```python
from cover_maker import make_covers, pick_candidates, render_cover, title_prompt, load_format
res = make_covers("v.mp4", "out", titles=["三个技巧"], subtitle="新手必看", n=6, fmt="vertical",
                  analysis=analysis_dict, quality=quality_dict, mode="auto")
img, info = render_cover(frame_bgr, load_format("vertical"), "标题", face_box=[x0, y0, x1, y1])
```

## 输出

输出目录里有以下文件：

- `cover_01.jpg` … `cover_NN.jpg`：候选封面，分辨率和画幅一致。
- `contact_sheet.jpg`：对比拼图。缩略图宽 300px，大约是手机上看到的大小，可以顺便检查小尺寸下标题读不读得清。
- `title_prompt.txt`：写标题的提示。要求 5 个候选，按 hook.md 的五类钩子（场景型、问题型、结果先行、反常识、直接利益）各写一个，另加一个副标题。
- `covers.json`：

```json
{"source": "...", "format": "vertical", "canvas": {"width": 1080, "height": 1920},
 "title_zones": {"top": [-0.64, 0.197, 0.64, 0.688], "bottom": [...]},
 "face_detector": "haar",
 "covers": [{"index": 1, "file": ".../cover_01.jpg", "t": 1.3, "cover_score": 91.9, "quality_score": 87.4,
   "face": {...}, "issues": [], "title": "她为什么震惊", "subtitle": "结局没想到", "style": "yellow_black",
   "mode": "fit", "zone": "top", "zone_px": [...],
   "title_layout": {"text": "...", "lines": [...], "size": 16.65, "px": 106.6, "w": ..., "h": ..., "fits": true,
                    "shrunk": 0.82, "ink_px": [...]},
   "subtitle_layout": {...}, "contrast": 7.1, "warnings": []}],
 "contact_sheet": "...", "title_prompt_file": "...", "title_prompt": "...", "warnings": []}
```

`--json` 输出 `{ok, code, reason, data}`，失败时 `code = "cover_maker_failed"`。

## 限制

- 封面是用 Pillow 渲染的静态图，不写进剪映草稿。要把封面放进草稿当首帧或封面，需要主线用 add_image 或 add_text 接上。字号和区域已经记在 `covers.json` 里，可以直接复用。
- 人脸检测依赖 shot_quality（opencv 5 下不可用，见 shot_quality.md）。没有人脸信息时标题默认放在上带，裁切取中心。
- 封面上的花字效果只有描边、底条、压暗三种，没有使用剪映的花字或字体特效；`--font` 只在剪映缓存里有对应字体文件时生效。
- 标题文案不由脚本生成，脚本只出提示。

## 给主线的集成建议

- SKILL.md 路由一行：`封面 / 标题 / 缩略图 → python scripts/cover_maker.py <视频> -o <目录> --prompt-only，写好标题后再加 --titles ... --json`
- rules：在 `rules/cli.md` 加一节；引导模式（guided）的导出步骤之后加"出封面"（先 `--prompt-only`，agent 写标题，再出图，把 contact_sheet 给用户选）。safe_area.md 的"封面"一条可以引用这个脚本。
- Mixin：没有，独立 CLI，不需要注册。
- requirements：不用改（Pillow、opencv-python、numpy 都已有）。
