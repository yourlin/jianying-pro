"""2026-11-10 宣传片实战踩坑的回归测试 (见 rules/pitfalls.md):

1. 草稿 ID / 时间线 ID 不能共用模板里写死的值 (剪映按 ID 缓存素材记录, 会串草稿)
2. 覆盖重建保留素材目录 (剪映按路径记住素材库, 删掉旧素材会「媒体丢失」), 清掉剪映另存的 Timelines
3. 剪映正打开着草稿 (.locked) 时拒绝覆盖重建
4. 外部音频复制进草稿目录 (macOS 沙盒读不到草稿外的文件)
5. 很短的音频对齐到整帧后超出素材不到 1 帧, 不报错
6. 贴纸写入本地资源路径; 本机没下载的贴纸给出告警
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "vendor"))

TEMPLATE_DRAFT_ID = "BC69C7CD-7C5E-4185-B284-AF3E1047A664"
TEMPLATE_TIMELINE_ID = "91E08AC5-22FB-47e2-9AA0-7DC300FAEA2B"
AUDIO = os.path.join(ROOT, "assets", "audio.mp3")


@pytest.fixture()
def drafts_root():
    root = tempfile.mkdtemp(prefix="jy_safety_")
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _new(drafts_root, name="Safety", **kw):
    from jy_wrapper import JyProject

    return JyProject(name, drafts_root=drafts_root, overwrite=True, **kw)


def _read(drafts_root, name, fname):
    with open(os.path.join(drafts_root, name, fname), encoding="utf-8") as f:
        return json.load(f)


def test_each_draft_gets_fresh_ids(drafts_root):
    a = _new(drafts_root, "A")
    a.save()
    b = _new(drafts_root, "B")
    b.save()
    ids = []
    for name in ("A", "B"):
        meta = _read(drafts_root, name, "draft_meta_info.json")
        info = _read(drafts_root, name, "draft_info.json")
        assert meta["draft_id"] != TEMPLATE_DRAFT_ID
        assert info["id"] != TEMPLATE_TIMELINE_ID
        ids += [meta["draft_id"], info["id"]]
    assert len(set(ids)) == 4


def test_overwrite_keeps_media_but_clears_jianying_timelines(drafts_root):
    p = _new(drafts_root)
    p.add_audio_safe(AUDIO, 0, "1s", "BGM")
    p.save()
    folder = os.path.join(drafts_root, "Safety")
    staged = [f for f in os.listdir(os.path.join(folder, "materials"))]
    assert staged
    # 模拟剪映打开过: 另存一份旧时间线
    os.makedirs(os.path.join(folder, "Timelines", "OLD"))
    open(os.path.join(folder, "Timelines", "OLD", "draft_info.json"), "w").write("{}")
    old_tts = os.path.join(folder, "temp_assets")
    os.makedirs(old_tts, exist_ok=True)
    open(os.path.join(old_tts, "tts_keepme.ogg"), "wb").write(b"x")

    _new(drafts_root).save()
    assert not os.path.exists(os.path.join(folder, "Timelines"))
    assert os.path.exists(os.path.join(old_tts, "tts_keepme.ogg"))
    for f in staged:
        assert os.path.exists(os.path.join(folder, "materials", f))
    assert not [d for d in os.listdir(drafts_root) if d.startswith(".")]  # 临时目录已清理


def test_refuses_to_overwrite_draft_open_in_editor(drafts_root, monkeypatch):
    from core.project_base import DraftOpenInEditorError

    _new(drafts_root).save()
    open(os.path.join(drafts_root, "Safety", ".locked"), "w").close()
    monkeypatch.delenv("JY_ALLOW_OVERWRITE_OPEN", raising=False)
    with pytest.raises(DraftOpenInEditorError):
        _new(drafts_root)
    monkeypatch.setenv("JY_ALLOW_OVERWRITE_OPEN", "1")
    _new(drafts_root)  # 显式放行


def test_external_audio_is_staged_into_draft(drafts_root):
    outside = tempfile.mkdtemp(prefix="jy_cloud_cache_")
    try:
        src = os.path.join(outside, "bgm.mp3")
        shutil.copy(AUDIO, src)
        p = _new(drafts_root)
        seg = p.add_audio_safe(src, 0, "1s", "BGM")
        assert seg is not None
        p.save()
        info = _read(drafts_root, "Safety", "draft_info.json")
        folder = os.path.join(drafts_root, "Safety")
        paths = [a["path"] for a in info["materials"]["audios"]]
        assert paths and all(os.path.abspath(x).startswith(os.path.abspath(folder)) for x in paths)
        assert all(os.path.exists(x) for x in paths)
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_short_audio_sub_frame_overshoot_is_tolerated(drafts_root):
    tmp = tempfile.mkdtemp(prefix="jy_short_")
    try:
        short = os.path.join(tmp, "short.wav")  # 0.82s: 对齐到 30fps 后是 0.8333s, 超出不到 1 帧
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.82", short], check=True)
        p = _new(drafts_root)
        assert p.add_audio_safe(short, 0, None, "VoiceOver") is not None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sticker_material_carries_local_path_or_warns(drafts_root, monkeypatch):
    import utils.panel_assets as pa

    row = next(r for r in pa.load_panel_rows("panel_stickers.csv") if r.get("md5"))
    fake_dir = tempfile.mkdtemp(prefix="jy_sticker_")
    try:
        monkeypatch.setattr(pa, "_local_md5_index", lambda: {row["md5"]: fake_dir})
        p = _new(drafts_root)
        p.add_sticker(row["resource_id"], 0, "1s")
        p.save()
        mat = _read(drafts_root, "Safety", "draft_info.json")["materials"]["stickers"][0]
        assert mat["path"] == fake_dir
        assert mat["name"] == row["name"]

        monkeypatch.setattr(pa, "_local_md5_index", lambda: {})
        p2 = _new(drafts_root, "Safety2")
        p2.add_sticker(row["resource_id"], 0, "1s")
        assert any("没有下载" in w for w in getattr(p2, "warnings", []))
    finally:
        shutil.rmtree(fake_dir, ignore_errors=True)
