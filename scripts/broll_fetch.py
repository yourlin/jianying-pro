"""
B-roll 视频供给: 从 Pexels / Pixabay 免费视频库按关键词搜索、过滤、打分、下载缓存, 并按旁白分句匹配候选。

    python broll_fetch.py search "city night timelapse" --format vertical --min-dur 4 [--download 1] [--json]
    python broll_fetch.py plan-template --analysis analysis.json -o broll_plan.json     # 生成分句计划骨架
    python broll_fetch.py match broll_plan.json --top 3 [--download] [-o broll_matches.json] [--json]
    python broll_fetch.py download --source pexels --id 856195 [--format vertical]
    python broll_fetch.py cache [--json]                                                # 列出已缓存素材

密钥 (环境变量, 绝不写进文件):
  - PEXELS_API_KEY: https://www.pexels.com/api/ 注册即得
  - PIXABAY_API_KEY (兼容 pixabay-images skill 的 PIXABAY_KEY): https://pixabay.com/api/docs/

语义匹配分工: 中文句子 -> 英文检索关键词由调用方 Agent 翻译后写进 plan JSON 的 keywords (英文素材量远大于中文);
本脚本只负责检索、过滤、按画幅匹配度 / 时长 / 分辨率 / 相关度打分。

缓存: ~/.cache/jianying-pro/broll/ (可用环境变量 JY_BROLL_CACHE 改)。每个素材一个视频文件
<来源>_<ID>_<宽>x<高>.mp4 + 同名 .json 素材记录 (来源 / 作者 / 许可证 / 尺寸 / 时长 / 检索词)。
genvideo_providers.py 生成的视频也落在这里, 用同一个素材记录格式。
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from utils.cli_protocol import emit_result, make_result  # noqa: E402

USER_AGENT = "Mozilla/5.0 (jianying-pro broll_fetch)"
SEARCH_TTL_S = 24 * 3600  # Pixabay 要求检索结果缓存 24 小时, Pexels 也按这个来, 减少配额消耗
RECORD_VERSION = 1

# 画幅别名 -> 画布尺寸 (和 references/formats 一致)
FORMAT_CANVAS = {
    "vertical": (1080, 1920),
    "horizontal": (1920, 1080),
    "portrait_3x4": (1080, 1440),
    "square": (1080, 1080),
}
FORMAT_ALIASES = {
    "9:16": "vertical",
    "16:9": "horizontal",
    "3:4": "portrait_3x4",
    "1:1": "square",
    "竖屏": "vertical",
    "横屏": "horizontal",
    "小红书": "portrait_3x4",
    "方形": "square",
}

LICENSES = {
    "pexels": {
        "name": "Pexels License",
        "url": "https://www.pexels.com/license/",
        "attribution_required": False,
        "note": "免费商用, 可修改; 不能原样转售或作为同类图库分发。署名非必须但建议",
    },
    "pixabay": {
        "name": "Pixabay Content License",
        "url": "https://pixabay.com/service/license-summary/",
        "attribution_required": False,
        "note": "免费商用, 可修改; 不能原样作为独立素材分发或售卖",
    },
}


class BrollError(RuntimeError):
    """带错误码的失败, CLI 用 code 输出 {ok: false, code}。"""

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


class MissingKeyError(BrollError):
    def __init__(self, source: str, env_names: Iterable[str], signup: str):
        names = " / ".join(env_names)
        super().__init__(
            f"{source} 需要 API key: 设置环境变量 {names} (免费注册: {signup})。"
            f"key 只放环境变量, 不要写进剧本 / 计划 / 提交的文件",
            code="missing_api_key",
        )
        self.source = source


# ---------------------------------------------------------------------------
# 路径与画幅
# ---------------------------------------------------------------------------


def default_cache_dir() -> str:
    env = os.environ.get("JY_BROLL_CACHE")
    if env:
        return os.path.expanduser(env)
    return os.path.join(os.path.expanduser("~"), ".cache", "jianying-pro", "broll")


def parse_canvas(target: Optional[str]) -> Tuple[int, int]:
    """画幅 -> (宽, 高)。接受 vertical / 9:16 / 竖屏 / 1080x1920; 也读 references/formats/<id>.json。"""
    if not target:
        return FORMAT_CANVAS["vertical"]
    t = str(target).strip()
    key = FORMAT_ALIASES.get(t, t)
    if key in FORMAT_CANVAS:
        return FORMAT_CANVAS[key]
    m = re.fullmatch(r"(\d+)\s*[x×*]\s*(\d+)", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)", t)
    if m:  # 任意比例: 短边按 1080
        a, b = float(m.group(1)), float(m.group(2))
        return (round(1080 * a / b), 1080) if a >= b else (1080, round(1080 * b / a))
    path = os.path.join(SKILL, "references", "formats", f"{key}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            c = json.load(f).get("canvas") or {}
        if c.get("width") and c.get("height"):
            return int(c["width"]), int(c["height"])
    raise BrollError(
        f"看不懂的画幅: {target} (可用 vertical / horizontal / portrait_3x4 / square / 9:16 / 1080x1920)",
        code="bad_format",
    )


def orientation(w: float, h: float) -> str:
    """和 analyze_footage.orientation 一致: 宽高比 0.9–1.1 算方形。"""
    if not w or not h:
        return "unknown"
    r = w / h
    return "square" if 0.9 <= r <= 1.1 else ("portrait" if r < 1 else "landscape")


# ---------------------------------------------------------------------------
# HTTP (标准库实现; 测试里替换成假客户端)
# ---------------------------------------------------------------------------


class HttpClient:
    """最小 HTTP 客户端: get_json / post_json / download。失败统一抛 BrollError(code=http_<状态码>)。"""

    def __init__(self, timeout: float = 30.0, user_agent: str = USER_AGENT, retries: int = 2):
        self.timeout = timeout
        self.user_agent = user_agent
        self.retries = retries

    def _open(self, req: urllib.request.Request):
        last: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                return urllib.request.urlopen(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    last = e
                    continue
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:300]
                except Exception:  # noqa: BLE001 - 读错误体失败不影响报错
                    pass
                hint = {
                    401: "密钥无效或过期",
                    403: "无权限 / 被拒绝",
                    429: "超出配额, 稍后再试",
                }.get(e.code, "")
                raise BrollError(
                    f"HTTP {e.code} {hint}: {req.full_url.split('?')[0]} {body}".strip(),
                    code=f"http_{e.code}",
                ) from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                if attempt < self.retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
        raise BrollError(f"网络请求失败: {req.full_url.split('?')[0]} ({last})", code="network")

    def _headers(self, headers: Optional[dict]) -> dict:
        h = {"User-Agent": self.user_agent, "Accept": "application/json"}
        h.update(headers or {})
        return h

    def get_json(
        self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None
    ) -> dict:
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._headers(headers), method="GET")
        with self._open(req) as r:
            return json.loads(r.read().decode("utf-8"))

    def post_json(self, url: str, body: dict, headers: Optional[dict] = None) -> dict:
        h = self._headers(headers)
        h["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=h, method="POST")
        with self._open(req) as r:
            return json.loads(r.read().decode("utf-8"))

    def download(self, url: str, dst: str, headers: Optional[dict] = None) -> int:
        """下载到 dst (先写 .part 再改名, 中断不会留下半个文件)。返回字节数。"""
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        h = {"User-Agent": self.user_agent}
        h.update(headers or {})
        req = urllib.request.Request(url, headers=h, method="GET")
        tmp = dst + ".part"
        n = 0
        with self._open(req) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                n += len(chunk)
        if n == 0:
            os.remove(tmp)
            raise BrollError(f"下载到空文件: {url.split('?')[0]}", code="empty_download")
        os.replace(tmp, dst)
        return n


# ---------------------------------------------------------------------------
# 素材记录 (B-roll 与生成视频共用)
# ---------------------------------------------------------------------------


def make_record(
    *,
    source: str,
    source_id: str,
    url: str,
    width: int,
    height: int,
    duration: float,
    license: dict,
    author: Optional[dict] = None,
    kind: str = "video",
    page_url: str = "",
    preview: str = "",
    fps: Optional[float] = None,
    query: str = "",
    tags: Optional[List[str]] = None,
    max_width: Optional[int] = None,
    max_height: Optional[int] = None,
    extra: Optional[dict] = None,
) -> dict:
    """统一素材记录。source 例: pexels / pixabay / genvideo-mock / genvideo-kling。"""
    w, h = int(width or 0), int(height or 0)
    rec = {
        "record_version": RECORD_VERSION,
        "id": f"{source}_{source_id}",
        "source": source,
        "source_id": str(source_id),
        "kind": kind,
        "url": url,
        "page_url": page_url,
        "preview": preview,
        "width": w,
        "height": h,
        "max_width": int(max_width or w),
        "max_height": int(max_height or h),
        "orientation": orientation(w, h),
        "duration": round(float(duration or 0), 3),
        "fps": fps,
        "license": dict(license or {}),
        "author": dict(author or {}),
        "query": query,
        "tags": list(tags or []),
        "path": None,
    }
    if extra:
        rec["extra"] = extra
    return rec


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(s)).strip("-") or "x"


def cache_file_for(rec: dict, cache_dir: Optional[str] = None, ext: str = ".mp4") -> str:
    cache_dir = cache_dir or default_cache_dir()
    name = f"{_safe(rec['source'])}_{_safe(rec['source_id'])}_{rec['width']}x{rec['height']}{ext}"
    return os.path.join(cache_dir, name)


def save_record(rec: dict, media_path: str) -> str:
    """把素材记录写到视频旁边的同名 .json。返回 .json 路径。"""
    rec = dict(rec)
    rec["path"] = os.path.abspath(media_path)
    rec.setdefault("fetched_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    side = os.path.splitext(media_path)[0] + ".json"
    with open(side, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    return side


def list_cached(cache_dir: Optional[str] = None) -> List[dict]:
    """列出缓存里所有 (视频文件仍存在的) 素材记录。"""
    cache_dir = cache_dir or default_cache_dir()
    out = []
    if not os.path.isdir(cache_dir):
        return out
    for fn in sorted(os.listdir(cache_dir)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(cache_dir, fn), "r", encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("path") and os.path.exists(rec["path"]):
            out.append(rec)
    return out


def find_cached(source: str, source_id: str, cache_dir: Optional[str] = None) -> Optional[dict]:
    for rec in list_cached(cache_dir):
        if rec.get("source") == source and str(rec.get("source_id")) == str(source_id):
            return rec
    return None


# ---------------------------------------------------------------------------
# 选版本与打分
# ---------------------------------------------------------------------------


def fill_scale(w: float, h: float, canvas: Tuple[int, int]) -> float:
    """素材铺满画布 (裁切填充) 需要的放大倍数; > 1 表示要放大 (会糊)。"""
    if not w or not h:
        return float("inf")
    return max(canvas[0] / w, canvas[1] / h)


def pick_rendition(files: List[dict], canvas: Tuple[int, int]) -> Optional[dict]:
    """在多个分辨率版本里选: 铺满画布不需要放大的最小 mp4; 都不够就选最大的。files: [{width, height, link, ...}]。"""
    files = [f for f in files if f.get("link") and f.get("width") and f.get("height")]
    if not files:
        return None
    mp4 = [f for f in files if "mp4" in str(f.get("file_type", "video/mp4")).lower()]
    files = mp4 or files
    enough = [f for f in files if fill_scale(f["width"], f["height"], canvas) <= 1.0001]
    if enough:
        return min(enough, key=lambda f: f["width"] * f["height"])
    return max(files, key=lambda f: f["width"] * f["height"])


def score_candidate(
    rec: dict,
    canvas: Tuple[int, int],
    need_duration: Optional[float] = None,
    rank: int = 0,
    hits: int = 1,
) -> Tuple[float, dict]:
    """打分 (0–1)。
    - aspect: 裁成目标画幅后保留的画面比例 (同方向 = 1, 横版进竖屏约 0.32)
    - duration: 够长 = 1 (太长略扣); 不够长按 (实际/需要)^2 扣
    - resolution: 铺满画布不需要放大 = 1, 需要放大 s 倍得 1/s
    - relevance: 检索结果排名 (API 的相关度) + 多个关键词同时命中的加成
    """
    w, h = rec.get("max_width") or rec["width"], rec.get("max_height") or rec["height"]
    ta = canvas[0] / canvas[1]
    ar = (w / h) if h else ta
    aspect = min(ar / ta, ta / ar) if ar and ta else 0.0
    d = float(rec.get("duration") or 0)
    if need_duration and need_duration > 0:
        if d >= need_duration:
            dur = 1.0 - min(0.2, (d - need_duration) / (need_duration * 20))
        else:
            dur = (d / need_duration) ** 2 * 0.8
    else:
        dur = 1.0
    s = fill_scale(w, h, canvas)
    res = 1.0 if s <= 1 else (1.0 / s if math.isfinite(s) else 0.0)
    relevance = min(1.0, 1.0 / (1.0 + 0.12 * rank) + 0.05 * max(0, hits - 1))
    total = 0.4 * aspect + 0.25 * dur + 0.15 * res + 0.2 * relevance
    detail = {
        "aspect": round(aspect, 3),
        "duration": round(dur, 3),
        "resolution": round(res, 3),
        "relevance": round(relevance, 3),
    }
    return round(total, 4), detail


def passes_filters(
    rec: dict,
    *,
    orientation_want: Optional[str] = None,
    min_duration: Optional[float] = None,
    max_duration: Optional[float] = None,
    min_short_side: Optional[int] = None,
) -> bool:
    if orientation_want and rec.get("orientation") != orientation_want:
        return False
    d = float(rec.get("duration") or 0)
    if min_duration and d < min_duration:
        return False
    if max_duration and d > max_duration:
        return False
    if min_short_side:
        short = min(rec.get("max_width") or rec["width"], rec.get("max_height") or rec["height"])
        if short < min_short_side:
            return False
    return True


# ---------------------------------------------------------------------------
# 视频源
# ---------------------------------------------------------------------------


class VideoSource:
    name = ""
    env_names: Tuple[str, ...] = ()
    signup = ""

    def __init__(
        self,
        api_key: Optional[str] = None,
        client: Optional[HttpClient] = None,
        cache_dir: Optional[str] = None,
    ):
        self._key = api_key
        self.client = client or HttpClient()
        self.cache_dir = cache_dir or default_cache_dir()

    @property
    def api_key(self) -> str:
        if self._key:
            return self._key
        for n in self.env_names:
            v = os.environ.get(n, "").strip()
            if v:
                return v
        raise MissingKeyError(self.name, self.env_names, self.signup)

    def has_key(self) -> bool:
        try:
            return bool(self.api_key)
        except MissingKeyError:
            return False

    # -- 检索结果缓存 24h --
    def _cached_get(self, url: str, params: dict, headers: Optional[dict] = None) -> dict:
        key = hashlib.sha1(
            (url + json.dumps(params, sort_keys=True, ensure_ascii=False)).encode("utf-8")
        ).hexdigest()[:20]
        d = os.path.join(self.cache_dir, "_search")
        p = os.path.join(d, f"{self.name}_{key}.json")
        if os.path.exists(p) and time.time() - os.path.getmtime(p) < SEARCH_TTL_S:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                pass
        data = self.client.get_json(url, params, headers)
        os.makedirs(d, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return data

    def search(
        self,
        query: str,
        canvas: Tuple[int, int],
        *,
        per_page: int = 15,
        orientation_hint: Optional[str] = None,
        min_duration: Optional[float] = None,
        max_duration: Optional[float] = None,
        page: int = 1,
    ) -> List[dict]:
        raise NotImplementedError

    def get(self, source_id: str, canvas: Tuple[int, int]) -> dict:
        raise NotImplementedError


class PexelsSource(VideoSource):
    """Pexels Video API: https://www.pexels.com/api/documentation/#videos-search"""

    name = "pexels"
    env_names = ("PEXELS_API_KEY",)
    signup = "https://www.pexels.com/api/"
    BASE = "https://api.pexels.com/videos"
    ORIENT = {"landscape": "landscape", "portrait": "portrait", "square": "square"}

    def _headers(self) -> dict:
        return {"Authorization": self.api_key}

    def to_record(self, v: dict, canvas: Tuple[int, int], query: str = "") -> Optional[dict]:
        files = [
            {
                "width": f.get("width"),
                "height": f.get("height"),
                "link": f.get("link"),
                "file_type": f.get("file_type", ""),
                "fps": f.get("fps"),
                "quality": f.get("quality"),
            }
            for f in v.get("video_files") or []
        ]
        best = pick_rendition(files, canvas)
        if not best:
            return None
        user = v.get("user") or {}
        tags = [t for t in (v.get("tags") or []) if isinstance(t, str)]
        return make_record(
            source="pexels",
            source_id=str(v["id"]),
            url=best["link"],
            width=best["width"],
            height=best["height"],
            max_width=v.get("width") or best["width"],
            max_height=v.get("height") or best["height"],
            duration=float(v.get("duration") or 0),
            fps=best.get("fps"),
            page_url=v.get("url", ""),
            preview=v.get("image", ""),
            license=LICENSES["pexels"],
            author={"name": user.get("name", ""), "url": user.get("url", "")},
            query=query,
            tags=tags,
        )

    def search(
        self,
        query,
        canvas,
        *,
        per_page=15,
        orientation_hint=None,
        min_duration=None,
        max_duration=None,
        page=1,
    ):
        params: Dict[str, Any] = {
            "query": query,
            "per_page": max(1, min(80, per_page)),
            "page": page,
        }
        o = orientation_hint or orientation(*canvas)
        if o in self.ORIENT:
            params["orientation"] = self.ORIENT[o]
        if min_duration:
            params["min_duration"] = int(math.floor(min_duration))
        if max_duration:
            params["max_duration"] = int(math.ceil(max_duration))
        data = self._cached_get(f"{self.BASE}/search", params, self._headers())
        out = []
        for v in data.get("videos") or []:
            rec = self.to_record(v, canvas, query)
            if rec:
                out.append(rec)
        return out

    def get(self, source_id, canvas):
        v = self.client.get_json(f"{self.BASE}/videos/{source_id}", None, self._headers())
        rec = self.to_record(v, canvas)
        if not rec:
            raise BrollError(f"pexels 视频 {source_id} 没有可下载的版本", code="not_found")
        return rec


class PixabaySource(VideoSource):
    """Pixabay Video API: https://pixabay.com/api/docs/#api_search_videos"""

    name = "pixabay"
    env_names = ("PIXABAY_API_KEY", "PIXABAY_KEY")
    signup = "https://pixabay.com/api/docs/"
    BASE = "https://pixabay.com/api/videos/"

    def to_record(self, hit: dict, canvas: Tuple[int, int], query: str = "") -> Optional[dict]:
        vids = hit.get("videos") or {}
        files = []
        for q in ("large", "medium", "small", "tiny"):
            f = vids.get(q) or {}
            if f.get("url") and f.get("width") and f.get("height"):
                files.append(
                    {
                        "width": f["width"],
                        "height": f["height"],
                        "link": f["url"],
                        "file_type": "video/mp4",
                        "quality": q,
                        "thumbnail": f.get("thumbnail", ""),
                    }
                )
        best = pick_rendition(files, canvas)
        if not best:
            return None
        biggest = max(files, key=lambda f: f["width"] * f["height"])
        thumb = next((f.get("thumbnail") for f in files if f.get("thumbnail")), "")
        user, uid = hit.get("user", ""), hit.get("user_id", "")
        return make_record(
            source="pixabay",
            source_id=str(hit["id"]),
            url=best["link"],
            width=best["width"],
            height=best["height"],
            max_width=biggest["width"],
            max_height=biggest["height"],
            duration=float(hit.get("duration") or 0),
            page_url=hit.get("pageURL", ""),
            preview=thumb,
            license=LICENSES["pixabay"],
            author={
                "name": user,
                "url": f"https://pixabay.com/users/{user}-{uid}/" if user else "",
            },
            query=query,
            tags=[t.strip() for t in str(hit.get("tags", "")).split(",") if t.strip()],
        )

    def search(
        self,
        query,
        canvas,
        *,
        per_page=15,
        orientation_hint=None,
        min_duration=None,
        max_duration=None,
        page=1,
    ):
        # Pixabay 视频接口没有方向 / 时长参数, 在本地过滤
        params = {
            "key": self.api_key,
            "q": query[:100],
            "per_page": max(3, min(200, per_page)),
            "page": page,
            "safesearch": "true",
            "order": "popular",
            "video_type": "film",
        }
        data = self._cached_get(self.BASE, params)
        out = []
        for hit in data.get("hits") or []:
            rec = self.to_record(hit, canvas, query)
            if rec:
                out.append(rec)
        return out

    def get(self, source_id, canvas):
        data = self.client.get_json(self.BASE, {"key": self.api_key, "id": source_id})
        hits = data.get("hits") or []
        rec = self.to_record(hits[0], canvas) if hits else None
        if not rec:
            raise BrollError(f"pixabay 视频 {source_id} 不存在或没有可下载的版本", code="not_found")
        return rec


SOURCES = {"pexels": PexelsSource, "pixabay": PixabaySource}


def make_sources(
    names: Optional[Iterable[str]] = None,
    client: Optional[HttpClient] = None,
    cache_dir: Optional[str] = None,
    require_key: bool = True,
) -> List[VideoSource]:
    """按名称建视频源; 默认用所有已配置密钥的源。一个都没有时抛 MissingKeyError。"""
    names = [n.strip().lower() for n in (names or SOURCES.keys()) if n.strip()]
    bad = [n for n in names if n not in SOURCES]
    if bad:
        raise BrollError(
            f"未知视频源: {', '.join(bad)} (可选: {', '.join(SOURCES)})", code="bad_source"
        )
    srcs = [SOURCES[n](client=client, cache_dir=cache_dir) for n in names]
    ready = [s for s in srcs if s.has_key()]
    if require_key and not ready:
        if len(srcs) == 1:
            _ = srcs[0].api_key  # 抛出带注册地址的 MissingKeyError
        raise BrollError(
            "没有可用的视频源密钥: 设置 PEXELS_API_KEY 和 / 或 PIXABAY_API_KEY 环境变量 "
            "(免费注册: https://www.pexels.com/api/ , https://pixabay.com/api/docs/)",
            code="missing_api_key",
        )
    return ready


# ---------------------------------------------------------------------------
# 检索 / 下载
# ---------------------------------------------------------------------------


def search(
    query: str,
    *,
    fmt: str = "vertical",
    sources: Optional[List[VideoSource]] = None,
    need_duration: Optional[float] = None,
    min_duration: Optional[float] = None,
    max_duration: Optional[float] = None,
    min_short_side: Optional[int] = None,
    strict_orientation: bool = False,
    per_source: int = 15,
    limit: int = 10,
    exclude_ids: Iterable[str] = (),
) -> List[dict]:
    """检索 + 过滤 + 打分, 返回按 score 降序的候选 (每个是素材记录 + score / score_detail)。"""
    canvas = parse_canvas(fmt)
    sources = sources if sources is not None else make_sources()
    want = orientation(*canvas) if strict_orientation else None
    exclude = set(exclude_ids)
    out = []
    for src in sources:
        recs = src.search(
            query, canvas, per_page=per_source, min_duration=min_duration, max_duration=max_duration
        )
        for rank, rec in enumerate(recs):
            if rec["id"] in exclude:
                continue
            if not passes_filters(
                rec,
                orientation_want=want,
                min_duration=min_duration,
                max_duration=max_duration,
                min_short_side=min_short_side,
            ):
                continue
            score, detail = score_candidate(rec, canvas, need_duration, rank)
            out.append({**rec, "score": score, "score_detail": detail})
    out.sort(key=lambda r: -r["score"])
    return out[:limit] if limit else out


def download(
    rec: dict, *, cache_dir: Optional[str] = None, client: Optional[HttpClient] = None
) -> dict:
    """下载素材到缓存 (已缓存则直接返回), 写素材记录, 返回带 path 的记录。"""
    cache_dir = cache_dir or default_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    dst = cache_file_for(rec, cache_dir)
    side = os.path.splitext(dst)[0] + ".json"
    if os.path.exists(dst) and os.path.getsize(dst) > 0 and os.path.exists(side):
        with open(side, "r", encoding="utf-8") as f:
            return json.load(f)
    (client or HttpClient(timeout=120)).download(rec["url"], dst)
    clean = {k: v for k, v in rec.items() if k not in ("score", "score_detail")}
    save_record(clean, dst)
    with open(side, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 分句计划 -> 候选
# ---------------------------------------------------------------------------


def _merge_sentences(segs: List[dict], min_len: float) -> List[dict]:
    """太短的句子并到下一句, B-roll 一个镜头至少 min_len 秒。"""
    out: List[dict] = []
    buf: Optional[dict] = None
    for s in segs:
        text = str(s.get("text", "")).strip()
        if not text:
            continue
        if buf is None:
            buf = {"start": float(s["start"]), "end": float(s["end"]), "text": text}
        else:
            buf["end"] = float(s["end"])
            buf["text"] = (buf["text"] + " " + text).strip()
        if buf["end"] - buf["start"] >= min_len:
            out.append(buf)
            buf = None
    if buf:
        if out and buf["end"] - buf["start"] < min_len:
            out[-1]["end"] = buf["end"]
            out[-1]["text"] = out[-1]["text"] + " " + buf["text"]
        else:
            out.append(buf)
    return out


def plan_template(
    *,
    analysis: Optional[dict] = None,
    script: Optional[dict] = None,
    fmt: Optional[str] = None,
    min_len: float = 2.0,
) -> dict:
    """生成分句计划骨架 (keywords 留空, 由调用方 Agent 按句意填英文关键词)。

    analysis: analyze_footage.py 的 analysis.json (用 transcript 分句);
    script: script_schema 格式的剧本 (每个 beat 一项, 文本取 line / purpose / 元素文字)。
    """
    items = []
    if analysis and analysis.get("transcript"):
        for i, s in enumerate(_merge_sentences(analysis["transcript"], min_len)):
            items.append(
                {
                    "id": f"L{i + 1:02d}",
                    "text": s["text"],
                    "start": round(s["start"], 3),
                    "end": round(s["end"], 3),
                    "keywords": [],
                }
            )
        fmt = fmt or (
            "vertical" if (analysis.get("video") or {}).get("orientation") == "portrait" else None
        )
    elif script:
        for i, b in enumerate(script.get("beats") or []):
            texts = [b.get("line", ""), b.get("purpose", "")]
            texts += [str(e.get("text") or e.get("label") or "") for e in b.get("elements") or []]
            t = b.get("time")
            kws = [k for k in b.get("broll_keywords") or [] if isinstance(k, str) and k.strip()]
            it = {
                "id": b.get("id", f"S{i + 1:02d}"),
                "text": " / ".join(x for x in texts if x),
                "keywords": kws,
            }  # hook_writer 产出的剧本可以直接带 broll_keywords
            if isinstance(t, (list, tuple)) and len(t) == 2:
                it.update(start=float(t[0]), end=float(t[1]))
            items.append(it)
        fmt = fmt or script.get("format")
    else:
        raise BrollError(
            "plan-template 需要带 transcript 的 analysis.json 或带 beats 的剧本", code="bad_input"
        )
    return {
        "_说明": "每项填 keywords: 2–4 个英文检索词 (具体名词 + 场景, 如 'city night traffic timelapse');"
        " 不需要 B-roll 的句子删掉或 keywords 留空",
        "format": FORMAT_ALIASES.get(fmt or "vertical", fmt or "vertical"),
        "sources": ["pexels", "pixabay"],
        "per_item": 3,
        "items": items,
    }


def match_plan(
    plan: dict,
    *,
    sources: Optional[List[VideoSource]] = None,
    top: Optional[int] = None,
    fmt: Optional[str] = None,
    do_download: bool = False,
    cache_dir: Optional[str] = None,
    client: Optional[HttpClient] = None,
    min_short_side: Optional[int] = None,
) -> dict:
    """按计划逐句检索: 每句的多个关键词合并去重 (多个词命中加分), 按画幅 / 时长打分取 top;
    chosen 是该句首选 (尽量不和前面句子重复)。do_download=True 时下载每句的 chosen。"""
    fmt = fmt or plan.get("format") or "vertical"
    canvas = parse_canvas(fmt)
    if sources is None:
        sources = make_sources(plan.get("sources"), client=client, cache_dir=cache_dir)
    top = int(top or plan.get("per_item") or 3)
    used: set = set()
    results = []
    for it in plan.get("items") or []:
        kws = [k.strip() for k in it.get("keywords") or [] if isinstance(k, str) and k.strip()]
        need = it.get("duration")
        if need is None and it.get("start") is not None and it.get("end") is not None:
            need = float(it["end"]) - float(it["start"])
        entry = {
            "id": it.get("id"),
            "text": it.get("text", ""),
            "need_duration": need,
            "keywords": kws,
            "candidates": [],
            "chosen": None,
        }
        if not kws:
            entry["skipped"] = "没有关键词"
            results.append(entry)
            continue
        o = it.get("orientation")
        merged: Dict[str, dict] = {}
        for kw in kws:
            for src in sources:
                recs = src.search(
                    kw,
                    canvas,
                    per_page=int(it.get("per_source", 15)),
                    min_duration=it.get("min_duration"),
                    max_duration=it.get("max_duration"),
                )
                for rank, rec in enumerate(recs):
                    if rec["id"] in (it.get("exclude_ids") or []):
                        continue
                    if not passes_filters(
                        rec,
                        orientation_want=o,
                        min_duration=it.get("min_duration"),
                        max_duration=it.get("max_duration"),
                        min_short_side=min_short_side,
                    ):
                        continue
                    cur = merged.get(rec["id"])
                    if cur is None:
                        merged[rec["id"]] = {"rec": rec, "rank": rank, "hits": 1, "queries": [kw]}
                    else:
                        cur["rank"] = min(cur["rank"], rank)
                        cur["hits"] += 1
                        cur["queries"].append(kw)
        cands = []
        for m in merged.values():
            score, detail = score_candidate(m["rec"], canvas, need, m["rank"], m["hits"])
            cands.append(
                {
                    **m["rec"],
                    "query": m["queries"][0],
                    "matched_queries": m["queries"],
                    "score": score,
                    "score_detail": detail,
                }
            )
        cands.sort(key=lambda r: -r["score"])
        entry["candidates"] = cands[:top]
        fresh = [c for c in cands if c["id"] not in used]
        chosen = (fresh or cands or [None])[0]
        if chosen:
            used.add(chosen["id"])
            if do_download:
                chosen = {
                    **download(chosen, cache_dir=cache_dir, client=client),
                    "score": chosen["score"],
                    "score_detail": chosen["score_detail"],
                }
            entry["chosen"] = chosen
        results.append(entry)
    return {
        "format": fmt,
        "canvas": list(canvas),
        "items": results,
        "summary": {
            "items": len(results),
            "matched": sum(1 for r in results if r["chosen"]),
            "downloaded": sum(1 for r in results if (r["chosen"] or {}).get("path")),
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _brief(rec: dict) -> str:
    sc = f"{rec['score']:.2f} " if "score" in rec else ""
    who = (rec.get("author") or {}).get("name", "")
    return (
        f"{sc}{rec['id']} {rec['width']}x{rec['height']} {rec['duration']:.1f}s "
        f"{rec['orientation']} by {who or '?'} {rec.get('path') or rec.get('page_url', '')}"
    )


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def _main(argv=None, client_factory: Callable[[], HttpClient] = HttpClient) -> int:
    ap = argparse.ArgumentParser(description="B-roll 视频检索 / 匹配 / 下载 (Pexels + Pixabay)")
    ap.add_argument("--cache-dir", help="缓存目录 (默认 ~/.cache/jianying-pro/broll)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", help="按关键词检索")
    p.add_argument("query")
    p.add_argument("--source", default="pexels,pixabay")
    p.add_argument(
        "--format",
        default="vertical",
        help="vertical / horizontal / portrait_3x4 / square / 9:16 / WxH",
    )
    p.add_argument("--need-dur", type=float, help="镜头需要的时长 (打分用)")
    p.add_argument("--min-dur", type=float)
    p.add_argument("--max-dur", type=float)
    p.add_argument("--min-res", type=int, help="短边最少像素 (如 720 / 1080)")
    p.add_argument("--strict-orientation", action="store_true", help="只要和画幅同方向的素材")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--download", type=int, default=0, help="下载前 N 个")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("plan-template", help="从 analysis.json / 剧本生成分句计划骨架")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--analysis")
    g.add_argument("--script")
    p.add_argument("--format")
    p.add_argument("--min-len", type=float, default=2.0, help="短于这个秒数的句子并到下一句")
    p.add_argument("-o", "--output", default="broll_plan.json")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("match", help="按分句计划检索候选")
    p.add_argument("plan")
    p.add_argument("--top", type=int)
    p.add_argument("--format")
    p.add_argument("--min-res", type=int)
    p.add_argument("--download", action="store_true", help="下载每句的首选")
    p.add_argument("-o", "--output")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("download", help="按来源 + ID 下载")
    p.add_argument("--source", required=True, choices=sorted(SOURCES))
    p.add_argument("--id", required=True)
    p.add_argument("--format", default="vertical")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("cache", help="列出已缓存素材")
    p.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    cache_dir = args.cache_dir or default_cache_dir()
    as_json = getattr(args, "json", False)
    client = client_factory()
    try:
        if args.cmd == "search":
            srcs = make_sources(args.source.split(","), client=client, cache_dir=cache_dir)
            cands = search(
                args.query,
                fmt=args.format,
                sources=srcs,
                need_duration=args.need_dur,
                min_duration=args.min_dur,
                max_duration=args.max_dur,
                min_short_side=args.min_res,
                strict_orientation=args.strict_orientation,
                limit=args.limit,
            )
            for i in range(min(args.download, len(cands))):
                cands[i] = {
                    **download(cands[i], cache_dir=cache_dir, client=client),
                    "score": cands[i]["score"],
                    "score_detail": cands[i]["score_detail"],
                }
            data = {"query": args.query, "format": args.format, "candidates": cands}
            if not as_json:
                print(f"🔎 {args.query}: {len(cands)} 个候选 ({', '.join(s.name for s in srcs)})")
                for c in cands:
                    print("  " + _brief(c))
        elif args.cmd == "plan-template":
            plan = plan_template(
                analysis=_load_json(args.analysis) if args.analysis else None,
                script=_load_json(args.script) if args.script else None,
                fmt=args.format,
                min_len=args.min_len,
            )
            _write_json(args.output, plan)
            data = {"output": os.path.abspath(args.output), "items": len(plan["items"])}
            if not as_json:
                print(
                    f"✅ 计划骨架 {args.output}: {len(plan['items'])} 句。给每句填英文 keywords 后运行 match"
                )
        elif args.cmd == "match":
            plan = _load_json(args.plan)
            srcs = make_sources(plan.get("sources"), client=client, cache_dir=cache_dir)
            data = match_plan(
                plan,
                sources=srcs,
                top=args.top,
                fmt=args.format,
                do_download=args.download,
                cache_dir=cache_dir,
                client=client,
                min_short_side=args.min_res,
            )
            if args.output:
                _write_json(args.output, data)
                data["output"] = os.path.abspath(args.output)
            if not as_json:
                s = data["summary"]
                print(f"✅ {s['matched']}/{s['items']} 句有候选, 已下载 {s['downloaded']}")
                for it in data["items"]:
                    c = it["chosen"]
                    print(
                        f"  {it['id']} {it['text'][:24]!r}: "
                        + (_brief(c) if c else it.get("skipped", "无结果"))
                    )
        elif args.cmd == "download":
            src = SOURCES[args.source](client=client, cache_dir=cache_dir)
            rec = download(
                src.get(args.id, parse_canvas(args.format)), cache_dir=cache_dir, client=client
            )
            data = {"record": rec}
            if not as_json:
                print(f"✅ {_brief(rec)}")
        else:
            recs = list_cached(cache_dir)
            data = {"cache_dir": cache_dir, "records": recs}
            if not as_json:
                print(f"📦 {cache_dir}: {len(recs)} 个素材")
                for r in recs:
                    print("  " + _brief(r))
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
