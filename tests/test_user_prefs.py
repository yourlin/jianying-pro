import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from utils import user_prefs  # noqa: E402
from utils.user_prefs import (  # noqa: E402
    UserPrefsError,
    apply_user_prefs,
    deep_merge,
    load_user_prefs,
    prefs_path,
)


def _profiles():
    scene = {"id": "talking_head", "audio": {"bgm_volume": 0.2, "loudness_lufs": -14}}
    style = {
        "id": "tech",
        "roles": {
            "title": {"font": "未来黑", "size": 11.0, "anim_in": "渐显"},
            "subtitle": {"font": "台北黑体-Regular", "size": 9.0, "border": True},
        },
        "grade": {"filter": {"name": "冷蓝", "intensity": 60}},
        "bgm": {"id": "123", "volume": 0.2, "fade_in": 1.0, "fade_out": 2.0},
    }
    fmt = {"id": "vertical", "canvas": {"width": 1080, "height": 1920}}
    return scene, style, fmt


PREFS = {
    "_说明": "comment",
    "global": {
        "_why": "x",
        "style": {"grade": None, "bgm": {"fade_in": 0, "_why": "y"}},
        "scene": {"audio": {"bgm_volume": 0.16}},
    },
    "formats": {
        "vertical": {
            "style": {
                "roles": {
                    "subtitle": {
                        "template": {"kind": "subtitle", "name": "KkQ2-1-10"},
                        "_why": "z",
                    },
                    "title": {"anim_in": "向上弹入"},
                }
            }
        },
        "horizontal": {"style": {"roles": {"title": {"anim_in": "横屏专用"}}}},
    },
    "styles": {"tech": {"style": {"bgm": {"fade_out": 3.0}}}},
    "scenes": {"talking_head": {"scene": {"audio": {"bgm_volume": 0.1}}}},
}


def _write(tmp_path, data):
    p = tmp_path / "style_overrides.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_deep_merge_keeps_siblings_and_replaces_leaves():
    base = {"a": {"b": 1, "c": {"d": 2, "e": 3}}, "l": [1, 2]}
    changes = []
    deep_merge(base, {"a": {"c": {"d": 20}}, "l": [9]}, changes=changes)
    assert base == {"a": {"b": 1, "c": {"d": 20, "e": 3}}, "l": [9]}
    assert [c[0] for c in changes] == ["a.c.d", "l"]


def test_groups_apply_in_order_and_report(tmp_path):
    scene, style, fmt = _profiles()
    path = _write(tmp_path, PREFS)
    s2, st2, f2, applied = apply_user_prefs(scene, style, fmt, path=path)
    # global
    assert st2["grade"] is None
    assert st2["bgm"] == {"id": "123", "volume": 0.2, "fade_in": 0, "fade_out": 3.0}
    # formats.vertical (horizontal 组不生效)
    assert st2["roles"]["subtitle"]["template"] == {"kind": "subtitle", "name": "KkQ2-1-10"}
    assert st2["roles"]["subtitle"]["font"] == "台北黑体-Regular"  # 兄弟键保留
    assert st2["roles"]["title"]["anim_in"] == "向上弹入"
    # scenes 组比 global 更具体, 后生效
    assert s2["audio"] == {"bgm_volume": 0.1, "loudness_lufs": -14}
    assert f2 == fmt
    # 输入没被修改
    assert style["grade"]["filter"]["name"] == "冷蓝" and scene["audio"]["bgm_volume"] == 0.2
    assert any(
        a.startswith("[formats.vertical] style.roles.title.anim_in: 渐显 -> 向上弹入")
        for a in applied
    )
    assert any(a.startswith("[scenes.talking_head] scene.audio.bgm_volume") for a in applied)
    assert not any("horizontal" in a for a in applied)


def test_ids_argument_selects_groups(tmp_path):
    scene, style, fmt = _profiles()
    path = _write(tmp_path, PREFS)
    _s, st2, _f, _a = apply_user_prefs(
        scene, style, fmt, ids={"format": "horizontal", "style": "fresh"}, path=path
    )
    assert st2["roles"]["title"]["anim_in"] == "横屏专用"
    assert "template" not in st2["roles"]["subtitle"]
    assert st2["bgm"]["fade_out"] == 2.0  # styles.tech 不生效


def test_underscore_keys_are_ignored(tmp_path):
    scene, style, fmt = _profiles()
    _s, st2, _f, applied = apply_user_prefs(scene, style, fmt, path=_write(tmp_path, PREFS))
    assert "_why" not in st2["roles"]["subtitle"] and "_why" not in st2["bgm"]
    assert not any("_why" in a or "_说明" in a for a in applied)
    data = load_user_prefs(_write(tmp_path, PREFS))
    assert "_说明" not in data and "_why" not in data["global"]


def test_unchanged_values_not_reported(tmp_path):
    scene, style, fmt = _profiles()
    prefs = {"global": {"style": {"bgm": {"fade_out": 2.0}}}}
    _s, st2, _f, applied = apply_user_prefs(scene, style, fmt, path=_write(tmp_path, prefs))
    assert st2 == style and applied == []


def test_missing_file_changes_nothing(tmp_path):
    scene, style, fmt = _profiles()
    s2, st2, f2, applied = apply_user_prefs(scene, style, fmt, path=str(tmp_path / "nope.json"))
    assert (s2, st2, f2, applied) == (scene, style, fmt, [])


@pytest.mark.parametrize("value", ["0", "off", "OFF", "false"])
def test_env_switch_disables(tmp_path, monkeypatch, value):
    monkeypatch.setenv(user_prefs.ENV_VAR, value)
    assert prefs_path() is None
    scene, style, fmt = _profiles()
    s2, st2, f2, applied = apply_user_prefs(scene, style, fmt)
    assert (s2, st2, f2, applied) == (scene, style, fmt, [])


def test_env_path_override(tmp_path, monkeypatch):
    monkeypatch.setenv(user_prefs.ENV_VAR, _write(tmp_path, PREFS))
    scene, style, fmt = _profiles()
    _s, st2, _f, applied = apply_user_prefs(scene, style, fmt)
    assert st2["grade"] is None and applied


def test_default_path_without_env(monkeypatch, tmp_path):
    monkeypatch.delenv(user_prefs.ENV_VAR, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows 的 expanduser 读 USERPROFILE
    assert prefs_path() == str(tmp_path / ".config" / "jianying-pro" / "style_overrides.json")
    scene, style, fmt = _profiles()
    assert apply_user_prefs(scene, style, fmt)[3] == []  # tmp HOME 下没有覆盖文件


def test_bad_structure_raises(tmp_path):
    with pytest.raises(UserPrefsError):
        load_user_prefs(_write(tmp_path, {"formats": {"vertical": {"roles": {}}}}))
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(UserPrefsError):
        load_user_prefs(str(p))


def test_real_user_file_is_valid_if_present():
    """用户机器上的覆盖文件 (存在时) 能被加载, 且只用了 scene / style / format 三个目标。"""
    path = os.path.expanduser(user_prefs.DEFAULT_PATH)
    if not os.path.isfile(path):
        pytest.skip("没有个人覆盖文件")
    assert isinstance(load_user_prefs(path), dict)
