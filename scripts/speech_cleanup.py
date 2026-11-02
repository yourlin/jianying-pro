"""
口播精剪 (T1): 根据词级转写, 规划要剪掉的口癖 / 结巴 / 说错重来 / 重复句, 并把过长气口压缩到上限。

用法:
  python scripts/speech_cleanup.py analysis.json [-o plan.json] [--max-pause 0.25] [--pad 0.05]
  python scripts/speech_cleanup.py transcript.json --video talk.mp4 --draft "口播精剪" [--punch-in 1.25]
  python scripts/speech_cleanup.py analysis.json --json        # 机器可读输出 {ok, code, reason, data}

输入:
  - analyze_footage.py 的 analysis.json (用其中 transcript / silences / video.duration / source);
  - 或单独的转写 JSON: [{start, end, text, words: [{w, s, e}]}], 也兼容 {"segments": [...]}
    以及 whisper 原生的 words: [{word, start, end}]。没有词级时间戳的句子按字数均分时长 (会给出警告)。

输出 (plan_cleanup 的返回值):
  keep        保留区间 [{start, end, duration, target_start, text}] (源视频时间, 秒)
  cuts        剪掉的区间 [{start, end, duration, reason, reasons, items: [{reason, text, start, end, detail}]}]
  transcript  精剪后时间线上的转写 (同 analyze_footage 格式, 可直接拿去做字幕)
  stats       原时长 / 保留时长 / 各原因剪掉的次数与秒数

判断规则 (见 references/methodology/pacing.md「剪气口」):
  - 口癖: 嗯/呃 直接删; 啊/哦 只在句首、前后有停顿或拖长时删 (句末语气词保留);
    那个/这个/就是说 只在后面有停顿、拖长、连说 (那个那个) 或整句都是口癖时删 —— 「那个按钮」这种指代不删;
  - 结巴: 「我我我」「我们，我们」只留最后一遍;
  - 说错重来: 「不对 / 重来 / 再说一遍…」单独成句时, 删掉标记本身和它前面说错的那句;
  - 重复句: 相邻句文本高度相似或前一句是后一句的开头 (起句重来), 只保留最后一遍;
  - 气口: 不全删, 压缩到 --max-pause (默认 0.25s), 切点前后各留 --pad (默认 0.05s) 防吞字。
"""

import argparse
import json
import os
import re
import sys
from bisect import bisect_right
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 口癖词表: 词 -> 模式
#   always   出现即删 (纯语气填充)
#   isolated 句首 / 前后有停顿 / 拖长时删; 句末且前面无停顿视为语气词保留
#   pause    后面有停顿、拖长、连说、接另一个口癖、整句都是口癖时删 (默认保留指代用法)
#   strict   只在前后都有停顿、连说或整句都是口癖时删 (高频连接词, 误删代价大)
DEFAULT_FILLERS: Dict[str, str] = {
    "嗯": "always",
    "呃": "always",
    "唔": "always",
    "em": "always",
    "um": "always",
    "umm": "always",
    "uh": "always",
    "uhm": "always",
    "er": "always",
    "erm": "always",
    "hmm": "always",
    "mm": "always",
    "啊": "isolated",
    "额": "isolated",
    "哎": "isolated",
    "诶": "isolated",
    "欸": "isolated",
    "哦": "isolated",
    "噢": "isolated",
    "ah": "isolated",
    "oh": "isolated",
    "eh": "isolated",
    "那个": "pause",
    "这个": "pause",
    "就是说": "pause",
    "然后呢": "pause",
    "怎么说呢": "pause",
    "怎么讲": "pause",
    "你知道吧": "pause",
    "you know": "pause",
    "i mean": "pause",
    "就是": "strict",
    "然后": "strict",
    "那么": "strict",
    "其实": "strict",
    "like": "strict",
    "so": "strict",
    "basically": "strict",
    "actually": "strict",
}

# 口癖前面紧跟这些字时不算口癖 (如「也就是说」「这就是」)
DEFAULT_KEEP_PREFIX: Dict[str, Tuple[str, ...]] = {
    "就是说": ("也", "就", "这"),
    "就是": ("也", "这", "那", "不", "就", "都", "正", "还", "只", "可", "便", "即", "要", "算"),
    "然后": ("先", "再"),
}

# 说错重来标记 (只在单独成句时生效, 「这样是不对的」不会触发)
#   强标记: 前面没找到同开头的句子时, 删紧挨着的上一句
DEFAULT_NG_MARKERS: Tuple[str, ...] = (
    "重来",
    "重新来",
    "再来一遍",
    "再来",
    "再说一遍",
    "重说",
    "重新说",
    "说错了",
    "讲错了",
    "我说错了",
    "cut",
    "let me restart",
    "start over",
    "let me start again",
)
#   弱标记: 「不对」「错了」也是正常表达, 只有后面确实重说了前面那句才剪
DEFAULT_NG_WEAK: Tuple[str, ...] = (
    "不对",
    "错了",
    "不是不是",
    "等一下",
    "ng",
    "sorry",
    "again",
)
# 标记句里可以附带的虚字 (「哎不对啊」「我重新说吧」仍算标记句)
_NG_FILLER_UNITS = set("啊呀吧了哈呢我们哎诶嗯呃额就那这个再一遍") | {"i", "let", "me", "oh", "ok"}

_PUNCT = set("，,。.!！?？;；:：、…—-~～\"'“”‘’()（）[]【】《》<>/\\|")
_CJK_RE = re.compile(r"[㐀-鿿豈-﫿]")
_TOKEN_RE = re.compile(r"[㐀-鿿豈-﫿]|[A-Za-z0-9']+|[^\sA-Za-z0-9'㐀-鿿]")

DEFAULTS: Dict[str, Any] = {
    "max_pause": 0.25,  # 保留的气口上限 (秒)
    "pad": 0.05,  # 切点前后至少留的余量, 防吞字
    "edge_keep": 0.15,  # 开头 / 结尾保留的静音
    "min_cut": 0.08,  # 只压缩超出上限这么多以上的停顿, 避免碎刀
    "pause_th": 0.18,  # 判定「有停顿」的间隔
    "prolong_th": 0.32,  # 单字平均时长超过它算「拖长音」
    "clause_gap": 0.45,  # 间隔超过它切分子句
    "stutter_gap": 0.1,  # 结巴: 两遍之间停顿超过它才算 (连读的「看看」「谢谢」不删)
    "similarity": 0.72,  # 重复句判定的文本相似度
    "retake_window": 12.0,  # 重复句 / 重来 最多往后看多少秒
    "ng_lookback": 3,  # 说错标记往前最多删几句
    "silence_trust": 0.3,  # analysis.silences 中长于它的静音用来修正被拉长的词尾/词头
}


# --------------------------------------------------------------------------------------
# 输入解析
# --------------------------------------------------------------------------------------


def _tokenize(text: str) -> List[str]:
    """拆成单元: 中文一个字一个单元, 英文 / 数字一个词一个单元, 标点单独一个。"""
    return _TOKEN_RE.findall(text or "")


def _is_punct(tok: str) -> bool:
    return all(c in _PUNCT or not c.strip() for c in tok)


def _weight(tok: str) -> float:
    return 1.0 if _CJK_RE.match(tok) else max(1.0, len(tok) / 3.0)


def _phrase_units(phrase: str) -> Tuple[str, ...]:
    return tuple(t.lower() for t in _tokenize(phrase) if not _is_punct(t))


def load_transcript(data: Any) -> Tuple[List[dict], List[dict], Optional[float], Optional[str]]:
    """解析 analysis.json / 转写 JSON, 返回 (segments, silences, duration, source)。"""
    silences: List[dict] = []
    duration = None
    source = None
    segs = data
    if isinstance(data, dict):
        silences = data.get("silences") or []
        video = data.get("video") or {}
        duration = video.get("duration") or data.get("duration")
        source = data.get("source")
        segs = data.get("transcript")
        if segs is None:
            segs = data.get("segments")
        if isinstance(segs, dict):  # {"transcript": {"segments": [...]}}
            segs = segs.get("segments")
    if segs is None:
        raise ValueError(
            "输入里没有转写 (transcript 为空): 先用 analyze_footage.py 转写, 或用 --transcript"
        )
    if not isinstance(segs, list):
        raise ValueError("转写格式不对: 需要 [{start, end, text, words: [{w, s, e}]}]")
    out = []
    for seg in segs:
        if not isinstance(seg, dict):
            continue
        words = []
        for w in seg.get("words") or []:
            txt = w.get("w", w.get("word", w.get("text", "")))
            s = w.get("s", w.get("start"))
            e = w.get("e", w.get("end"))
            if s is None or e is None:
                continue
            words.append({"w": str(txt), "s": float(s), "e": float(e)})
        out.append(
            {
                "start": float(seg.get("start", words[0]["s"] if words else 0.0)),
                "end": float(seg.get("end", words[-1]["e"] if words else 0.0)),
                "text": str(seg.get("text", "")),
                "words": words,
            }
        )
    return out, silences, (float(duration) if duration else None), source


def _build_units(segments: List[dict], warnings: List[str]) -> List[dict]:
    """把转写拆成带时间的单元 (中文按字, 英文按词), 词内按字数权重均分时长。"""
    units: List[dict] = []
    approx = 0
    for si, seg in enumerate(segments):
        words = seg["words"]
        if not words:  # 没有词级时间戳: 整句均分
            if not seg["text"].strip():
                continue
            words = [{"w": seg["text"], "s": seg["start"], "e": seg["end"]}]
            approx += 1
        for w in words:
            toks = _tokenize(w["w"])
            real = [t for t in toks if not _is_punct(t)]
            if not real:
                if units and toks:
                    units[-1]["punct_after"] = toks[-1]
                continue
            total = sum(_weight(t) for t in real)
            cur = w["s"]
            span = max(0.0, w["e"] - w["s"])
            for t in toks:
                if _is_punct(t):
                    if units:
                        units[-1]["punct_after"] = t
                    continue
                d = span * _weight(t) / total
                units.append(
                    {
                        "t": t.lower(),
                        "raw": t,
                        "s": cur,
                        "e": cur + d,
                        "seg": si,
                        "punct_after": False,
                        "cut": None,
                    }
                )
                cur += d
    units.sort(key=lambda u: (u["s"], u["e"]))
    for i, u in enumerate(units):
        u["i"] = i
    if approx:
        warnings.append(f"{approx} 句没有词级时间戳, 按字数均分时长, 口癖切点可能不准")
    return units


def _split_clauses(units: List[dict], clause_gap: float) -> List[List[int]]:
    """按标点、长停顿、转写分句切子句。"""
    clauses: List[List[int]] = []
    cur: List[int] = []
    for i, u in enumerate(units):
        if cur:
            prev = units[cur[-1]]
            if prev["punct_after"] or prev["seg"] != u["seg"] or u["s"] - prev["e"] >= clause_gap:
                clauses.append(cur)
                cur = []
        cur.append(i)
    if cur:
        clauses.append(cur)
    return clauses


def _join(toks: Sequence[str]) -> str:
    out = ""
    for t in toks:
        if out and not _CJK_RE.match(t) and not _CJK_RE.match(out[-1]):
            out += " "
        out += t
    return out


# --------------------------------------------------------------------------------------
# 检测
# --------------------------------------------------------------------------------------


def _mark(units, idxs, reason, detail, items, group=None):
    """把单元标记为剪掉 (已被标记的不覆盖), 记录一条剪辑原因。"""
    idxs = [i for i in idxs if units[i]["cut"] is None]
    if not idxs:
        return
    gid = group if group is not None else len(items)
    for i in idxs:
        units[i]["cut"] = gid
    if group is None:
        items.append(
            {
                "reason": reason,
                "text": _join([units[i]["raw"] for i in idxs]),
                "start": round(units[idxs[0]]["s"], 3),
                "end": round(units[idxs[-1]]["e"], 3),
                "detail": detail,
                "_units": list(idxs),
            }
        )
    else:
        items[gid]["_units"].extend(idxs)


def _detect_fillers(units, clauses, fillers, keep_prefix, cfg, items):
    phrases = sorted(
        ((p, _phrase_units(p), m) for p, m in fillers.items() if _phrase_units(p)),
        key=lambda x: -len(x[1]),
    )
    th, prolong = cfg["pause_th"], cfg["prolong_th"]

    def gap_before(i):
        return units[i]["s"] - units[i - 1]["e"] if i > 0 else 99.0

    def gap_after(j):
        return units[j + 1]["s"] - units[j]["e"] if j + 1 < len(units) else 99.0

    for cl in clauses:
        toks = [units[i]["t"] for i in cl]
        # 1) 找出子句里所有口癖命中 (长词优先, 不重叠)
        hits = []  # (pos, length, phrase, mode)
        taken = [False] * len(cl)
        for phrase, pu, mode in phrases:
            n = len(pu)
            for p in range(len(cl) - n + 1):
                if any(taken[p : p + n]) or tuple(toks[p : p + n]) != pu:
                    continue
                hits.append((p, n, phrase, mode))
                for k in range(p, p + n):
                    taken[k] = True
        if not hits:
            continue
        hits.sort()
        only_fillers = all(taken)
        starts = {h[0] for h in hits}
        for p, n, phrase, mode in hits:
            i, j = cl[p], cl[p + n - 1]
            gb, ga = gap_before(i), gap_after(j)
            first, last = p == 0, p + n == len(cl)
            per_unit = (units[j]["e"] - units[i]["s"]) / n
            prolonged = per_unit >= prolong * (1.0 if n == 1 else 0.8)
            chained = (p + n) in starts  # 后面紧跟另一个口癖 (那个那个 / 那个嗯)
            prev_tok = toks[p - 1] if p > 0 else ""
            if prev_tok and prev_tok in keep_prefix.get(phrase, ()):
                continue
            paused_after = ga >= th or (units[j]["punct_after"] and ga >= th * 0.5)
            paused_before = gb >= th
            delete, why = False, ""
            if mode == "always":
                delete, why = True, "填充音"
            elif only_fillers:
                delete, why = True, "整句只有口癖"
            elif mode == "isolated":
                if last and not paused_before and not first:
                    delete = False  # 句末语气词「好啊」
                elif first or paused_before or paused_after or prolonged:
                    delete, why = True, "孤立语气词"
            elif mode == "pause":
                if chained:
                    delete, why = True, "连说口癖"
                elif paused_after:
                    delete, why = True, "口癖后停顿"
                elif prolonged:
                    delete, why = True, "拖长音"
                elif first and paused_before and n >= 3:
                    delete, why = True, "句首口癖"
            elif mode == "strict":
                if chained:
                    delete, why = True, "连说口癖"
                elif (paused_before or first) and paused_after:
                    delete, why = True, "前后停顿的口癖"
            if delete:
                _mark(units, cl[p : p + n], "filler", f"{phrase}: {why}", items)


def _detect_stutters(units, clauses, cfg, items):
    """结巴 / 重复起词: 「我我我」「我们，我们」→ 删前面几遍。"""
    gap_th = cfg["stutter_gap"]
    for cl in clauses + [list(range(len(units)))]:  # 子句内 + 跨标点的相邻重复
        live = [i for i in cl if units[i]["cut"] is None]
        toks = [units[i]["t"] for i in live]
        p = 0
        while p < len(live):
            done = False
            for n in (3, 2, 1):
                if p + 2 * n > len(live):
                    continue
                unit = toks[p : p + n]
                if not any(_CJK_RE.match(t) or t.isalpha() for t in unit):
                    continue
                k = 1
                while toks[p + k * n : p + (k + 1) * n] == unit:
                    k += 1
                if k < 2:
                    continue
                gaps = [
                    units[live[p + c * n]]["s"] - units[live[p + c * n - 1]]["e"]
                    for c in range(1, k)
                ]
                if max(gaps) <= 1.0 and (k >= 3 or max(gaps) >= gap_th):
                    idxs = live[p : p + (k - 1) * n]
                    _mark(
                        units, idxs, "stutter", f"「{_join(unit)}」说了 {k} 遍, 留最后一遍", items
                    )
                    p += k * n
                    done = True
                    break
            if not done:
                p += 1


def _clean_toks(units, cl):
    return [units[i]["t"] for i in cl if units[i]["cut"] is None]


def _ratio(a: Sequence[str], b: Sequence[str]) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, list(a), list(b), autojunk=False).ratio()


def _common_prefix(a: Sequence[str], b: Sequence[str]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _same_start(a: Sequence[str], b: Sequence[str]) -> bool:
    """两句开头是不是同一句话的两次尝试。"""
    if len(a) < 2 or len(b) < 2:
        return False
    if _common_prefix(a, b) >= 2:
        return True
    k = min(len(a), len(b), 6)
    return k >= 3 and _ratio(a[:k], b[:k]) >= 0.7


_NUM_UNITS = set("零一二三四五六七八九十百千万两") | set("0123456789")


def _numeric_diff(a: Sequence[str], b: Sequence[str]) -> bool:
    """两句只差在数字 / 序号上 (排比), 不算重复。"""
    sm = SequenceMatcher(None, list(a), list(b), autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        seg = list(a[i1:i2]) + list(b[j1:j2])
        if seg and all(all(c in _NUM_UNITS for c in t) for t in seg):
            return True
    return False


def _strip_ng(toks: List[str], markers) -> Tuple[List[str], Optional[str]]:
    """去掉标记词后剩下的单元 (只剩虚字说明整句就是一个 NG 标记)。"""
    rest = list(toks)
    found = None
    changed = True
    while changed:
        changed = False
        for m in markers:
            n = len(m)
            for p in range(len(rest) - n + 1):
                if tuple(rest[p : p + n]) == m:
                    found = found or _join(m)
                    rest = rest[:p] + rest[p + n :]
                    changed = True
                    break
    return [t for t in rest if t not in _NG_FILLER_UNITS], found


def _detect_ng(units, clauses, markers, weak, cfg, items):
    """说错重来: 标记句 + 它前面说错的那句一起删。弱标记 (不对/错了) 要求后面确实重说了同一句。"""
    strong_mk = {_phrase_units(m) for m in markers if _phrase_units(m)}
    weak_mk = {_phrase_units(m) for m in weak if _phrase_units(m)} - strong_mk
    mk = sorted(strong_mk | weak_mk, key=lambda m: -len(m))
    for ci, cl in enumerate(clauses):
        toks = _clean_toks(units, cl)
        if not toks:
            continue
        rest, found = _strip_ng(toks, mk)
        if not found or rest:
            continue
        is_weak = _phrase_units(found) in weak_mk
        mstart = units[cl[0]]["s"]
        nxt = next((c for c in clauses[ci + 1 :] if _clean_toks(units, c)), None)
        redo = _clean_toks(units, nxt) if nxt else []
        cand = []
        for k in range(ci - 1, max(-1, ci - 1 - cfg["ng_lookback"]), -1):
            if mstart - units[clauses[k][-1]]["e"] > cfg["retake_window"]:
                break
            if _clean_toks(units, clauses[k]):
                cand.append(k)
        if is_weak and ci > 0 and units[clauses[ci - 1][-1]]["punct_after"] in ("?", "？"):
            continue  # 「这样对吗？不对。」是正常表达, 不剪
        # 往回找说错的那句: 开头和标记后重说的一样 (可以从子句中间开始, 子句切分不一定准)
        start_unit = None
        for k in cand:
            live = [i for i in clauses[k] if units[i]["cut"] is None]
            toks_k = [units[i]["t"] for i in live]
            exact = [p for p in range(len(live)) if _common_prefix(toks_k[p:], redo) >= 2]
            fuzzy = [p for p in range(len(live)) if redo and _same_start(toks_k[p:], redo)]
            if exact or fuzzy:
                start_unit = live[exact[-1] if exact else fuzzy[0]]
                break
        if start_unit is None:
            if is_weak:
                continue  # 弱标记后面没有重说同一句, 不剪
            if cand and mstart - units[clauses[cand[0]][-1]]["e"] <= 2.0:
                start_unit = clauses[cand[0]][0]  # 强标记: 删紧挨着的上一句
            else:
                start_unit = cl[0]
        idxs = list(range(start_unit, cl[-1] + 1))
        _mark(units, idxs, "ng", f"说错重来 (标记「{found}」), 保留后面重说的一遍", items)


def _retake_kind(a: Sequence[str], b: Sequence[str], sim: float, whole: bool) -> Optional[str]:
    """a 是不是 b 的一次失败尝试 (whole=False 时 a 是某句的后半截, 要求开头和 b 对上)。"""
    if len(a) < 2 or not b:
        return None
    if not whole and (len(a) < 3 or _common_prefix(a, b) < 2):
        return None
    if _numeric_diff(a, b):
        return None  # 「第一步… / 第二步…」是排比, 不是重说
    if len(a) < len(b) and list(b[: len(a)]) == list(a):
        return "起句重来"
    if whole and len(a) >= 4 and _ratio(a, b) >= sim:
        return "重复句"
    if len(a) >= 3 and len(b) > len(a) and _ratio(a, b[: len(a) + 2]) >= 0.75:
        return "起句重来"
    return None


def _detect_repeats(units, clauses, cfg, items):
    """重复句 / 起句重来: 只保留最后一遍。"""
    sim = cfg["similarity"]
    for ci in range(len(clauses)):
        live = [i for i in clauses[ci] if units[i]["cut"] is None]
        a = [units[i]["t"] for i in live]
        if len(a) < 2:
            continue
        a_end = units[live[-1]]["e"]
        for cj in range(ci + 1, min(len(clauses), ci + 4)):
            b = _clean_toks(units, clauses[cj])
            if not b:
                continue
            if units[clauses[cj][0]]["s"] - a_end > cfg["retake_window"]:
                break
            between = [_clean_toks(units, clauses[k]) for k in range(ci + 1, cj)]
            if any(len(x) > 6 for x in between):
                break  # 中间隔着正常的句子, 不当作重来
            hit = None
            for p in range(0, len(a) - 1):
                why = _retake_kind(a[p:], b, sim, whole=p == 0)
                if why:
                    hit = (p, why)
                    break
            if hit:
                p, why = hit
                idxs = live[p:] + [i for k in range(ci + 1, cj) for i in clauses[k]]
                _mark(units, idxs, "repeat", f"{why}, 保留后一遍「{_join(b)}」", items)
                break


# --------------------------------------------------------------------------------------
# 区间计算
# --------------------------------------------------------------------------------------


def _refine_spans(units, silences, trust):
    """词级时间戳常把字拖进后面的静音里: 用 ffmpeg 静音段修正保留字的首尾。"""
    sil = sorted(
        (float(s["start"]), float(s["end"]))
        for s in silences or []
        if float(s.get("end", 0)) - float(s.get("start", 0)) >= trust
    )
    spans = {}
    for u in units:
        if u["cut"] is not None:
            continue
        s, e = u["s"], u["e"]
        for a, b in sil:
            if b <= s or a >= e:
                continue
            if a <= s and b >= e:
                continue  # 整个字都在「静音」里: 可能是小声说话, 不信静音
            if a > s and b >= e and a - s >= 0.04:
                e = a
            elif a <= s and b < e and e - b >= 0.04:
                s = b
        spans[u["i"]] = (s, e)
    return spans


def _build_keep(units, spans, items, duration, cfg):
    max_pause, pad, min_cut, edge = cfg["max_pause"], cfg["pad"], cfg["min_cut"], cfg["edge_keep"]
    removed = sorted(
        (units[it["_units"][0]]["s"], max(units[i]["e"] for i in it["_units"])) for it in items
    )
    kept = [(spans[u["i"]][0], spans[u["i"]][1]) for u in units if u["cut"] is None]
    if not kept:
        return []

    def removed_between(a, b):
        return [(max(a, x), min(b, y)) for x, y in removed if x < b and y > a]

    pieces = []
    ps, pe = kept[0]
    for s, e in kept[1:]:
        s = max(s, pe)
        gap = s - pe
        rm = removed_between(pe, s)
        if not rm and gap <= max_pause + min_cut:
            pe = max(pe, e)
            continue
        pieces.append([ps, pe])
        ps, pe = s, e
    pieces.append([ps, pe])

    # 两个保留段之间: 把停顿 (去掉被剪内容后的部分) 压到 max_pause, 首尾各分一半
    for k in range(len(pieces) - 1):
        a, b = pieces[k][1], pieces[k + 1][0]
        rm = removed_between(a, b)
        if rm:
            left_avail = rm[0][0] - a
            right_avail = b - rm[-1][1]
        else:
            left_avail = right_avail = b - a
        pause_total = max(0.0, (b - a) - sum(y - x for x, y in rm))
        target = min(pause_total, max_pause)
        tail = min(left_avail, max(target / 2, min(pad, target)))
        head = min(right_avail, target - tail)
        if head < target - tail:
            tail = min(left_avail, target - head)
        if not rm:
            tail = min(tail, (b - a) / 2)
            head = min(head, (b - a) / 2)
        pieces[k][1] = a + max(0.0, tail)
        pieces[k + 1][0] = b - max(0.0, head)

    # 开头 / 结尾
    first_rm = removed_between(0.0, pieces[0][0])
    lo = first_rm[-1][1] if first_rm else 0.0
    pieces[0][0] = max(lo, pieces[0][0] - edge)
    end_lim = duration if duration else pieces[-1][1] + edge
    last_rm = removed_between(pieces[-1][1], end_lim)
    hi = last_rm[0][0] if last_rm else end_lim
    pieces[-1][1] = min(hi, pieces[-1][1] + edge)
    return [(round(max(0.0, s), 3), round(e, 3)) for s, e in pieces if e - s > 0.001]


def remap_time(plan: Dict[str, Any], t: float) -> Optional[float]:
    """源视频时间 -> 精剪后时间线时间; 落在被剪区间里返回 None。"""
    keep = plan["keep"]
    starts = [k["start"] for k in keep]
    i = bisect_right(starts, t) - 1
    if i < 0 or t > keep[i]["end"] + 1e-6:
        return None
    return round(keep[i]["target_start"] + (t - keep[i]["start"]), 3)


def _clamp_remap(keep, t, side):
    """把落在剪掉区间里的时间吸到最近的保留边界。"""
    starts = [k["start"] for k in keep]
    i = bisect_right(starts, t) - 1
    if i >= 0 and t <= keep[i]["end"]:
        return keep[i]["target_start"] + (t - keep[i]["start"])
    if side == "start":
        j = i + 1
        return (
            keep[j]["target_start"]
            if j < len(keep)
            else keep[-1]["target_start"] + keep[-1]["duration"]
        )
    if i < 0:
        return 0.0
    return keep[i]["target_start"] + keep[i]["duration"]


def _clean_transcript(units, spans, segments, keep):
    out = []
    for si, seg in enumerate(segments):
        us = [u for u in units if u["seg"] == si and u["cut"] is None]
        if not us:
            continue
        words = []
        for u in us:
            s = _clamp_remap(keep, spans[u["i"]][0], "start")
            e = _clamp_remap(keep, spans[u["i"]][1], "end")
            words.append({"w": u["raw"], "s": round(s, 3), "e": round(max(e, s), 3)})
        out.append(
            {
                "start": words[0]["s"],
                "end": words[-1]["e"],
                "text": _join([u["raw"] for u in us]),
                "words": words,
            }
        )
    return out


# --------------------------------------------------------------------------------------
# 公开 API
# --------------------------------------------------------------------------------------


def plan_cleanup(
    transcript: Any,
    silences: Optional[List[dict]] = None,
    duration: Optional[float] = None,
    *,
    max_pause: float = DEFAULTS["max_pause"],
    pad: float = DEFAULTS["pad"],
    fillers: Optional[Dict[str, str]] = None,
    extra_fillers: Optional[Dict[str, str]] = None,
    keep_prefix: Optional[Dict[str, Sequence[str]]] = None,
    ng_markers: Optional[Sequence[str]] = None,
    remove_fillers: bool = True,
    remove_stutters: bool = True,
    remove_ng: bool = True,
    remove_repeats: bool = True,
    compress_pauses: bool = True,
    **overrides,
) -> Dict[str, Any]:
    """
    规划口播精剪。

    参数:
      transcript   analysis.json 的 dict (自动读取 transcript / silences / video.duration),
                   或转写列表 [{start, end, text, words: [{w, s, e}]}]
      silences     [{start, end}] 静音段 (可选, 用来修正被拉进静音的字)
      duration     源视频时长 (秒), 不给则取 analysis 里的值或最后一个字 + edge_keep
      max_pause    保留的气口上限; pad 切点余量
      fillers      覆盖默认口癖表 {词: always|isolated|pause|strict}; extra_fillers 在默认表上追加
      ng_markers   覆盖默认的「说错重来」标记
      其余开关关闭对应检测; overrides 可覆盖 DEFAULTS 里的阈值 (如 similarity=0.7)
    返回: {keep, cuts, transcript, stats, settings, warnings, source}
    """
    unknown = set(overrides) - set(DEFAULTS)
    if unknown:
        raise TypeError(f"未知参数: {', '.join(sorted(unknown))}")
    cfg = dict(DEFAULTS, **overrides, max_pause=max_pause, pad=pad)
    if not compress_pauses:
        cfg["max_pause"] = 1e9
    segs, sil, dur, source = load_transcript(transcript)
    if silences is not None:
        sil = silences
    duration = duration or dur
    table = dict(DEFAULT_FILLERS if fillers is None else fillers)
    table.update(extra_fillers or {})
    for w, m in table.items():
        if m not in ("always", "isolated", "pause", "strict"):
            raise ValueError(f"口癖「{w}」的模式 {m!r} 无效 (always/isolated/pause/strict)")
    kp = {k: tuple(v) for k, v in (keep_prefix or DEFAULT_KEEP_PREFIX).items()}

    warnings: List[str] = []
    units = _build_units(segs, warnings)
    items: List[dict] = []
    if units:
        clauses = _split_clauses(units, cfg["clause_gap"])
        if remove_fillers:
            _detect_fillers(units, clauses, table, kp, cfg, items)
        if remove_stutters:
            _detect_stutters(units, clauses, cfg, items)
        if remove_ng:
            if ng_markers is None:
                _detect_ng(units, clauses, DEFAULT_NG_MARKERS, DEFAULT_NG_WEAK, cfg, items)
            else:
                _detect_ng(units, clauses, ng_markers, (), cfg, items)
        if remove_repeats:
            _detect_repeats(units, clauses, cfg, items)
    else:
        warnings.append("转写里没有可用的字, 不做剪辑")

    spans = _refine_spans(units, sil, cfg["silence_trust"])
    pieces = _build_keep(units, spans, items, duration, cfg)
    if not pieces:
        total = duration or 0.0
        pieces = [(0.0, round(total, 3))] if total > 0 else []
        if units:
            warnings.append("所有内容都被判定为要剪掉, 已回退为保留全片, 请检查参数")
            items = []
            for u in units:
                u["cut"] = None
            spans = _refine_spans(units, sil, cfg["silence_trust"])
    total = duration or (pieces[-1][1] if pieces else 0.0)

    keep = []
    t = 0.0
    for s, e in pieces:
        txt = _join([u["raw"] for u in units if u["cut"] is None and s <= u["s"] < e])
        keep.append(
            {
                "start": s,
                "end": e,
                "duration": round(e - s, 3),
                "target_start": round(t, 3),
                "text": txt,
            }
        )
        t += e - s

    # 剪掉的区间 = 保留区间的补集, 标注原因
    cuts = []
    bounds = [(0.0, keep[0]["start"])] if keep else []
    bounds += [(keep[k]["end"], keep[k + 1]["start"]) for k in range(len(keep) - 1)]
    if keep:
        bounds.append((keep[-1]["end"], total))
    by_reason: Dict[str, Dict[str, float]] = {}

    def add(reason, secs, count=0):
        r = by_reason.setdefault(reason, {"count": 0, "seconds": 0.0})
        r["count"] += count
        r["seconds"] = round(r["seconds"] + secs, 3)

    for it in items:
        add(it["reason"], 0.0, 1)
    for a, b in bounds:
        if b - a <= 0.001:
            continue
        its = [it for it in items if it["start"] < b and it["end"] > a]
        # 把区间按条目边界切开, 每小段记到覆盖它的最小条目 (说错重来里嵌着的口癖算口癖), 其余是气口
        filler_reason = "edge" if (a <= 0.001 or b >= total - 0.001) else "pause"
        edges = sorted({a, b} | {min(b, max(a, x)) for it in its for x in (it["start"], it["end"])})
        secs: Dict[str, float] = {}
        for x, y in zip(edges, edges[1:]):
            mid = (x + y) / 2
            cover = [it for it in its if it["start"] <= mid <= it["end"]]
            r = (
                min(cover, key=lambda it: it["end"] - it["start"])["reason"]
                if cover
                else filler_reason
            )
            secs[r] = secs.get(r, 0.0) + (y - x)
        secs = {r: v for r, v in secs.items() if v > 0.001}
        for r, v in secs.items():
            add(r, v, 1 if r in ("pause", "edge") else 0)
        order = sorted(secs, key=lambda r: (r in ("pause", "edge"), -secs[r]))
        cuts.append(
            {
                "start": round(a, 3),
                "end": round(b, 3),
                "duration": round(b - a, 3),
                "reason": order[0] if order else filler_reason,
                "reasons": order or [filler_reason],
                "seconds": {r: round(v, 3) for r, v in secs.items()},
                "items": [{k: v for k, v in it.items() if k != "_units"} for it in its],
            }
        )

    item_counts: Dict[str, int] = {}
    for it in items:
        item_counts[it["reason"]] = item_counts.get(it["reason"], 0) + 1
    kept = round(sum(k["duration"] for k in keep), 3)
    stats = {
        "original": round(total, 3),
        "kept": kept,
        "removed": round(total - kept, 3),
        "keep_ratio": round(kept / total, 3) if total else 0.0,
        "pieces": len(keep),
        "by_reason": by_reason,
        "items": item_counts,
    }
    return {
        "source": source,
        "duration": round(total, 3),
        "settings": {
            **{k: cfg[k] for k in DEFAULTS},
            "max_pause": max_pause if compress_pauses else None,
            "remove_fillers": remove_fillers,
            "remove_stutters": remove_stutters,
            "remove_ng": remove_ng,
            "remove_repeats": remove_repeats,
        },
        "keep": keep,
        "cuts": cuts,
        "transcript": _clean_transcript(units, spans, segs, keep) if keep else [],
        "stats": stats,
        "warnings": warnings,
    }


def build_cleanup_draft(
    plan: Dict[str, Any],
    video_path: str,
    project_name: str,
    drafts_root: Optional[str] = None,
    track_name: str = "VideoTrack",
    punch_in: Optional[float] = None,
    overwrite: bool = True,
    save: bool = True,
) -> Dict[str, Any]:
    """按 plan['keep'] 把 video_path 依次 add_clip 进新草稿 (可选假景别交替缩放), 返回 save() 结果。"""
    from core.speech_cut_ops import SpeechCutOpsMixin
    from jy_wrapper import JyProject

    class _Project(JyProject, SpeechCutOpsMixin):
        pass

    project = _Project(project_name, drafts_root=drafts_root, overwrite=overwrite)
    segs = project.add_speech_cleanup(video_path, plan, track_name=track_name, punch_in=punch_in)
    result = {"draft_name": project.name, "segments": len(segs)}
    if save:
        result.update(project.save())
    return result


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _parse_filler_args(items: Optional[List[str]]) -> Dict[str, str]:
    out = {}
    for raw in items or []:
        for part in re.split(r"[,，]", raw):
            part = part.strip()
            if not part:
                continue
            word, _, mode = part.partition(":")
            out[word.strip()] = (mode.strip() or "pause").lower()
    return out


def _main(argv=None) -> int:
    from utils.cli_protocol import emit_result, make_result

    ap = argparse.ArgumentParser(
        description="口播精剪: 删口癖 / 结巴 / 说错重来 / 重复句, 压缩气口"
    )
    ap.add_argument("input", help="analysis.json 或转写 JSON")
    ap.add_argument("--transcript", help="另给转写 JSON (覆盖 input 里的 transcript)")
    ap.add_argument("--video", help="源视频 (生成草稿用, 默认取 analysis.json 的 source)")
    ap.add_argument("--duration", type=float, help="源视频时长 (秒)")
    ap.add_argument("-o", "--output", help="输出 plan JSON (默认 <input 目录>/cleanup_plan.json)")
    ap.add_argument("--max-pause", type=float, default=DEFAULTS["max_pause"], help="气口上限 (秒)")
    ap.add_argument("--pad", type=float, default=DEFAULTS["pad"], help="切点余量 (秒)")
    ap.add_argument("--similarity", type=float, default=DEFAULTS["similarity"])
    ap.add_argument("--fillers", action="append", help="覆盖口癖表: 嗯:always,那个:pause")
    ap.add_argument("--extra-fillers", action="append", help="追加口癖: 对吧:strict,然后呢")
    ap.add_argument("--filler-file", help="口癖表 JSON {词: 模式}, 覆盖默认表")
    ap.add_argument("--ng-markers", help="说错重来标记, 逗号分隔 (覆盖默认)")
    ap.add_argument("--no-fillers", action="store_true")
    ap.add_argument("--no-stutters", action="store_true")
    ap.add_argument("--no-ng", action="store_true")
    ap.add_argument("--no-repeats", action="store_true")
    ap.add_argument("--keep-pauses", action="store_true", help="不压缩气口")
    ap.add_argument("--draft", help="直接生成剪映草稿 (草稿名)")
    ap.add_argument("--drafts-root", help="草稿根目录 (默认剪映草稿目录)")
    ap.add_argument("--track", default="VideoTrack")
    ap.add_argument("--punch-in", type=float, help="剪点后交替放大 (假景别), 如 1.25")
    ap.add_argument("--json", action="store_true", help="输出 {ok, code, reason, data}")
    args = ap.parse_args(argv)

    def fail(code, reason):
        emit_result(make_result(False, code, reason), args.json)
        if not args.json:
            print(f"❌ {reason}", file=sys.stderr)
        return 1

    try:
        with open(args.input, "r", encoding="utf-8") as f:
            data = json.load(f)
        if args.transcript:
            with open(args.transcript, "r", encoding="utf-8") as f:
                tr = json.load(f)
            tr_segs = tr.get("transcript", tr.get("segments")) if isinstance(tr, dict) else tr
            data = dict(data, transcript=tr_segs) if isinstance(data, dict) else tr_segs
        fillers = None
        if args.filler_file:
            with open(args.filler_file, "r", encoding="utf-8") as f:
                fillers = json.load(f)
        if args.fillers:
            fillers = _parse_filler_args(args.fillers)
        markers = None
        if args.ng_markers:
            markers = [m.strip() for m in re.split(r"[,，]", args.ng_markers) if m.strip()]
        plan = plan_cleanup(
            data,
            duration=args.duration,
            max_pause=args.max_pause,
            pad=args.pad,
            fillers=fillers,
            extra_fillers=_parse_filler_args(args.extra_fillers),
            ng_markers=markers,
            remove_fillers=not args.no_fillers,
            remove_stutters=not args.no_stutters,
            remove_ng=not args.no_ng,
            remove_repeats=not args.no_repeats,
            compress_pauses=not args.keep_pauses,
            similarity=args.similarity,
        )
    except FileNotFoundError as e:
        return fail("not_found", f"文件不存在: {e.filename}")
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        return fail("invalid_input", str(e))

    out = args.output or os.path.join(
        os.path.dirname(os.path.abspath(args.input)), "cleanup_plan.json"
    )
    with open(out, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)

    draft_info = None
    if args.draft:
        video = args.video or plan.get("source")
        if not video or not os.path.exists(video):
            return fail("not_found", f"生成草稿需要源视频: --video (当前: {video})")
        if not plan["keep"]:
            return fail("empty_plan", "没有可保留的区间")
        draft_info = build_cleanup_draft(
            plan, video, args.draft, args.drafts_root, args.track, args.punch_in
        )

    st = plan["stats"]
    data_out = {"plan_path": out, "stats": st, "warnings": plan["warnings"], "draft": draft_info}
    if args.json:
        emit_result(make_result(True, "ok", "", data_out), True)
        return 0
    print(
        f"✂️  原 {st['original']:.2f}s → 保留 {st['kept']:.2f}s ({st['keep_ratio']:.0%}), "
        f"{st['pieces']} 段, 剪掉 {len(plan['cuts'])} 处"
    )
    names = {
        "filler": "口癖",
        "stutter": "结巴",
        "ng": "说错重来",
        "repeat": "重复句",
        "pause": "气口压缩",
        "edge": "首尾静音",
    }
    for r, v in st["by_reason"].items():
        print(f"   - {names.get(r, r)}: {v['count']} 处, {v['seconds']:.2f}s")
    for c in plan["cuts"]:
        for it in c["items"]:
            print(f"     {it['start']:7.2f}s 「{it['text']}」 {it['detail']}")
    for w in plan["warnings"]:
        print(f"⚠️ {w}")
    if draft_info:
        print(f"🎬 草稿: {draft_info.get('draft_path', draft_info['draft_name'])}")
    print(f"✅ {out}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from utils.env_setup import setup_env

    setup_env()
    sys.exit(_main())
