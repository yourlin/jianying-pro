# 剪映 11.x 适配：面板素材、VIP、多轨与 AI 字幕

适用：剪映专业版 11.x（已在 macOS 11.6.0 上验证）。skill 生成的仍是 5.9 格式的明文 `draft_info.json`，剪映 11.x 打开后会自动升级并加密保存，不需要额外操作。

## 1. 刷新素材库（必做一次，之后按需）

剪映 11.x 会把素材面板数据缓存在本地 `User Data/Cache/ressdk_db/*/rp.db`。用下面的命令只读提取，生成 `data/panel_*.csv`：

```bash
python <SKILL_ROOT>/scripts/sync_jy_panel.py          # 人类可读
python <SKILL_ROOT>/scripts/sync_jy_panel.py --json   # 机器可读
```

- 剪映里浏览过的面板（转场、特效、动画、字体、朗读音色、花字……）越多，索引就越全。
- 生成的索引覆盖：转场、画面特效、视频入场/出场/组合动画、文字入场/出场/循环动画、字体、TTS 音色、声音效果、花字、文字模板、贴纸、音效、蒙版、混合模式。
- **可以直接用名称调用**：`add_transition_simple("炫光", seg)`、`add_video_animation(seg, "3D关盒", "outro")`、`add_text_simple(..., font="AbrilFatface-Regular")`。名称解析顺序：内置枚举精确匹配 → 面板索引精确匹配 → 同义词/模糊匹配。

### 音乐库 / 音效库（增量同步）

```bash
python <SKILL_ROOT>/scripts/sync_jy_music.py            # 增量同步, 并打印每段浏览对应的歌单
python <SKILL_ROOT>/scripts/sync_jy_music.py --dry-run
python <SKILL_ROOT>/scripts/sync_jy_music.py --assign 17=AI音乐   # 修正某段浏览的歌单名
```

- 在剪映音乐面板里**从上往下**逐个浏览歌单（每个歌单滚到底），浏览过的歌曲就会进入缓存。在音效面板里浏览过的音效同理。
- 歌曲分页不带歌单信息，脚本按"浏览顺序 = 面板顺序"推断歌单名，并打印每段浏览的例子供核对。同一歌单同一页的缓存地址是固定的，所以确认过的歌单名会记在 `data/.music_sync_state.json` 里，以后增量同步时直接沿用。
- 结果合并进 `cloud_music_library.csv`（`categories` = 歌单|曲风）和 `cloud_sound_effects.csv`，已有条目保留不删。
- 同一脚本也会同步「素材库」的视频和图片（片头、片尾、空镜、情绪、科技……），合并进 `cloud_video_assets.csv`，用 `add_cloud_media(id)` 加入草稿。
- VIP（会员）曲目和音效只收录进索引，skill 不下载，需要在剪映里添加；免费的可以用 `add_cloud_music(music_id)` 直接加入草稿。

### 素材封面预览

面板同步时会把每个素材的封面地址（`cover_url` 静态图、`preview_url` 动图）一起存进 `panel_*.csv`，覆盖率 86%–100%。封面就是剪映面板里给所有用户看的缩略图，免费和 VIP 素材都有，只用于挑选。

```bash
python scripts/preview_assets.py "震惊" -c panel_text_templates -l 12      # 文字模板对比图
python scripts/preview_assets.py "口播" -c panel_subtitle --frames 3       # 动图抽 3 帧，看动画过程
python scripts/preview_assets.py --ready                                  # 已可直接使用的模板
python scripts/preview_assets.py --ids 7542199191150431513 7416724149906705683
```

- 输出一张带编号的对比图（VIP 素材的标签显示为黄色）和一张对照表（名称、类型、VIP、是否可直接用、resource_id）。
- 封面缓存在 `~/.cache/jianying-pro/covers/`。只从剪映的图片 CDN 下载（`*.byteimg.com` 等）。CDN 有防爬：下载会带常规 UA，并限速到每秒约 4 个请求；请不要调高并发。
- **自动刷新**：`asset_search.py` 和 `preview_assets.py` 运行前，会检查剪映面板缓存（`rp.db`）在上次同步后有没有更新（用户在剪映里浏览过面板就会更新）。有更新就自动同步一次（约 10–15 秒，只读），没有更新则不做任何事。同步时间记录在 `data/.panel_sync_stamp`。设置 `JY_AUTO_SYNC=0` 或传 `--no-sync` 可关闭。
- 预下载：`preview_assets.py --prefetch panel_subtitle_templates`（只下静态封面，加 `--animated` 连动图一起下，动图体积大很多）。字幕模板的静态封面已经全部缓存。
- 封面链接带签名，有效期约半年。下载过的封面会一直缓存在本地，之后不受过期影响。链接过期、剪映缓存里也没有新链接时，`preview_assets.py` 会按 ID 向剪映的素材接口重新取封面地址。文字模板、特效、贴纸这类可以取到；**字幕模板取不到**（走另一套接口），只能靠在剪映里浏览字幕模板面板来刷新缓存。
- 封面看不出动画细节和时长；版式和配色要以剪映里的实际效果为准。

## 2. VIP 素材

- 内置枚举和面板索引都带 VIP 标记，`asset_search.py` 结果里有 `VIP` 列：
  ```bash
  python <SKILL_ROOT>/scripts/asset_search.py "光效" -c transitions            # 全部
  python <SKILL_ROOT>/scripts/asset_search.py "光效" -c transitions --free-only
  python <SKILL_ROOT>/scripts/asset_search.py "入场" -c video_animations --vip-only
  ```
- 默认允许在草稿里使用 VIP 特效/转场/动画/字体。剪映会在**导出时**校验会员资格。
- `project.save()` 会打印用到的 VIP 素材，返回值里有 `vip_assets` 字段。
- 只想用免费素材（保证没有会员也能导出）时：`JyProject("名称", allow_vip=False)` 或设置环境变量 `JY_ALLOW_VIP=0`。
- **TTS 音色**：skill 不会直接合成剪映 11.x 新增的 VIP 音色。需要 VIP 音色时，请在剪映里对字幕使用「文本朗读」，由剪映校验会员资格。可以直接合成的免费音色：`asset_search.py -c tts --free-only`。

## 3. 多轨增强 API

```python
main = project.add_media_safe("a.mp4", "0s", "6s", "Main")

# 画中画: position 可用预设名或 (x, y)，单位为半个画布宽/高
pip = project.add_overlay("b.mp4", "1s", "4s", position="bottom_right", scale=0.35)
project.add_mask_simple(pip, "圆形", size=0.8, feather=10)      # 内置: 线性/镜面/圆形/矩形/爱心/星形
project.add_mask_simple(pip, "八边形", size=0.7)                 # 11.x 高阶形状: 圆角瓶盖/饼干/六边形/爆炸/花朵/...
project.add_keyframes(pip, "alpha", [("0s", 0.0), ("0.5s", 1.0)])  # x/y/scale/rotation/alpha/亮度/...

# 分屏: 2~4 个素材, layout = horizontal / vertical / grid
project.add_split_screen(["a.mp4", "b.mp4"], "6s", "3s", layout="horizontal")

# 片段级效果
project.add_video_animation(main, "2026开场", "intro")       # intro / outro / group
project.add_segment_effect(main, "90s")                      # 只作用于这个片段
project.add_filter_simple(main, "_160C", intensity=80)
project.add_ken_burns(main, zoom_from=1.0, zoom_to=1.15, pan=(0.1, 0.0))
project.set_background_fill(main, "blur")                    # 只对主视频轨生效
project.add_character_effect(main, "OK墨镜")                 # 人物特效; 头饰/挡脸/情绪类需要画面中有正脸
project.add_sticker("China中国", "0s", "3s", position="top_left", scale=0.8)
```

### 蒙版说明

- 剪映 11.x（草稿版本 125.0.0 起）把蒙版存放在 `materials.common_mask` 中，skill 已按这个格式写入。
- 基础 6 种蒙版：如果本地已下载 11.x 版本（在剪映的「蒙版 → 基础」面板里点过），会优先使用；否则用内置旧版。实测在 11.6 中，旧版「爱心」不生效，其余 5 种正常；新版的爱心、圆形、矩形（含圆角），以及高阶形状均已验证可正确显示。
- 高阶形状（`asset_search.py -c masks`）需要剪映本地已下载过该资源：在剪映的「蒙版 → 高阶」面板里点一次对应形状即可。没下载时 `add_mask_simple` 会提示，并返回 `None`。
- 钢笔、抠像、文字蒙版需要手绘路径或文字内容，暂不支持自动添加。

### 花字

```python
project.add_text_simple("标题", "0s", "3s", flower="复古花字")             # 名称见 asset_search.py -c flower
project.add_narrated_subtitles("文案……", speaker="BV104_streaming", flower="白字黑边裂痕综艺花字")
project.add_flower_effect(existing_text_segment, "蓝色黄色渐变立体描边花字")  # 已上轨的文字也可以加
```

- 已在 11.6 中验证：不需要剪映本地已下载该花字，打开草稿时剪映会按 ID 获取；本地有资源时 skill 会写入真实路径。

### 变速与动效（`core/motion_ops.py`）

```python
p.set_speed(seg, 2.0, change_pitch=True)          # 常规变速 + 声音变调; 同轨道后面的片段跟着移动
p.set_curve_speed(seg, "蒙太奇")                    # 蒙太奇/英雄时刻/子弹时间/跳接/闪进/闪出/自定
p.add_keyframes(seg, "x", [(0, -0.5), ("3s", 0.5)], ease="回弹离开")   # 关键帧曲线 11 种
p.set_keyframe_ease(seg, "scale", "三次方缓动")     # 已有关键帧也可以改曲线
p.add_text_glow(text_seg, "轮廓光", color="#ff0000", strength=70, range=40, angle=0)
```

- 关键帧曲线的入口：在时间线上选中关键帧（小菱形）后调出"关键帧曲线"。草稿里只保存贝塞尔控制点，不保存预设名。
- 曲线变速的片段时长：剪映 11.7 会沿用 skill 算出的平均速度，时长对齐到整帧（skill 也会对齐）。
- 文字发光：剪映 11.7 面板里只提供 2 种（轮廓光、外发光[VIP]），索引里另外两个是旧样式。

### 音频处理（`core/audio_ops.py`）

```python
p.set_volume_db(audio_seg, -6)                      # 面板上的 dB
p.set_loudness_normalize(audio_seg, target_lufs=-23) # 响度统一; 用 ffmpeg 测量后写入
p.set_audio_denoise(audio_seg, strength=85)          # 音频降噪 (实时, 剪映内置模型)
p.set_vocal_beautify(audio_seg, ambient_level=75)    # 人声美化
p.set_vocal_separation(audio_seg, "人声")             # 人声分离: "人声" / "背景声"
p.set_speed(audio_seg, 1.5, change_pitch=True)       # 音频变速 + 声音变调
```

视频片段自带的声音也可以用这些接口（传视频片段即可）。

### 贴纸动画

```python
p.add_sticker("play正在播放界面", "0s", "3s", anim_in="向右滑动", anim_loop="轻微跳动", anim_out="向右滑动")
p.add_sticker_animation(sticker_seg, "轻微跳动", "loop")    # kind: in / out / loop
```

贴纸动画的索引是 `panel_sticker_animations.csv`，共 33 种。它和文字循环动画有不少同名项，但并不通用。

### 文字气泡

```python
project.add_text_simple("今天也要元气满满", "0s", "3s", bubble="会话01")
project.add_text_bubble(existing_text_segment, "标题59")   # 已上轨的文字也可以加; 可与花字同时用
```

- 气泡索引是 `panel_text_bubbles.csv`，共 335 种（新面板的 137 个也指向同一批 ID）。挑选前先看封面：`preview_assets.py "会话" -c panel_text_bubbles`。
- 剪映面板上的气泡不显示名称，可以用封面对比图来对照。本地没下载的气泡，剪映打开草稿时会按 ID 获取。

### 字幕模板 / 文字模板

```python
project.add_subtitle_template("今天天气真好", "向下弹出-方块粗体黄", "0s", "2s")
project.add_text_template("我的天呐", "我的天呐", "1s", "3s", track_name="Title")
# 批量字幕直接带上模板
project.add_auto_subtitles("talk.mp4", subtitle_template="口播-小马奔腾童趣字幕")
project.add_narrated_subtitles("文案……", subtitle_template="向下弹出-方块粗体黄")
```

- 模板由剪映用模板包里的脚本渲染，草稿字段很多，而且每个模板都不一样。所以 skill 不手工拼装，而是从剪映保存的明文 `template.json` 里**采集**你在剪映中套用过的模板（存到 `data/template_presets/`），生成草稿时再克隆，替换文字、逐字时间和时长。
- 新模板：在剪映的任意草稿里给文字套用一次（等剪映自动保存），然后运行 `python scripts/sync_jy_templates.py`，或用 `python scripts/jy_wrapper.py sync --only templates`；`jy_wrapper.py sync` 会一次同步全部（面板索引、音乐音效、模板预设）。首次使用时也会自动采集；`--list` 可查看已有预设。
- 字幕位置默认沿用剪映里套用时的位置，也可以传 `clip_settings`。套用模板时会忽略 `style`、`font`、`flower`、`anim_*` 等样式参数。
- 多段样式（比如关键词高亮）只保留第一段样式，并铺满全文。

### 画面设置（`core/picture_ops.py`）

滑块参数统一用**剪映面板上显示的数值**（0–100，角度用度数），内部会换算成剪映的存储值。

```python
p.set_blend_mode(overlay_seg, "滤色", opacity=0.8)              # 10 种混合模式; "正常" 表示去掉
p.add_mask_simple(seg, "圆形"); p.add_mask_stroke(seg, "发光", color="#fff496", size=30, alpha=60)
p.add_mask_shadow(seg, color="#000000", alpha=50, blur=30, distance=20, angle=45)
p.add_video_stroke(seg, "单层描边", color="#ff0000", size=50)   # 画面描边: 单层/发光/手绘/虚线描边
p.add_video_shadow(seg, alpha=60, blur=30, distance=20, angle=45)
p.set_corner_pin(seg, upper_left=(-0.5, -1), upper_right=(0.5, -1), lower_left=(-1, 1), lower_right=(1, 1))
p.set_chroma_key(seg, color="#00ff00", intensity=40, shadow=20)
```

```python
p.set_stabilize(seg)                      # 视频防抖「推荐」, 剪映打开草稿时自动处理
p.set_motion_blur(seg)                    # 运动模糊, 同上
p.set_beauty(seg, 磨皮=50, 美白=30, 瘦脸=30, 大眼=20, 瘦腰=40, 杏仁烤奶=100)  # 美颜/美型/美妆/美体 216 项 (panel_beauty.csv), 需要人脸/人体
p.set_manual_slim(seg, strength=50, region=(0.33, 0.1, 0.33, 0.57))   # 手动美体·瘦身瘦腿
```

AI 画质与智能调色（`core/quality_ops.py`，多为 VIP；只写开关，剪映打开草稿时会自动处理，状态栏显示"N 个任务处理中"）：

```python
p.set_one_click_quality(seg)              # 一键画质提升; set_ultra_hd(seg) 为超清画质
p.set_denoise(seg); p.set_deflicker(seg)  # 视频降噪 / 去频闪, 可与补帧、补分辨率、运动模糊叠加
p.set_frame_interpolation(seg, fps=50); p.set_super_resolution(seg, mode=1)
p.set_smart_relight(seg, "青橙逆光")       # 智能打光: 18 个预设 (基础面光/氛围彩光/创意光效), 需在剪映里点过一次以下载
p.set_color_correct(seg, strength=80); p.set_color_clone(seg, "汐蓝", strength=60)   # 汐蓝/透亮/食色/秋枫
```

- 美颜美体：剪映打开草稿后在后台做人脸检测，检测完成前会短暂提示"当前画面暂未识别到人脸"，完成后自动生效，属性面板的数值也正确。同名项（如「磨皮」「美白」在美颜和美体里都有）优先取美颜里的；美妆的数值是浓度，面板默认 80。
- 智能抠像（VIP）：`set_smart_matting(seg)` 会写入开关，但剪映不会自动开始计算。需要在剪映里对该片段"取消勾选 → 重新勾选"一次，`save()` 时会列出这些片段的位置。
- 蒙版描边、蒙版阴影、变形写在视频素材上，所以 skill 会给该片段单独复制一份素材，不影响用同一文件的其他片段。
- 混合模式的资源取自剪映安装包里的 `MixMode/MixMode.json`；色度抠图用安装包里的 `Chroma2`。
- 字段对照样本：`references/jy11_samples/`。

### 调整（调色）设置（`core/adjust_ops.py`）

```python
p.set_adjust(seg, 色温=-14, 色调=15, 饱和度=-7, 亮度=9, 高光=-19, 锐化=34, 颗粒=46, 暗角=21)  # 面板名称或英文 type
p.set_hsl(seg, "蓝", saturation=-100)                 # 红/橙/黄/绿/青/蓝/紫/洋红, -100~100
p.set_curves(seg, luma=[(0.5, 0.7)])                  # 锚点 0~1; 只传 luma 时 RGB 联动
p.set_color_wheels(seg, lift=(0, 0, 0.15), gain=(0.1, 0.05, 0))
p.set_smart_color(seg, strength=80)                   # VIP; 剪映按开关自动计算
p.set_lut(seg, "~/looks/film.cube", strength=70, skin_protect=20)
p.add_local_adjust(seg, "圆形", mask_kwargs={"size": 0.5, "feather": 30}, 亮度=30)  # 只调圆内, 保留整个画面
```

- 基础调节有 16 项：色温、色调、饱和度、自然饱和度、亮度、对比度、高光、阴影、白色、黑色、光感、锐化、清晰、颗粒、褪色、暗角。双向滑块取值 -50~50；锐化、清晰、颗粒、褪色是单向滑块，取值 0~100。
- 都依赖剪映的调整资源包（在剪映里打开过一次调整面板就会下载）。本地没有时，路径留空。
- `set_lut` 的导入方式和剪映"导入 LUT"一样：.cube 文件复制到剪映的 LUT 库 `User Data/Resources/Lut/<名称>/`，同时放入安装包里的特效配置，并用剪映的示例照片生成缩略图，然后登记到 `LutImportCfg.json`（第一次修改前会备份为 `.skill.bak`）。之后这个 LUT 也会出现在剪映的 LUT 列表里。
- `set_adjust` 和 `add_keyframes` 的亮度、对比度、饱和度关键帧是两套机制，同一个片段请只用一种。

## 4. AI 智能字幕（本地运行）

```python
# 需要先安装: pip install faster-whisper "av<16"  （首次运行会下载约 460MB 的 small 模型）
# 注意: PyAV 16+ 与 faster-whisper 1.2.1 不兼容 (open() 不再接受 metadata_errors)
project.add_auto_subtitles("talk.mp4", start_time="0s", model="small", language="zh", max_chars=18,
                           prompt="剪映 自动化 智能字幕")   # 可选: 专有名词, 显著改善人名/产品名识别
```

识别结果会写进 `Subtitles` 轨，同时在草稿目录生成 `auto_subtitles.srt`。已在剪映 11.6 中验证：字幕文字正确，时间与配音对齐。

## 5. 剪映升级后的兼容性检查

```bash
python <SKILL_ROOT>/tools/audit_jianying_compat.py          # 生成样例草稿并审计
python <SKILL_ROOT>/tools/audit_jianying_compat.py --json
```

- **迁移检查**：列出剪映内置的、版本号高于 skill 草稿版本（110.0.0）的迁移步骤。标 ★ 的步骤是新的，且涉及 skill 用到的素材类型，需要人工判断。已审阅过的步骤记录在脚本的 `REVIEWED_MIGRATIONS` 中，显示为 ✓（截至剪映 11.6.0 全部已审阅）。适配完新步骤后，把它加进这个表。实测剪映**不会**对 skill 生成的草稿执行这些迁移（masks → common_mask 就没有被转换），所以每一项相关的格式变化都必须由 skill 直接按新格式写入。
- **字段检查**：把样例草稿中各类素材的字段与剪映数据模型比对，列出缺少和多出的字段。目前能从剪映代码中识别约 35 种模型；videos、audios、texts、滤镜/特效等没有识别到模型，只能靠在剪映里实际查看来验证。
- 数据来源是剪映安装包内随软件发布的明文 JS（`canvas_agent/app/static/js`），不涉及加密草稿。

### 属性面板的选中状态

- 剪映的属性面板靠素材的**本地资源路径**来显示"选中了哪个动画/转场/滤镜/特效"。skill 保存时会自动给剪映已下载过的素材补上路径。
- 本机没下载过的素材效果照常，只是面板不显示选中；在剪映里用过一次后，下次保存就会补上。

## 6. 剪映加密过的草稿（明文副本）

剪映 11.x 打开过的草稿会被加密，skill 读不了。skill 每次保存时会在草稿目录里额外写一份明文副本 `draft_info.skill.json`，下面这些操作会自动使用它：

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py clone --template 原草稿 --name 新草稿   # 另存，不修改原草稿
python <SKILL_ROOT>/scripts/jy_wrapper.py export-srt --name 草稿名
python <SKILL_ROOT>/scripts/draft_inspector.py summary --name 草稿名
```

- 副本只包含 skill 最后一次保存的内容；之后在剪映里的手动修改不在副本中。
- 在原草稿上继续编辑：`JyProject("草稿名", overwrite=False, continue_from_skill_copy=True)`。剪映里的手动修改会丢失，剪映的版本会先备份到草稿目录下的 `.jy_app_backup_<时间>/`。不传这个参数时会报错，不会改动草稿。
- 剪映自己在草稿目录里也会写一份明文 `template.json`（根目录或 `Timelines/<id>/`），**关闭草稿、回到首页时更新**，里面包含剪映里的手动修改。剪映原生创建的草稿也有。查看（`draft_inspector.py`）、克隆（`clone`）、导出字幕（`export-srt`）会在 skill 副本和这份文件中取**较新的一份**；输出里的 `source: jianying_plain` 就表示用的是剪映这份。
- 克隆剪映原生草稿时，相对路径的媒体（如 `materials/video/x.mp4`，实际放在草稿的 `textReading/` 或剪映缓存 `Cache/onlineMaterial`、`Cache/music` 里）会被复制进新草稿，并改成绝对路径。本机找不到的文件会列出来提示。
- 草稿还开着时，`template.json` 可能不是最新的；要先在剪映里回到首页。

## 7. 已知限制

- 剪映 11.x 原生创建的草稿是加密的，skill **不能直接修改**。只读操作（查看、克隆、导出字幕）可以通过剪映写的明文 `template.json` 进行，见第 6 节；要在原生草稿基础上继续编辑，就先 `clone` 成新草稿。
- **做不了（依赖剪映云端或交互式 AI 处理，草稿里表达不了）**：AI消除、智能运镜、AI扩展、镜头追踪、智能裁剪、眼神修正、AI对口型、自定义抠像（画笔）、复合片段、数字人、AI 写真/AI 绘画/AI 风格、AI 配乐。请在剪映里操作。
- **只写开关、由剪映打开草稿时自动处理（已验证）**：人声美化、人声分离、美颜/美型/美妆/美体（后台人脸检测）、视频防抖、运动模糊、一键画质提升、超清画质、视频降噪、AI补帧、补分辨率、视频去频闪、智能打光、智能调色、色彩校正、色彩克隆。
- **需要在剪映里手动触发**：智能抠像（`set_smart_matting` 写好开关后，要在剪映里"取消勾选 → 重新勾选"一次）。
- **电脑版面板里没有的**：文字填充纹理（`panel_text_textures.csv`）、背景填充的画布样式（`panel_canvas_styles.csv`），只建了索引。
- **会员**：上面的 AI 画质类功能多为 VIP。草稿可以照常生成，导出时剪映会检查会员；skill 不在剪映外下载或合成任何 VIP 内容。
- 面板素材的 `effect_id` 取的是 `resource_id`（11.x 缓存里没有旧版的短 ID）。已在 macOS 剪映 11.6.0 上验证：面板转场、特效、视频/文字动画、字体，以及画中画、蒙版、关键帧、背景模糊、推拉摇移都能正确显示和播放。如果个别素材显示不出来，换一个同名的内置枚举素材即可。

## 8. 批量预热字幕模板

```bash
python scripts/jy_wrapper.py warmup --limit 50 [--free-only | --vip-only] [--category 口播] [--batches 0]
# 在剪映里逐个打开预热草稿, 每个等 1 分钟左右 (剪映会下载模板包), 回到首页
python scripts/sync_jy_templates.py --from-packages                               # 由模板包生成预设
```

- 预热草稿只是为了让剪映**下载模板包**，不用点"重试"，也不用手动套用。
- `--batches N` 一次生成 N 个草稿（`<name>_1`、`<name>_2`…），`0` 表示覆盖全部剩余模板。每批 50 个、等 1 分钟左右，大部分模板包都能下完；没下完的会被下一批或补漏草稿（再运行一次 `warmup`）选中。2026-10-22 已为全部 1,725 个字幕模板生成预设，之后只有面板新增的模板需要预热。
- 预热草稿生成预设后就可以删掉，预设不依赖它们。
- 预设由 skill 根据模板包（`content.json`、`extra.json`）生成。拿 16 个剪映真实套用的预设对比过：依赖资源、字号、颜色、字体、花字、动画、行距、字距都一致。渐变和纹理填充来自花字，剪映渲染时会按花字处理。
- 剪映真实套用过的预设优先，生成的预设不会覆盖它们。生成的预设，`source` 字段记为 `package`。
- **文字模板**（`--kind text`，2026-10-22）：
  - 剪映打开预热草稿时会下载较新的文字模板包。较老的模板（大致 2023 年以前）多数已经下载不到，会显示「文本下载失败」。可以用 `--min-id` 跳过老模板。
  - 生成器按模板包写入：多段文字、贴纸元素（位置、大小、时间、入场 / 循环动画；剪映会按 `layout_params` 贴着文字重新排版）、动画时长（循环动画取单次循环时长）、默认文字（`extra.json` 的 `texts`，或富文本里方括号中的原文字）。和 19 个剪映真实预设对比过，结构一致。在剪映里截图核对了 17 个，和面板封面一致。
  - **贴纸上挂着下载不到的动画时，剪映整个不画这个贴纸**（「闪动机械蝴蝶」实测）。所以预热后本机仍没有的贴纸动画会去掉。文字动画缺失时剪映会自己补下载，保留。
  - 文字背景：`text_params.canvas*` 对应 `background_*`（颜色、透明度、圆角、宽高、偏移，纹理图用模板包 `image/` 下的同名文件），并且**要打开文字 `check_flag` 的第 16 位**（剪映真实预设里带背景的都是 63，不带的是 47），否则不显示。字幕模板同样适用。
  - 含形状元素（`shape`）的文字模板暂不支持，没有真实预设可以对照。
  - 多段文字：`add_text_template(["2025", None, ...], 模板)` 按顺序填写，`None` 保留原文字。用 `p.template_texts(模板)` 查看各段的默认文字。
- **全自动预热**（macOS）：`warmup --auto [--wait 60]` 生成预热草稿后，用 `jy_preview.py` 的方式逐个打开，等待后回首页，把用完的预热草稿移到废纸篓，最后运行 `--from-packages`。需要屏幕录制 + 辅助功能权限，运行期间不要操作电脑。
  - 同一秒生成的草稿在首页的顺序不固定，所以先打开当前看得到的，没打开的下一轮重试（最多 3 轮）。
  - 回首页后要等几秒再移走草稿，否则剪映还在保存，会在原处重建一个打不开的空目录（只有 `common_attachment`、`deepagent` 等，没有 `draft_info.json`）。
  - 实测每批 50 个约 75 秒，1 小时约 44 批。2026-10-22 预热了最新的 2,200 个 VIP 文字模板，1,944 个可用。
- **采集时跳过 skill 自己写入的模板**：剪映原样保存的，模板素材 ID 不变。剪映重新套用（比如点了重试）时会换成新 ID，仍然会被采集。

### 历史记录：用剪映补全的尝试（2026-10-20/21）

目标：不用在剪映里逐个手动套用，就能把面板里的模板（免费：字幕 338 个 + 文字 605 个；VIP 更多）批量变成可用的预设。

**已验证的结论**（2026-10-20，剪映 11.7.0-beta2；实验草稿 `JY116_TemplateWarmup_Test`、`JY116_TemplateWarmup_VIP20`，生成脚本不在 skill 内）：
- skill 生成的草稿里，模板素材可以只写 ID（借用同类预设的骨架，去掉样式）。剪映打开时会按 ID 渲染模板。
- 明文 `Timelines/<id>/template.json` 只在**关闭草稿、回到首页**时才会写出。
- 打开时**直接下载成功**的模板（目前看免费字幕模板都是这样）：剪映会按模板渲染，但保存时沿用 skill 的骨架，所以**不能当预设用**。采集时已自动跳过（判断依据：`resources` 为空）。
- 显示**"文本下载失败，点击重试"**的模板（文字模板、VIP 字幕模板都会这样）：点重试后，剪映会完整重新套用模板，得到真实的字体、花字、动画和依赖资源，可以直接采集。重试后剪映会清空 `name` 和 `effect_id`，采集和克隆时已补回。
- 20 个 VIP 模板（字幕 10 + 文字 10）打开后**全部**显示需要重试，点重试后 17 个加载成功。最后采集成功 16 个：VIP 字幕「IP奶茶鼠-节日彩色可爱字幕」实际是直接下载成功的，保存的还是骨架。文字模板「02」「025全系列」「2024元旦-万事顺意」重试后还是失败，原因未知，可能是下架或者旧版本模板。
- 剪映**不能批量重试**：未加载的片段没法右键，也没有"全部重试"，只能逐个点。
- 面板里模板名称**不唯一**（比如有两个「我的天呐」），批量处理要用 `resource_id`。

**待解决**：
1. 让免费字幕模板也稳定进入"失败 → 重试"这条路（找出触发失败的骨架写法），或者换别的方法拿到真实结构。VIP 字幕模板也可能直接下载成功，不经过重试（比如「IP奶茶鼠-节日彩色可爱字幕」）。这种情况同样拿不到真实结构，已被采集跳过。
2. 批量点击的方案二选一：
   - 自动点击：截图识别红色的"重试"片段，模拟鼠标点击，并滚动时间线。需要"屏幕录制"和"辅助功能"权限，依赖剪映的界面布局。
   - 按需预热：用户指定模板或分类，skill 生成预热草稿，用户手动逐个点重试。
3. 预热草稿生成器正式放进 skill（比如 `jy_wrapper.py warmup --kind --category --limit`）。按 `resource_id` 选模板，跳过已有预设和已知会失败的模板。
4. 记录重试也失败的模板，以后不再选它们。
5. 下载与版权：预热时由剪映自己下载、自己校验会员，skill 不在剪映外下载模板包（VIP 和免费都一样）。


## 9. 在剪映里截图检查（macOS）

```bash
python scripts/jy_preview.py <草稿名> --at 1 --at 3.5 [-o 输出目录] [--full]   # 打开草稿 → 定位 → 截播放器 → 回首页
python scripts/jy_preview.py --snapshot                                         # 只截当前剪映窗口
```

或者在代码里 `p.save()` 之后调用 `p.preview(at=[1, 3.5])`，返回 PNG 路径。然后用 Read 查看图片。

- **权限**：屏幕录制（截图）+ 辅助功能（点击、按键），勾选运行脚本的终端/应用。截图只截剪映窗口（`screencapture -l <窗口号>`），不截其他屏幕内容。
- **前提**：剪映已打开，并停在**首页**。剪映停在编辑页时会报错，因为剪映关闭草稿时会保存它内存里的版本，可能覆盖 skill 刚保存的修改。确认无妨时加 `--close-editor`。
- **做法**（剪映 11.7 实测）：
  - 首页草稿标题可以通过辅助功能读到（`HomePageDraftTitle:<名称>`），点击标题左边的封面就会打开草稿。要用 CGEvent 模拟鼠标，System Events 的点击剪映不响应。
  - 首页列表不会自动发现新草稿：切到「模板」再切回「首页」就会重新扫描。从编辑页回首页时也会。
  - 编辑页：Home 键回到开头，Shift+→ 前进 10 帧，→ 前进 1 帧。按键直接发给剪映进程。
  - 播放器位置由时间码（`currentProgress`）找到，再在面板里找画布，裁掉界面。
  - 回首页用菜单「剪映专业版 → 返回首页」（剪映前面常浮着一个小窗口，Cmd+W 可能被它接走），剪映会自动保存。
  - 跳转后读时间码（`currentProgress` 的辅助功能描述 `currentProgress|HH:MM:SS:FF`）校验。剪映忙时会丢按键，不对就补按差值。
  - 草稿不在首页第一屏时，会在列表上滚动查找。
- **限制**：操作时剪映会切到前台、鼠标会移动，不要同时操作电脑。首页草稿特别多时，往下滚动可能找不到，可以先清理不用的草稿。窗口最小化时截不到。一次预览约 30 秒（打开草稿约 10 秒）。
- 截图看的是剪映实际渲染的效果（模板、花字、调色、AI 处理），可以补充 `layout_preview.py` 的结构检查，但不能替代它。
