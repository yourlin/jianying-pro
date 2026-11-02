import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import broll_fetch as bf  # noqa: E402


def _pexels_video(vid, w, h, dur, files):
    return {
        "id": vid,
        "width": w,
        "height": h,
        "duration": dur,
        "url": f"https://www.pexels.com/video/x-{vid}/",
        "image": f"https://images.pexels.com/videos/{vid}/thumb.jpg",
        "user": {"name": f"user{vid}", "url": f"https://www.pexels.com/@u{vid}"},
        "video_files": [
            {
                "id": i,
                "quality": q,
                "file_type": "video/mp4",
                "width": fw,
                "height": fh,
                "fps": 25,
                "link": f"https://videos.pexels.com/{vid}_{fw}x{fh}.mp4",
            }
            for i, (q, fw, fh) in enumerate(files)
        ],
    }


def _pixabay_hit(hid, w, h, dur):
    return {
        "id": hid,
        "pageURL": f"https://pixabay.com/videos/x-{hid}/",
        "tags": "city, night, traffic",
        "duration": dur,
        "user": "pxuser",
        "user_id": 42,
        "videos": {
            "large": {
                "url": f"https://cdn.pixabay.com/{hid}_large.mp4",
                "width": w,
                "height": h,
                "thumbnail": f"https://cdn.pixabay.com/{hid}_large.jpg",
            },
            "medium": {
                "url": f"https://cdn.pixabay.com/{hid}_medium.mp4",
                "width": w // 2,
                "height": h // 2,
                "thumbnail": "",
            },
            "small": {"url": "", "width": 0, "height": 0},
        },
    }


class FakeClient:
    """模拟 Pexels / Pixabay HTTP 接口, 记录请求。"""

    def __init__(self):
        self.calls = []
        self.downloads = []

    def get_json(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        if "api.pexels.com/videos/search" in url:
            q = params["query"]
            if q == "nothing":
                return {"videos": []}
            return {
                "videos": (
                    [
                        _pexels_video(
                            1,
                            1920,
                            1080,
                            12,
                            [("hd", 1280, 720), ("hd", 1920, 1080), ("uhd", 3840, 2160)],
                        ),
                        _pexels_video(2, 1080, 1920, 8, [("sd", 540, 960), ("hd", 1080, 1920)]),
                        _pexels_video(3, 1080, 1920, 2, [("hd", 1080, 1920)]),
                    ]
                    if q != "ocean"
                    else [_pexels_video(9, 1080, 1920, 10, [("hd", 1080, 1920)])]
                )
            }
        if "api.pexels.com/videos/videos/" in url:
            vid = int(url.rsplit("/", 1)[1])
            return _pexels_video(vid, 1080, 1920, 6, [("hd", 1080, 1920)])
        if "pixabay.com/api/videos" in url:
            if params.get("id"):
                return {"hits": [_pixabay_hit(int(params["id"]), 1920, 1080, 15)]}
            return {"hits": [_pixabay_hit(100, 1920, 1080, 20), _pixabay_hit(101, 1080, 1920, 6)]}
        raise AssertionError(f"unexpected url {url}")

    def download(self, url, dst, headers=None):
        self.downloads.append(url)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42" + b"0" * 100)
        return 112


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "pk-test")
    monkeypatch.setenv("PIXABAY_API_KEY", "px-test")
    monkeypatch.delenv("PIXABAY_KEY", raising=False)


def test_parse_canvas_aliases():
    assert bf.parse_canvas("vertical") == (1080, 1920)
    assert bf.parse_canvas("16:9") == (1920, 1080)
    assert bf.parse_canvas("小红书") == (1080, 1440)
    assert bf.parse_canvas("720x1280") == (720, 1280)
    assert bf.parse_canvas("4:5") == (1080, 1350)
    with pytest.raises(bf.BrollError):
        bf.parse_canvas("weird")


def test_pick_rendition_smallest_that_fills_canvas():
    files = [
        {"width": w, "height": h, "link": f"{w}"}
        for w, h in [(1280, 720), (1920, 1080), (3840, 2160)]
    ]
    assert bf.pick_rendition(files, (1920, 1080))["width"] == 1920
    assert (
        bf.pick_rendition(files, (1080, 1920))["width"] == 3840
    )  # 横版进竖屏要裁: 只有 4K 不用放大
    assert bf.pick_rendition(files, (4000, 4000))["width"] == 3840  # 都不够 -> 最大的


def test_score_prefers_matching_aspect_and_enough_duration():
    canvas = (1080, 1920)
    port = bf.make_record(
        source="t", source_id="p", url="u", width=1080, height=1920, duration=8, license={}
    )
    land = bf.make_record(
        source="t", source_id="l", url="u", width=1920, height=1080, duration=8, license={}
    )
    short = bf.make_record(
        source="t", source_id="s", url="u", width=1080, height=1920, duration=2, license={}
    )
    sp, dp = bf.score_candidate(port, canvas, 5)
    sl, dl = bf.score_candidate(land, canvas, 5)
    ss, ds = bf.score_candidate(short, canvas, 5)
    assert dp["aspect"] == 1.0 and dl["aspect"] < 0.35
    assert sp > sl and sp > ss
    assert ds["duration"] < 0.2


def test_missing_keys_error_is_clear(monkeypatch):
    for k in ("PEXELS_API_KEY", "PIXABAY_API_KEY", "PIXABAY_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(bf.BrollError) as e:
        bf.make_sources()
    assert e.value.code == "missing_api_key" and "PEXELS_API_KEY" in str(e.value)
    with pytest.raises(bf.MissingKeyError) as e:
        bf.make_sources(["pexels"])
    assert "pexels.com/api" in str(e.value)


def test_pixabay_accepts_legacy_env_name(monkeypatch):
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    monkeypatch.setenv("PIXABAY_KEY", "legacy")
    assert bf.PixabaySource().api_key == "legacy"


def test_search_filters_scores_and_caches(keys, tmp_path):
    client = FakeClient()
    srcs = bf.make_sources(client=client, cache_dir=str(tmp_path))
    cands = bf.search("city night", fmt="vertical", sources=srcs, need_duration=5, min_duration=3)
    ids = [c["id"] for c in cands]
    assert "pexels_3" not in ids  # 2s < min_duration
    assert ids[0] in ("pexels_2", "pixabay_101")  # 竖版、够长的排前面
    pex = [c for c in client.calls if "pexels" in c[0]][0]
    assert pex[1]["orientation"] == "portrait" and pex[2]["Authorization"] == "pk-test"
    pix = next(c for c in cands if c["id"] == "pixabay_100")
    assert pix["license"]["name"] == "Pixabay Content License"
    assert pix["author"]["url"] == "https://pixabay.com/users/pxuser-42/"
    assert pix["max_width"] == 1920 and pix["tags"] == ["city", "night", "traffic"]
    n = len(client.calls)
    bf.search("city night", fmt="vertical", sources=srcs, need_duration=5, min_duration=3)
    assert len(client.calls) == n  # 24h 检索缓存
    strict = bf.search("city night", fmt="vertical", sources=srcs, strict_orientation=True)
    assert all(c["orientation"] == "portrait" for c in strict)


def test_download_writes_record_and_reuses_cache(keys, tmp_path):
    client = FakeClient()
    srcs = bf.make_sources(["pexels"], client=client, cache_dir=str(tmp_path))
    c = bf.search("city", fmt="vertical", sources=srcs, limit=1)[0]
    rec = bf.download(c, cache_dir=str(tmp_path), client=client)
    assert os.path.exists(rec["path"]) and os.path.basename(rec["path"]).startswith("pexels_2_")
    side = os.path.splitext(rec["path"])[0] + ".json"
    meta = json.load(open(side, encoding="utf-8"))
    assert (
        meta["license"]["url"] == "https://www.pexels.com/license/"
        and meta["author"]["name"] == "user2"
    )
    assert "score" not in meta
    bf.download(c, cache_dir=str(tmp_path), client=client)
    assert len(client.downloads) == 1
    assert [r["id"] for r in bf.list_cached(str(tmp_path))] == ["pexels_2"]
    assert bf.find_cached("pexels", "2", str(tmp_path))["path"] == rec["path"]


def test_plan_template_from_analysis_merges_short_sentences():
    analysis = {
        "video": {"orientation": "portrait"},
        "transcript": [
            {"start": 0.0, "end": 0.8, "text": "你好"},
            {"start": 0.9, "end": 3.5, "text": "今天去海边"},
            {"start": 3.8, "end": 7.0, "text": "城市夜景很美"},
            {"start": 7.1, "end": 7.6, "text": "对"},
        ],
    }
    plan = bf.plan_template(analysis=analysis)
    assert plan["format"] == "vertical"
    assert [i["text"] for i in plan["items"]] == ["你好 今天去海边", "城市夜景很美 对"]
    assert plan["items"][1]["end"] == 7.6 and plan["items"][0]["keywords"] == []


def test_match_plan_dedups_and_downloads(keys, tmp_path):
    client = FakeClient()
    plan = {
        "format": "vertical",
        "sources": ["pexels", "pixabay"],
        "per_item": 2,
        "items": [
            {
                "id": "L01",
                "text": "城市夜景",
                "start": 0,
                "end": 5,
                "keywords": ["city night", "traffic"],
            },
            {"id": "L02", "text": "同样的城市", "start": 5, "end": 10, "keywords": ["city night"]},
            {"id": "L03", "text": "海", "start": 10, "end": 14, "keywords": ["ocean"]},
            {"id": "L04", "text": "不需要", "keywords": []},
        ],
    }
    res = bf.match_plan(plan, client=client, cache_dir=str(tmp_path), do_download=True)
    items = {i["id"]: i for i in res["items"]}
    assert len(items["L01"]["candidates"]) == 2
    top = items["L01"]["candidates"][0]
    assert set(top["matched_queries"]) == {"city night", "traffic"}  # 两个词都命中
    assert items["L01"]["chosen"]["id"] != items["L02"]["chosen"]["id"]  # 不重复用同一个镜头
    assert items["L03"]["chosen"]["id"] in ("pexels_9", "pixabay_101")
    assert items["L04"]["skipped"] and items["L04"]["chosen"] is None
    assert res["summary"] == {"items": 4, "matched": 3, "downloaded": 3}
    assert all(os.path.exists(items[k]["chosen"]["path"]) for k in ("L01", "L02", "L03"))


def test_cli_json_missing_key(monkeypatch, capsys, tmp_path):
    for k in ("PEXELS_API_KEY", "PIXABAY_API_KEY", "PIXABAY_KEY"):
        monkeypatch.delenv(k, raising=False)
    rc = bf._main(
        ["--cache-dir", str(tmp_path), "search", "city", "--json"], client_factory=FakeClient
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["ok"] is False and out["code"] == "missing_api_key"


def test_cli_search_and_download(keys, capsys, tmp_path):
    rc = bf._main(
        [
            "--cache-dir",
            str(tmp_path),
            "search",
            "city",
            "--source",
            "pixabay",
            "--download",
            "1",
            "--json",
        ],
        client_factory=FakeClient,
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] and out["data"]["candidates"][0]["path"]
    rc = bf._main(
        ["--cache-dir", str(tmp_path), "download", "--source", "pexels", "--id", "77", "--json"],
        client_factory=FakeClient,
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["data"]["record"]["id"] == "pexels_77"
    rc = bf._main(["--cache-dir", str(tmp_path), "cache", "--json"], client_factory=FakeClient)
    out = json.loads(capsys.readouterr().out)
    assert {r["id"] for r in out["data"]["records"]} == {"pixabay_101", "pexels_77"}


def test_plan_template_from_script_uses_broll_keywords():
    script = {
        "format": "横屏",
        "beats": [
            {
                "id": "S01",
                "purpose": "钩子",
                "time": [0, 3],
                "line": "城市的夜",
                "broll_keywords": ["city night", " "],
                "elements": [{"kind": "title", "text": "夜"}],
            },
            {"id": "S02", "purpose": "CTA", "time": {"from": "关注"}},
        ],
    }
    plan = bf.plan_template(script=script)
    assert plan["format"] == "horizontal"
    a, b = plan["items"]
    assert (
        a["keywords"] == ["city night"] and a["start"] == 0 and a["text"] == "城市的夜 / 钩子 / 夜"
    )
    assert b["keywords"] == [] and "start" not in b
