"""
模板化批量出片: 素材槽位替换与素材重连。

典型流程:
    p = JyProject.from_template("Master_Template", "Customer_A")   # 克隆模板 (不改原草稿) 并打开
    p.replace_material_by_name("Intro_Slot", "/path/customer_a/intro.mp4")
    p.save()
"""

import os
from typing import Dict, List, Optional

import pyJianYingDraft as draft

AUDIO_EXTS = {".mp3", ".wav", ".aac", ".flac", ".m4a", ".ogg"}


def _stem(name: str) -> str:
    return os.path.splitext(os.path.basename(name or ""))[0].lower()


class ProductionOpsMixin:
    """JyProject 的模板化出片 Mixin。"""

    def _all_media_materials(self) -> List[dict]:
        """[{kind, name, path, ref}]: 打开已有草稿时导入的素材 (dict) 与本次新加的素材 (对象)。"""
        out = []
        for kind, name_key in (("videos", "material_name"), ("audios", "name")):
            for m in self.script.imported_materials.get(kind, []):
                out.append({"kind": kind, "name": m.get(name_key, ""), "path": m.get("path", ""), "ref": m,
                            "name_key": name_key})
            for m in getattr(self.script.materials, kind, []):
                out.append({"kind": kind, "name": m.material_name, "path": m.path, "ref": m, "name_key": None})
        return out

    def _match_materials(self, name: str) -> List[dict]:
        mats = self._all_media_materials()
        for pick in (lambda m: m["name"] == name,
                     lambda m: _stem(m["name"]) == _stem(name),
                     lambda m: name.lower() in (m["name"] or "").lower()):
            hits = [m for m in mats if pick(m)]
            if hits:
                return hits
        return []

    def replace_material_by_name(self, name: str, media_path: str, replace_crop: bool = False) -> int:
        """
        把名为 name 的素材 (素材名/文件名, 不区分大小写, 可不带扩展名; 也接受唯一的部分匹配) 替换为新文件。
        片段的时长与截取范围不变, 适合模板里的「素材槽位」。新文件会复制进草稿目录。返回替换的素材数。
        新素材比原片段用到的范围短时会提示 (剪映里会显示为黑帧/静帧)。
        """
        if not os.path.exists(media_path):
            raise FileNotFoundError(media_path)
        hits = self._match_materials(name)
        if not hits:
            names = "、".join(sorted({m["name"] for m in self._all_media_materials()})[:20])
            raise KeyError(f"没有找到素材「{name}」。草稿里的素材: {names}")
        if len({_stem(h["name"]) for h in hits}) > 1:
            raise KeyError(f"「{name}」匹配到多个不同素材: {'、'.join(sorted({h['name'] for h in hits}))}, 请写完整名称")

        staged = self._stage_local_asset(media_path)
        display = os.path.basename(media_path)
        is_audio = os.path.splitext(media_path)[1].lower() in AUDIO_EXTS
        new = draft.AudioMaterial(staged, display) if is_audio else draft.VideoMaterial(staged, display)
        count = 0
        for h in hits:
            if (h["kind"] == "audios") != is_audio:
                continue
            ref = h["ref"]
            if isinstance(ref, dict):
                ref.update({h["name_key"]: new.material_name, "path": new.path, "duration": new.duration})
                if not is_audio:
                    ref.update({"width": new.width, "height": new.height, "material_type": new.material_type})
                    if replace_crop:
                        ref["crop"] = new.crop_settings.export_json()
            else:
                for attr in ("path", "material_name", "duration", "width", "height", "material_type"):
                    if hasattr(new, attr):
                        setattr(ref, attr, getattr(new, attr))
            count += 1
            self._warn_if_too_short(h, new.duration, is_photo=getattr(new, "material_type", "") == "photo")
        if not count:
            raise TypeError(f"「{name}」的类型与新文件不一致 (音频/视频)")
        print(f"🔁 已替换素材「{hits[0]['name']}」→ {display} ({count} 处)")
        return count

    def _warn_if_too_short(self, hit: dict, new_duration: int, is_photo: bool = False) -> None:
        if is_photo:
            return
        mid = hit["ref"].get("id") if isinstance(hit["ref"], dict) else hit["ref"].material_id
        need = 0
        for track in list(self.script.imported_tracks) + list(self.script.tracks.values()):
            for seg in getattr(track, "segments", []):
                if getattr(seg, "material_id", None) == mid and getattr(seg, "source_timerange", None):
                    need = max(need, seg.source_timerange.start + seg.source_timerange.duration)
        if need and new_duration and new_duration < need:
            print(f"⚠️ 新素材只有 {new_duration / 1e6:.2f}s, 原片段用到了 {need / 1e6:.2f}s; 超出部分在剪映里会缺画面")

    def reconnect_all_assets(self, media_root: str, copy_into_draft: bool = True) -> Dict[str, List[str]]:
        """
        重连丢失的素材: 草稿里路径失效的视频/音频, 按文件名到 media_root (递归) 找回。
        copy_into_draft=True 时把找到的文件复制进草稿目录 (macOS 剪映沙盒下外部路径可能无法访问)。
        返回 {"fixed": [...], "missing": [...]}。
        """
        index: Dict[str, str] = {}
        for root, _dirs, files in os.walk(media_root):
            for f in files:
                index.setdefault(f.lower(), os.path.join(root, f))
        report = {"fixed": [], "missing": []}
        draft_dir = getattr(self, "draft_dir", "")
        for m in self._all_media_materials():
            path = m["path"] or ""
            full = path if os.path.isabs(path) or not draft_dir else os.path.join(draft_dir, path)
            if path and os.path.exists(full):
                continue
            found: Optional[str] = None
            for key in (os.path.basename(path), m["name"]):
                if key and key.lower() in index:
                    found = index[key.lower()]
                    break
            label = m["name"] or os.path.basename(path)
            if not found:
                report["missing"].append(label)
                continue
            new_path = self._stage_local_asset(found) if copy_into_draft else os.path.abspath(found)
            if isinstance(m["ref"], dict):
                m["ref"]["path"] = new_path
            else:
                m["ref"].path = new_path
            report["fixed"].append(label)
        print(f"🔗 重连素材: 找回 {len(report['fixed'])} 个, 仍缺 {len(report['missing'])} 个"
              + (f" ({'、'.join(report['missing'][:8])})" if report["missing"] else ""))
        return report
