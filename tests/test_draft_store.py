# ruff: noqa: E402

import json
import os
import shutil
import sys
import tempfile
import unittest

current_dir = os.path.dirname(os.path.abspath(__file__))
skill_root = os.path.dirname(current_dir)
for p in (os.path.join(skill_root, "scripts"), os.path.join(skill_root, "scripts", "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

from jy_wrapper import JyProject, clone_draft, export_srt
from utils.draft_store import SKILL_CONTENT, EncryptedDraftError


def _encrypt_like_jianying(draft_dir):
    for name in ("draft_info.json", "draft_meta_info.json"):
        with open(os.path.join(draft_dir, name), "w") as f:
            f.write("d56jFBzdd57UGfjNpPzu77IHh3qsTc6E")  # 非 JSON, 模拟剪映加密


class TestDraftStore(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="jy_store_")
        p = JyProject("Src", drafts_root=self.root, overwrite=True)
        p.add_text_simple("第一句", "0s", "1s")
        p.add_text_simple("第二句", "1s", "2s")
        p.save()
        self.src = os.path.join(self.root, "Src")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_save_writes_plaintext_copy(self):
        self.assertTrue(os.path.exists(os.path.join(self.src, SKILL_CONTENT)))

    def test_encrypted_draft_is_never_silently_recreated(self):
        _encrypt_like_jianying(self.src)
        with self.assertRaises(EncryptedDraftError):
            JyProject("Src", drafts_root=self.root, overwrite=False)
        with open(os.path.join(self.src, "draft_info.json")) as f:
            self.assertTrue(f.read().startswith("d56j"))  # 原草稿未被改动

    def test_continue_from_copy_backs_up_app_version(self):
        _encrypt_like_jianying(self.src)
        p = JyProject("Src", drafts_root=self.root, overwrite=False, continue_from_skill_copy=True)
        self.assertEqual(len(p.script.imported_tracks), 1)
        backups = [d for d in os.listdir(self.src) if d.startswith(".jy_app_backup_")]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.src, backups[0], "draft_info.json")) as f:
            self.assertTrue(f.read().startswith("d56j"))

    def test_clone_and_export_srt_from_encrypted_draft(self):
        _encrypt_like_jianying(self.src)
        dst = clone_draft("Src", "Copy", self.root)
        with open(os.path.join(dst, "draft_info.json"), encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["materials"]["texts"]), 2)
        srt = export_srt("Src", drafts_root=self.root)
        with open(srt, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("00:00:01,000 --> 00:00:03,000\n第二句", body)

    def _write_app_plain(self, content, mtime_offset=60):
        """模拟剪映关闭草稿时写出的明文 Timelines/<id>/template.json (比 skill 副本新)。"""
        tl = os.path.join(self.src, "Timelines", "ABC")
        os.makedirs(tl, exist_ok=True)
        path = os.path.join(tl, "template.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(content, f, ensure_ascii=False)
        t = os.path.getmtime(os.path.join(self.src, SKILL_CONTENT)) + mtime_offset
        os.utime(path, (t, t))
        return path

    def test_newer_jianying_plain_copy_wins_and_templates_export(self):
        from utils.draft_store import read_draft_content

        _encrypt_like_jianying(self.src)
        # 剪映里改过: 一条套用了字幕模板的字幕 (片段指向 text_templates, 文字在子文字里)
        self._write_app_plain({
            "materials": {
                "texts": [{"id": "T1", "content": json.dumps({"text": "剪映里改的", "styles": []},
                                                             ensure_ascii=False)}],
                "text_templates": [{"id": "TPL", "text_info_resources": [{"text_material_id": "T1"}]}],
                "videos": [{"id": "V1", "path": "materials/video/missing.mp4"}],
            },
            "tracks": [{"type": "text", "segments": [
                {"material_id": "TPL", "target_timerange": {"start": 0, "duration": 2000000}}]}],
        })
        content, source = read_draft_content(self.src)
        self.assertEqual(source, "jianying_plain")
        with open(export_srt("Src", drafts_root=self.root), encoding="utf-8") as f:
            self.assertIn("剪映里改的", f.read())
        dst = clone_draft("Src", "CopyApp", self.root)  # 找不到的相对路径素材只提示, 不报错
        with open(os.path.join(dst, "draft_info.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["materials"]["videos"][0]["path"], "materials/video/missing.mp4")

    def test_older_jianying_plain_copy_loses_to_skill_copy(self):
        from utils.draft_store import read_draft_content

        _encrypt_like_jianying(self.src)
        self._write_app_plain({"materials": {}, "tracks": []}, mtime_offset=-60)
        self.assertEqual(read_draft_content(self.src)[1], "skill_copy")


if __name__ == "__main__":
    unittest.main()
