# B-roll 视频供给 + 文生视频 provider（T10 / T11，分组 content）

## 用途

- **T10 `scripts/broll_fetch.py`**：从 Pexels / Pixabay 免费视频库按关键词搜视频，按画幅、时长、分辨率过滤，再按画幅匹配度、时长、分辨率、相关度打分，下载到 `~/.cache/jianying-pro/broll/`。语义匹配：输入转写或剧本的分句计划（每句的英文关键词由调用方 Agent 翻译后填进去），每句返回打好分的候选和首选，相邻句子不重复用同一个镜头。
- **T11 `scripts/genvideo_providers.py`**：文生视频 / 图生视频的统一接口（`submit` → `poll` → `download`）。已实现 `mock`（本地用 OpenCV 合成，不联网）和 `kling`（可灵开放平台的 HTTP 适配）。生成结果和 B-roll 放在同一个缓存目录，素材记录格式也一样。

## 密钥

| 环境变量 | 用途 | 注册 |
|---|---|---|
| `PEXELS_API_KEY` | Pexels | https://www.pexels.com/api/ |
| `PIXABAY_API_KEY`（也认 pixabay-images skill 用的 `PIXABAY_KEY`） | Pixabay | https://pixabay.com/api/docs/ |
| `KLING_ACCESS_KEY` + `KLING_SECRET_KEY` | 可灵 | 可灵开放平台控制台 → API 密钥 |
| `KLING_API_BASE`（可选） | 可灵接口地址，默认 `https://api-beijing.klingai.com`，海外用 `https://api-singapore.klingai.com` | |
| `KLING_MODEL`（可选） | 默认 `kling-v1-6` | |
| `JY_BROLL_CACHE`（可选） | 改缓存目录 | |

缺密钥时 CLI 返回 `code: "missing_api_key"`，并给出变量名和注册地址。密钥只从环境变量读，不写进计划、剧本或缓存（检索缓存的文件名只用请求的哈希）。

## CLI

```bash
# 检索 (默认两个源都查, 只用配了密钥的)
python scripts/broll_fetch.py search "city night traffic timelapse" --format vertical --need-dur 4 \
       [--min-dur 3] [--max-dur 30] [--min-res 1080] [--strict-orientation] [--limit 10] [--download 1] [--json]

# 语义匹配: 1) 出分句计划骨架  2) Agent 给每句填英文 keywords  3) match
python scripts/broll_fetch.py plan-template --analysis analysis.json -o broll_plan.json [--min-len 2.0]
python scripts/broll_fetch.py plan-template --script script.json -o broll_plan.json    # 剧本 beats 的 broll_keywords 自动带入
python scripts/broll_fetch.py match broll_plan.json --top 3 --download -o broll_matches.json [--json]

python scripts/broll_fetch.py download --source pexels --id 856195 --format vertical
python scripts/broll_fetch.py cache [--json]

# 文生 / 图生视频
python scripts/genvideo_providers.py providers
python scripts/genvideo_providers.py generate --provider mock --prompt "a red fox in snow" --aspect 9:16 --duration 5
python scripts/genvideo_providers.py submit --provider kling --prompt "山间云海, 航拍, 清晨" --aspect 9:16 --duration 5
python scripts/genvideo_providers.py poll --provider kling --job <task_id> --download
python scripts/genvideo_providers.py submit --provider kling --mode image2video --image cover.jpg --prompt "镜头缓慢推进"
```

所有子命令都支持 `--json`，输出 `{"ok", "code", "reason", "data"}`（见 `rules/cli.md`）；全局参数 `--cache-dir DIR`。

## Python API

```python
from broll_fetch import make_sources, search, download, plan_template, match_plan, list_cached
srcs = make_sources(["pexels", "pixabay"])                 # 没有任何密钥 -> BrollError(code="missing_api_key")
cands = search("ocean waves", fmt="vertical", sources=srcs, need_duration=4, min_duration=3)
rec = download(cands[0])                                    # -> 素材记录, rec["path"] 是本地 mp4
res = match_plan(plan_dict, sources=srcs, do_download=True) # 每句 candidates + chosen

from genvideo_providers import get_provider, GenVideoRequest
prov = get_provider("kling")                               # 或 "mock"
rec = prov.generate(GenVideoRequest(prompt="...", mode="text2video", duration=5, aspect_ratio="9:16"))
job = prov.submit(req); job = prov.poll(prov.load_job(job.job_id)); rec = prov.download(job)
```

新增 provider：继承 `GenVideoProvider`，设好 `name` / `modes` / `aspects` / `durations` / `license`，实现 `_submit(req) -> GenVideoJob` 和 `_poll(job) -> GenVideoJob`（`status` 取 queued / running / succeeded / failed，成功时填 `video_url`），再加进 `PROVIDERS`。时长和画幅会自动吸附到支持的值，调整写在 `request.extra["_adjusted"]`。

## 输出格式

### 素材记录（缓存里每个视频旁边的同名 `.json`，B-roll 和生成视频共用）

```json
{
 "record_version": 1, "id": "pexels_856195", "source": "pexels", "source_id": "856195",
 "kind": "video",                       // 生成视频是 "generated_video", source 是 "genvideo-kling" / "genvideo-mock"
 "url": "下载地址", "page_url": "素材页", "preview": "缩略图",
 "width": 1080, "height": 1920,          // 下载的版本
 "max_width": 2160, "max_height": 3840,  // 源站最高分辨率
 "orientation": "portrait", "duration": 12.0, "fps": 25,
 "license": {"name": "Pexels License", "url": "https://www.pexels.com/license/", "attribution_required": false, "note": "..."},
 "author": {"name": "...", "url": "..."},
 "query": "city night", "tags": ["city", "night"],
 "path": "/Users/.../.cache/jianying-pro/broll/pexels_856195_1080x1920.mp4",
 "fetched_at": "2026-11-01T12:00:00",
 "extra": {"prompt": "...", "mode": "text2video", "model": "kling-v1-6"}   // 只有生成视频有
}
```

文件名：`<来源>_<ID>_<宽>x<高>.mp4`。检索结果缓存 24 小时（Pixabay API 条款要求缓存），放在 `broll/_search/`；生成任务的状态放在 `broll/_jobs/`，所以 submit 和 poll 可以分两次命令执行。

### 打分（`score` 0–1，`score_detail` 给出各项）

| 项 | 权重 | 怎么算 |
|---|---|---|
| aspect | 0.4 | 裁成目标画幅后保留的画面比例（同方向 = 1，横版进竖屏约 0.32） |
| duration | 0.25 | 够长 = 1（太长最多扣 0.2）；不够长按 (实际/需要)² × 0.8 |
| resolution | 0.15 | 铺满画布不需要放大 = 1；需要放大 s 倍得 1/s |
| relevance | 0.2 | API 返回的排名，多个关键词同时命中有加成 |

下载版本：选铺满画布不需要放大的最小 mp4（横版进竖屏会选 4K）；都不够就选最大的。

### match 输出

`{"format", "canvas", "items": [{"id", "text", "need_duration", "keywords", "candidates": [记录 + score + matched_queries], "chosen": 记录|null, "skipped"?}], "summary": {"items", "matched", "downloaded"}}`

## 限制

- 中文句子翻成英文关键词由调用方 Agent 做，脚本不做翻译或语义向量匹配。相关度只来自 API 排名和多个关键词同时命中的加成。
- Pixabay 视频接口没有方向和时长参数，这两项在本地过滤，所以每页能用的候选会少一些。
- Pexels 每小时 200 次、每月 2 万次请求；Pixabay 每 60 秒 100 次。检索缓存能省掉重复请求。
- 可灵适配是按公开文档写的（JWT HS256、`/v1/videos/{text2video|image2video}`、`task_status` 取 submitted / processing / succeed / failed），**测试里只用 mock HTTP 跑过，没有用真实密钥联调**。接口地址和模型名可以用环境变量改。生成视频的商用授权取决于账号套餐。
- 即梦、Veo 还没有适配；按上面「新增 provider」的方法加。
- mock provider 合成的是短边 360 的渐变测试视频，只用来跑通流程。

## 给主线的集成建议

- **SKILL.md Agent Quick Routing**：
  - `| 找 B-roll / 空镜 / 按旁白配视频素材 | scripts/broll_fetch.py search / plan-template → (填 keywords) → match --download |`
  - `| 文生视频 / 图生视频 (可灵) | scripts/genvideo_providers.py generate --provider kling |`
- **rules**：挂到 `rules/media.md`（B-roll 来源、许可证和作者记录）和 `rules/generative.md`（生成视频 provider），`rules/cli.md` 里补两个 CLI 的子命令。引导模式 `rules/guided.md` 第 ③ 步「选素材」可以加：「素材不够时，按剧本分句出 B-roll 计划 → match → 用户挑」。
- **Mixin**：无。下载得到的 `rec["path"]` 直接作为剧本 `sources` 或分镜表 `overlays[].source` 使用（`card` 的 `mode: "cover"` 就是整屏 B-roll）。
- **requirements**：无新增，只用标准库（urllib、hmac）；mock 的合成用 requirements 里已有的 opencv-python + numpy。
- **CI**：测试全部 mock HTTP，不联网。
