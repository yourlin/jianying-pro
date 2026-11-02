"""
最佳 take 挑选 (T7): 同一句话说了多遍 (或多个机位 / 多次拍摄的同一段) 时, 给每一遍综合打分, 选出最好的一遍。

用法:
  python scripts/best_take.py analysis.json [-o takes.json] [--script script.txt]
  python scripts/best_take.py camA.json camB.json --script lines.txt --json      # 多机位 / 多次拍摄
  python scripts/best_take.py analysis.json --no-visual --plan-out plan.json     # 只看语音
  python scripts/best_take.py analysis.json --draft "最佳take"                   # 直接生成剪映草稿

输入:
  - analyze_footage.py 的 analysis.json (transcript / silences / video.duration / source), 可以给多个;
  - 或转写 JSON ([{start, end, text, words}]), 用 --video 指定源视频 (只能给一个输入)。
  - 剧本 (可选): .txt (按行 / 句号切句), JSON 字符串列表, {"lines": [...]}, 或 script.json
    (beats[*].line / say / text / narration)。

打分 (0–100, 缺的维度不参与, 权重按剩下的重新归一):
  fluency  语音流畅度  口癖 / 结巴 (复用 speech_cleanup 的检测)、卡顿停顿、语速是否在正常区间
  complete 是否说完整  和剧本目标句 (没有剧本时用这一组里最长的一遍) 的字覆盖率; 后面跟着「不对 / 重来」的减半
  script   剧本相似度  去掉口癖后的文本和剧本目标句的相似度 (有剧本时)
  visual   画面质量    shot_quality.score_segments 的段分 (有源视频且装了 opencv 时)
  face     正对镜头    采样点人脸分的均值 (没检测到人脸记 0; 人脸检测器不可用时不参与)
  没说完整 (覆盖率 < 0.9) 的总分再乘 0.6 + 0.4 x 覆盖率; 最后一遍加一点点分 (recency, 默认 2 分) 打破平局。

输出 (select_takes 的返回值):
  groups    每组候选 take 的分数、指标、问题, 以及 best / reason
  rejected  要剪掉的区间 (落选的 take、NG 标记句)
  sequence  最终保留的句子顺序 (每组最佳 + 只说了一遍的句子), 多机位 / 按剧本重排时用
  plan      单个源视频时: speech_cleanup 格式的精剪计划 (keep / cuts / transcript / stats),
            落选 take 记为 reason="take", 可以直接替换 cleanup_plan.json
  plans     多个源视频时: 每个源各自的精剪计划
"""

import argparse
import json
import os
import re
import sys
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import speech_cleanup as sc  # noqa: E402

DEFAULTS: Dict[str, Any] = {
    "similarity": 0.75,  # 两遍算同一句的文本相似度
    "script_match": 0.5,  # 匹配剧本目标句的最低相似度
    "sentence_gap": 0.7,  # 句间停顿 (秒), 超过它切句
    "window": 60.0,  # 同一源里同一句的多遍最多相隔多少秒
    "lookback": 3,  # 往回最多和几组比较
    "hesitation": 0.35,  # 句中 (非标点处) 停顿超过它算卡顿
    "long_pause": 0.8,  # 任何位置停顿超过它算长停顿
    "recency": 2.0,  # 最后一遍的加分 (平局时倾向后面说的)
    "every": 0.5,  # 画面采样间隔
}
WEIGHTS: Dict[str, float] = {
    "fluency": 0.35,
    "complete": 0.20,
    "script": 0.20,
    "visual": 0.15,
    "face": 0.10,
}
# 正常语速: 中文 字/秒, 英文 词/秒
RATE_CJK = (3.5, 6.2)
RATE_WORD = (1.8, 3.6)
_SENT_END = set("。.!！?？…")
_NAMES = {
    "fluency": "流畅度",
    "complete": "完整度",
    "script": "剧本相似度",
    "visual": "画面",
    "face": "正对镜头",
}


# --------------------------------------------------------------------------------------
# 剧本
# --------------------------------------------------------------------------------------


def _split_sentences(text: str) -> List[str]:
    out = []
    for line in str(text).splitlines():
        for part in re.split(r"(?<=[。！？!?；;])|(?<=\.)\s+", line):
            part = part.strip()
            if sc._phrase_units(part):
                out.append(part)
    return out


def load_script(data: Any) -> List[str]:
    """剧本 → 目标句列表。支持文本、字符串列表、{"lines"/"sentences"}、script.json 的 beats。"""
    if data is None:
        return []
    if isinstance(data, str):
        return _split_sentences(data)
    if isinstance(data, list):
        out = []
        for x in data:
            if isinstance(x, dict):
                out += load_script(x)
            else:
                out += _split_sentences(str(x))
        return out
    if isinstance(data, dict):
        for key in ("lines", "sentences", "script"):
            if key in data:
                return load_script(data[key])
        for key in ("line", "say", "narration", "voiceover", "text"):
            if isinstance(data.get(key), str):
                return _split_sentences(data[key])
        if isinstance(data.get("beats"), list):
            return load_script(data["beats"])
    return []


def read_script(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    if path.lower().endswith(".json"):
        return load_script(json.loads(raw))
    return load_script(raw)


# --------------------------------------------------------------------------------------
# 句子切分
# --------------------------------------------------------------------------------------


def _ng_markers():
    strong = {sc._phrase_units(m) for m in sc.DEFAULT_NG_MARKERS if sc._phrase_units(m)}
    weak = {sc._phrase_units(m) for m in sc.DEFAULT_NG_WEAK if sc._phrase_units(m)} - strong
    return strong, weak


def _disfluent_items(segs, sil, dur) -> List[dict]:
    """用 speech_cleanup 找口癖 / 结巴 (不剪重复句和 NG, 那些由本模块按 take 处理)。"""
    if not segs:
        return []
    plan = sc.plan_cleanup(segs, silences=sil, duration=dur, remove_repeats=False, remove_ng=False)
    seen, out = set(), []
    for c in plan["cuts"]:
        for it in c["items"]:
            key = (it["reason"], it["start"], it["end"])
            if it["reason"] in ("filler", "stutter") and key not in seen:
                seen.add(key)
                out.append(it)
    return out


def build_utterances(source: dict, cfg: dict) -> List[dict]:
    """
    把一个源的转写切成句子 (take 的最小单位)。source: {key, segments, silences, duration, path}。
    切句: 句末标点 / 转写分句 / 停顿 >= sentence_gap / NG 标记句 / 子句开头和本句开头一样 (起句重来)。
    """
    warnings: List[str] = []
    units = sc._build_units(source["segments"], warnings)
    source.setdefault("warnings", []).extend(warnings)
    if not units:
        return []
    items = _disfluent_items(source["segments"], source["silences"], source["duration"])
    for u in units:
        mid = (u["s"] + u["e"]) / 2
        hit = next((it for it in items if it["start"] - 1e-3 <= mid <= it["end"] + 1e-3), None)
        u["dis"] = hit["reason"] if hit else None
    strong, weak = _ng_markers()
    mk = sorted(strong | weak, key=lambda m: -len(m))
    clauses = sc._split_clauses(units, sc.DEFAULTS["clause_gap"])

    utts: List[dict] = []
    cur: List[int] = []

    def close(kind="speech", marker=None, strength=None, idxs=None):
        nonlocal cur
        idxs = cur if idxs is None else idxs
        if idxs:
            us = [units[i] for i in idxs]
            utts.append(
                {
                    "source": source["key"],
                    "kind": kind,
                    "marker": marker,
                    "strength": strength,
                    "units": us,
                    "start": round(us[0]["s"], 3),
                    "end": round(us[-1]["e"], 3),
                    "text": sc._join([u["raw"] for u in us]),
                    "toks": [u["t"] for u in us if not u["dis"]],
                    "items": [
                        it for it in items if us[0]["s"] - 1e-3 <= it["start"] <= us[-1]["e"]
                    ],
                }
            )
        if idxs is cur:
            cur = []

    for ci, cl in enumerate(clauses):
        toks = [units[i]["t"] for i in cl if not units[i]["dis"]]
        rest, found = sc._strip_ng(toks, mk)
        if found and not rest:  # 单独成句的 NG 标记
            close()
            strength = "strong" if sc._phrase_units(found) in strong else "weak"
            close("marker", found, strength, idxs=list(cl))
            continue
        if cur:
            prev = units[cur[-1]]
            head = [units[i]["t"] for i in cur if not units[i]["dis"]]
            gap = units[cl[0]]["s"] - prev["e"]
            if (
                prev["punct_after"] in _SENT_END
                or prev["seg"] != units[cl[0]]["seg"]
                or gap >= cfg["sentence_gap"]
                or (len(head) >= 2 and sc._same_start(toks, head))
            ):
                close()
        cur.extend(cl)
    close()
    for k, u in enumerate(utts):
        u["order"] = k
    return utts


# --------------------------------------------------------------------------------------
# 分组
# --------------------------------------------------------------------------------------


def take_similarity(a: Sequence[str], b: Sequence[str]) -> float:
    """两遍是不是同一句: 整句相似度; 短的一遍是长的一遍的开头 (起句重来) 也算。排比 (只差数字) 记 0。"""
    if len(a) < 2 or len(b) < 2:
        return 0.0
    if sc._numeric_diff(a, b):
        return 0.0
    full = sc._ratio(a, b)
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    partial = 0.0
    if len(short) >= 3 and len(short) <= len(long_) - 2 and sc._common_prefix(short, long_) >= 2:
        r = sc._ratio(short, long_[: len(short)])
        if r >= 0.8:
            partial = 0.8 * r
    return max(full, partial)


def _same_sentence(a: Sequence[str], b: Sequence[str], similarity: float) -> float:
    """够得上「同一句」时返回相似度, 否则 0 (起句重来的部分匹配阈值固定为 0.64)。"""
    sim = take_similarity(a, b)
    if sim >= similarity:
        return sim
    full = sc._ratio(a, b) if len(a) >= 2 and len(b) >= 2 else 0.0
    return sim if sim > full + 1e-9 and sim >= 0.64 else 0.0


def _script_score(toks: Sequence[str], target: Sequence[str]) -> float:
    """take 和剧本句的匹配度: 整句相似度, 或者是剧本句的开头 (没说完的一遍)。"""
    if not toks or not target:
        return 0.0
    full = sc._ratio(toks, target)
    if len(toks) < len(target) and len(toks) >= 3 and sc._common_prefix(toks, target) >= 2:
        full = max(full, 0.8 * sc._ratio(toks, target[: len(toks)]))
    return full


def _resolve_markers(utts: List[dict], group_of: Dict[int, int]) -> None:
    """
    NG 标记句: 强标记 (重来/说错了) 一律剪; 弱标记 (不对/错了) 只有前后两句是同一组时才剪,
    「对吗？不对。」这种正常对话不剪。被剪的标记让它前面那一遍记为「被打断」。
    """
    for k, u in enumerate(utts):
        if u["kind"] != "marker":
            continue
        prev = next((utts[j] for j in range(k - 1, -1, -1) if utts[j]["kind"] == "speech"), None)
        nxt = next((x for x in utts[k + 1 :] if x["kind"] == "speech"), None)
        same = (
            prev is not None
            and nxt is not None
            and group_of.get(id(prev)) is not None
            and group_of.get(id(prev)) == group_of.get(id(nxt))
        )
        asked = prev is not None and prev["units"][-1]["punct_after"] in ("?", "？")
        if same or (u["strength"] == "strong" and not asked):
            u["ng"] = True
            if prev is not None and u["start"] - prev["end"] <= 3.0:
                prev["aborted"] = u["marker"]
        else:
            u["kind"] = "speech"  # 不是 NG, 当正常句子
            u["toks"] = [x["t"] for x in u["units"] if not x["dis"]]


def group_takes(utts: List[dict], script: Sequence[str], cfg: dict) -> Tuple[List[dict], list]:
    """
    把句子分成「同一句的多遍」。有剧本时按剧本目标句归组, 对不上剧本的句子再按文本相似度互相归组。
    返回 (groups, missing_script_indexes)。group: {takes: [utt], target, target_index}。
    """
    targets = [sc._phrase_units(s) for s in script]
    groups: List[dict] = []
    by_target: Dict[int, dict] = {}
    speech = [u for u in utts if u["kind"] == "speech" and u["toks"]]
    free = []
    for u in speech:
        best_i, best = None, 0.0
        for i, t in enumerate(targets):
            s = _script_score(u["toks"], t)
            if s > best + 1e-9:
                best_i, best = i, s
        if best_i is not None and best >= cfg["script_match"]:
            g = by_target.get(best_i)
            if g is None:
                g = {"takes": [], "target": script[best_i], "target_index": best_i}
                by_target[best_i] = g
                groups.append(g)
            g["takes"].append(u)
        else:
            free.append(u)

    recent: Dict[str, List[dict]] = {}
    for u in free:
        cands = []
        for g in groups:
            if g["target"] is not None:
                continue
            last = g["takes"][-1]
            if last["source"] == u["source"]:
                if g not in recent.get(u["source"], [])[-cfg["lookback"] :]:
                    continue
                if u["start"] - last["end"] > cfg["window"]:
                    continue
            sim = max(_same_sentence(u["toks"], t["toks"], cfg["similarity"]) for t in g["takes"])
            if sim > 0:
                cands.append((sim, g))
        if cands:
            g = max(cands, key=lambda x: x[0])[1]
        else:
            g = {"takes": [], "target": None, "target_index": None}
            groups.append(g)
        g["takes"].append(u)
        lst = recent.setdefault(u["source"], [])
        if g in lst:
            lst.remove(g)
        lst.append(g)

    group_of = {id(t): gi for gi, g in enumerate(groups) for t in g["takes"]}
    _resolve_markers(utts, group_of)
    missing = [i for i in range(len(script)) if i not in by_target]
    return groups, missing


# --------------------------------------------------------------------------------------
# 打分
# --------------------------------------------------------------------------------------


def _coverage(ref: Sequence[str], toks: Sequence[str]) -> float:
    if not ref:
        return 1.0
    sm = SequenceMatcher(None, list(ref), list(toks), autojunk=False)
    return min(1.0, sum(b.size for b in sm.get_matching_blocks()) / len(ref))


def speech_metrics(take: dict, cfg: dict) -> dict:
    """词级转写上的流畅度指标: 口癖、结巴、卡顿、长停顿、语速。"""
    us = take["units"]
    fillers = sum(1 for it in take["items"] if it["reason"] == "filler")
    stutters = sum(1 for it in take["items"] if it["reason"] == "stutter")
    hes = longp = 0
    talk = 0.0
    for a, b in zip(us, us[1:]):
        gap = max(0.0, b["s"] - a["e"])
        if gap >= cfg["long_pause"]:
            longp += 1
        elif gap >= cfg["hesitation"] and not a["punct_after"] and not (a["dis"] or b["dis"]):
            hes += 1
        if not (a["dis"] or b["dis"]):
            talk += min(gap, 0.15)  # 字间正常的小间隔算说话时间, 停顿和口癖不算
    clean = [u for u in us if not u["dis"]]
    talk += sum(u["e"] - u["s"] for u in clean)
    cjk = sum(1 for u in clean if sc._CJK_RE.match(u["t"]))
    lo, hi = RATE_CJK if cjk * 2 >= len(clean) else RATE_WORD
    rate = len(clean) / talk if talk > 0 else 0.0
    if lo <= rate <= hi:
        rate_score = 100.0
    else:
        dist = (lo - rate) if rate < lo else (rate - hi)
        rate_score = max(0.0, 100.0 - 30.0 * dist)
    base = max(0.0, 100.0 - 12 * fillers - 10 * stutters - 5 * hes - 8 * longp)
    return {
        "fillers": fillers,
        "stutters": stutters,
        "hesitations": hes,
        "long_pauses": longp,
        "rate": round(rate, 2),
        "rate_unit": "字/秒" if (lo, hi) == RATE_CJK else "词/秒",
        "rate_range": [lo, hi],
        "rate_score": round(rate_score, 1),
        "fluency": round(0.8 * base + 0.2 * rate_score, 1),
    }


def visual_scores(path: str, ranges: List[Tuple[float, float]], every_s: float) -> dict:
    """调 shot_quality 给每个区间打画面分和正对镜头分。返回 {(start, end): {visual, face, issues}}。"""
    import shot_quality as sq

    res = sq.score_segments(path, ranges, every_s=every_s, min_samples=2)
    face_ok = res.get("face_detector") == "haar"
    out = {}
    for (a, b), seg in zip(ranges, res["segments"]):
        inside = [s for s in res["samples"] if seg["start"] <= s["t"] <= seg["end"]]
        face = None
        facing = None
        if face_ok and inside:
            vals = [(s["face"] or {}).get("score") or 0.0 for s in inside]
            face = round(sum(vals) / len(vals), 1)
            facing = round(
                sum(1 for s in inside if (s["face"] or {}).get("facing")) / len(inside), 2
            )
        out[(a, b)] = {
            "visual": seg["score"],
            "face": face,
            "facing_share": facing,
            "issues": seg["issues"],
        }
    return out


def _total(scores: dict, weights: dict) -> float:
    parts = {k: v for k, v in scores.items() if v is not None and weights.get(k, 0) > 0}
    if not parts:
        return 0.0
    w = sum(weights[k] for k in parts)
    return sum(weights[k] * v for k, v in parts.items()) / w


def _notes(t: dict) -> List[str]:
    m, s = t["metrics"], t["scores"]
    out = []
    if m["fillers"]:
        out.append(f"{m['fillers']} 个口癖")
    if m["stutters"]:
        out.append(f"{m['stutters']} 处结巴")
    if m["hesitations"]:
        out.append(f"{m['hesitations']} 处卡顿")
    if m["long_pauses"]:
        out.append(f"{m['long_pauses']} 处长停顿")
    if m["rate_score"] < 100:
        lo, hi = m["rate_range"]
        out.append(f"语速{'偏慢' if m['rate'] < lo else '偏快'} {m['rate']} {m['rate_unit']}")
    if t.get("aborted"):
        out.append(f"被「{t['aborted']}」打断")
    if m["coverage"] < 0.9:
        out.append(f"没说完 (覆盖 {m['coverage']:.0%})")
    if s.get("script") is not None and s["script"] < 80:
        out.append(f"和剧本差异大 ({s['script']:.0f})")
    if s.get("visual") is not None and s["visual"] < 60:
        tags = "、".join(m.get("visual_issues") or [])
        out.append(f"画面 {s['visual']:.0f} 分" + (f" ({tags})" if tags else ""))
    if s.get("face") is not None and s["face"] < 60:
        out.append("没正对镜头" if (m.get("facing_share") or 0) < 0.5 else "人脸状态差")
    return out


def score_group(group: dict, cfg: dict, weights: dict, visual: dict) -> None:
    """给一组 take 打分, 写回 group: takes[*].scores/metrics/total, best, reason。"""
    takes = group["takes"]
    target = sc._phrase_units(group["target"]) if group["target"] else None
    ref = target or max((t["toks"] for t in takes), key=len)
    n = len(takes)
    for k, t in enumerate(takes):
        m = speech_metrics(t, cfg)
        cov = _coverage(ref, t["toks"])
        m["coverage"] = round(cov, 3)
        complete = 100.0 * cov * (0.5 if t.get("aborted") else 1.0)
        scores = {
            "fluency": m["fluency"],
            "complete": round(complete, 1),
            "script": round(100.0 * sc._ratio(t["toks"], target), 1) if target else None,
            "visual": None,
            "face": None,
        }
        v = visual.get((t["source"], t["start"], t["end"]))
        if v:
            scores["visual"], scores["face"] = v["visual"], v["face"]
            m["visual_issues"], m["facing_share"] = v["issues"], v["facing_share"]
        total = _total(scores, weights)
        if cov < 0.9:
            total *= 0.6 + 0.4 * cov
        if n > 1 and k == n - 1:
            total += cfg["recency"]
        t["metrics"], t["scores"], t["total"] = m, scores, round(min(100.0, total), 1)
        t["notes"] = _notes(t)
    best = max(range(n), key=lambda i: (takes[i]["total"], i))
    group["best"] = best
    b = takes[best]
    strengths = []
    if not b["notes"]:
        strengths.append("没有明显问题")
    else:
        strengths.append("问题: " + "、".join(b["notes"]))
    dims = [k for k, v in b["scores"].items() if v is not None]
    others = []
    for i, t in enumerate(takes):
        if i == best:
            continue
        lose = [
            _NAMES[k]
            for k in dims
            if t["scores"].get(k) is not None and t["scores"][k] + 5 < b["scores"][k]
        ]
        why = "、".join(t["notes"]) or "各项接近, 总分略低"
        extra = f"; {'/'.join(lose)}更差" if lose else ""
        others.append(f"第{i + 1}遍 {t['total']:.0f} 分 ({why}{extra})")
    group["reason"] = (
        f"选第{best + 1}遍 ({b['start']:.2f}–{b['end']:.2f}s, {b['total']:.0f} 分): "
        + "; ".join(strengths)
        + ("。落选: " + "; ".join(others) if others else "")
    )


# --------------------------------------------------------------------------------------
# 精剪计划
# --------------------------------------------------------------------------------------


def _relabel_take_cuts(plan: dict, rejected: List[dict]) -> None:
    """把含落选 take 的剪切区间里的空档从 pause / edge 挪到 reason="take", 保证 by_reason 秒数之和 = removed。"""
    br = plan["stats"]["by_reason"]
    used = set()
    for c in plan["cuts"]:
        secs = dict(c["seconds"])
        hits = [r for r in rejected if r["start"] < c["end"] and r["end"] > c["start"]]
        if not hits:
            continue
        # 落选 take 前后的停顿也是因为剪掉这一遍才去掉的, 整段空档都记到 take
        ov = sum(secs.get(k, 0.0) for k in ("pause", "edge"))
        if ov <= 0.001:
            continue
        left = ov
        for k in ("pause", "edge"):
            take = min(left, secs.get(k, 0.0))
            if take <= 0:
                continue
            secs[k] = round(secs[k] - take, 3)
            br[k]["seconds"] = round(br[k]["seconds"] - take, 3)
            if secs[k] <= 0.001:
                del secs[k]
                br[k]["count"] -= 1
            left -= take
        secs["take"] = round(ov, 3)
        r = br.setdefault("take", {"count": 0, "seconds": 0.0})
        r["seconds"] = round(r["seconds"] + ov, 3)
        for h in hits:
            if id(h) not in used:
                used.add(id(h))
                r["count"] += 1
            c["items"].append(
                {
                    "reason": "take",
                    "text": h["text"],
                    "start": h["start"],
                    "end": h["end"],
                    "detail": h["detail"],
                }
            )
        order = sorted(secs, key=lambda k: (k in ("pause", "edge"), -secs[k]))
        c["seconds"], c["reasons"], c["reason"] = secs, order, order[0]
    for k in [k for k, v in br.items() if v["count"] <= 0 and v["seconds"] <= 0.001]:
        del br[k]
    plan["stats"]["items"]["take"] = len(rejected)


def build_plan(source: dict, rejected: List[dict], cleanup: bool = True, **cleanup_kwargs) -> dict:
    """
    去掉落选 take 的字, 再用 speech_cleanup.plan_cleanup 规划 (落选 take 留下的空档被当作气口压缩),
    最后把这些空档标成 reason="take"。cleanup=False 时只剪 take, 不删口癖 / 结巴。
    """
    spans = [(r["start"], r["end"]) for r in rejected]
    segs = []
    for seg in source["segments"]:
        words = [
            w
            for w in seg["words"]
            if not any(a - 1e-3 <= (w["s"] + w["e"]) / 2 <= b + 1e-3 for a, b in spans)
        ]
        if seg["words"] and not words:
            continue
        if not seg["words"] and any(
            a - 1e-3 <= (seg["start"] + seg["end"]) / 2 <= b + 1e-3 for a, b in spans
        ):
            continue
        segs.append(dict(seg, words=words, text="".join(w["w"] for w in words) or seg["text"]))
    kw = dict(cleanup_kwargs)
    kw.setdefault("remove_repeats", False)
    plan = sc.plan_cleanup(
        segs,
        silences=source["silences"],
        duration=source["duration"],
        remove_ng=False,
        remove_fillers=cleanup,
        remove_stutters=cleanup,
        compress_pauses=True,
        **kw,
    )
    plan["source"] = source.get("path")
    _relabel_take_cuts(plan, rejected)
    plan["settings"]["best_take"] = True
    return plan


# --------------------------------------------------------------------------------------
# 公开 API
# --------------------------------------------------------------------------------------


def _norm_source(src: Any, i: int) -> dict:
    """analysis dict / 转写列表 / {"analysis", "video"} → 内部源描述。"""
    video = None
    if isinstance(src, dict) and "analysis" in src:
        video = src.get("video")
        src = src["analysis"]
    segs, sil, dur, path = sc.load_transcript(src)
    path = video or path
    key = os.path.abspath(path) if path else f"source{i + 1}"
    return {
        "key": key,
        "path": os.path.abspath(path) if path else None,
        "segments": segs,
        "silences": sil,
        "duration": dur,
    }


def _public(t: dict, gi: int, ti: int, best: bool) -> dict:
    return {
        "take": ti + 1,
        "source": t["source"],
        "start": t["start"],
        "end": t["end"],
        "text": t["text"],
        "total": t["total"],
        "scores": t["scores"],
        "metrics": t["metrics"],
        "notes": t["notes"],
        "aborted": t.get("aborted"),
        "chosen": best,
    }


def select_takes(
    sources: Any,
    script: Any = None,
    *,
    visual: bool = True,
    weights: Optional[Dict[str, float]] = None,
    cleanup: bool = True,
    cleanup_kwargs: Optional[Dict[str, Any]] = None,
    **overrides,
) -> Dict[str, Any]:
    """
    挑最佳 take。

    sources   一个或多个源: analysis.json 的 dict / 转写列表 / {"analysis": ..., "video": "a.mp4"}
    script    剧本 (目标句列表、文本或 script.json dict), 可选
    visual    有源视频时用 shot_quality 给画面和人脸打分
    weights   覆盖 WEIGHTS (fluency/complete/script/visual/face), 设 0 关闭某维度
    cleanup   生成的 plan 里是否同时删口癖 / 结巴 (speech_cleanup); cleanup_kwargs 透传给 plan_cleanup
    overrides 覆盖 DEFAULTS 里的阈值
    返回: {groups, rejected, sequence, plan | plans, missing, stats, warnings, settings}
    """
    unknown = set(overrides) - set(DEFAULTS)
    if unknown:
        raise TypeError(f"未知参数: {', '.join(sorted(unknown))}")
    cfg = dict(DEFAULTS, **overrides)
    w = dict(WEIGHTS)
    for k, v in (weights or {}).items():
        if k not in WEIGHTS:
            raise ValueError(f"未知权重 {k!r} (可选: {', '.join(WEIGHTS)})")
        w[k] = float(v)
    if isinstance(sources, dict) or (
        isinstance(sources, list)
        and sources
        and isinstance(sources[0], dict)
        and "words" in sources[0]
    ):
        sources = [sources]  # 单个 analysis / 单个转写列表
    srcs = [_norm_source(s, i) for i, s in enumerate(sources or [])]
    if not srcs:
        raise ValueError("没有输入源")
    lines = load_script(script)
    warnings: List[str] = []

    utts: List[dict] = []
    for s in srcs:
        utts += build_utterances(s, cfg)
        warnings += s.get("warnings", [])
    if not utts:
        raise ValueError("转写里没有可用的句子")
    groups, missing = group_takes(utts, lines, cfg)

    multi = [g for g in groups if len(g["takes"]) > 1]
    vis: Dict[tuple, dict] = {}
    if visual and (w["visual"] > 0 or w["face"] > 0) and multi:
        by_src: Dict[str, List[Tuple[float, float]]] = {}
        for g in multi:
            for t in g["takes"]:
                by_src.setdefault(t["source"], []).append((t["start"], t["end"]))
        for s in srcs:
            rngs = by_src.get(s["key"])
            if not rngs:
                continue
            if not s["path"] or not os.path.exists(s["path"]):
                warnings.append(f"找不到源视频 {s['path'] or s['key']}, 不做画面打分")
                continue
            try:
                res = visual_scores(s["path"], rngs, cfg["every"])
            except Exception as e:  # opencv 缺失 / 视频打不开
                warnings.append(f"画面打分失败 ({s['path']}): {e}")
                continue
            for (a, b), v in res.items():
                vis[(s["key"], a, b)] = v
    for g in groups:
        score_group(g, cfg, w, vis)

    # 落选 take + 被剪的 NG 标记
    rejected = []
    for gi, g in enumerate(groups):
        for ti, t in enumerate(g["takes"]):
            if ti != g["best"]:
                rejected.append(
                    {
                        "source": t["source"],
                        "start": t["start"],
                        "end": t["end"],
                        "text": t["text"],
                        "group": gi + 1,
                        "kind": "take",
                        "detail": f"第{gi + 1}组落选的第{ti + 1}遍, 保留第{g['best'] + 1}遍",
                    }
                )
    for u in utts:
        if u.get("ng"):
            rejected.append(
                {
                    "source": u["source"],
                    "start": u["start"],
                    "end": u["end"],
                    "text": u["text"],
                    "group": None,
                    "kind": "ng_marker",
                    "detail": f"NG 标记「{u['marker']}」",
                }
            )
    rejected.sort(key=lambda r: (r["source"], r["start"]))

    # 最终句子顺序
    src_rank = {s["key"]: i for i, s in enumerate(srcs)}
    seq = []
    last_target = -1.0
    for gi, g in enumerate(
        sorted(groups, key=lambda g: (src_rank[g["takes"][0]["source"]], g["takes"][0]["start"]))
    ):
        b = g["takes"][g["best"]]
        if g["target_index"] is not None:
            last_target = float(g["target_index"])
            key = (last_target, 0.0)
        else:
            key = (last_target + 0.5, src_rank[b["source"]] * 1e6 + b["start"])
        seq.append((key, b, g))
    if lines:
        seq.sort(key=lambda x: (x[0], x[1]["start"]))
    elif len(srcs) == 1:
        seq.sort(key=lambda x: x[1]["start"])
    gid = {id(g): i + 1 for i, g in enumerate(groups)}
    sequence = [
        {
            "source": b["source"],
            "start": b["start"],
            "end": b["end"],
            "text": b["text"],
            "group": gid[id(g)] if len(g["takes"]) > 1 else None,
            "target_index": g["target_index"],
        }
        for _, b, g in seq
    ]

    plans = {}
    for s in srcs:
        rj = [r for r in rejected if r["source"] == s["key"]]
        plans[s["key"]] = build_plan(s, rj, cleanup=cleanup, **(cleanup_kwargs or {}))

    out_groups = []
    for gi, g in enumerate(groups):
        if len(g["takes"]) < 2 and g["target"] is None:
            continue
        out_groups.append(
            {
                "id": gi + 1,
                "target": g["target"],
                "target_index": g["target_index"],
                "best": g["best"] + 1,
                "reason": g["reason"],
                "takes": [_public(t, gi, ti, ti == g["best"]) for ti, t in enumerate(g["takes"])],
            }
        )
    return {
        "sources": [s["key"] for s in srcs],
        "settings": {**cfg, "weights": w, "visual": bool(vis), "cleanup": cleanup},
        "script": lines or None,
        "groups": out_groups,
        "missing": [{"index": i, "text": lines[i]} for i in missing],
        "rejected": rejected,
        "sequence": sequence,
        "plan": plans[srcs[0]["key"]] if len(srcs) == 1 else None,
        "plans": plans if len(srcs) > 1 else None,
        "stats": {
            "sentences": sum(1 for u in utts if u["kind"] == "speech"),
            "groups": sum(1 for g in groups if len(g["takes"]) > 1),
            "rejected_takes": sum(1 for r in rejected if r["kind"] == "take"),
            "ng_markers": sum(1 for r in rejected if r["kind"] == "ng_marker"),
            "rejected_seconds": round(sum(r["end"] - r["start"] for r in rejected), 3),
        },
        "warnings": warnings,
    }


def build_best_take_draft(
    selection: Dict[str, Any],
    project_name: str,
    drafts_root: Optional[str] = None,
    track_name: str = "VideoTrack",
    punch_in: Optional[float] = None,
    use_plan: bool = True,
    overwrite: bool = True,
    save: bool = True,
) -> Dict[str, Any]:
    """按挑选结果生成草稿: 单源默认走精剪计划 (plan), 多源 / use_plan=False 时按 sequence 拼接。"""
    from core.take_ops import BestTakeOpsMixin
    from jy_wrapper import JyProject

    class _Project(JyProject, BestTakeOpsMixin):
        pass

    project = _Project(project_name, drafts_root=drafts_root, overwrite=overwrite)
    segs = project.add_best_takes(
        selection, track_name=track_name, punch_in=punch_in, use_plan=use_plan
    )
    result = {"draft_name": project.name, "segments": len(segs)}
    if save:
        result.update(project.save())
    return result


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _parse_weights(text: Optional[str]) -> Dict[str, float]:
    out = {}
    for part in re.split(r"[,，]", text or ""):
        if not part.strip():
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = float(v)
    return out


def _main(argv=None) -> int:
    from utils.cli_protocol import emit_result, make_result

    ap = argparse.ArgumentParser(description="最佳 take 挑选: 同一句说了多遍时选最好的一遍")
    ap.add_argument(
        "inputs", nargs="+", help="analysis.json (可多个: 多机位 / 多次拍摄) 或转写 JSON"
    )
    ap.add_argument("--video", help="源视频 (只有一个输入时; 默认取 analysis.json 的 source)")
    ap.add_argument("--script", help="剧本: .txt (一行一句) / JSON 列表 / script.json")
    ap.add_argument("-o", "--output", help="挑选结果 JSON (默认 <第一个输入目录>/takes.json)")
    ap.add_argument(
        "--plan-out", help="精剪计划 (speech_cleanup 格式, 单源时; 默认 best_take_plan.json)"
    )
    ap.add_argument("--no-visual", action="store_true", help="不做画面 / 人脸打分")
    ap.add_argument(
        "--no-cleanup", action="store_true", help="plan 里只剪落选 take, 不删口癖 / 结巴"
    )
    ap.add_argument("--weights", help="覆盖权重: fluency=0.4,visual=0.2,face=0")
    ap.add_argument("--similarity", type=float, default=DEFAULTS["similarity"])
    ap.add_argument("--every", type=float, default=DEFAULTS["every"], help="画面采样间隔 (秒)")
    ap.add_argument("--max-pause", type=float, default=sc.DEFAULTS["max_pause"])
    ap.add_argument("--draft", help="直接生成剪映草稿 (草稿名)")
    ap.add_argument("--drafts-root", help="草稿根目录 (默认剪映草稿目录)")
    ap.add_argument("--track", default="VideoTrack")
    ap.add_argument("--punch-in", type=float, help="剪点后交替放大 (假景别), 如 1.25")
    ap.add_argument("--sequence", action="store_true", help="草稿按 sequence (剧本顺序) 拼接")
    ap.add_argument("--json", action="store_true", help="输出 {ok, code, reason, data}")
    args = ap.parse_args(argv)

    def fail(code, reason):
        emit_result(make_result(False, code, reason), args.json)
        if not args.json:
            print(f"❌ {reason}", file=sys.stderr)
        return 1

    try:
        if args.video and len(args.inputs) > 1:
            raise ValueError("--video 只能配一个输入; 多个输入请用各自 analysis.json 的 source")
        sources = []
        for p in args.inputs:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            sources.append({"analysis": data, "video": args.video} if args.video else data)
        script = read_script(args.script) if args.script else None
        if args.script and not script:
            raise ValueError(f"剧本里没有句子: {args.script}")
        sel = select_takes(
            sources if len(sources) > 1 else sources[0],
            script,
            visual=not args.no_visual,
            weights=_parse_weights(args.weights),
            cleanup=not args.no_cleanup,
            cleanup_kwargs={"max_pause": args.max_pause},
            similarity=args.similarity,
            every=args.every,
        )
    except FileNotFoundError as e:
        return fail("not_found", f"文件不存在: {e.filename}")
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        return fail("invalid_input", str(e))

    base = os.path.dirname(os.path.abspath(args.inputs[0]))
    out = args.output or os.path.join(base, "takes.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(sel, f, ensure_ascii=False, indent=1)
    plan_path = None
    if sel["plan"] is not None:
        plan_path = args.plan_out or os.path.join(base, "best_take_plan.json")
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(sel["plan"], f, ensure_ascii=False, indent=1)

    draft_info = None
    if args.draft:
        missing = [s for s in {x["source"] for x in sel["sequence"]} if not os.path.exists(s)]
        if missing:
            return fail("not_found", f"生成草稿需要源视频: {', '.join(missing)}")
        if not sel["sequence"]:
            return fail("empty_plan", "没有可保留的句子")
        draft_info = build_best_take_draft(
            sel,
            args.draft,
            args.drafts_root,
            args.track,
            args.punch_in,
            use_plan=not args.sequence,
        )

    data_out = {
        "takes_path": out,
        "plan_path": plan_path,
        "stats": sel["stats"],
        "groups": [
            {"id": g["id"], "best": g["best"], "reason": g["reason"]} for g in sel["groups"]
        ],
        "missing": sel["missing"],
        "warnings": sel["warnings"],
        "draft": draft_info,
    }
    if args.json:
        emit_result(make_result(True, "ok", "", data_out), True)
        return 0
    st = sel["stats"]
    print(
        f"🎯 {st['sentences']} 句, {st['groups']} 组多遍, 剪掉 {st['rejected_takes']} 遍 "
        f"+ {st['ng_markers']} 个 NG 标记 ({st['rejected_seconds']:.2f}s)"
    )
    for g in sel["groups"]:
        head = f"「{g['target']}」" if g["target"] else f"「{g['takes'][g['best'] - 1]['text']}」"
        print(f"   [{g['id']}] {head}\n       {g['reason']}")
    for m in sel["missing"]:
        print(f"⚠️ 剧本第 {m['index'] + 1} 句没找到: {m['text']}")
    for w in sel["warnings"]:
        print(f"⚠️ {w}")
    if draft_info:
        print(f"🎬 草稿: {draft_info.get('draft_path', draft_info['draft_name'])}")
    print(f"✅ {out}" + (f"\n✅ {plan_path}" if plan_path else ""))
    return 0


if __name__ == "__main__":
    from utils.env_setup import setup_env

    setup_env()
    sys.exit(_main())
