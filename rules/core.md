---
name: core
description: Core JyProject operations including saving and exporting.
metadata:
  tags: core, save, export, save_project
---

# Core Operations

All operations are performed through the `JyProject` instance.

## 创建项目 (Project Creation)

在初始化 `JyProject` 时，请务必根据主视频素材的比例设置分辨率。**默认值为横屏 (1920x1080)**。

```python
# 默认：横屏 (16:9)
project = JyProject("Horizontal_Project") 

# 竖屏 (9:16)：必须在初始时指定，否则会有黑边
project = JyProject("Portrait_Project", width=1080, height=1920)
```


## Saving

You **MUST** call `project.save()` at the end of your script. 
This operation not only saves the JSON changes but also triggers a refresh in the Jianying UI (if applicable) or ensures the filesystem is synced.

```python
# Save changes and refresh draft state
project.save()
```

## Template-Based Production (Mass Creation)

For heavy-duty automation scenarios (e.g., creating 100 personalized ads from 1 template), follow the **Clone-First** strategy:

### 1. Secure Cloning
**CRITICAL**: Never modify the shared "Template Draft" directly. Always create a volatile copy.

```python
# Create a new draft copy based on an existing template
project = JyProject.from_template("Master_Template", "Target_Customer_A")
```

### 2. Slot Replacement and Reconnecting Media

In the template, put placeholder files with meaningful names, e.g. `Intro_Slot.mp4`. The skill keeps the original file name as the material name, so the timeline shows `Intro_Slot.mp4` and slots can be found by name. The copy inside the draft gets an md5 name.

```python
project = JyProject.from_template("Master_Template", "Customer_A")      # clone, then open the clone
project.replace_material_by_name("Intro_Slot", "/path/customer_a/intro.mp4")   # case-insensitive, extension optional
project.reconnect_all_assets("/path/to/local_media_root")                 # find missing media by file name
project.save()
```

- `replace_material_by_name` keeps each segment's duration and source range. It warns if the new clip is shorter than the range in use; that part then shows as missing frames in JianYing.
- `reconnect_all_assets` copies found files into the draft (`copy_into_draft=True`), because external paths may be blocked by the macOS sandbox. It returns `{"fixed": [...], "missing": [...]}`.
- Drafts made in JianYing (encrypted) can also be used as templates: `from_template` clones them via JianYing's plaintext `template.json` (see `rules/jianying-11.md` section 6).

## Automated Exporting

You can trigger a headless export (using `uiautomation`) without manual clicking:

```bash
# Using the CLI tool
python <SKILL_ROOT>/scripts/auto_exporter.py "ProjectName" "custom_output.mp4" --res 1080 --fps 60
```

## Constraints

- **Draft Recognition**: The wrapper automatically handles `DraftFolder` structure. Do not manually manipulate `draft_content.json` unless you know exactly what you are doing.
- **Exporting Requirements**: Auto-exporting via `uiautomation` 仅支持 **Windows**，剪映 5.9 或更低版本最稳。macOS 上请生成草稿后在剪映里手动导出。
- **UI Refresh**: After the script runs, if Jianying is open, the user may need to exit and re-enter the draft to see changes.

## Quick Edit Execution Template (Standard)

For generic requests like "来个剪辑", execute in this order:

1. Minimal environment checks (python + drafts root)
2. Resolve required assets (local first, cloud second)
3. Generate one deterministic edit script
4. Run script once and collect output
5. Perform acceptance checks and report concrete results

## Acceptance Checks (Standard)

After execution, verify:

- Draft directory exists
- Save completed (`project.save()` success)
- At least one segment exists on a video track
- BGM (if used) is on audio track
- Narration/subtitle pairing exists when TTS was requested
