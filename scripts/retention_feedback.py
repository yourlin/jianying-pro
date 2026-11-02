#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据回流 (Retention Feedback): 平台留存曲线 → 掉人点 → 和成片时间线对齐 → 规则修改建议。

输入:
  --csv     平台导出的留存曲线 (时间, 留存%)。兼容抖音 / 视频号 / B 站 / YouTube 常见列名和格式:
            时间列: 时间 / 时间点 / 播放时长 / 秒 / 进度 / time / second ...  (5, 5.5, 0:05, 00:00:05, 5s, 5秒, 10%)
            数值列: 留存率 / 观众留存 / 完播率 / 在看人数占比 / 观看人数 / retention ...  (85.3%, 85.3, 0.853, 人数)
  --report  quality_review.py 生成的 report.json (镜头 / 字幕 / 死区 / 问题列表), 用于归因。可省略 (只找掉人点)。

输出 (只给建议, 不改任何文件):
  - 曲线指标: 3s / 5s / 50% 处留存、完播率、平均观看比例
  - 掉人点: 时间、掉了多少、比正常衰减快多少
  - 每个掉人点那一刻: 什么镜头、什么字幕、是否死区、附近有哪些审片问题 → 归因
  - 对 references/methodology/*.md 的修改建议 (引用现有条文 + 建议改法 + 证据)

用法:
  python retention_feedback.py --csv 留存.csv --report /tmp/jy_quality_review/<名>/report.json
  python retention_feedback.py --csv 留存.csv --duration 58 --json
退出码: 0 = 分析完成, 2 = 输入错误。
"""

import argparse
import csv
import io
import json
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.cli_protocol import emit_result, make_result  # noqa: E402

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METHODOLOGY = os.path.join(SKILL_ROOT, "references", "methodology")

TIME_KEYS = ("时间点", "时间", "播放时长", "播放时间", "播放进度", "视频进度", "进度", "秒", "时刻",
             "time", "second", "sec", "timestamp", "position", "progress", "t")  # fmt: skip
RATE_KEYS = ("留存率", "观众留存", "留存", "完播率", "在看人数占比", "观看人数占比", "人数占比", "占比",
             "剩余观众", "retention", "audienceretention", "percent", "ratio", "rate", "%")  # fmt: skip
COUNT_KEYS = ("观看人数", "在看人数", "人数", "播放量", "viewers", "views", "count", "audience")
PROGRESS_KEYS = ("进度", "progress", "百分比", "percent")
REACTION_LAG = 2.0  # 观众看到问题到划走的延迟窗口 (秒): 掉人点往前看 2s
HOOK_S = 3.0


class InputError(Exception):
    """CSV / 报告读不了, CLI 退出码 2。"""


# ---------------- CSV ----------------
def _norm(s: str) -> str:
    return re.sub(r"[\s()（）\[\]【】_\-:：/]", "", str(s)).lower()


def _read_text(path: str) -> str:
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise InputError(f"CSV 编码无法识别: {path}")


def parse_time(v: str):
    """秒数: 5 / 5.5 / 0:05 / 00:00:05 / 5s / 5秒 / 1分5秒; 百分比返回 ('%', x)。"""
    s = str(v).strip().replace("：", ":")
    if not s:
        return None
    if s.endswith("%"):
        try:
            return ("%", float(s[:-1]))
        except ValueError:
            return None
    m = re.fullmatch(r"(?:(\d+)\s*(?:分|min|m)\s*)?(\d+(?:\.\d+)?)\s*(?:秒|s|sec)?", s, re.I)
    if m and (m.group(1) or not re.search(r"[:]", s)):
        return (int(m.group(1) or 0)) * 60 + float(m.group(2))
    if ":" in s:
        parts = s.split(":")
        try:
            vals = [float(p) for p in parts]
        except ValueError:
            return None
        sec = 0.0
        for p in vals:
            sec = sec * 60 + p
        return sec
    return None


def parse_value(v: str):
    s = str(v).strip().replace(",", "")
    if not s or s in ("-", "--"):
        return None
    try:
        return float(s[:-1]) if s.endswith("%") else float(s)
    except ValueError:
        return None


def _pick(header, keys, exclude=()):
    normed = [_norm(h) for h in header]
    for k in keys:  # 按关键词优先级找
        for i, h in enumerate(normed):
            if i in exclude:
                continue
            if h == k or (len(k) > 1 and k in h):
                return i
    return None


def load_curve(path: str, duration=None) -> dict:
    """读留存 CSV, 返回 {t: [...秒], r: [...%], time_col, value_col, kind}。"""
    if not os.path.exists(path):
        raise InputError(f"找不到 CSV: {path}")
    text = _read_text(path)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if len(rows) < 3:
        raise InputError("CSV 行数太少, 至少要 3 个时间点")
    header = rows[0]
    has_header = parse_time(header[0]) is None or parse_value(header[-1]) is None
    if has_header:
        ti = _pick(header, TIME_KEYS)
        vi = _pick(header, RATE_KEYS, exclude={ti})
        kind = "rate"
        if vi is None:
            vi = _pick(header, COUNT_KEYS, exclude={ti})
            kind = "count"
        if ti is None or vi is None:
            raise InputError(f"认不出时间列 / 留存列: {header} (需要类似「时间, 留存率」的表头)")
        body = rows[1:]
        tname, vname = header[ti], header[vi]
    else:
        ti, vi, kind, body, tname, vname = 0, 1, "rate", rows, "col0", "col1"
    pts = []
    progress = False
    for r in body:
        if max(ti, vi) >= len(r):
            continue
        t, v = parse_time(r[ti]), parse_value(r[vi])
        if t is None or v is None:
            continue
        if isinstance(t, tuple):
            progress, t = True, t[1]
        pts.append((t, v))
    if has_header and any(k in _norm(tname) for k in PROGRESS_KEYS) and not progress:
        progress = max(t for t, _ in pts) <= 100 and bool(duration)
    if progress:
        if not duration:
            raise InputError("时间列是播放进度百分比, 需要 --duration 或 --report 给出视频时长")
        pts = [(t * duration / 100, v) for t, v in pts]
    if len(pts) < 3:
        raise InputError("有效数据点少于 3 个")
    pts.sort()
    t = np.array([p[0] for p in pts], float)
    r = np.array([p[1] for p in pts], float)
    _, uniq = np.unique(t, return_index=True)
    t, r = t[uniq], r[uniq]
    if kind == "count" or "人数" in vname or r.max() > 100.5:
        r = r / max(1e-9, r.max()) * 100
        kind = "count"
    elif r.max() <= 1.0:
        r = r * 100
    return {"t": t.tolist(), "r": r.tolist(), "time_col": tname, "value_col": vname, "kind": kind}


# ---------------- 掉人点 ----------------
def curve_metrics(t, r, duration=None) -> dict:
    t, r = np.asarray(t), np.asarray(r)
    dur = float(duration or t[-1])
    at = lambda x: round(float(np.interp(x, t, r)), 1)  # noqa: E731
    grid = np.linspace(0, dur, 200)
    return {
        "duration": round(dur, 2),
        "retention_3s": at(3.0),
        "retention_5s": at(5.0),
        "retention_50pct": at(dur / 2),
        "completion": at(dur),
        "avg_view_pct": round(float(np.interp(grid, t, r).mean()), 1),
    }


def find_drops(t, r, top=5, min_rate=1.5, step=0.5) -> list:
    """
    掉人点: 每秒流失速度显著快于正常衰减的区间。
    rate(t) = 每秒掉的百分点 (1s 中心差分); 基线 = 3s 以后 rate 的中位数;
    rate > max(min_rate, 基线 × 2.5 + 0.5) 的连续区间算一次陡降, 按掉的总量取前 top 个。
    """
    t, r = np.asarray(t, float), np.asarray(r, float)
    grid = np.arange(t[0], t[-1] + 1e-9, step)
    rr = np.interp(grid, t, r)
    rate = (np.interp(grid - 0.5, t, r) - np.interp(grid + 0.5, t, r)) / 1.0
    rate[0] = rr[0] - np.interp(grid[0] + 1, t, r)  # 开头用前向差分
    tail = rate[grid >= HOOK_S]
    base = float(np.median(tail)) if len(tail) else float(np.median(rate))
    thr = max(min_rate, base * 2.5 + 0.5)
    drops, on = [], None
    for i, v in enumerate(list(rate) + [-1e9]):
        if v > thr and on is None:
            on = i
        elif v <= thr and on is not None:
            seg = slice(on, i)
            k = on + int(np.argmax(rate[seg]))
            s, e = float(grid[on]), float(grid[min(i, len(grid) - 1)])
            s0, e0 = max(t[0], s - 0.5), min(t[-1], e + 0.5)
            amount = float(np.interp(s0, t, r) - np.interp(e0, t, r))
            drops.append({
                "start": round(s0, 2), "end": round(e0, 2), "peak": round(float(grid[k]), 2),
                "drop_pp": round(amount, 2), "peak_rate_pp_s": round(float(rate[k]), 2),
                "vs_baseline": round(float(rate[k]) / max(0.1, base), 1),
                "retention_before": round(float(np.interp(s0, t, r)), 1),
            })  # fmt: skip
            on = None
    drops.sort(key=lambda d: -d["drop_pp"])
    drops = sorted(drops[:top], key=lambda d: d["start"])
    return [{"index": i + 1, **d} for i, d in enumerate(drops)], round(base, 3), round(thr, 3)


# ---------------- 对齐与归因 ----------------
ISSUE_CAUSE = {
    "首帧黑场": "hook", "慢淡入": "hook", "前3秒无人声无文字": "hook", "人声进入太晚": "hook",
    "前3秒画面静止": "hook", "寒暄式开场": "hook",
    "死区": "dead_zone",
    "节奏偏慢": "slow_shot", "长镜头": "slow_shot", "节奏单调": "monotone", "节奏过碎": "choppy",
    "亮度跳变": "color_jump", "色温跳变": "color_jump", "色偏跳变": "color_jump",
    "静音空洞": "audio_hole", "BGM压人声": "bgm_conflict", "全片无声": "audio_hole",
    "结尾拖沓": "ending", "配乐提前结束": "ending", "配乐未淡出": "ending", "片尾静音": "ending",
    "没有CTA": "ending",
}  # fmt: skip
CAUSE_CN = {
    "hook": "开头没拦住", "dead_zone": "死区 (画面和信息都停住了)", "slow_shot": "镜头太长",
    "monotone": "节奏单调", "choppy": "节奏过碎", "color_jump": "画面明暗 / 色调跳变",
    "audio_hole": "声音断档", "bgm_conflict": "BGM 压人声听不清", "ending": "结尾拖沓 / 提前出现结束感",
    "low_energy": "节奏低谷", "finale_signal": "提前给出「讲完了」的信号", "content": "内容本身 (剪辑上没找到原因)",
}  # fmt: skip
FINALE_WORDS = re.compile(r"总结|最后|以上|就是这些|就到这|好了|谢谢|拜拜|下期")


def _visible(items, t):
    return [x for x in items if x["start"] <= t < x["end"]]


def align(drop, report) -> dict:
    """掉人点那一刻 (以及前 REACTION_LAG 秒) 的镜头 / 字幕 / 死区 / 问题。"""
    tl = report.get("timeline") or {}
    peak, w0 = drop["peak"], max(0.0, drop["start"] - REACTION_LAG)
    shots = (report.get("pacing") or {}).get("shots") or []
    shot = next((s for s in shots if s["start"] <= peak < s["end"]), shots[-1] if shots else None)
    cuts = [c for c in tl.get("cuts") or [] if c <= peak]
    texts = tl.get("texts") or []
    on_screen = [x["text"] for x in _visible(texts, peak)]
    recent = [x["text"] for x in texts if w0 <= x["start"] <= peak]
    speech = [s["text"] for s in tl.get("speech_text") or [] if w0 - 3 <= s["start"] <= peak]
    # 死区要在掉人前已持续 ≥ 1s 才算原因; 问题要在掉人峰值前已经出现
    zones = [
        {**z, "tolerated": round(min(z["dur"], max(0.0, peak - z["start"])), 2)}
        for z in report.get("dead_zones") or []
        if z["start"] + 1.0 <= peak + 0.5 and z["end"] > w0
    ]
    issues = [i for i in report.get("issues") or [] if i["start"] <= peak + 0.5 and i["end"] > w0
              and i["end"] - i["start"] < report["source"]["duration"] * 0.9]  # fmt: skip
    curve = (report.get("pacing") or {}).get("curve") or []
    en = next((c["energy"] for c in curve if c["t"] <= peak < c["t"] + 1), None)
    med = float(np.median([c["energy"] for c in curve])) if curve else None
    return {
        "shot": shot,
        "since_last_cut": round(peak - cuts[-1], 2) if cuts else round(peak, 2),
        "on_screen_text": on_screen,
        "new_text_in_window": recent,
        "speech_before": speech[-2:],
        "dead_zones": zones,
        "issues": [{"id": i.get("id"), "type": i["type"], "severity": i["severity"],
                    "start": i["start"], "end": i["end"]} for i in issues],  # fmt: skip
        "energy": en,
        "energy_median": round(med, 3) if med is not None else None,
    }


def attribute(drop, ctx, report) -> list:
    """归因: 返回 [{cause, label, confidence, evidence}], 按置信度排序。"""
    dur = report["source"]["duration"]
    target = (report.get("pacing") or {}).get("target_shot_s") or [3, 6]
    out = {}

    def add(cause, conf, ev):
        if cause not in out or out[cause]["confidence"] < conf:
            out[cause] = {"cause": cause, "label": CAUSE_CN[cause], "confidence": round(conf, 2),
                          "evidence": ev}  # fmt: skip

    sev_w = {"high": 0.85, "medium": 0.7, "low": 0.5}
    for i in ctx["issues"]:
        c = ISSUE_CAUSE.get(i["type"])
        if c and c != "dead_zone":  # 死区按下面的 dead_zones (要求掉人前已持续 ≥ 1s) 判断
            add(
                c, sev_w[i["severity"]], f"{i['id']} [{i['type']}] {i['start']:.1f}–{i['end']:.1f}s"
            )
    for z in ctx["dead_zones"]:
        add("dead_zone", 0.85,
            f"死区 {z['start']:.1f}–{z['end']:.1f}s ({z['dur']:.1f}s), 掉人时已持续 {z['tolerated']:.1f}s")  # fmt: skip
    if drop["peak"] <= HOOK_S + 0.5:
        add("hook", 0.75 if "hook" not in out else out["hook"]["confidence"],
            f"掉人发生在前 {HOOK_S:.0f} 秒 ({drop['peak']:.1f}s)")  # fmt: skip
    shot = ctx["shot"]
    if shot and shot["dur"] > target[1] * 1.5 and ctx["since_last_cut"] > target[1]:
        add(
            "slow_shot",
            0.6,
            f"第 {shot['index']} 个镜头 {shot['dur']:.1f}s, 掉人时已 {ctx['since_last_cut']:.1f}s 没换画面",
        )
    en, med = ctx["energy"], ctx["energy_median"]
    if en is not None and med and en < 0.3 and en < med * 0.6:
        add("low_energy", 0.5, f"节奏能量 {en:.2f} (全片中位 {med:.2f})")
    said = " ".join(ctx["speech_before"] + ctx["new_text_in_window"] + ctx["on_screen_text"])
    if drop["peak"] < dur * 0.9 and FINALE_WORDS.search(said):
        add("finale_signal", 0.6, f"掉人前出现「{FINALE_WORDS.search(said).group(0)}」")
    if not out:
        add("content", 0.3, "附近没有审片问题; 看这段字幕 / 旁白是否兑现了开头的承诺")
    return sorted(out.values(), key=lambda x: -x["confidence"])


# ---------------- 规则修改建议 (只输出, 不改文件) ----------------
def quote_rule(fname: str, pattern: str, root: str = METHODOLOGY):
    """在方法论文件里找现有条文, 返回 {file, line, text}; 找不到返回只有 file 的 dict。"""
    path = os.path.join(root, fname)
    rel = os.path.relpath(path, SKILL_ROOT)
    if not os.path.exists(path):
        return {"file": rel}
    with open(path, "r", encoding="utf-8") as f:
        for n, ln in enumerate(f, 1):
            if re.search(pattern, ln):
                return {"file": rel, "line": n, "text": ln.strip()}
    return {"file": rel}


def suggest_rules(drops, report, metrics, root: str = METHODOLOGY) -> list:
    """按归因聚合掉人点, 给出对 references/methodology 的修改建议。"""
    by = {}
    for d in drops:
        for a in d.get("attribution") or []:
            if a["confidence"] >= 0.5:
                by.setdefault(a["cause"], []).append(d)
    genre = report.get("genre") if report else None
    target = ((report or {}).get("pacing") or {}).get("target_shot_s")
    out = []

    def add(cause, fname, pattern, proposal, extra=None):
        ds = by.get(cause, [])
        lost = round(sum(x["drop_pp"] for x in ds), 1)
        conf = min(0.9, 0.35 + 0.15 * len(ds) + 0.02 * lost)
        out.append({
            "cause": cause, "label": CAUSE_CN[cause], "target": quote_rule(fname, pattern, root),
            "proposal": proposal, "evidence": [f"#{x['index']} {x['peak']:.1f}s 掉 {x['drop_pp']:.1f}pp" for x in ds],
            "lost_pp": lost, "confidence": round(conf, 2), **(extra or {}),
        })  # fmt: skip

    if "dead_zone" in by:
        zs = [z["tolerated"] for d in by["dead_zone"] for z in d["context"]["dead_zones"]]
        lim = max(1.5, round(min(zs) - 0.5, 1)) if zs else 2.5
        add("dead_zone", "hook.md", r"每 3–5 秒一个信息点",
            f"观众在死区持续 {min(zs) if zs else lim:.1f}s 时就开始走: 信息点间隔上限收紧到 {lim:g}s; "
            f"审片器死区阈值建议 --dead-zone {lim:g}", {"param": {"quality_review --dead-zone": lim}})  # fmt: skip
    if "slow_shot" in by or "low_energy" in by:
        cause = "slow_shot" if "slow_shot" in by else "low_energy"
        durs = [d["context"]["since_last_cut"] for d in by[cause]]
        hi = max(1.0, round(min(durs) - 0.5, 1)) if durs else None
        g = genre or "口播知识"
        add(cause, "pacing.md", rf"(?i)^\|\s*{re.escape(g)}\s*\|",
            f"「{g}」平均镜头上限从 {target[1] if target else '?'}s 收紧到 {hi}s"
            f" (掉人点距上一次换画面 {', '.join(f'{x:.1f}s' for x in durs)})")  # fmt: skip
    if "hook" in by or metrics.get("retention_3s", 100) < 60:
        r3 = metrics.get("retention_3s")
        hook_issues = sorted(
            {i["type"] for d in by.get("hook", []) for i in d["context"]["issues"]}
        )
        what = f"; 审片器同时报了 {'、'.join(hook_issues)}" if hook_issues else ""
        add("hook", "hook.md", r"0–1\.5s",
            f"3 秒留存 {r3}%{what}: 把「证据画面 0.3–0.8s 内亮出」改为硬约束, 前 3 秒至少 2 个新信息; "
            "没有天然钩子时默认冷开场")  # fmt: skip
    if "color_jump" in by:
        add(
            "color_jump",
            "review_rules.md",
            r"亮度差 > 35%",
            "掉人点紧跟明暗 / 色调跳变: 相邻片段亮度差阈值从 35% 收紧到 25%, 并把色温差 (Lab b* ≥ 10) 加入检查",
        )
    if "bgm_conflict" in by:
        add(
            "bgm_conflict",
            "edit_grammar.md",
            r"配乐不压人声",
            "人声段 BGM 低于人声的下限从 12dB 提高到 15dB, 或默认开启人声段自动 ducking",
        )
    if "audio_hole" in by:
        add(
            "audio_hole",
            "pacing.md",
            r"0\.3s 静音",
            "段间静音上限 0.3s 写成硬约束, 超过 0.8s 的空洞必须用环境声 / BGM 垫底",
        )
    if "ending" in by or "finale_signal" in by:
        cause = "ending" if "ending" in by else "finale_signal"
        add(
            cause,
            "hook.md",
            r"结尾放行动号召",
            "掉人点出现在结尾前: 「总结 / 最后」类结束信号不要早于最后 5 秒; 最后一个信息点后 ≤ 1s 接 CTA",
        )
    if "content" in by:
        add(
            "content",
            "hook.md",
            r"开头的承诺必须在正片兑现",
            "剪辑上没找到原因的掉人点: 人工复看这几处字幕, 检查是否偏题 / 重复 / 没兑现开头承诺 (不建议改规则)",
        )
    return sorted(out, key=lambda s: -s["lost_pp"])


def analyze(csv_path, report=None, duration=None, top=5, min_rate=1.5, root=METHODOLOGY) -> dict:
    dur = duration or ((report or {}).get("source") or {}).get("duration")
    curve = load_curve(csv_path, dur)
    t, r = curve["t"], curve["r"]
    metrics = curve_metrics(t, r, dur)
    drops, base, thr = find_drops(t, r, top, min_rate)
    notes = []
    if report:
        if dur and abs(t[-1] - dur) > max(2.0, dur * 0.1):
            notes.append(f"留存曲线长度 {t[-1]:.1f}s 和成片 {dur:.1f}s 对不上, 是不是导错了视频?")
        for d in drops:
            d["context"] = align(d, report)
            d["attribution"] = attribute(d, d["context"], report)
    else:
        notes.append("没有 --report, 只找出掉人点, 不做归因 (先跑 quality_review.py)")
    suggestions = suggest_rules(drops, report, metrics, root) if report else []
    return {
        "version": 1,
        "tool": "retention_feedback",
        "csv": os.path.abspath(csv_path),
        "report": (report or {}).get("report_path"),
        "curve": {"time_col": curve["time_col"], "value_col": curve["value_col"], "kind": curve["kind"],
                  "points": len(t), "baseline_rate_pp_s": base, "drop_threshold_pp_s": thr},  # fmt: skip
        "metrics": metrics,
        "drops": drops,
        "suggestions": suggestions,
        "notes": notes,
    }


def summarize(res) -> str:
    m = res["metrics"]
    lines = [
        f"留存曲线: {res['csv']}",
        f"  3s {m['retention_3s']}%  5s {m['retention_5s']}%  一半处 {m['retention_50pct']}%  "
        f"完播 {m['completion']}%  平均观看 {m['avg_view_pct']}%",
        f"  正常衰减 {res['curve']['baseline_rate_pp_s']}pp/s, 陡降阈值 {res['curve']['drop_threshold_pp_s']}pp/s",
    ]
    if not res["drops"]:
        lines.append("✅ 没有明显的陡降点")
    for d in res["drops"]:
        lines.append(f"\n#{d['index']} {d['start']:.1f}–{d['end']:.1f}s 掉 {d['drop_pp']:.1f}pp "
                     f"(峰值 {d['peak_rate_pp_s']}pp/s, 正常的 {d['vs_baseline']} 倍)")  # fmt: skip
        ctx = d.get("context")
        if ctx:
            sh = ctx["shot"]
            if sh:
                lines.append(
                    f"   镜头 #{sh['index']} {sh['start']:.1f}–{sh['end']:.1f}s, 距上次换画面 {ctx['since_last_cut']}s"
                )
            if ctx["on_screen_text"]:
                lines.append(f"   字幕: {' / '.join(ctx['on_screen_text'])[:60]}")
        for a in d.get("attribution") or []:
            lines.append(f"   → {a['label']} ({a['confidence']:.0%}): {a['evidence']}")
    if res["suggestions"]:
        lines.append("\n===== 规则修改建议 (未修改任何文件) =====")
        for s in res["suggestions"]:
            tg = s["target"]
            where = f"{tg['file']}:{tg['line']}" if tg.get("line") else tg["file"]
            lines.append(f"• [{s['label']}] {where}")
            if tg.get("text"):
                lines.append(f"    现有: {tg['text'][:90]}")
            lines.append(f"    建议: {s['proposal']}")
            lines.append(f"    证据: {'; '.join(s['evidence'])}  (置信度 {s['confidence']:.0%})")
    for n in res["notes"]:
        lines.append(f"⚠️ {n}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="数据回流: 留存曲线 → 掉人点 → 归因 → 规则修改建议")
    ap.add_argument("--csv", required=True, help="平台导出的留存曲线 CSV (时间, 留存%%)")
    ap.add_argument("--report", help="quality_review.py 的 report.json")
    ap.add_argument("--duration", type=float, help="成片时长 (秒); 时间列是进度百分比时必须给")
    ap.add_argument("--top", type=int, default=5, help="最多报告几个掉人点")
    ap.add_argument("--min-rate", type=float, default=1.5, help="陡降最低速度 (百分点 / 秒)")
    ap.add_argument("--out", help="把结果写到这个 JSON 文件")
    ap.add_argument("--json", action="store_true", help="stdout 输出 {ok, code, reason, data}")
    args = ap.parse_args(argv)
    try:
        report = None
        if args.report:
            if not os.path.exists(args.report):
                raise InputError(f"找不到报告: {args.report}")
            with open(args.report, "r", encoding="utf-8") as f:
                report = json.load(f)
            report.setdefault("report_path", os.path.abspath(args.report))
            if report.get("tool") != "quality_review":
                raise InputError("--report 不是 quality_review.py 生成的 report.json")
        res = analyze(args.csv, report, args.duration, args.top, args.min_rate)
    except (InputError, ValueError, json.JSONDecodeError) as e:
        if args.json:
            emit_result(make_result(False, "input_error", str(e)), True)
        else:
            print(f"❌ {e}", file=sys.stderr)
        return 2
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    if args.json:
        emit_result(make_result(True, "ok", "", res), True)
    else:
        print(summarize(res))
        if args.out:
            print(f"\n结果: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
