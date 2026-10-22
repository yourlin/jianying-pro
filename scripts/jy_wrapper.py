"""
JianYing Pro Skill - High Level Wrapper (Mixin Based)
旨在解决路径依赖、API 复杂度及严格校验问题。
"""

import os
import sys
import uuid
from typing import Union, Optional

# 环境初始化
from utils.env_setup import setup_env
setup_env()

# 导入工具函数
from utils.constants import SYNONYMS
from utils.formatters import (
    resolve_enum_with_synonyms, format_srt_time, safe_tim, 
    get_duration_ffprobe_cached, get_default_drafts_root, get_all_drafts
)

# 导入基类与 Mixins
from core.project_base import JyProjectBase
from core.media_ops import MediaOpsMixin
from core.text_ops import TextOpsMixin
from core.vfx_ops import VfxOpsMixin
from core.mocking_ops import MockingOpsMixin
from core.track_ops import TrackOpsMixin
from core.ai_ops import AiOpsMixin
from core.template_ops import TemplateOpsMixin
from core.picture_ops import PictureOpsMixin
from core.adjust_ops import AdjustOpsMixin
from core.quality_ops import QualityOpsMixin
from core.production_ops import ProductionOpsMixin
from core.motion_ops import MotionOpsMixin
from core.audio_ops import AudioOpsMixin
from utils.draft_store import write_skill_copy
from utils.panel_assets import (
    collect_vip_usage, fill_local_resource_paths, is_vip_member, member_display_name,
    resolve_panel_member,
)

try:
    import pyJianYingDraft as draft
    from pyJianYingDraft import VideoSceneEffectType, TransitionType
except ImportError:
    draft = None

class JyProject(
    JyProjectBase, MediaOpsMixin, TextOpsMixin, VfxOpsMixin, TrackOpsMixin, AiOpsMixin, TemplateOpsMixin,
    PictureOpsMixin, AdjustOpsMixin, QualityOpsMixin, ProductionOpsMixin, MotionOpsMixin, AudioOpsMixin,
    MockingOpsMixin,
):
    """
    高层封装工程类。通过多重继承 Mixins 实现功能解耦。
    """
    @classmethod
    def from_template(cls, template: str, new_name: str, drafts_root: str = None, allow_replace: bool = False,
                      **kwargs) -> "JyProject":
        """基于模板草稿另存一份新草稿 (不修改模板) 并打开它, 之后可替换素材槽位、继续编辑。"""
        clone_draft(template, new_name, drafts_root, allow_replace)
        return cls(new_name, drafts_root=drafts_root, overwrite=False, **kwargs)

    def _resolve_enum(self, enum_cls, name: str):
        """解析顺序: 内置枚举精确匹配 -> 剪映 11.x 面板索引精确匹配 -> 同义词/模糊匹配。"""
        if not name:
            return None
        member = None
        name_lower = str(name).lower()
        exact = {k.lower(): k for k in enum_cls.__members__.keys()}
        if name_lower in exact:
            member = enum_cls.__members__[exact[name_lower]]
        if member is None:
            member = resolve_panel_member(enum_cls, str(name), draft)
            if member is not None:
                print(f"ℹ️ Panel Match (11.x): '{name}' -> {member_display_name(member)}")
        if member is None:
            member = resolve_enum_with_synonyms(enum_cls, name, SYNONYMS)
        if member is not None and is_vip_member(member) and not getattr(self, "allow_vip", True):
            print(f"⛔ '{member_display_name(member)}' 为 VIP 素材，已按 allow_vip=False 跳过")
            return None
        return member

    def _fill_local_resource_paths(self):
        """补本地资源路径, 让剪映属性面板正确显示选中的动画/转场/滤镜/特效。"""
        import json

        path = os.path.join(self.root, self.name, "draft_info.json")
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            content = json.load(f)
        if fill_local_resource_paths(content):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(content, f, ensure_ascii=False)

    def get_vip_usage(self):
        """返回当前草稿中用到的 VIP 素材列表 [{name, resource_id, type}]。"""
        return collect_vip_usage(self.script.dumps() if hasattr(self.script, "dumps") else "{}")

    def add_clip(self, media_path: str, source_start: Union[str, int], duration: Union[str, int], 
                 target_start: Union[str, int] = None, track_name: str = "VideoTrack", **kwargs):
        """高层剪辑接口：从媒体指定位置裁剪指定长度，并放入轨道。"""
        if target_start is None:
            target_start = self.get_track_duration(track_name)
        return self.add_media_safe(media_path, target_start, duration, track_name, source_start=source_start, **kwargs)

    def save(self):
        """保存并执行质检报告。"""
        self.script.save()
        self._fill_local_resource_paths()
        self._patch_cloud_material_ids()
        self._force_activate_adjustments()
        # 明文副本: 剪映打开后会加密草稿, 之后的继续编辑/克隆/导出字幕依赖此副本
        write_skill_copy(os.path.join(self.root, self.name))
        
        draft_path = os.path.join(self.root, self.name)
        if os.path.exists(draft_path):
            os.utime(draft_path, None)
        self._report_pending_matting()
        vip_usage = self.get_vip_usage()
        if vip_usage:
            names = "、".join(sorted({v["name"] for v in vip_usage})[:12])
            print(f"💎 草稿使用了 {len(vip_usage)} 个 VIP 素材（{names}）。导出时需剪映会员。")
        print(f"✅ Project '{self.name}' saved and patched.")
        return {"status": "SUCCESS", "draft_path": draft_path, "vip_assets": vip_usage}

    def preview(self, at=None, out_dir: str = None, full: bool = False, keep_open: bool = False):
        """
        在剪映里打开本草稿, 截取各时间点 (秒) 的播放器画面, 返回 PNG 路径 (macOS; 需要屏幕录制 + 辅助功能权限,
        剪映要停在首页)。先 save() 再调用。见 scripts/jy_preview.py。
        """
        from jy_preview import preview

        return preview(self.name, list(at) if at else [0], out_dir, full, keep_open)

# 导出工具函数以便向下兼容
__all__ = ["JyProject", "get_default_drafts_root", "get_all_drafts", "safe_tim", "format_srt_time"]

def clone_draft(template: str, new_name: str, drafts_root: str = None, allow_replace: bool = False) -> str:
    """
    基于已有草稿另存一个新草稿 (不修改原草稿)。
    原草稿已被剪映加密时, 使用 skill 明文副本或剪映写的明文 template.json 中较新的一份
    (后者包含剪映里的手动修改, 剪映原生草稿也有; 在剪映里关闭草稿时更新)。
    """
    import json
    import shutil

    from utils.draft_store import read_draft_content

    root = os.path.abspath(drafts_root or get_default_drafts_root())
    src = os.path.join(root, template)
    if not os.path.isdir(src):
        raise FileNotFoundError(f"模板草稿不存在: {template}")
    content, source = read_draft_content(src)

    project = JyProject(new_name, drafts_root=root, overwrite=allow_replace)
    dst = project.draft_dir
    # 复制 skill 导入的素材和剪映的草稿内资源, 并把路径指向新草稿
    for sub in ("materials", "temp_assets", "Resources"):
        if os.path.isdir(os.path.join(src, sub)):
            shutil.copytree(os.path.join(src, sub), os.path.join(dst, sub), dirs_exist_ok=True)
    text = json.dumps(content, ensure_ascii=False).replace(
        json.dumps(src, ensure_ascii=False)[1:-1], json.dumps(dst, ensure_ascii=False)[1:-1]
    )
    content = json.loads(text)
    if source == "jianying_plain":
        missing = _localize_app_media(content, src, dst)
        if missing:
            print(f"⚠️ {len(missing)} 个素材在本机找不到, 新草稿里会显示素材丢失: {', '.join(missing[:5])}")
    content["id"] = str(uuid.uuid4()).upper()
    with open(os.path.join(dst, "draft_info.json"), "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False)
    from utils.draft_store import write_skill_copy

    write_skill_copy(dst)
    print(f"✅ Cloned '{template}' -> '{new_name}' (source: {source})")
    return dst


def _localize_app_media(content: dict, src: str, dst: str) -> list:
    """
    剪映原生草稿的媒体路径是相对的 (如 materials/video/x.mp4), 实际文件在草稿子目录 (textReading/ 等)
    或剪映缓存 (Cache/onlineMaterial、Cache/music)。复制进新草稿的 materials/<类型>/ 并改为绝对路径。
    返回找不到的文件名。
    """
    import glob
    import shutil

    from utils.panel_assets import get_jianying_user_data_dirs

    def locate(name: str):
        for pattern in [os.path.join(glob.escape(src), "**", glob.escape(name))] + [
                os.path.join(glob.escape(ud), "Cache", "*", glob.escape(name)) for ud in get_jianying_user_data_dirs()]:
            hits = [h for h in glob.glob(pattern, recursive=True) if os.path.isfile(h)]
            if hits:
                return hits[0]
        return None

    missing, done = [], {}
    for kind in ("videos", "audios"):
        for mat in content.get("materials", {}).get(kind, []):
            rel = mat.get("path") or ""
            if not rel or os.path.isabs(rel):
                continue
            if rel not in done:
                found = locate(os.path.basename(rel))
                if found:
                    target = os.path.join(dst, "materials", kind[:-1], os.path.basename(rel))
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    if not os.path.exists(target):
                        shutil.copy2(found, target)
                    done[rel] = target
                else:
                    done[rel] = None
                    missing.append(os.path.basename(rel))
            if done[rel]:
                mat["path"] = done[rel]
    return missing


def export_srt(name: str, output: str = None, drafts_root: str = None) -> str:
    """把草稿文字轨导出为 SRT (草稿已加密时读取 skill 明文副本或剪映的明文 template.json)。"""
    import json

    from utils.draft_store import read_draft_content

    root = os.path.abspath(drafts_root or get_default_drafts_root())
    draft_dir = os.path.join(root, name)
    content, _source = read_draft_content(draft_dir)
    mats = content.get("materials", {})
    texts = {t["id"]: t for t in mats.get("texts", [])}
    # 套用了字幕/文字模板的片段指向模板素材, 文字在模板的第一个子文字里
    for tpl in mats.get("text_templates", []):
        infos = tpl.get("text_info_resources") or []
        if infos and infos[0].get("text_material_id") in texts:
            texts[tpl["id"]] = texts[infos[0]["text_material_id"]]
    cues = []
    for track in content.get("tracks", []):
        if track.get("type") != "text":
            continue
        for seg in track.get("segments", []):
            mat = texts.get(seg.get("material_id"))
            if not mat:
                continue
            try:
                text = json.loads(mat.get("content", "{}")).get("text", "")
            except ValueError:
                text = ""
            tr = seg["target_timerange"]
            if text.strip():
                cues.append((tr["start"], tr["start"] + tr["duration"], text.strip()))
    cues.sort()
    output = output or os.path.join(draft_dir, f"{name}.srt")
    with open(output, "w", encoding="utf-8") as f:
        for i, (s, e, t) in enumerate(cues, 1):
            f.write(f"{i}\n{format_srt_time(s)} --> {format_srt_time(e)}\n{t}\n\n")
    print(f"📝 SRT ({len(cues)} cues): {output}")
    return output


def warmup_templates(limit: int = 50, free_only: bool = False, category: str = "", name: str = "JY_Template_Warmup",
                     drafts_root: str = None, batches: int = 1, vip_only: bool = False, kind: str = "subtitle",
                     min_id: int = 0):
    """
    生成「模板预热」草稿: 挑出还没有预设、本地也没有模板包的字幕模板, 每个放一段只写 ID 的文字。
    在剪映里打开该草稿 (等 1 分钟左右, 让剪映下载模板包) 后回到首页, 再运行
    `sync_jy_templates.py --from-packages`, 即可由模板包批量生成预设。
    batches > 1 时一次生成多个草稿 (<name>_1, <name>_2, ...), 每个 limit 个模板; batches=0 表示覆盖全部剩余模板。
    kind="text" 预热文字模板 (面板「文字模板」), 默认 "subtitle" 为字幕模板。
    返回草稿名 (batches=1) 或草稿名列表。
    """
    from utils.panel_assets import get_jianying_user_data_dirs, load_panel_rows
    from utils.template_presets import PRESET_DIR, find_preset

    text = kind == "text"
    donor_names = ("Thanks You", "穿搭-强烈安利") if text else ("创意粗体", "KkQ1-4", "向下弹出-方块粗体黄")
    donor = next((find_preset(n) for n in donor_names if find_preset(n)), None)
    if donor is None:
        raise RuntimeError("需要至少一个同类模板预设作骨架: 先在剪映里套用任意模板并运行 sync_jy_templates.py")
    roots = [os.path.join(ud, "Cache", "artistEffect") for ud in get_jianying_user_data_dirs()]
    rows = []
    for r in load_panel_rows("panel_text_templates.csv" if text else "panel_subtitle_templates.csv"):
        if free_only and r.get("is_vip") == "1" or vip_only and r.get("is_vip") != "1":
            continue
        if category and category not in (r.get("categories") or ""):
            continue
        if min_id and int(r["resource_id"]) < min_id:
            continue
        if os.path.exists(os.path.join(PRESET_DIR, f"{r['resource_id']}.json")):
            continue
        if any(os.path.isdir(os.path.join(root, r["resource_id"], r.get("md5", ""))) for root in roots if r.get("md5")):
            continue
        rows.append(r)
        if batches and len(rows) >= limit * batches:
            break
    if not rows:
        print("✅ 没有需要预热的模板 (都已有预设或模板包)")
        return "" if batches == 1 else []
    if batches == 1:
        return _warmup_draft(rows, donor, name, drafts_root)
    chunks = [rows[i:i + limit] for i in range(0, len(rows), limit)]
    names = [_warmup_draft(c, donor, f"{name}_{k}", drafts_root) for k, c in enumerate(chunks, 1)]
    print(f"🔥 共 {len(names)} 个预热草稿, {len(rows)} 个模板: 逐个打开 (每个等 1 分钟左右) 后再运行 "
          f"sync_jy_templates.py --from-packages")
    return names


def _warmup_draft(rows, donor, name: str, drafts_root: str = None) -> str:
    import copy as _copy

    from core.template_ops import TemplateTextSegment, _clip_from_json
    from utils.template_presets import instantiate_preset

    p = JyProject(name, drafts_root=drafts_root, overwrite=True)
    dur = 2_000_000
    for i, r in enumerate(rows):
        inst = instantiate_preset(_copy.deepcopy(donor), r["name"], dur)
        tpl = inst["materials"]["text_templates"][0]
        tpl.update(resource_id=r["resource_id"], effect_id=r["resource_id"], name=r["name"], path="", resources=[])
        if tpl.get("type") == "text_template":  # 文字模板: 去掉骨架自带的贴纸 / 动画 / 特效, 只留 ID
            tpl["non_text_info_resources"] = []
            for info in tpl.get("text_info_resources") or []:
                info["extra_material_refs"] = []
            inst["materials"] = {k: v for k, v in inst["materials"].items() if k in ("text_templates", "texts")}
            inst["extra_material_refs"] = []
        track = f"Warmup{i % 10}"
        p._ensure_track(draft.TrackType.text, track)
        seg = TemplateTextSegment(r["name"], draft.Timerange((i // 10) * dur, dur), inst,
                                  clip_settings=_clip_from_json(inst["clip"]))
        p.script.add_segment(seg, track)
    p.save()
    print(f"🔥 预热草稿「{name}」: {len(rows)} 个模板。在剪映里打开它, 等 1 分钟左右后回到首页, 再运行 "
          f"sync_jy_templates.py --from-packages")
    return name


def sync_all(only=None) -> int:
    """依次运行面板索引、音乐音效、模板预设同步 (都只读剪映缓存/草稿, 不修改剪映数据)。"""
    import subprocess

    here = os.path.dirname(os.path.abspath(__file__))
    steps = [("panel", "sync_jy_panel.py"), ("music", "sync_jy_music.py"), ("templates", "sync_jy_templates.py")]
    failed = []
    for key, script in steps:
        if only and key not in only:
            continue
        print(f"\n=== 同步 {key} ({script}) ===", flush=True)
        if subprocess.call([sys.executable, os.path.join(here, script)]) != 0:
            failed.append(key)
    if failed:
        print(f"\n⚠️ 同步失败: {', '.join(failed)}")
        return 1
    print("\n✅ 同步完成")
    return 0


def _main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="JianYing Pro Skill CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("clone", help="基于已有草稿另存新草稿 (不修改原草稿)")
    c.add_argument("--template", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--replace", action="store_true", help="允许覆盖同名的新草稿")
    e = sub.add_parser("export-srt", help="导出草稿字幕为 SRT")
    e.add_argument("--name", required=True)
    e.add_argument("--output")
    z = sub.add_parser("apply-zoom", help="按录屏点击事件 (events.json) 生成自动变焦草稿")
    z.add_argument("--name", required=True)
    z.add_argument("--video", required=True)
    z.add_argument("--json", required=True, help="录屏生成的 *_events.json")
    z.add_argument("--scale", type=int, default=150, help="放大比例 (%%)")
    for p in (c, e, z):
        p.add_argument("--root", help="草稿根目录 (默认自动探测)")
    w = sub.add_parser("warmup", help="生成字幕模板预热草稿 (剪映打开后下载模板包, 再由模板包生成预设)")
    w.add_argument("--limit", type=int, default=50)
    w.add_argument("--free-only", action="store_true")
    w.add_argument("--vip-only", action="store_true")
    w.add_argument("--kind", choices=["subtitle", "text"], default="subtitle", help="字幕模板 / 文字模板")
    w.add_argument("--auto", action="store_true",
                   help="生成后自动在剪映里逐个打开预热草稿、等待后回首页 (用完的预热草稿移到废纸篓), 再生成预设 (macOS, 需要辅助功能 + 屏幕录制权限)")
    w.add_argument("--wait", type=float, default=60, help="--auto 时每个草稿停留的秒数")
    w.add_argument("--min-id", type=int, default=0,
                   help="只预热 resource_id 不小于它的模板 (较老的文字模板多数已下载不到, 如 7200000000000000000)")
    w.add_argument("--category", default="", help="只预热某个面板分类, 如 口播 / 综艺 / 热门")
    w.add_argument("--name", default="JY_Template_Warmup")
    w.add_argument("--batches", type=int, default=1, help="一次生成多个预热草稿 (每个 --limit 个); 0 = 覆盖全部剩余模板")
    s = sub.add_parser("sync", help="同步剪映 11.x 本地缓存: 面板素材索引 / 音乐音效 / 字幕与文字模板预设 (只读)")
    s.add_argument("--only", choices=["panel", "music", "templates"], action="append",
                   help="只同步指定部分, 可重复; 默认全部")
    args = ap.parse_args(argv)
    if args.cmd == "sync":
        return sync_all(args.only)
    if args.cmd == "warmup":
        made = warmup_templates(args.limit, args.free_only, args.category, args.name, batches=args.batches,
                                vip_only=args.vip_only, kind=args.kind, min_id=args.min_id)
        names = [made] if isinstance(made, str) and made else list(made or [])
        if args.auto and names:
            import subprocess

            from jy_preview import open_and_wait

            open_and_wait(list(reversed(names)), wait=args.wait, discard=True)  # 新生成的排在首页最前面
            subprocess.call([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                          "sync_jy_templates.py"), "--from-packages"],
                            stdout=subprocess.DEVNULL)
            print("✅ 预热完成, 已由模板包生成预设 (sync_jy_templates.py --list 查看)")
        return 0
    if args.cmd == "clone":
        clone_draft(args.template, args.name, args.root, args.replace)
    elif args.cmd == "export-srt":
        export_srt(args.name, args.output, args.root)
    else:
        from smart_zoomer import apply_smart_zoom

        project = JyProject(args.name, drafts_root=args.root, overwrite=True)
        seg = project.add_media_safe(args.video, "0s", track_name="Main")
        apply_smart_zoom(project, seg, args.json, zoom_scale=args.scale)
        project.save()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
