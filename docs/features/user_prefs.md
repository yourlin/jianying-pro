# 个人偏好覆盖层（user_prefs）

## 用途

`references/{scenes,styles,formats}/*.json` 是随仓库公开发布的通用规范，个人习惯不能写进去。个人偏好放在仓库外的覆盖文件里，编译器加载完规范以后按组深合并上去，以后新生成的片子默认就用这些设置。

- 模块：`scripts/utils/user_prefs.py`
- 覆盖文件：`~/.config/jianying-pro/style_overrides.json`
- 环境变量 `JY_USER_PREFS`：设成文件路径就改用这个文件；设成 `0` / `off` / `false` / `no` / `none` 就禁用覆盖层
- 测试：`tests/test_user_prefs.py`

覆盖文件不存在、被禁用或内容为空时，规范保持原样，编译结果和没有覆盖层时一样。

## 覆盖文件格式

```jsonc
{
  "global":  {"scene": {...}, "style": {...}, "format": {...}},   // 所有片子
  "formats": {"vertical": {"style": {...}}},                       // 按画幅 id (vertical / horizontal / portrait_3x4 / square)
  "styles":  {"tech": {"style": {...}}},                           // 按风格 id (guofeng / tech / fresh / variety / business)
  "scenes":  {"talking_head": {"scene": {...}}}                    // 按场景 id
}
```

- 每组下面只能有 `scene` / `style` / `format` 三个键，分别合并到对应的规范上，字段名和 `references/` 里的 JSON 完全一样。
- 生效顺序是 `global` → `formats` → `styles` → `scenes`，后面的覆盖前面的（越具体越优先）。
- 合并规则：dict 递归合并，其它值（list、数字、字符串、`null`）直接替换。要关掉某一项就写 `null`，比如 `"grade": null`，和 `guofeng.json` 的写法一样。
- 以 `_` 开头的键在任何层级都当作注释，加载时忽略。建议每一项都写 `_why`，说明证据来源。
- 组的 id 用规范 JSON 里的 `id`，也就是别名解析以后的结果。写 `--style 科技` 时，匹配的是 `styles.tech`。

## API

```python
from utils.user_prefs import apply_user_prefs, load_user_prefs, prefs_path, UserPrefsError

scene, style, fmt, applied = apply_user_prefs(scene, style, fmt, ids=None, prefs=None, path=None)
```

- 输入的三个 dict 不会被修改，返回的是合并后的副本。
- `ids`：`{"scene": ..., "style": ..., "format": ...}`，不传时用各规范自己的 `"id"`。
- `path`：指定覆盖文件。不传时按 `JY_USER_PREFS` 或默认路径查找；传 `""` 等于禁用。`prefs`：直接传覆盖内容（测试用）。
- `applied`：真正改了值的覆盖项，每项一行，例如 `[formats.vertical] style.roles.title.anim_in: 渐显 -> 向上弹入`。原值和新值相同的项不列。
- 覆盖文件不是合法 JSON，或者结构不对时，抛出 `UserPrefsError`（继承自 `ValueError`）。

## 主线怎么接入 script_compiler.py

本次没有改 `script_compiler.py`。接入只需要在 `Compiler.__init__` 里，加载完三个规范、并初始化 `self.notes` 之后调用一次。后面所有读取 `self.scene` / `self.style` / `self.fmt` 的代码（`role()`、`_subtitles()`、`_storyboard()` 里的 grade / bgm 等）都会自动用上合并后的值。

```python
# script_compiler.py 顶部的 import 区
from utils.user_prefs import UserPrefsError, apply_user_prefs

# Compiler.__init__ 里, 紧跟在下面三行之后:
#     self.fmt = load_profile("formats", ...)
#     self.style = load_profile("styles", ...)
#     self.scene = load_profile("scenes", ...)
        try:
            self.scene, self.style, self.fmt, self.user_prefs = apply_user_prefs(
                self.scene, self.style, self.fmt)
        except UserPrefsError as e:
            raise ScriptError(str(e)) from e
# ... 在 self.notes: List[str] = [] 这一行之后:
        self.notes += [f"个人偏好: {a}" for a in self.user_prefs]
```

建议同时在 `_storyboard()` 的 `compiled_from` 里记下用了哪些覆盖项，方便以后排查：

```python
            "compiled_from": {..., "format": self.fmt["id"], "user_prefs": self.user_prefs},
```

如果要加命令行开关 `--no-user-prefs`，在 `_main()` 里解析参数之后设置 `os.environ["JY_USER_PREFS"] = "off"` 就行，不用改函数签名。

`compile_script()` 已经会把 `sb["notes"]` 逐条打印出来，所以编译报告里会直接出现生效的覆盖项。

## 这位用户的覆盖文件：每一项映射到哪个字段

证据来自 `~/.config/jianying-pro/learned/style_profile.md`（7 个手剪竖屏短视频草稿）。用户确认要用的两组如下。

### 字幕和标题（`formats.vertical`，只对竖屏生效）

| 偏好 | 字段 | 编译器 / 装配器里怎么生效 |
|---|---|---|
| 字幕默认用字幕模板 KkQ2-1-10 | `style.roles.subtitle.template = {"kind": "subtitle", "name": "KkQ2-1-10"}` | `Compiler._subtitles()` 用 `self.role("subtitle")` 生成 `subtitles.primary`，角色里的键（`_` 开头的和 `font_zh` 除外）都会带过去；`storyboard_builder._text()` 看到 `template` 就改用 `add_subtitle_template`（`storyboard_schema.md`：“也可以在 primary 里给 template 用字幕模板”） |
| 纯文字字幕不加描边 | `style.roles.subtitle.border = false` | `_text()` 遇到 `border is False` 时传 `border=None`；有模板时走模板分支，这一项不起作用 |
| 标题优先用文字模板 互动引导-划重点 | `style.roles.title.template = {"kind": "text", "name": "7435182917229350182"}` | `Compiler.text()` 把角色参数带进 `shots[].texts[]`，`_text()` 改用 `add_text_template` |
| 模板缩放约 0.67 | `style.roles.title.scale = 0.666` | `_text()` 用 `t["scale"]` 生成 clip 的缩放；角色里有 x / y（来自 slot），所以会传 clip，模板自带的 clip 不再使用 |
| 标题入场动画用 向上弹入 | `style.roles.{title,hero,label,keyword,cta}.anim_in = "向上弹入"` | 纯文字分支用 `add_text_simple(anim_in=...)`，再解析成 `TextIntro.向上弹入` |

### 调色和配乐（`global`）

| 偏好 | 字段 | 编译器 / 装配器里怎么生效 |
|---|---|---|
| 不再默认加滤镜 | `style.grade = null` | `_storyboard()` 里 `if self.style.get("grade")` 不成立，就不写 `main.grade`，滤镜和调节都不加 |
| BGM 开头不淡入 | `style.bgm.fade_in = 0` | `bgm` 取自 `style.bgm`；`_audio()` 传 `fade_in="0.000s"` |
| BGM 结尾淡出 2s | `style.bgm.fade_out = 2.0` | 同上 |
| BGM 音量 0.16 | `scene.audio.bgm_volume = 0.16` | 剧本没写 `bgm.volume` 时，`_storyboard()` 用场景的 `bgm_volume` 覆盖 `style.bgm.volume`。所以必须写在场景上，写在风格上不会生效 |
| （例外）卡点混剪保留 0.9 | `scenes.beat_montage.scene.audio.bgm_volume = 0.9` | 卡点混剪里音乐就是主音轨，0.16 的证据来自有配音的口播短视频，套用不合理 |

### 名称核对

- 字幕模板：`KkQ2-1-10`（7379144030594075923，免费）、`创意粗体`（7369798555680214322，免费）、`KkQ1-4`（7381313557465550119，免费）都在 `data/panel_subtitle_templates.csv` 里，`data/template_presets/` 里也都有预设（`find_preset` 能找到）。默认用最常用的 KkQ2-1-10，另外两个写在 `_alternatives` 里。
- 文字模板：`data/panel_text_templates.csv` 里有 6 个同名的“互动引导-划重点”，全部是 VIP。本地只有 `7435182917229350182` 有预设，是从用户自己的草稿里采集的，只有 1 段文字。所以用 resource_id 指定，避免同名歧义。`find_preset` 和 `find_panel_asset` 都支持用 id 查。
- 动画：`向上弹入` 在 `panel_text_animations.csv`（intro）和 `pyJianYingDraft TextIntro` 里都有，是 VIP。`asset_search.py 向上弹入` 已确认。
- VIP：标题模板和动画都是 VIP，依赖剧本的 `allow_vip`（默认 `true`）。剧本写 `allow_vip: false` 时，`_load_template_preset` 会拒绝使用这个模板。

## 现有规范表达不了的部分和已知局限

1. **出现频率**：用户只有 66% 的字幕、74% 的标题用了模板。规范只有“用或不用”，没有“优先 / 混用比例 / 多个模板轮换”的字段。现在是全部用 KkQ2-1-10。如果要轮换或按比例混用，需要新增字段，例如 `roles.subtitle.template_pool: [...]`，同时在编译器里实现选择逻辑。
2. **剧本里关不掉模板**：剧本的 `subtitles` 配置只认 `mode` / `lang` / `max_chars` / `fixes` / `anchor` / `y`，`title` 元素也没有 `template` 开关。某一条片子想回到纯文字字幕或标题，现在只能用 `JY_USER_PREFS=off` 整体关掉覆盖层，或者另写一个覆盖文件。缺少的是剧本级的 `subtitles.template` 和元素级的 `template: null`。
3. **模板和动画互斥**：`storyboard_builder._text()` 的模板分支会直接 return，不处理 `anim_in` / `anim_out`、`border`、`font`、`size`（模板自带这些）。所以 `title.anim_in` 和 `subtitle.border` 只在去掉模板后才生效；动画真正起作用的是 hero / label / keyword / cta 这几个纯文字角色。
4. **“替代渐显”没法按条件写**：深合并只能整项替换，写不出“原来是渐显才换成向上弹入”。竖屏下所有风格的这 5 个角色都会改成向上弹入，包括 variety 原来的“弹入”。
5. **字幕模板的竖向位置**：`_subtitles()` 的 `anchor_bottom` 按纯文字的字号和行高推算 y，模板的实际字高可能不同，位置可能偏一点。本次没有改字幕位置。用户习惯的 -0.64 超出了安全区，`preferences.md` 也明确说暂不改。
6. **模板缺预设时会中断装配**：`storyboard_builder` 没有捕获 `PanelAssetUnavailable`。在没有这些预设的机器上（例如 CI），模板会失败（macOS 上会先尝试按需预热）。覆盖文件只在用户本机存在，所以不影响仓库测试。
7. **剧本自带 bgm**：剧本写了 `bgm` 时，`_storyboard()` 会整个替换 `style.bgm`，覆盖层里的 `fade_in` / `fade_out` 不再生效（`volume` 仍然来自场景）。要让淡入淡出在这种情况下也生效，需要把 `sc.bgm` 合并到 `style.bgm` 上，而不是直接替换。
8. **vlog 的 BGM 音量**：vlog 场景原来是 0.5，现在全局改成 0.16 后也一起变了，这是按用户要求“全局生效”做的。如果觉得不合适，在 `scenes.vlog` 里加例外就行。
9. **合并结果展示**：`applied` 会把 global 和 scenes 两层都列出来（例如卡点混剪先 0.9 → 0.16，再 0.16 → 0.9），方便看清每个值的来源，但不会合并成净变化。
