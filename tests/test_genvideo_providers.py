import base64
import hashlib
import hmac
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import broll_fetch as bf  # noqa: E402
import genvideo_providers as gv  # noqa: E402


def test_request_validation():
    with pytest.raises(gv.GenVideoError):
        gv.GenVideoRequest(prompt="x", mode="video2video").validate()
    with pytest.raises(gv.GenVideoError):
        gv.GenVideoRequest(prompt="x", mode="image2video").validate()
    with pytest.raises(gv.GenVideoError):
        gv.GenVideoRequest(prompt="", mode="text2video").validate()
    with pytest.raises(bf.BrollError):
        gv.GenVideoRequest(prompt="x", aspect_ratio="banana").validate()
    gv.GenVideoRequest(prompt="x", aspect_ratio="vertical").validate()


def test_aspect_label():
    assert gv.aspect_label("vertical") == "9:16"
    assert gv.aspect_label("1920x1080") == "16:9"
    assert gv.aspect_label("小红书") == "3:4"


def test_normalize_snaps_to_supported_values(tmp_path):
    p = gv.KlingProvider(cache_dir=str(tmp_path), access_key="a", secret_key="b")
    r = p.normalize(gv.GenVideoRequest(prompt="x", duration=7, aspect_ratio="3:4"))
    assert r.duration == 10.0 and r.aspect_ratio == "9:16"
    assert r.extra["_adjusted"]["aspect_ratio"] == ["3:4", "9:16"]
    r = p.normalize(gv.GenVideoRequest(prompt="x", duration=4, aspect_ratio="horizontal"))
    assert r.duration == 5.0 and r.aspect_ratio == "16:9" and "_adjusted" in r.extra


def test_mock_generate_produces_playable_video_with_shared_record(tmp_path):
    cv2 = pytest.importorskip("cv2")
    p = gv.MockProvider(cache_dir=str(tmp_path), polls_to_finish=2)
    sleeps = []
    rec = p.generate(
        gv.GenVideoRequest(prompt="red fox in snow", duration=1.5, aspect_ratio="9:16"),
        interval=3,
        sleep=sleeps.append,
    )
    assert sleeps == [3, 3]
    assert rec["source"] == "genvideo-mock" and rec["kind"] == "generated_video"
    assert rec["width"] == 360 and rec["height"] == 640 and rec["orientation"] == "portrait"
    assert abs(rec["duration"] - 1.5) < 0.2
    assert rec["extra"]["prompt"] == "red fox in snow"
    cap = cv2.VideoCapture(rec["path"])
    ok, frame = cap.read()
    cap.release()
    assert ok and frame.shape[:2] == (640, 360)
    # 和 B-roll 同一缓存 / 同一记录格式
    assert [r["id"] for r in bf.list_cached(str(tmp_path))] == [rec["id"]]
    for k in (
        "id",
        "source",
        "source_id",
        "url",
        "width",
        "height",
        "duration",
        "license",
        "author",
        "path",
    ):
        assert k in rec


def test_mock_failure_and_persisted_jobs(tmp_path):
    p = gv.MockProvider(cache_dir=str(tmp_path))
    with pytest.raises(gv.GenVideoError) as e:
        p.generate(gv.GenVideoRequest(prompt="boom [fail]"), sleep=lambda s: None)
    assert e.value.code == "generation_failed"
    job = p.submit(gv.GenVideoRequest(prompt="cat"))
    with pytest.raises(gv.GenVideoError) as e:
        p.download(job)
    assert e.value.code == "not_ready"
    p2 = gv.MockProvider(cache_dir=str(tmp_path))  # 另一次命令: 从磁盘读任务
    j = p2.poll(p2.load_job(job.job_id))
    assert j.status == "running"
    j = p2.poll(p2.load_job(job.job_id))
    assert j.status == "succeeded" and j.video_url


def test_jwt_hs256_signature():
    tok = gv.make_jwt_hs256({"iss": "ak", "exp": 2, "nbf": 1}, "sk")
    h, p, s = tok.split(".")
    assert json.loads(base64.urlsafe_b64decode(p + "==")) == {"iss": "ak", "exp": 2, "nbf": 1}
    want = hmac.new(b"sk", f"{h}.{p}".encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)) == want


class FakeKling:
    def __init__(self):
        self.posts, self.gets, self.polls = [], [], 0

    def post_json(self, url, body, headers=None):
        self.posts.append((url, body, headers))
        return {
            "code": 0,
            "message": "SUCCEED",
            "data": {"task_id": "T123", "task_status": "submitted"},
        }

    def get_json(self, url, params=None, headers=None):
        self.gets.append((url, headers))
        self.polls += 1
        if self.polls < 2:
            return {"code": 0, "data": {"task_id": "T123", "task_status": "processing"}}
        return {
            "code": 0,
            "data": {
                "task_id": "T123",
                "task_status": "succeed",
                "task_result": {
                    "videos": [
                        {"id": "v1", "url": "https://cdn.kling.example/T123.mp4", "duration": "5.1"}
                    ]
                },
            },
        }

    def download(self, url, dst, headers=None):
        with open(dst, "wb") as f:
            f.write(b"not-really-a-video")
        return 18


def test_kling_flow_with_mock_http(tmp_path):
    http = FakeKling()
    p = gv.KlingProvider(
        client=http,
        cache_dir=str(tmp_path),
        access_key="ak",
        secret_key="sk",
        base_url="https://api.example.com",
    )
    rec = p.generate(
        gv.GenVideoRequest(
            prompt="山间云海", duration=5, aspect_ratio="9:16", negative_prompt="blurry"
        ),
        sleep=lambda s: None,
    )
    url, body, headers = http.posts[0]
    assert url == "https://api.example.com/v1/videos/text2video"
    assert body == {
        "model_name": "kling-v1-6",
        "prompt": "山间云海",
        "mode": "std",
        "duration": "5",
        "negative_prompt": "blurry",
        "aspect_ratio": "9:16",
    }
    assert (
        headers["Authorization"].startswith("Bearer ") and headers["Authorization"].count(".") == 2
    )
    assert http.gets[0][0] == "https://api.example.com/v1/videos/text2video/T123"
    assert rec["id"] == "genvideo-kling_T123" and rec["duration"] == 5.1
    assert rec["url"] == "https://cdn.kling.example/T123.mp4" and os.path.exists(rec["path"])


def test_kling_image2video_body_uses_base64(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG fake")
    p = gv.KlingProvider(cache_dir=str(tmp_path), access_key="a", secret_key="b")
    body = p.build_body(
        p.normalize(gv.GenVideoRequest(prompt="", mode="image2video", image=str(img)))
    )
    assert (
        body["image"] == base64.b64encode(b"\x89PNG fake").decode() and "aspect_ratio" not in body
    )


def test_kling_missing_keys_and_api_error(monkeypatch, tmp_path):
    monkeypatch.delenv("KLING_ACCESS_KEY", raising=False)
    monkeypatch.delenv("KLING_SECRET_KEY", raising=False)
    p = gv.KlingProvider(client=FakeKling(), cache_dir=str(tmp_path))
    with pytest.raises(gv.GenVideoError) as e:
        p.submit(gv.GenVideoRequest(prompt="x"))
    assert e.value.code == "missing_api_key" and "KLING_ACCESS_KEY" in str(e.value)

    class Bad(FakeKling):
        def post_json(self, url, body, headers=None):
            return {"code": 1102, "message": "balance not enough"}

    p = gv.KlingProvider(client=Bad(), cache_dir=str(tmp_path), access_key="a", secret_key="b")
    with pytest.raises(gv.GenVideoError) as e:
        p.submit(gv.GenVideoRequest(prompt="x"))
    assert e.value.code == "provider_error" and "balance" in str(e.value)


def test_cli_generate_mock_and_providers(tmp_path, capsys):
    pytest.importorskip("cv2")
    rc = gv._main(
        [
            "--cache-dir",
            str(tmp_path),
            "generate",
            "--provider",
            "mock",
            "--prompt",
            "sea",
            "--duration",
            "1",
            "--aspect",
            "16:9",
            "--json",
        ],
        sleep=lambda s: None,
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["data"]["record"]["width"] == 640
    rc = gv._main(["providers", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert {p["name"] for p in out["data"]["providers"]} == {"mock", "kling"}
    rc = gv._main(
        ["--cache-dir", str(tmp_path), "poll", "--provider", "mock", "--job", "nope", "--json"]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["code"] == "not_found"
