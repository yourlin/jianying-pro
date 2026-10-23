import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402

import jy_preview  # noqa: E402


def test_seek_presses_home_then_tens_and_frames(monkeypatch):
    keys = []
    monkeypatch.setattr(jy_preview, "_activate", lambda: None)
    monkeypatch.setattr(jy_preview, "_key", lambda code, shift=False, count=1: keys.append((code, shift, count)))
    monkeypatch.setattr(jy_preview.time, "sleep", lambda s: None)
    monkeypatch.setattr(jy_preview, "_total_frames", lambda fps=30: None)  # 不去问真的剪映
    monkeypatch.setattr(jy_preview, "current_frame", lambda fps=30: 75)
    jy_preview.seek(2.5, fps=30)  # 75 帧
    assert keys == [(jy_preview.KEY_HOME, False, 1), (jy_preview.KEY_RIGHT, True, 7), (jy_preview.KEY_RIGHT, False, 5)]


def test_seek_corrects_dropped_keys(monkeypatch):
    keys, frames = [], iter([60, 75])  # 第一次读到 60 帧 (丢了按键), 补按后到 75
    monkeypatch.setattr(jy_preview, "_activate", lambda: None)
    monkeypatch.setattr(jy_preview, "_key", lambda code, shift=False, count=1: keys.append((code, shift, count)))
    monkeypatch.setattr(jy_preview.time, "sleep", lambda s: None)
    monkeypatch.setattr(jy_preview, "_total_frames", lambda fps=30: None)  # 不去问真的剪映
    monkeypatch.setattr(jy_preview, "current_frame", lambda fps=30: next(frames))
    jy_preview.seek(2.5, fps=30)
    assert keys[-2:] == [(jy_preview.KEY_RIGHT, True, 1), (jy_preview.KEY_RIGHT, False, 5)]


def test_player_box_finds_canvas_inside_panel(monkeypatch):
    # 合成一张 2 倍像素的编辑页: 面板缝隙 20, 面板 38, 标题栏 48, 画布 (黑边 + 画面)
    win = {"x": 0, "y": 0, "w": 500, "h": 300}
    img = Image.new("RGB", (1000, 600), (20, 20, 20))
    d = ImageDraw.Draw(img)
    d.rectangle((300, 40, 699, 559), fill=(38, 38, 38))  # 播放器面板
    d.rectangle((300, 40, 699, 60), fill=(48, 48, 48))  # 标题栏
    d.rectangle((310, 150, 689, 450), fill=(0, 0, 0))  # 画布黑边
    d.rectangle((310, 180, 689, 420), fill=(200, 120, 60))  # 画面
    monkeypatch.setattr(jy_preview, "_center", lambda name: (160, 250, 30, 7))  # 时间码 (点)
    x0, y0, x1, y1 = jy_preview.player_box(img, win)
    assert abs(x0 - 310) <= 2 and abs(x1 - 690) <= 3
    assert abs(y0 - 150) <= 2 and abs(y1 - 451) <= 3


def test_open_draft_refuses_when_editor_open(monkeypatch):
    monkeypatch.setattr(jy_preview, "_activate", lambda: None)
    monkeypatch.setattr(jy_preview, "in_editor", lambda: True)
    with pytest.raises(jy_preview.PreviewError):
        jy_preview.open_draft("X")
