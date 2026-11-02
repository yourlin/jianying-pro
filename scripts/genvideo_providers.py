"""
文生视频 / 图生视频 provider 统一接口: submit -> poll -> download, 产物落到 B-roll 同一缓存目录,
用同一种素材记录 (见 broll_fetch.make_record)。

    python genvideo_providers.py providers [--json]
    python genvideo_providers.py generate --provider mock --prompt "a red fox running in snow" --aspect 9:16 --duration 5
    python genvideo_providers.py submit   --provider kling --prompt "..." [--mode image2video --image cat.jpg] [--json]
    python genvideo_providers.py poll     --provider kling --job <job_id> [--download] [--json]

Provider:
  - mock: 不联网, 用 OpenCV 合成一段渐变 + 帧计数的 mp4。用来跑通流程和测试
  - kling: 可灵 (Kling AI) 开放平台。密钥: KLING_ACCESS_KEY + KLING_SECRET_KEY (控制台 -> API 密钥);
           可选 KLING_API_BASE (默认 https://api-beijing.klingai.com, 海外用 https://api-singapore.klingai.com)、
           KLING_MODEL (默认 kling-v1-6)。鉴权是 HS256 JWT (iss = access key, 30 分钟有效)

新增 provider: 继承 GenVideoProvider, 实现 _submit / _poll (download 一般不用改), 加进 PROVIDERS。
任务状态存在 <缓存目录>/_jobs/<provider>_<job_id>.json, submit 和 poll 可以分两次命令执行。
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from broll_fetch import (  # noqa: E402
    BrollError,
    HttpClient,
    cache_file_for,
    default_cache_dir,
    make_record,
    parse_canvas,
    save_record,
)
from utils.cli_protocol import emit_result, make_result  # noqa: E402

MODES = ("text2video", "image2video")
STATUSES = ("queued", "running", "succeeded", "failed")


class GenVideoError(BrollError):
    pass


@dataclass
class GenVideoRequest:
    prompt: str
    mode: str = "text2video"
    image: Optional[str] = None  # image2video: 本地路径或 http(s) URL
    duration: float = 5.0
    aspect_ratio: str = "9:16"
    negative_prompt: str = ""
    seed: Optional[int] = None
    model: Optional[str] = None
    extra: Dict = field(default_factory=dict)

    def validate(self) -> None:
        if self.mode not in MODES:
            raise GenVideoError(f"mode 只能是 {' / '.join(MODES)}: {self.mode}", code="bad_request")
        if self.mode == "text2video" and not self.prompt.strip():
            raise GenVideoError("text2video 需要 prompt", code="bad_request")
        if self.mode == "image2video" and not self.image:
            raise GenVideoError("image2video 需要 image (本地路径或 URL)", code="bad_request")
        if (
            self.image
            and not self.image.startswith(("http://", "https://"))
            and not os.path.exists(self.image)
        ):
            raise GenVideoError(f"找不到图片: {self.image}", code="bad_request")
        if self.duration <= 0:
            raise GenVideoError("duration 必须 > 0", code="bad_request")
        parse_canvas(self.aspect_ratio)  # 看不懂的画幅会抛错


@dataclass
class GenVideoJob:
    provider: str
    job_id: str
    request: GenVideoRequest
    status: str = "queued"
    progress: float = 0.0
    video_url: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    raw: Dict = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.status in ("succeeded", "failed")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GenVideoJob":
        d = dict(d)
        d["request"] = GenVideoRequest(**d["request"])
        return cls(**d)


def aspect_label(aspect: str) -> str:
    """任意画幅写法 -> 'W:H' 最简比 (9:16 / 16:9 / 1:1 / 3:4)。"""
    w, h = parse_canvas(aspect)
    a, b = w, h
    while b:
        a, b = b, a % b
    return f"{w // a}:{h // a}"


class GenVideoProvider:
    """统一接口。子类实现 _submit / _poll, 必要时覆盖 download。"""

    name = ""
    modes: Tuple[str, ...] = MODES
    aspects: Tuple[str, ...] = ("16:9", "9:16", "1:1")
    durations: Tuple[float, ...] = (5.0, 10.0)
    license = {
        "name": "AI generated",
        "url": "",
        "attribution_required": False,
        "note": "按该服务的用户协议使用生成内容",
    }

    def __init__(self, client: Optional[HttpClient] = None, cache_dir: Optional[str] = None):
        self.client = client or HttpClient(timeout=60)
        self.cache_dir = cache_dir or default_cache_dir()

    # -- 参数归一 --
    def normalize(self, req: GenVideoRequest) -> GenVideoRequest:
        """校验并把时长 / 画幅吸附到 provider 支持的值 (调整写进 extra['_adjusted'])。"""
        req.validate()
        if req.mode not in self.modes:
            raise GenVideoError(
                f"{self.name} 不支持 {req.mode} (支持: {', '.join(self.modes)})", code="unsupported"
            )
        adjusted = {}
        ar = aspect_label(req.aspect_ratio)
        if self.aspects and ar not in self.aspects:
            tw, th = parse_canvas(ar)
            best = min(self.aspects, key=lambda a: abs(_ratio(a) - tw / th))
            adjusted["aspect_ratio"] = [ar, best]
            ar = best
        dur = float(req.duration)
        if self.durations and dur not in self.durations:
            best_d = min(self.durations, key=lambda d: (d < dur, abs(d - dur)))  # 优先够长的
            adjusted["duration"] = [dur, best_d]
            dur = best_d
        extra = dict(req.extra)
        if adjusted:
            extra["_adjusted"] = adjusted
        return GenVideoRequest(
            **{**asdict(req), "aspect_ratio": ar, "duration": dur, "extra": extra}
        )

    # -- 任务持久化 --
    def _job_path(self, job_id: str) -> str:
        return os.path.join(self.cache_dir, "_jobs", f"{self.name}_{job_id}.json")

    def save_job(self, job: GenVideoJob) -> str:
        p = self._job_path(job.job_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(job.to_dict(), f, ensure_ascii=False, indent=1)
        return p

    def load_job(self, job_id: str) -> GenVideoJob:
        p = self._job_path(job_id)
        if not os.path.exists(p):
            raise GenVideoError(f"找不到任务 {self.name}/{job_id} ({p})", code="not_found")
        with open(p, "r", encoding="utf-8") as f:
            return GenVideoJob.from_dict(json.load(f))

    # -- 公开接口 --
    def submit(self, req: GenVideoRequest) -> GenVideoJob:
        job = self._submit(self.normalize(req))
        self.save_job(job)
        return job

    def poll(self, job: GenVideoJob) -> GenVideoJob:
        if not job.done:
            job = self._poll(job)
            self.save_job(job)
        return job

    def download(self, job: GenVideoJob) -> dict:
        """任务成功后下载视频到缓存, 写素材记录, 返回记录 (path 为本地文件)。"""
        if job.status != "succeeded" or not job.video_url:
            raise GenVideoError(
                f"任务 {job.job_id} 还没成功 (status={job.status}{', ' + job.error if job.error else ''})",
                code="not_ready",
            )
        rec = self._record(job)
        dst = cache_file_for(rec, self.cache_dir)
        if not (os.path.exists(dst) and os.path.getsize(dst) > 0):
            self._fetch_video(job, dst)
        rec = _probe_into(rec, dst)
        dst2 = cache_file_for(rec, self.cache_dir)  # 实际尺寸可能和请求不同
        if dst2 != dst:
            os.replace(dst, dst2)
            dst = dst2
        side = save_record(rec, dst)
        with open(side, "r", encoding="utf-8") as f:
            return json.load(f)

    def generate(
        self,
        req: GenVideoRequest,
        *,
        timeout: float = 900,
        interval: float = 8,
        sleep: Callable[[float], None] = time.sleep,
    ) -> dict:
        """submit + 轮询到完成 + download。超时抛 GenVideoError(code=timeout), 任务可之后再 poll。"""
        job = self.submit(req)
        t0 = time.time()
        while not job.done:
            if time.time() - t0 > timeout:
                raise GenVideoError(
                    f"任务 {job.job_id} 超时 ({timeout:.0f}s), 稍后用 poll --job {job.job_id} 继续",
                    code="timeout",
                )
            sleep(interval)
            job = self.poll(job)
        if job.status == "failed":
            raise GenVideoError(f"生成失败: {job.error or '未知原因'}", code="generation_failed")
        return self.download(job)

    # -- 子类实现 --
    def _submit(self, req: GenVideoRequest) -> GenVideoJob:
        raise NotImplementedError

    def _poll(self, job: GenVideoJob) -> GenVideoJob:
        raise NotImplementedError

    def _fetch_video(self, job: GenVideoJob, dst: str) -> None:
        self.client.download(job.video_url, dst)

    def _record(self, job: GenVideoJob) -> dict:
        w, h = parse_canvas(job.request.aspect_ratio)
        r = job.request
        return make_record(
            source=f"genvideo-{self.name}",
            source_id=job.job_id,
            url=job.video_url or "",
            width=w,
            height=h,
            duration=r.duration,
            license=self.license,
            author={"name": self.name, "url": ""},
            kind="generated_video",
            query=r.prompt,
            extra={
                "mode": r.mode,
                "prompt": r.prompt,
                "negative_prompt": r.negative_prompt,
                "image": r.image,
                "model": r.model,
                "seed": r.seed,
                "aspect_ratio": r.aspect_ratio,
            },
        )


def _ratio(a: str) -> float:
    x, y = a.split(":")
    return float(x) / float(y)


def _probe_into(rec: dict, path: str) -> dict:
    """用 OpenCV 读真实宽高 / 时长 / 帧率 (读不到就保留请求值)。"""
    try:
        import cv2  # 懒加载: requirements 里有 opencv-python
    except ImportError:
        return rec
    cap = cv2.VideoCapture(path)
    try:
        if not cap.isOpened():
            return rec
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        n = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    finally:
        cap.release()
    if w and h:
        rec = make_record(
            **{
                **_record_kwargs(rec),
                "width": w,
                "height": h,
                "max_width": w,
                "max_height": h,
                "fps": round(fps, 3) if fps else rec.get("fps"),
                "duration": round(n / fps, 3) if fps and n else rec["duration"],
            }
        )
    return rec


def _record_kwargs(rec: dict) -> dict:
    keys = (
        "source",
        "source_id",
        "url",
        "width",
        "height",
        "duration",
        "license",
        "author",
        "kind",
        "page_url",
        "preview",
        "fps",
        "query",
        "tags",
        "max_width",
        "max_height",
        "extra",
    )
    return {k: rec.get(k) for k in keys}


# ---------------------------------------------------------------------------
# Mock: 本地合成, 不联网
# ---------------------------------------------------------------------------


class MockProvider(GenVideoProvider):
    """模拟异步任务: 提交后第 1 次 poll 为 running, 第 polls_to_finish 次为 succeeded。
    prompt 含 "[fail]" 时模拟失败。下载时用 OpenCV 合成真实可播放的 mp4 (短边 360)。"""

    name = "mock"
    durations = ()  # 任意时长
    aspects = ()  # 任意画幅
    license = {
        "name": "mock (本地合成)",
        "url": "",
        "attribution_required": False,
        "note": "测试用占位视频",
    }

    def __init__(self, client=None, cache_dir=None, polls_to_finish: int = 2, fps: int = 12):
        super().__init__(client, cache_dir)
        self.polls_to_finish = polls_to_finish
        self.fps = fps

    def _submit(self, req):
        h = hashlib.sha1(
            json.dumps(asdict(req), sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:12]
        return GenVideoJob(
            provider=self.name,
            job_id=f"{h}{int(time.time() * 1000) % 100000:05d}",
            request=req,
            raw={"polls": 0},
        )

    def _poll(self, job):
        job.raw["polls"] = int(job.raw.get("polls", 0)) + 1
        if "[fail]" in job.request.prompt:
            job.status, job.error = "failed", "mock: prompt 要求失败"
        elif job.raw["polls"] >= self.polls_to_finish:
            job.status, job.progress, job.video_url = "succeeded", 1.0, f"mock://{job.job_id}.mp4"
        else:
            job.status, job.progress = "running", job.raw["polls"] / self.polls_to_finish
        return job

    def _fetch_video(self, job, dst):
        try:
            import cv2
            import numpy as np
        except ImportError as e:  # pragma: no cover - requirements 里都有
            raise GenVideoError(
                "mock provider 需要 opencv-python + numpy", code="missing_dependency"
            ) from e
        w, h = parse_canvas(job.request.aspect_ratio)
        k = 360 / min(w, h)
        w, h = int(round(w * k / 2) * 2), int(round(h * k / 2) * 2)
        n = max(1, int(round(job.request.duration * self.fps)))
        seed = int(hashlib.md5(job.request.prompt.encode("utf-8")).hexdigest()[:6], 16)
        base = np.array([seed % 256, (seed >> 8) % 256, (seed >> 16) % 256], dtype=np.float32)
        tmp = dst + ".part.mp4"
        vw = cv2.VideoWriter(tmp, cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (w, h))
        if not vw.isOpened():
            raise GenVideoError("OpenCV 无法写 mp4 (缺少 mp4v 编码器)", code="missing_dependency")
        ys = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
        xs = np.linspace(0, 1, w, dtype=np.float32)[None, :, None]
        try:
            for i in range(n):
                t = i / max(1, n - 1)
                img = (base * (0.4 + 0.6 * ys) + 120 * xs * t) % 256
                frame = np.ascontiguousarray(np.broadcast_to(img, (h, w, 3)).astype(np.uint8))
                cv2.putText(
                    frame,
                    f"MOCK {i + 1}/{n}",
                    (12, h // 2),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (255, 255, 255),
                    2,
                )
                vw.write(frame)
        finally:
            vw.release()
        os.replace(tmp, dst)


# ---------------------------------------------------------------------------
# 可灵 Kling AI
# ---------------------------------------------------------------------------


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def make_jwt_hs256(payload: dict, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    seg = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    sig = hmac.new(secret.encode("utf-8"), seg.encode("ascii"), hashlib.sha256).digest()
    return seg + "." + _b64url(sig)


class KlingProvider(GenVideoProvider):
    """可灵开放平台 HTTP 适配。
    提交: POST {base}/v1/videos/text2video | image2video
    查询: GET  {base}/v1/videos/<mode>/<task_id>
    返回: {"code": 0, "message": "...", "data": {"task_id", "task_status": submitted|processing|succeed|failed,
           "task_status_msg", "task_result": {"videos": [{"id", "url", "duration"}]}}}
    """

    name = "kling"
    aspects = ("16:9", "9:16", "1:1")
    durations = (5.0, 10.0)
    license = {
        "name": "Kling AI 生成内容",
        "url": "https://klingai.com/",
        "attribution_required": False,
        "note": "按可灵用户协议使用; 商用前确认账号套餐的商用授权",
    }
    STATUS = {
        "submitted": "queued",
        "processing": "running",
        "succeed": "succeeded",
        "failed": "failed",
    }

    def __init__(
        self,
        client=None,
        cache_dir=None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        mode_quality: str = "std",
    ):
        super().__init__(client, cache_dir)
        self.access_key = access_key or os.environ.get("KLING_ACCESS_KEY", "").strip()
        self.secret_key = secret_key or os.environ.get("KLING_SECRET_KEY", "").strip()
        self.base_url = (
            base_url or os.environ.get("KLING_API_BASE") or "https://api-beijing.klingai.com"
        ).rstrip("/")
        self.model = model or os.environ.get("KLING_MODEL") or "kling-v1-6"
        self.mode_quality = mode_quality

    def _auth(self) -> dict:
        if not self.access_key or not self.secret_key:
            raise GenVideoError(
                "可灵需要密钥: 设置环境变量 KLING_ACCESS_KEY 和 KLING_SECRET_KEY "
                "(可灵开放平台控制台 -> API 密钥)。密钥只放环境变量",
                code="missing_api_key",
            )
        now = int(time.time())
        token = make_jwt_hs256(
            {"iss": self.access_key, "exp": now + 1800, "nbf": now - 5}, self.secret_key
        )
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _image_field(image: str) -> str:
        if image.startswith(("http://", "https://")):
            return image
        with open(image, "rb") as f:  # 可灵要纯 base64 (不带 data: 前缀)
            return base64.b64encode(f.read()).decode("ascii")

    def build_body(self, req: GenVideoRequest) -> dict:
        body = {
            "model_name": req.model or self.model,
            "prompt": req.prompt,
            "mode": self.mode_quality,
            "duration": str(int(req.duration)),
        }
        if req.negative_prompt:
            body["negative_prompt"] = req.negative_prompt
        if req.mode == "text2video":
            body["aspect_ratio"] = req.aspect_ratio
        else:
            body["image"] = self._image_field(req.image)
        for k, v in (req.extra or {}).items():
            if not k.startswith("_"):
                body[k] = v
        return body

    def _check(self, resp: dict) -> dict:
        if not isinstance(resp, dict) or resp.get("code") not in (0, "0"):
            msg = (resp or {}).get("message") if isinstance(resp, dict) else str(resp)
            raise GenVideoError(
                f"可灵返回错误: code={(resp or {}).get('code')} {msg}", code="provider_error"
            )
        return resp.get("data") or {}

    def _apply(self, job: GenVideoJob, data: dict) -> GenVideoJob:
        st = data.get("task_status", "submitted")
        job.status = self.STATUS.get(st, "running")
        job.raw = {"task_status": st, "task_status_msg": data.get("task_status_msg", "")}
        if job.status == "failed":
            job.error = data.get("task_status_msg") or "failed"
        vids = (data.get("task_result") or {}).get("videos") or []
        if job.status == "succeeded":
            if not vids or not vids[0].get("url"):
                job.status, job.error = "failed", "任务成功但没有返回视频地址"
            else:
                job.video_url = vids[0]["url"]
                job.progress = 1.0
                if vids[0].get("duration"):
                    job.request.duration = float(vids[0]["duration"])
        return job

    def _submit(self, req):
        resp = self.client.post_json(
            f"{self.base_url}/v1/videos/{req.mode}", self.build_body(req), self._auth()
        )
        data = self._check(resp)
        if not data.get("task_id"):
            raise GenVideoError(f"可灵没有返回 task_id: {resp}", code="provider_error")
        return self._apply(
            GenVideoJob(provider=self.name, job_id=str(data["task_id"]), request=req), data
        )

    def _poll(self, job):
        resp = self.client.get_json(
            f"{self.base_url}/v1/videos/{job.request.mode}/{job.job_id}", None, self._auth()
        )
        return self._apply(job, self._check(resp))


PROVIDERS: Dict[str, type] = {"mock": MockProvider, "kling": KlingProvider}


def get_provider(name: str, **kw) -> GenVideoProvider:
    cls = PROVIDERS.get((name or "").lower())
    if not cls:
        raise GenVideoError(
            f"未知 provider: {name} (可选: {', '.join(PROVIDERS)})", code="bad_provider"
        )
    return cls(**kw)


def list_providers() -> List[dict]:
    out = []
    for n, cls in PROVIDERS.items():
        env = {"kling": ["KLING_ACCESS_KEY", "KLING_SECRET_KEY"]}.get(n, [])
        out.append(
            {
                "name": n,
                "modes": list(cls.modes),
                "aspects": list(cls.aspects) or "any",
                "durations": list(cls.durations) or "any",
                "env": env,
                "configured": all(os.environ.get(e) for e in env),
            }
        )
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _req_from_args(a) -> GenVideoRequest:
    return GenVideoRequest(
        prompt=a.prompt or "",
        mode=a.mode,
        image=a.image,
        duration=a.duration,
        aspect_ratio=a.aspect,
        negative_prompt=a.negative or "",
        seed=a.seed,
        model=a.model,
    )


def _main(
    argv=None,
    client_factory: Callable[[], HttpClient] = lambda: HttpClient(timeout=120),
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    ap = argparse.ArgumentParser(description="文生 / 图生视频 provider (mock / kling)")
    ap.add_argument("--cache-dir", help="缓存目录 (默认 ~/.cache/jianying-pro/broll)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("providers")
    p.add_argument("--json", action="store_true")
    for c in ("submit", "generate"):
        p = sub.add_parser(c)
        p.add_argument("--provider", default="mock")
        p.add_argument("--prompt", default="")
        p.add_argument("--mode", default="text2video", choices=MODES)
        p.add_argument("--image")
        p.add_argument("--duration", type=float, default=5.0)
        p.add_argument(
            "--aspect", default="9:16", help="9:16 / 16:9 / 1:1 / vertical / horizontal ..."
        )
        p.add_argument("--negative")
        p.add_argument("--seed", type=int)
        p.add_argument("--model")
        p.add_argument("--json", action="store_true")
        if c == "generate":
            p.add_argument("--timeout", type=float, default=900)
            p.add_argument("--interval", type=float, default=8)
    p = sub.add_parser("poll")
    p.add_argument("--provider", default="mock")
    p.add_argument("--job", required=True)
    p.add_argument("--download", action="store_true", help="成功后下载")
    p.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    as_json = getattr(a, "json", False)
    try:
        if a.cmd == "providers":
            data = {"providers": list_providers()}
            if not as_json:
                for p in data["providers"]:
                    print(
                        f"  {p['name']}: {'/'.join(p['modes'])} 画幅 {p['aspects']} 时长 {p['durations']} "
                        f"{'✅ 已配置' if p['configured'] else '❌ 缺 ' + ' '.join(p['env'])}"
                    )
        else:
            prov = get_provider(a.provider, client=client_factory(), cache_dir=a.cache_dir)
            if a.cmd == "submit":
                job = prov.submit(_req_from_args(a))
                data = {"job": job.to_dict()}
                if not as_json:
                    print(
                        f"✅ 已提交 {prov.name}/{job.job_id} ({job.status}); 查询: poll --provider {prov.name} "
                        f"--job {job.job_id} --download"
                    )
            elif a.cmd == "generate":
                rec = prov.generate(
                    _req_from_args(a), timeout=a.timeout, interval=a.interval, sleep=sleep
                )
                data = {"record": rec}
                if not as_json:
                    print(
                        f"✅ {rec['id']} {rec['width']}x{rec['height']} {rec['duration']}s -> {rec['path']}"
                    )
            else:
                job = prov.poll(prov.load_job(a.job))
                data = {"job": job.to_dict()}
                if a.download and job.status == "succeeded":
                    data["record"] = prov.download(job)
                if not as_json:
                    print(
                        f"{prov.name}/{job.job_id}: {job.status}"
                        + (f" ({job.error})" if job.error else "")
                        + (f" -> {data['record']['path']}" if data.get("record") else "")
                    )
    except BrollError as e:
        if as_json:
            emit_result(make_result(False, e.code, str(e)), True)
        else:
            print(f"❌ {e}", file=sys.stderr)
        return 2
    if as_json:
        emit_result(make_result(True, "ok", "", data), True)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
