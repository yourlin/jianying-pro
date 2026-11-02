"""
多版本输出: 一份剧本 (或已编译的分镜表) -> 15s / 30s / 60s 删减版计划 + 每个版本 × 画幅的编译产物。

    python variants.py script.json [--targets 15,30,60] [--formats vertical,horizontal] [--style tech]
                       [-o variants/] [--plan-only] [--json]
    python variants.py storyboard.json --targets 15,30            # 已生成的分镜表: 只删减, 不换画幅

删减规则 (按段落优先级, 数字越小越重要):
  - 段落可写 "priority": 0–5, 或 "tags": ["must" / "hook" / "cta" / "optional"],
    或 "keep_in": ["15s"] / "drop_in": ["60s"] 指定某个版本必须保留 / 去掉;
  - 没写时按 purpose 推断: 钩子 / CTA = 0 (必留), 点题 = 1, 总结 / 金句 = 2, 干货 / 要点 = 3, 铺垫 / 过渡 / 彩蛋 = 4;
  - 先放必留段, 再按优先级 (同级按原顺序) 往里加, 不超过 目标时长 × (1 + 容差);
  - 放不下整段时, 有转写的段落按句子截短 (保留前面的句子), 不在句子中间切;
  - 最后按原时间顺序排列, 相邻区间合并 (少一个剪点)。

编译: 每个版本写一份变体剧本 (只含选中的段落, 时间写成源视频秒), 用 script_compiler.Compiler 编译,
再把分镜表的主轨 keep / 字幕 / 运镜 / 各元素裁到选中区间。装配命令写在输出的 builds[].build_cmd 里
(本脚本不装配, 不碰剪映草稿目录)。
"""

import argparse
import copy
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from script_compiler import (  # noqa: E402
    Compiler,
    ScriptError,
    Transcript,
    load_profile,
    resolve_time,
)
from utils.cli_protocol import emit_result, make_result  # noqa: E402

DEFAULT_TARGETS = (15, 30, 60)
TOLERANCE = 0.1
SLACK = 0.05

# purpose 关键词 -> 优先级 (先匹配先用)
PURPOSE_PRIORITY: List[Tuple[Tuple[str, ...], int]] = [
    (("钩子", "hook", "冷开场", "开头", "开场"), 0),
    (("cta", "互动", "关注", "行动号召", "结尾", "ending"), 0),
    (("点题", "主题", "intro", "引入"), 1),
    (("总结", "结论", "金句", "收束", "summary", "conclusion"), 2),
    (("干货", "要点", "论证", "证据", "例子", "案例", "溯源", "核心", "point", "proof"), 3),
    (("铺垫", "过渡", "背景", "彩蛋", "花絮", "bonus", "transition", "background"), 4),
]
TAG_PRIORITY = {"must": 0, "hook": 0, "cta": 0, "core": 2, "optional": 4, "cut_first": 5}


class VariantError(ValueError):
    pass


def _label(target: float) -> str:
    return f"{int(target)}s" if float(target).is_integer() else f"{target}s"


def _in_list(target: float, lst) -> bool:
    for x in lst or []:
        s = str(x).strip().lower().rstrip("s")
        try:
            if abs(float(s) - float(target)) < 1e-6:
                return True
        except ValueError:
            continue
    return False


def beat_priority(beat: dict, index: int, count: int) -> int:
    """段落优先级: 显式 priority > tags > purpose 关键词 > 元素推断 > 位置 (首段当钩子, 带 cta 的当 CTA)。"""
    if beat.get("priority") is not None:
        return int(beat["priority"])
    tags = [str(t).lower() for t in beat.get("tags") or []]
    tp = [TAG_PRIORITY[t] for t in tags if t in TAG_PRIORITY]
    if tp:
        return min(tp)
    purpose = str(beat.get("purpose", "")).lower()
    for keys, p in PURPOSE_PRIORITY:
        if any(k in purpose for k in keys):
            return p
    kinds = {e.get("kind") for e in beat.get("elements") or []}
    if "cta" in kinds or "end_card" in kinds:
        return 0
    if index == 0:
        return 0
    if "hero" in kinds:
        return 1
    return 3


# ---------------------------------------------------------------------------
# 段落 -> 可删减单元
# ---------------------------------------------------------------------------


def _sentences(tr: Transcript, s: float, e: float) -> List[Tuple[float, float]]:
    """段落内的句子边界 (源时间); 句子按段落边界截齐。"""
    out = []
    for seg in tr.segments:
        a, b = float(seg["start"]), float(seg["end"])
        if b <= s + SLACK or a >= e - SLACK:
            continue
        out.append((max(a, s), min(b, e)))
    return out


def units_from_script(script: dict, base_dir: str) -> Tuple[List[dict], float]:
    """剧本 beats -> 单元 [{id, order, start, end, priority, sentences, beat}]; 返回 (单元, 全片时长)。"""
    analysis = {}
    if script.get("analysis"):
        p = script["analysis"]
        p = p if os.path.isabs(p) else os.path.join(base_dir, p)
        with open(p, "r", encoding="utf-8") as f:
            analysis = json.load(f)
    tr = Transcript(analysis.get("transcript") or [])
    video_end = float((analysis.get("video") or {}).get("duration") or 0) or tr.end
    beats = script.get("beats") or []
    if not beats:
        raise VariantError("剧本没有 beats")
    units, prev = [], 0.0
    for i, b in enumerate(beats):
        try:
            bt = resolve_time(b.get("time"), tr, prev)
        except ScriptError as e:
            raise VariantError(f"段落 {b.get('id', i + 1)}: {e}") from e
        if bt is None:
            raise VariantError(f"段落 {b.get('id', i + 1)} 需要 time (时间或台词)")
        s, e = bt
        if i == len(beats) - 1 and video_end > e:  # 和编译器一致: 最后一段延到成片结尾
            e = min(video_end, max(e, tr.end + 0.35) if tr.words else video_end)
        units.append(
            {
                "id": b.get("id", f"S{i + 1:02d}"),
                "order": i,
                "start": round(s, 3),
                "end": round(e, 3),
                "priority": beat_priority(b, i, len(beats)),
                "sentences": _sentences(tr, s, e),
                "keep_in": b.get("keep_in") or [],
                "drop_in": b.get("drop_in") or [],
                "trim": b.get("trim", True),
                "purpose": b.get("purpose", ""),
            }
        )
        prev = e
    full = max(u["end"] for u in units) - min(u["start"] for u in units)
    return units, round(full, 3)


def units_from_storyboard(sb: dict) -> Tuple[List[dict], float]:
    """已编译 / 手写分镜表的 shots -> 单元 (句子边界取 subtitles.lines)。"""
    shots = sb.get("shots") or []
    if not shots:
        raise VariantError("分镜表没有 shots")
    lines = (sb.get("subtitles") or {}).get("lines") or []
    tr = Transcript(
        [{"start": ln["start"], "end": ln["end"], "text": ln.get("text", "")} for ln in lines]
    )
    units = []
    for i, sh in enumerate(shots):
        s, e = float(sh["time"][0]), float(sh["time"][1])
        units.append(
            {
                "id": sh.get("id", f"S{i + 1:02d}"),
                "order": i,
                "start": s,
                "end": e,
                "priority": beat_priority(sh, i, len(shots)),
                "sentences": _sentences(tr, s, e),
                "keep_in": sh.get("keep_in") or [],
                "drop_in": sh.get("drop_in") or [],
                "trim": sh.get("trim", True),
                "purpose": sh.get("purpose", ""),
            }
        )
    full = sum(u["end"] - u["start"] for u in units)
    return units, round(full, 3)


# ---------------------------------------------------------------------------
# 删减计划
# ---------------------------------------------------------------------------


def _dur(u: dict) -> float:
    return u["end"] - u["start"]


def _trimmed(u: dict, max_len: float) -> Optional[dict]:
    """保留前面的句子, 截到 <= max_len; 至少要留一句, 不行返回 None。"""
    if not u.get("trim", True) or len(u["sentences"]) < 2:
        return None
    best = None
    for k in range(1, len(u["sentences"])):
        end = round(
            min(u["end"], u["sentences"][k - 1][1] + 0.25, u["sentences"][k][0]), 3
        )  # 留一点呼吸
        if end - u["start"] <= max_len + 1e-6:
            best = end
        else:
            break
    if best is None:
        return None
    return {**u, "end": best, "trimmed": True, "full_end": u["end"]}


def plan_variant(units: List[dict], target: float, tolerance: float = TOLERANCE) -> dict:
    budget = target * (1 + tolerance)
    forced = [u for u in units if _in_list(target, u["keep_in"])]
    banned = {u["id"] for u in units if _in_list(target, u["drop_in"])}
    must = [u for u in units if (u["priority"] <= 0 or u in forced) and u["id"] not in banned]
    warnings = []
    sel: Dict[str, dict] = {u["id"]: dict(u) for u in must}
    total = sum(_dur(u) for u in sel.values())
    if total > budget:  # 必留段就超了: 先截长的、非开头的段落
        for u in sorted(sel.values(), key=lambda x: (x["order"] == 0, -_dur(x))):
            if total <= budget:
                break
            t = _trimmed(u, _dur(u) - (total - budget)) or _shortest(u)  # 截不够就至少留第一句
            if t and _dur(t) < _dur(u):
                total -= _dur(u) - _dur(t)
                sel[u["id"]] = t
        if total > budget:
            warnings.append(
                f"必留段落共 {total:.1f}s, 超过 {_label(target)} 上限 {budget:.1f}s: "
                f"给段落标 priority / drop_in, 或换更短的钩子 / CTA"
            )
    rest = sorted(
        (u for u in units if u["id"] not in sel and u["id"] not in banned),
        key=lambda x: (x["priority"], x["order"]),
    )
    for u in rest:
        room = budget - total
        if room <= 0.5:
            break
        if _dur(u) <= room + 1e-6:
            sel[u["id"]] = dict(u)
            total += _dur(u)
        else:
            t = _trimmed(u, room)
            if t and _dur(t) >= min(3.0, _dur(u) * 0.5):
                sel[u["id"]] = t
                total += _dur(t)
            elif u["priority"] <= 2 and not u["sentences"]:
                warnings.append(
                    f"{u['id']} ({_dur(u):.1f}s) 放不下, 又没有转写可按句截短: "
                    f"给剧本配 analysis.json, 或把这段拆成更短的段落"
                )
    chosen = sorted(sel.values(), key=lambda x: x["order"])
    if total < target * 0.6:
        warnings.append(
            f"只有 {total:.1f}s 内容, 不到 {_label(target)} 的 60%: 素材本身太短, 这个版本可以不出"
        )
    ranges = merge_ranges([(u["start"], u["end"]) for u in chosen])
    return {
        "target": target,
        "label": _label(target),
        "duration": round(total, 2),
        "budget": round(budget, 2),
        "beats": [
            {
                "id": u["id"],
                "range": [u["start"], u["end"]],
                "priority": u["priority"],
                "purpose": u["purpose"],
                "trimmed": bool(u.get("trimmed")),
            }
            for u in chosen
        ],
        "dropped": [u["id"] for u in units if u["id"] not in sel],
        "ranges": [[round(a, 3), round(b, 3)] for a, b in ranges],
        "warnings": warnings,
    }


def _shortest(u: dict) -> Optional[dict]:
    if not u.get("trim", True) or len(u["sentences"]) < 2:
        return None
    end = round(min(u["end"], u["sentences"][0][1] + 0.25, u["sentences"][1][0]), 3)
    return {**u, "end": end, "trimmed": True, "full_end": u["end"]}


def merge_ranges(
    ranges: List[Tuple[float, float]], gap: float = SLACK
) -> List[Tuple[float, float]]:
    out: List[List[float]] = []
    for a, b in sorted(ranges):
        if b - a <= 0:
            continue
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def intersect_ranges(a: List[List[float]], b: List[List[float]]) -> List[List[float]]:
    out = []
    for s1, e1 in a:
        for s2, e2 in b:
            s, e = max(s1, s2), min(e1, e2)
            if e - s > 0.04:
                out.append([round(s, 3), round(e, 3)])
    return [list(r) for r in merge_ranges([tuple(r) for r in out], gap=0.0)]


# ---------------------------------------------------------------------------
# 分镜表裁剪
# ---------------------------------------------------------------------------


def _range_of(t: float, ranges: List[List[float]]) -> Optional[List[float]]:
    for a, b in ranges:
        if a - SLACK <= t < b:
            return [a, b]
    return None


def restrict_storyboard(sb: dict, ranges: List[List[float]]) -> dict:
    """把分镜表裁到 ranges (源时间): 主轨 keep 取交集; 镜头 / 元素 / 运镜 / 字幕只留开始时间落在区间里的,
    时长截到区间末尾。"""
    sb = copy.deepcopy(sb)
    main = sb.setdefault("main", {})
    keep = main.get("keep") or []
    if sum(max(0.0, b - a) for a, b in keep) > 0.05:
        main["keep"] = intersect_ranges(keep, ranges)
    else:  # 没有转写 / 没有分析时编译器给的 keep 是空的: 直接用区间
        main["keep"] = [list(r) for r in ranges]
    if isinstance(main.get("transitions"), list):  # 剪点数变了, 按剪点顺序的列表不再对应
        main["transitions"] = next((t for t in main["transitions"] if t), None)
    cams = []
    for c in main.get("camera") or []:
        r = _range_of(float(c["at"]), ranges)
        if r:
            cams.append({**c, "dur": round(min(float(c.get("dur", 0)), r[1] - float(c["at"])), 3)})
    main["camera"] = cams
    shots = []
    for sh in sb.get("shots") or []:
        s, e = float(sh["time"][0]), float(sh["time"][1])
        inside = [r for r in ranges if r[1] > s + SLACK and r[0] < e - SLACK]
        if not inside:
            continue
        sh["time"] = [round(max(s, inside[0][0]), 3), round(min(e, inside[-1][1]), 3)]
        for key in ("overlays", "texts", "effects", "stickers"):
            items = []
            for it in sh.get(key) or []:
                st = float(it.get("start", sh["time"][0]))
                r = _range_of(st, ranges)
                if not r:
                    continue
                if it.get("dur") is not None:
                    it = {**it, "dur": round(min(float(it["dur"]), r[1] - st), 3)}
                if it.get("dur") is None or it["dur"] > 0:
                    items.append(it)
            if key in sh:
                sh[key] = items
        if "sfx" in sh:
            sh["sfx"] = [x for x in sh["sfx"] if _range_of(float(x.get("at", 0)), ranges)]
        shots.append(sh)
    sb["shots"] = shots
    subs = sb.get("subtitles")
    if subs and subs.get("lines"):
        lines = []
        for ln in subs["lines"]:
            r = _range_of(float(ln["start"]), ranges)
            if r:
                lines.append({**ln, "end": round(min(float(ln["end"]), r[1]), 3)})
        subs["lines"] = lines
    return sb


# ---------------------------------------------------------------------------
# 编译
# ---------------------------------------------------------------------------


def _abs_paths(script: dict, base_dir: str) -> dict:
    sc = copy.deepcopy(script)
    if sc.get("analysis") and not os.path.isabs(sc["analysis"]):
        sc["analysis"] = os.path.abspath(os.path.join(base_dir, sc["analysis"]))
    srcs = {}
    for k, v in (sc.get("sources") or {}).items():
        srcs[k] = (
            v
            if (not isinstance(v, str) or os.path.isabs(v))
            else os.path.abspath(os.path.join(base_dir, v))
        )
    if srcs:
        sc["sources"] = srcs
    return sc


def variant_script(script: dict, plan: dict, base_dir: str) -> dict:
    """只含选中段落的剧本: 时间写成源视频秒 (截短段落用截短后的区间), 草稿名加版本后缀。"""
    sc = _abs_paths(script, base_dir)
    keep = {b["id"]: b for b in plan["beats"]}
    beats = []
    for i, b in enumerate(script.get("beats") or []):
        bid = b.get("id", f"S{i + 1:02d}")
        if bid in keep:
            nb = copy.deepcopy(b)
            nb["id"] = bid
            nb["time"] = list(keep[bid]["range"])
            beats.append(nb)
    sc["beats"] = beats
    sc["name"] = f"{script.get('name') or script.get('title') or 'draft'}_{plan['label']}"
    sc["variant"] = {
        "target": plan["target"],
        "label": plan["label"],
        "ranges": plan["ranges"],
        "from": script.get("name") or script.get("title"),
    }
    return sc


class VariantCompiler(Compiler):
    """只把主轨保留区间限制到版本区间, 其余全部沿用编译器: 最后一段延长、CTA 的 last、剪点遮盖都按版本的剪点算。"""

    def __init__(
        self, script: dict, base_dir: str, ranges: List[List[float]], fmt=None, style=None
    ):
        super().__init__(script, base_dir, fmt, style)
        self.variant_ranges = [list(r) for r in ranges]

    def keep_ranges(self) -> List[List[float]]:
        base = super().keep_ranges()
        if sum(max(0.0, b - a) for a, b in base) > 0.05:
            return intersect_ranges(base, self.variant_ranges) or [
                list(r) for r in self.variant_ranges
            ]
        return [list(r) for r in self.variant_ranges]


def compile_variant(
    vscript: dict,
    ranges: List[List[float]],
    fmt: str,
    style: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> dict:
    """调用 script_compiler.Compiler 编译变体剧本, 再裁剪到选中区间。"""
    sc = dict(vscript)
    sc["format"] = fmt
    sb = VariantCompiler(sc, base_dir or os.getcwd(), ranges, fmt, style).compile()
    sb = restrict_storyboard(sb, ranges)
    sb["compiled_from"]["variant"] = vscript.get("variant")
    return sb


def _slug(s: str) -> str:
    return re.sub(r"[^\w.-]+", "_", s).strip("_") or "variant"


def make_variants(
    path: str,
    *,
    targets=DEFAULT_TARGETS,
    formats: Optional[List[str]] = None,
    style: Optional[str] = None,
    out_dir: Optional[str] = None,
    plan_only: bool = False,
    tolerance: float = TOLERANCE,
) -> dict:
    """主入口: 剧本或分镜表 -> 删减计划 (+ 变体剧本 / 分镜表文件)。返回汇总 dict。"""
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    base = os.path.dirname(os.path.abspath(path))
    stem = _slug(os.path.splitext(os.path.basename(path))[0])
    out_dir = os.path.abspath(out_dir or os.path.join(base, "variants"))
    is_sb = "shots" in doc and "beats" not in doc
    if is_sb:
        units, full = units_from_storyboard(doc)
        fmt0 = (doc.get("compiled_from") or {}).get("format") or "vertical"
        if formats and any(load_profile("formats", f)["id"] != fmt0 for f in formats):
            raise VariantError(
                f"分镜表已经按 {fmt0} 排好版, 换画幅需要从剧本重新编译 (传 script.json)"
            )
        formats = [fmt0]
    else:
        units, full = units_from_script(doc, base)
        formats = formats or [doc.get("format", "vertical")]
    fmt_orig = load_profile("formats", doc.get("format", "vertical"))["id"] if not is_sb else None
    formats = [load_profile("formats", f)["id"] for f in formats]  # 别名 -> id, 不存在会报错
    targets = sorted({float(t) for t in targets})
    result = {
        "source": os.path.abspath(path),
        "kind": "storyboard" if is_sb else "script",
        "full_duration": full,
        "formats": formats,
        "out_dir": out_dir,
        "variants": [],
    }
    for t in targets:
        if t >= full - 0.5:
            result["variants"].append(
                {"target": t, "label": _label(t), "skipped": f"原片只有 {full:.1f}s", "builds": []}
            )
            continue
        plan = plan_variant(units, t, tolerance)
        plan["builds"] = []
        if plan_only:
            for fmt in formats:
                plan["builds"].append({"format": fmt, "style": style})
            result["variants"].append(plan)
            continue
        os.makedirs(out_dir, exist_ok=True)
        if is_sb:
            sb = restrict_storyboard(doc, plan["ranges"])
            sb["project"]["name"] = f"{doc['project']['name']}_{plan['label']}"
            sb["sources"] = {
                k: (v if os.path.isabs(v) else os.path.abspath(os.path.join(base, v)))
                for k, v in (sb.get("sources") or {}).items()
            }
            sb.setdefault("compiled_from", {})["variant"] = {
                "target": t,
                "label": plan["label"],
                "ranges": plan["ranges"],
            }
            sb_path = os.path.join(out_dir, f"{stem}_{plan['label']}.storyboard.json")
            _write(sb_path, sb)
            plan["builds"].append(_build_entry(fmt0, None, None, sb_path, sb))
        else:
            vs = variant_script(doc, plan, base)
            vs_path = os.path.join(out_dir, f"{stem}_{plan['label']}.script.json")
            _write(vs_path, vs)
            plan["script"] = vs_path
            for fmt in formats:
                sb = compile_variant(vs, plan["ranges"], fmt, style, base)
                suffix = "_".join(x for x in ((fmt if fmt != fmt_orig else None), style) if x)
                if suffix:  # 和 compile_script 一致: 换画幅 / 风格时草稿名加后缀, 不覆盖原草稿
                    sb["project"]["name"] = f"{vs['name']}_{suffix}"
                sb_path = os.path.join(
                    out_dir,
                    f"{stem}_{plan['label']}_{fmt}{'_' + style if style else ''}"
                    ".storyboard.json",
                )
                _write(sb_path, sb)
                plan["builds"].append(_build_entry(fmt, style, vs_path, sb_path, sb))
        result["variants"].append(plan)
    return result


def _build_entry(
    fmt: str, style: Optional[str], script_path: Optional[str], sb_path: str, sb: dict
) -> dict:
    sc = os.path.join(SKILL, "scripts")
    e = {
        "format": fmt,
        "style": style,
        "storyboard": sb_path,
        "draft_name": sb["project"]["name"],
        "output_duration": round(sum(b - a for a, b in sb["main"]["keep"]), 2),
        "build_cmd": f"python {os.path.join(sc, 'storyboard_builder.py')} \"{sb_path}\"",
    }
    if script_path:
        e["script"] = script_path
        e["compile_args"] = {"script": script_path, "format": fmt, "style": style}
    if sb.get("notes"):
        e["notes"] = sb["notes"]
    return e


def _write(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="多版本输出: 15/30/60s 删减版 × 画幅")
    ap.add_argument("input", help="script.json (剧本) 或 storyboard.json (已编译分镜表)")
    ap.add_argument("--targets", default="15,30,60", help="目标时长 (秒), 逗号分隔")
    ap.add_argument(
        "--formats", help="画幅, 逗号分隔: vertical,horizontal,portrait_3x4,square (默认剧本的画幅)"
    )
    ap.add_argument("--style", help="换风格 (可选)")
    ap.add_argument(
        "--tolerance", type=float, default=TOLERANCE, help="允许超出目标时长的比例 (默认 0.1)"
    )
    ap.add_argument("-o", "--out-dir", help="输出目录 (默认 <输入目录>/variants)")
    ap.add_argument("--plan-only", action="store_true", help="只出删减计划, 不写文件")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        targets = [float(x) for x in a.targets.split(",") if x.strip()]
        formats = [x.strip() for x in a.formats.split(",") if x.strip()] if a.formats else None
        res = make_variants(
            a.input,
            targets=targets,
            formats=formats,
            style=a.style,
            out_dir=a.out_dir,
            plan_only=a.plan_only,
            tolerance=a.tolerance,
        )
    except (VariantError, ScriptError, ValueError, OSError) as e:
        code = "bad_input" if isinstance(e, (VariantError, ScriptError, ValueError)) else "io_error"
        if a.json:
            emit_result(make_result(False, code, str(e)), True)
        else:
            print(f"❌ {e}", file=sys.stderr)
        return 2
    if a.json:
        emit_result(make_result(True, "ok", "", res), True)
        return 0
    print(
        f"🎬 {os.path.basename(a.input)}: 原片 {res['full_duration']:.1f}s, 画幅 {', '.join(res['formats'])}"
    )
    for v in res["variants"]:
        if v.get("skipped"):
            print(f"  {v['label']}: 跳过 ({v['skipped']})")
            continue
        ids = ", ".join(b["id"] + ("✂" if b["trimmed"] else "") for b in v["beats"])
        print(f"  {v['label']}: {v['duration']:.1f}s  保留 [{ids}]  去掉 {v['dropped'] or '-'}")
        for w in v["warnings"]:
            print(f"     ⚠️ {w}")
        for b in v["builds"]:
            if b.get("storyboard"):
                print(f"     {b['format']}: {b['storyboard']} ({b['output_duration']:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
