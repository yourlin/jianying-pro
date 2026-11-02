"""T5a 音效库索引: 名称分类、云端 + 本地目录构建、校验、仓库自带索引可用。"""

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import build_sfx_library as lib  # noqa: E402
import pytest  # noqa: E402

AUDIO = os.path.join(ROOT, "assets", "audio.mp3")


@pytest.fixture()
def tmp():
    d = tempfile.mkdtemp(prefix="jy_sfxlib_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.mark.parametrize(
    "name,cat",
    [
        ("“呼”的转场音效", "whoosh"),
        ("Woosh", "whoosh"),
        ("Swish", "whoosh"),
        ("唰", "whoosh"),
        ("啵一声", "pop"),
        ("气泡弹出", "pop"),
        ("鼠标单击", "click"),
        ("打响指声", "click"),
        ("砰，撞击声", "hit"),
        ("综艺 重音", "hit"),
        ("渐升混响音", "riser"),
        ("叮", "ding"),
        ("打字机打字声（短）", "typing"),
        ("Typing (one key)", "typing"),
        ("咔嚓，拍照声3", "camera"),
        ("人群欢呼鼓掌声1", "applause"),
        ("火球爆炸", "boom"),
        ("翻书声", "page"),
        ("10秒倒计时", "countdown"),
        ("Gentle and pop marimba BGM(1040887)", ""),
        ("猫打呼噜声", ""),
    ],
)
def test_classify(name, cat):
    assert lib.classify(name) == cat


def test_classify_tag_fallback():
    assert lib.classify("某个声音", "提示音|热门") == "ding"
    assert lib.classify("某个声音", "热门") == ""
    assert lib.classify("嗖", "BGM") == ""


def test_build_from_cloud_and_local(tmp):
    cloud = os.path.join(tmp, "cloud.csv")
    with open(cloud, "w", encoding="utf-8") as f:
        f.write("# comment\neffect_id,title,duration_s,categories,is_vip\n")
        f.write(
            "1,嗖(123456),1.0,转场,0\n2,砰，撞击声,1,热门,1\n3,狗叫,2,动物,0\n4,叮,0.8,提示音,\n"
        )
    local = os.path.join(tmp, "sfx")
    os.makedirs(os.path.join(local, "riser"))
    shutil.copy(AUDIO, os.path.join(local, "riser", "build_up.mp3"))
    shutil.copy(AUDIO, os.path.join(local, "pop_soft.mp3"))
    shutil.copy(AUDIO, os.path.join(local, "unknown.mp3"))
    open(os.path.join(local, "notes.txt"), "w").close()

    rows = lib.cloud_entries(cloud) + lib.local_entries(local)
    by_id = {r["id"]: r for r in rows}
    assert "3" not in by_id  # 分类不出的云端音效不收录
    assert (
        by_id["1"]["name"] == "嗖"
        and by_id["1"]["category"] == "whoosh"
        and by_id["1"]["available"] == "1"
    )
    assert by_id["2"]["available"] == "0"  # 会员
    assert by_id["4"]["available"] == "0"  # is_vip 未知
    assert by_id["local:riser/build_up.mp3"]["category"] == "riser"  # 子目录名优先
    assert by_id["local:pop_soft.mp3"]["category"] == "pop"
    assert by_id["local:unknown.mp3"]["category"] == "misc"
    assert all(
        r["source"] == "local" and os.path.exists(r["path"])
        for r in rows
        if r["id"].startswith("local:")
    )

    out = os.path.join(tmp, "sfx_library.csv")
    assert lib.write_library(rows, out) == len(rows)
    assert lib.validate_library(out) == []
    loaded = lib.load_library(out)
    assert {r["id"] for r in loaded} == set(by_id)
    assert isinstance(next(r for r in loaded if r["id"] == "1")["duration_s"], float)

    os.remove(os.path.join(local, "pop_soft.mp3"))
    assert any("local file missing" in e for e in lib.validate_library(out))


def test_cli_build_and_validate(tmp, capsys):
    cloud = os.path.join(tmp, "cloud.csv")
    with open(cloud, "w", encoding="utf-8") as f:
        f.write("effect_id,title,duration_s,categories,is_vip\n1,Woosh,0.4,转场,0\n")
    out = os.path.join(tmp, "lib.csv")
    assert lib._main(["--cloud", cloud, "-o", out, "--json"]) == 0
    assert '"whoosh"' in capsys.readouterr().out
    assert lib._main(["--validate", "-o", out]) == 0
    with open(out, "w", encoding="utf-8") as f:
        f.write("id,name\n1,x\n")
    assert lib._main(["--validate", "-o", out]) == 1


def test_shipped_library_is_valid_and_covers_core_categories():
    assert lib.validate_library(lib.LIBRARY) == []
    rows = lib.load_library(lib.LIBRARY)
    usable = {r["category"] for r in rows if r["available"] == "1"}
    for cat in ("whoosh", "pop", "click", "hit", "riser", "ding", "typing", "camera"):
        assert cat in usable, cat
