import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "vendor"))

import motion_graphics as mg  # noqa: E402
import pytest  # noqa: E402

SAMPLES = {
    "bar_chart": {"title": "播放量", "unit": "万", "items": [{"label": "抖音", "value": 820}, {"label": "B站", "value": 460}]},
    "line_chart": {"labels": ["1月", "2月", "3月"], "series": [{"name": "粉丝", "values": [1, 5, 9]}]},
    "counter": {"to": 12800, "suffix": "+", "label": "累计用户"},
    "callout": {"marks": [{"type": "circle", "x": 0.3, "y": 0.4}, {"type": "arrow", "x": 0.5, "y": 0.5, "from": [0.8, 0.8], "text": "看这里"}]},
    "progress_bar": {"value": 72, "label": "完成度"},
    "map_route": {"points": [{"x": 0.1, "y": 0.8, "label": "成都"}, [0.5, 0.5], {"x": 0.9, "y": 0.2, "label": "拉萨"}]},
    "list_reveal": {"title": "三个技巧", "items": ["先写钩子", {"text": "节奏要快", "sub": "3 秒一个信息点"}]},
}  # fmt: skip


def _config_of(html: str) -> dict:
    m = re.search(r"window\.MG_CONFIG = (.*?);</script>", html, re.S)
    assert m, "HTML 里没有注入配置"
    return json.loads(m.group(1))


def test_every_template_file_exists_with_placeholders():
    for name in mg.TEMPLATES:
        path = os.path.join(mg.TEMPLATE_DIR, f"{name}.html")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        assert "/*MG_CONFIG*/null" in src and "/*MG_RUNTIME*/" in src and "MG.run(" in src
    assert set(SAMPLES) == set(mg.TEMPLATES)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_render_html_inlines_runtime_and_config(name):
    html = mg.render_html(name, SAMPLES[name])
    assert "/*MG_CONFIG*/" not in html and "/*MG_RUNTIME*/" not in html
    assert "window.animationFinished = true" in html  # web-vfx 录制协议
    assert "window.__mgSeek" in html
    assert "<script src=" not in html  # 不依赖 CDN, 可离线
    cfg = _config_of(html)
    assert cfg["template"] == name and (cfg["width"], cfg["height"]) == (1920, 1080)
    assert cfg["duration"] > cfg["hold"] and cfg["background"] == "transparent"


def test_aspect_size_background_and_theme():
    cfg = _config_of(mg.render_html("counter", {"to": 5}, aspect="portrait"))
    assert (cfg["width"], cfg["height"]) == (1080, 1920)
    cfg = _config_of(mg.render_html("counter", {"to": 5}, size="1080x1350", background="#FFFFFF"))
    assert (cfg["width"], cfg["height"]) == (1080, 1350)
    assert cfg["background"] == "#FFFFFF" and cfg["theme"]["text"] == "#1B1F27"  # 浅底深字
    cfg = _config_of(mg.render_html("counter", {"to": 5}, background="#101820"))
    assert cfg["theme"]["text"] == "#FFFFFF"
    cfg = _config_of(
        mg.render_html("bar_chart", SAMPLES["bar_chart"], theme={"primary": "#FF5A36"})
    )
    assert cfg["theme"]["palette"][0] == "#FF5A36"
    # auto 背景: mp4 不支持透明 -> 黑底; mov 透明
    assert mg.build_config("counter", {"to": 1}, out_path="a.mp4")["background"] == "#000000"
    assert mg.build_config("counter", {"to": 1}, out_path="a.mov")["background"] == "transparent"


def test_data_is_normalized():
    cfg = mg.build_config("progress_bar", {"value": 40, "label": "进度"})
    assert cfg["data"]["items"] == [{"label": "进度", "value": 40.0, "color": None}]
    cfg = mg.build_config("list_reveal", {"items": ["a", {"text": "b", "sub": "c"}]})
    assert cfg["data"]["items"][0] == {"text": "a", "sub": "", "color": None}
    cfg = mg.build_config("map_route", SAMPLES["map_route"])
    assert cfg["data"]["points"][1] == {"x": 0.5, "y": 0.5, "label": ""}
    cfg = mg.build_config("list_reveal", {"items": ["a"] * 4})
    assert cfg["duration"] == pytest.approx(2.0 + 0.7 * 4 + 1.0)  # 默认时长随条目数增加
    cfg = mg.build_config("counter", {"to": 1}, duration=8, hold=2)
    assert cfg["duration"] == 8 and cfg["hold"] == 2


@pytest.mark.parametrize(
    "name, data, field",
    [
        ("bar_chart", {"items": []}, "items"),
        ("bar_chart", {"items": [{"label": "a", "value": "3"}]}, "items[0].value"),
        ("bar_chart", {"items": [{"label": "a", "value": -1}]}, "items[0].value"),
        ("bar_chart", {"items": [{"label": "a", "value": 5}], "max": 3}, "max"),
        ("bar_chart", {"items": [{"label": "a", "value": 1, "color": "red;x"}]}, "items[0].color"),
        ("line_chart", {"labels": ["a", "b"], "series": [{"values": [1]}]}, "series[0].values"),
        ("counter", {}, "to"),
        ("counter", {"to": float("nan")}, "to"),
        ("callout", {"marks": [{"type": "arrow", "x": 0.5, "y": 0.5}]}, "marks[0].from"),
        ("callout", {"marks": [{"type": "star", "x": 0.5, "y": 0.5}]}, "marks[0].type"),
        ("callout", {"marks": [{"x": 1.5, "y": 0.5}]}, "marks[0].x"),
        ("progress_bar", {"value": 120}, "items[0].value"),
        ("map_route", {"points": [[0.1, 0.1]]}, "points"),
        ("map_route", {"points": [[0, 0], [1, 1]], "path": "M0 0 <script>"}, "path"),
        ("list_reveal", {"items": [""]}, "items[0].text"),
        ("list_reveal", {"items": ["x" * 41]}, "items[0].text"),
    ],
)
def test_validation_errors_name_the_field(name, data, field):
    with pytest.raises(mg.MGValidationError) as e:
        mg.build_config(name, data)
    assert str(e.value).startswith(field + ":")


def test_option_validation():
    with pytest.raises(mg.MGValidationError, match="未知模板"):
        mg.build_config("pie", {})
    with pytest.raises(mg.MGValidationError, match="size"):
        mg.build_config("counter", {"to": 1}, size="1081x1920")
    with pytest.raises(mg.MGValidationError, match="aspect"):
        mg.build_config("counter", {"to": 1}, aspect="wide")
    with pytest.raises(mg.MGValidationError, match="background"):
        mg.build_config("counter", {"to": 1}, background="url(x)")
    with pytest.raises(mg.MGValidationError, match="duration"):
        mg.build_config("counter", {"to": 1}, duration=1, hold=1)
    with pytest.raises(mg.MGValidationError, match="theme.font"):
        mg.build_config("counter", {"to": 1}, theme={"font": "a;}</style>"})


def test_text_is_escaped_inside_script():
    evil = "</script><img src=x onerror=alert(1)>"
    html = mg.render_html("list_reveal", {"items": [evil[:40]]})
    body = html.split("window.MG_CONFIG = ", 1)[1].split(";</script>", 1)[0]
    assert "</script" not in body and "<\\/script" in body
    assert _config_of(html)["data"]["items"][0]["text"] == evil[:40]
    assert "innerHTML" not in html  # 文字一律 textContent


def test_encoder_args():
    assert "prores_ks" in mg.encoder_args("a.mov", True)
    assert "yuva420p" in mg.encoder_args("a.webm", True)
    assert "libx264" in mg.encoder_args("a.mp4", False)
    with pytest.raises(mg.MGValidationError, match="透明"):
        mg.encoder_args("a.mp4", True)
    with pytest.raises(mg.MGValidationError):
        mg.encoder_args("a.gif", False)


def test_render_without_playwright_gives_clear_error(monkeypatch, tmp_path):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(mg.MGDependencyError, match="pip install playwright"):
        mg.render_video("counter", {"to": 1}, str(tmp_path / "a.mov"))
    # 参数错误优先于依赖检查
    with pytest.raises(mg.MGValidationError):
        mg.render_video("counter", {}, str(tmp_path / "a.mov"))
    with pytest.raises(mg.MGValidationError, match="1920x1080"):
        mg.render_video(
            "counter", {"to": 1}, str(tmp_path / "a.mp4"), mode="record", aspect="portrait"
        )


def test_cli_list_html_and_errors(tmp_path, capsys):
    assert mg._main(["list", "--json"]) == 0
    names = [t["name"] for t in json.loads(capsys.readouterr().out)["data"]["templates"]]
    assert names == list(mg.TEMPLATES)
    data_file = tmp_path / "d.json"
    data_file.write_text(json.dumps(SAMPLES["bar_chart"], ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "x.html"
    rc = mg._main(
        [
            "html",
            "bar_chart",
            "--data",
            str(data_file),
            "--out",
            str(out),
            "--aspect",
            "square",
            "--json",
        ]
    )
    assert rc == 0 and json.loads(capsys.readouterr().out)["ok"]
    assert _config_of(out.read_text(encoding="utf-8"))["width"] == 1080
    rc = mg._main(["html", "counter", "--data", "{bad", "--out", str(out), "--json"])
    res = json.loads(capsys.readouterr().out)
    assert rc == 2 and res["code"] == "invalid_input"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="需要 ffmpeg 合成测试视频")
def test_mixin_adds_rendered_clip(monkeypatch, tmp_path):
    """渲染用 ffmpeg 合成的视频代替 (不启动浏览器), 验证 Mixin 的尺寸透传和入轨。"""
    from core.motion_graphics_ops import MotionGraphicsOpsMixin
    from jy_wrapper import JyProject

    class P(JyProject, MotionGraphicsOpsMixin):
        pass

    calls = {}

    def fake_render(template, data, out, background="transparent", **opts):
        cfg = mg.build_config(template, data, background=background, out_path=out, **opts)
        calls.update(cfg=cfg)
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
               f"color=c=black:s=320x180:d={cfg['duration']}:r=25", "-pix_fmt", "yuv420p", out]  # fmt: skip
        subprocess.run(cmd, check=True)
        return {"output": out, "duration": cfg["duration"], "transparent": False}

    monkeypatch.setattr(mg, "render_video", fake_render)
    root = tempfile.mkdtemp(prefix="jy_mg_")
    try:
        p = P("MG", width=1080, height=1920, drafts_root=root, overwrite=True)
        seg = p.add_motion_graphic(
            "counter", {"to": 99}, "1s", "3s", background="#000000", fmt="mp4"
        )
        assert seg is not None
        assert (calls["cfg"]["width"], calls["cfg"]["height"]) == (1080, 1920)
        assert calls["cfg"]["duration"] == 3
        assert (
            seg.target_timerange.start == 1_000_000 and seg.target_timerange.duration == 3_000_000
        )
        with pytest.raises(mg.MGValidationError):
            p.add_motion_graphic("counter", {"to": 1}, fmt="gif")
    finally:
        shutil.rmtree(root, ignore_errors=True)
