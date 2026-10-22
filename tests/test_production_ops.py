import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "vendor"))

import pytest  # noqa: E402

from jy_wrapper import JyProject  # noqa: E402

VIDEO = os.path.join(ROOT, "assets", "video.mp4")


@pytest.fixture()
def workspace():
    root = tempfile.mkdtemp(prefix="jy_prod_")
    media = os.path.join(root, "media")
    os.makedirs(media)
    for name in ("Intro_Slot.mp4", "customer_a_intro.mp4"):
        shutil.copy(VIDEO, os.path.join(media, name))
    drafts = os.path.join(root, "drafts")
    os.makedirs(drafts)
    yield drafts, media
    shutil.rmtree(root, ignore_errors=True)


def _videos(p):
    with open(os.path.join(p.root, p.name, "draft_info.json"), encoding="utf-8") as f:
        return json.load(f)["materials"]["videos"]


def test_template_slot_replace_and_reconnect(workspace):
    drafts, media = workspace
    t = JyProject("Master", drafts_root=drafts, overwrite=True)
    t.add_media_safe(os.path.join(media, "Intro_Slot.mp4"), "0s", "3s", track_name="Main")
    t.save()
    assert _videos(t)[0]["material_name"] == "Intro_Slot.mp4"  # 素材名保留原文件名

    p = JyProject.from_template("Master", "Customer_A", drafts_root=drafts)
    assert p.replace_material_by_name("intro_slot", os.path.join(media, "customer_a_intro.mp4")) == 1
    p.save()
    v = _videos(p)[0]
    assert v["material_name"] == "customer_a_intro.mp4" and os.path.exists(v["path"])
    assert _videos(t)[0]["material_name"] == "Intro_Slot.mp4"  # 模板没被改动
    with pytest.raises(KeyError):
        p.replace_material_by_name("not_exist", os.path.join(media, "customer_a_intro.mp4"))

    os.remove(v["path"])  # 模拟素材丢失
    q = JyProject("Customer_A", drafts_root=drafts, overwrite=False)
    report = q.reconnect_all_assets(media)
    assert report == {"fixed": ["customer_a_intro.mp4"], "missing": []}
    q.save()
    assert os.path.exists(_videos(q)[0]["path"])
