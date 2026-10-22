import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "vendor"))

from PIL import Image  # noqa: E402

import preview_assets as pa  # noqa: E402


def _gif(n=5):
    frames = [Image.new("RGB", (120, 80), (i * 50, 0, 0)) for i in range(n)]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:])
    return buf.getvalue()


def test_allowed_hosts_only_cdn():
    assert pa._allowed("https://p3-heycan-jy-sign.byteimg.com/a.png")
    assert not pa._allowed("http://p3.byteimg.com/a.png")
    assert not pa._allowed("https://evil.example.com/a.png")
    assert not pa._allowed("https://byteimg.com.evil.com/a.png")


def test_frames_and_sheet(tmp_path, monkeypatch):
    assert len(pa._frames(_gif(), 3)) == 3
    monkeypatch.setattr(pa, "SHEET_DIR", str(tmp_path))
    rows = [{"name": "测试模板", "is_vip": "1"}, {"name": "无封面", "is_vip": "0"}]
    out = pa.build_sheet(rows, [_gif(), None], frames=3, cols=2)
    img = Image.open(out)
    assert img.width > pa.TILE * 3 * 2


def test_cover_expiry_and_staleness(tmp_path, monkeypatch):
    import utils.panel_assets as pas

    assert pas.cover_expired("https://a.byteimg.com/x.png?x-expires=1700000000&x-signature=s", now=1700000000)
    assert not pas.cover_expired("https://a.byteimg.com/x.png?x-expires=1900000000", now=1700000000)
    assert not pas.cover_expired("https://a.byteeffecttos.com/obj/x")  # 不带签名的链接不过期

    db = tmp_path / "rp.db"
    db.write_text("x")
    monkeypatch.setattr(pas, "find_panel_dbs", lambda *a, **k: [str(db)])
    assert pas.panel_index_stale(str(tmp_path))  # 从未同步
    pas.write_sync_stamp([str(db)], str(tmp_path))
    assert not pas.panel_index_stale(str(tmp_path))
    os.utime(db, (db.stat().st_mtime + 60, db.stat().st_mtime + 60))  # 剪映又更新了缓存
    assert pas.panel_index_stale(str(tmp_path))
