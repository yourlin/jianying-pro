#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剪映草稿版式预览与审片器 (Layout Preview & Audit)

不打开剪映，直接从草稿 JSON 伪渲染关键时刻的画面，用于自检:
  - 字幕/标题是否超出安全区、是否落在平台 UI 遮挡区
  - 字号是否小于短视频可读下限
  - 插卡是否遮挡人脸区、是否与文字重叠
  - 横版素材放进竖屏画布后的实际占比

用法:
  python layout_preview.py --draft "草稿名" --auto
  python layout_preview.py --draft "草稿名" --at 5.5 9.8 30 43
  python layout_preview.py --draft "草稿名" --auto --report-only
"""
import argparse
import json
import math
import os
import subprocess
import sys
import tempfile

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:          # 缺 Pillow 时仍可做时间线结构检查
    HAS_PIL = False

DRAFT_ROOTS = [
    os.path.expanduser("~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"),
    os.path.expanduser("~/Library/Containers/com.lemon.lvpro/Data/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"),
]

CJK_FONTS = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
]

# 字号换算: 剪映 size 单位 -> 画布像素 (经实测校准, 可用 --px-per-size 覆盖)
PX_PER_SIZE_AT_1920 = 6.0

# 短视频可读下限 (1080x1920 画布, 单位 px 字高)
MIN_PRIMARY_PX = 48
MIN_SECONDARY_PX = 38

# 平台 UI 遮挡区 (占画布比例): 抖音/TikTok/Reels 通用保守值
UI_ZONES = {
    "顶部状态区": (0.0, 0.0, 1.0, 0.09),
    "底部文案区": (0.0, 0.84, 1.0, 1.0),
    "右侧按钮区": (0.86, 0.30, 1.0, 0.84),
}
# 标题安全区: 四边留白
SAFE_MARGIN = {"x": 0.06, "top": 0.10, "bottom": 0.16}


def find_draft(name_or_path):
    if os.path.isdir(name_or_path):
        return name_or_path
    for root in DRAFT_ROOTS:
        cand = os.path.join(root, name_or_path)
        if os.path.isdir(cand):
            return cand
    raise SystemExit(f"找不到草稿: {name_or_path}")


def load_draft(draft_dir):
    for fn in ("draft_info.skill.json", "draft_info.json"):
        path = os.path.join(draft_dir, fn)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue  # 剪映打开过的草稿已加密, 跳过
    # 剪映自己写的明文副本
    for sub in ("", "Timelines"):
        base = os.path.join(draft_dir, sub) if sub else draft_dir
        if not os.path.isdir(base):
            continue
        for dirpath, _, files in os.walk(base):
            if "template.json" in files:
                with open(os.path.join(dirpath, "template.json"), encoding="utf-8") as f:
                    return json.load(f)
    raise SystemExit("无法读取草稿 JSON (可能已被剪映加密, 且无明文副本)")


def pick_font(size_px, prefer=None):
    for path in ([prefer] if prefer else []) + CJK_FONTS:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, max(8, int(size_px)))
            except OSError:
                continue
    return ImageFont.load_default()


def kf_value(segment, prop, default):
    """读取关键帧序列, 返回 [(t_us, value), ...]; 无关键帧返回 None"""
    for kf in segment.get("common_keyframes") or []:
        if kf.get("property_type") == prop:
            pts = [(k["time_offset"], k["values"][0]) for k in kf.get("keyframe_list", [])
                   if k.get("values")]
            return sorted(pts) or None
    return None


def interp(points, t_us, default):
    if not points:
        return default
    if t_us <= points[0][0]:
        return points[0][1]
    if t_us >= points[-1][0]:
        return points[-1][1]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= t_us <= t1:
            if t1 == t0:
                return v1
            r = (t_us - t0) / (t1 - t0)
            return v0 + (v1 - v0) * r
    return default


def seg_props(segment, local_us):
    """返回该时刻的 (scale, x, y, alpha), 已合并关键帧"""
    # 剪映的关键帧时间是素材时间 (截取起点 + 片段内时间 x 速度), 不是片段内时间
    src = segment.get("source_timerange") or {}
    local_us = (src.get("start") or 0) + local_us * float(segment.get("speed") or 1.0)
    clip = segment.get("clip") or {}
    tr = clip.get("transform") or {}
    sc = clip.get("scale") or {}
    scale = interp(kf_value(segment, "KFTypeScaleX", None), local_us, sc.get("x", 1.0))
    x = interp(kf_value(segment, "KFTypePositionX", None), local_us, tr.get("x", 0.0))
    y = interp(kf_value(segment, "KFTypePositionY", None), local_us, tr.get("y", 0.0))
    alpha = interp(kf_value(segment, "KFTypeAlpha", None), local_us, clip.get("alpha", 1.0))
    return scale, x, y, alpha


def covering(track, t_us):
    for s in track.get("segments", []):
        tr = s["target_timerange"]
        if tr["start"] <= t_us < tr["start"] + tr["duration"]:
            return s
    return None


def extract_frame(media_path, at_s, size):
    if not os.path.exists(media_path):
        return None
    ext = os.path.splitext(media_path)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        try:
            return Image.open(media_path).convert("RGBA")
        except OSError:
            return None
    tmp = os.path.join(tempfile.gettempdir(), f"_lp_{abs(hash((media_path, round(at_s, 2))))}.jpg")
    if not os.path.exists(tmp):
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, at_s):.3f}",
               "-i", media_path, "-frames:v", "1", tmp]
        if subprocess.run(cmd, capture_output=True).returncode != 0:
            return None
    try:
        return Image.open(tmp).convert("RGBA")
    except OSError:
        return None


def contain_fit(img_w, img_h, canvas_w, canvas_h):
    """剪映默认把素材按 contain 适配画布"""
    r = min(canvas_w / img_w, canvas_h / img_h)
    return int(img_w * r), int(img_h * r)


def text_of(material):
    """解析文字素材, 返回 (文本, 字号, 描边宽, 对齐, 最大行宽)"""
    try:
        content = json.loads(material.get("content") or "{}")
    except json.JSONDecodeError:
        return None
    text = content.get("text") or ""
    styles = content.get("styles") or []
    size = styles[0].get("size", 8.0) if styles else 8.0
    stroke = 0.0
    if styles and styles[0].get("strokes"):
        stroke = styles[0]["strokes"][0].get("width", 0.0)
    return text, size, stroke, material.get("alignment", 1), material.get("line_max_width", 0.82)


def wrap_text(draw, text, font, max_px):
    lines = []
    for raw in text.split("\n"):
        cur = ""
        for ch in raw:
            trial = cur + ch
            if draw.textlength(trial, font=font) > max_px and cur:
                lines.append(cur)
                cur = ch
            else:
                cur = trial
        lines.append(cur)
    return lines


def build_frame(draft, t_s, px_per_size, show_guides=True):
    canvas = draft.get("canvas_config") or {}
    W, H = canvas.get("width", 1080), canvas.get("height", 1920)
    t_us = int(t_s * 1_000_000)
    mats = draft.get("materials", {})
    vids = {m["id"]: m for m in mats.get("videos", [])}
    texts = {m["id"]: m for m in mats.get("texts", [])}
    masks = {m["id"]: m for m in mats.get("common_mask", []) + mats.get("masks", [])}

    base = Image.new("RGBA", (W, H), (18, 18, 18, 255))
    findings = []

    video_tracks = [t for t in draft["tracks"] if t.get("type") == "video"]
    text_tracks = [t for t in draft["tracks"] if t.get("type") == "text"]

    boxes = []  # (类型, 名称, bbox)
    for idx, track in enumerate(video_tracks):
        seg = covering(track, t_us)
        if seg is None:
            continue
        mat = vids.get(seg.get("material_id"))
        if not mat:
            continue
        local = t_us - seg["target_timerange"]["start"]
        src = (seg.get("source_timerange") or {}).get("start", 0)
        frame = extract_frame(mat.get("path", ""), (src + local) / 1_000_000, (W, H))
        if frame is None:
            continue
        scale, x, y, alpha = seg_props(seg, local)
        fw, fh = contain_fit(frame.width, frame.height, W, H)
        fw, fh = max(1, int(fw * scale)), max(1, int(fh * scale))
        frame = frame.resize((fw, fh), Image.LANCZOS)
        mask = next((masks[r] for r in seg.get("extra_material_refs", []) if r in masks), None)
        if mask and mask.get("resource_type") in ("circle", "rectangle"):
            # 圆形 / 矩形蒙版: 只保留蒙版内 (config: centerX/Y 为半宽 / 半高单位, 上为正; width/height 为素材比例)
            cfg = mask.get("config") or {}
            mcx, mcy = fw / 2 + cfg.get("centerX", 0) * fw / 2, fh / 2 - cfg.get("centerY", 0) * fh / 2
            rx, ry = cfg.get("width", 1) * fw / 2, cfg.get("height", 1) * fh / 2
            m = Image.new("L", (fw, fh), 0)
            shape = ImageDraw.Draw(m).ellipse if mask["resource_type"] == "circle" else ImageDraw.Draw(m).rectangle
            shape((mcx - rx, mcy - ry, mcx + rx, mcy + ry), fill=255)
            frame.putalpha(Image.composite(frame.getchannel("A"), Image.new("L", (fw, fh), 0), m))
        if alpha < 1.0:
            a = frame.getchannel("A").point(lambda v: int(v * alpha))
            frame.putalpha(a)
        cx, cy = W / 2 + x * (W / 2), H / 2 - y * (H / 2)
        pos = (int(cx - fw / 2), int(cy - fh / 2))
        base.alpha_composite(frame, pos)
        if idx > 0 and alpha > 0.35:
            boxes.append(("插卡", track.get("name") or f"video{idx}",
                          (pos[0], pos[1], pos[0] + fw, pos[1] + fh)))
            if fw / W < 0.45 and not mask:  # 带蒙版的小窗 (如圆形讲解人) 本来就小
                findings.append(f"[插卡] {track.get('name')} 宽度仅占画面 {fw/W:.0%}，竖屏里偏小")

    draw = ImageDraw.Draw(base)
    for track in text_tracks:
        seg = covering(track, t_us)
        if seg is None:
            continue
        parsed = text_of(texts.get(seg.get("material_id"), {}))
        if not parsed:
            continue
        text, size, stroke_w, _align, max_lw = parsed
        local = t_us - seg["target_timerange"]["start"]
        scale, x, y, alpha = seg_props(seg, local)
        if alpha < 0.2 or not text.strip():
            continue
        px = size * scale * px_per_size * (H / 1920)
        font = pick_font(px)
        lines = wrap_text(draw, text, font, max_lw * W)
        lh = px * 1.28
        block_h = lh * len(lines)
        cx, cy = W / 2 + x * (W / 2), H / 2 - y * (H / 2)
        top = cy - block_h / 2
        widest = max(draw.textlength(ln, font=font) for ln in lines) if lines else 0
        sw = max(0, int(stroke_w * px * 0.6))
        for i, ln in enumerate(lines):
            lw = draw.textlength(ln, font=font)
            draw.text((cx - lw / 2, top + i * lh), ln, font=font,
                      fill=(255, 255, 255, int(255 * alpha)),
                      stroke_width=sw, stroke_fill=(0, 0, 0, int(255 * alpha)))
        bbox = (int(cx - widest / 2), int(top), int(cx + widest / 2), int(top + block_h))
        name = track.get("name") or "text"
        boxes.append(("文字", name, bbox))

        # --- 审查 ---
        is_secondary = any(k in name.lower() for k in ("en", "trans", "sub2", "译", "副"))
        limit = MIN_SECONDARY_PX if is_secondary else MIN_PRIMARY_PX
        if px < limit:
            tag = "副字幕/译文" if is_secondary else "主字幕/标题"
            findings.append(f"[字号] {name} 字高约 {px:.0f}px，低于{tag}下限 {limit}px，手机上难读")
        if bbox[0] < SAFE_MARGIN["x"] * W or bbox[2] > (1 - SAFE_MARGIN["x"]) * W:
            findings.append(f"[安全区] {name} 左右超出留白: 「{text[:16]}」")
        if bbox[1] < SAFE_MARGIN["top"] * H:
            findings.append(f"[安全区] {name} 顶部越界: 「{text[:16]}」")
        if bbox[3] > (1 - SAFE_MARGIN["bottom"]) * H:
            findings.append(f"[安全区] {name} 底部越界(易被平台 UI 盖住): 「{text[:16]}」")
        for zone, (zx0, zy0, zx1, zy1) in UI_ZONES.items():
            z = (zx0 * W, zy0 * H, zx1 * W, zy1 * H)
            if bbox[0] < z[2] and bbox[2] > z[0] and bbox[1] < z[3] and bbox[3] > z[1]:
                findings.append(f"[平台UI] {name} 进入{zone}: 「{text[:16]}」")

    # 文字之间重叠
    txt_boxes = [b for b in boxes if b[0] == "文字"]
    for i in range(len(txt_boxes)):
        for j in range(i + 1, len(txt_boxes)):
            a, b = txt_boxes[i][2], txt_boxes[j][2]
            if a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]:
                findings.append(f"[重叠] 文字轨「{txt_boxes[i][1]}」与「{txt_boxes[j][1]}」画面重叠")

    if show_guides:
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        g = ImageDraw.Draw(layer)
        for zx0, zy0, zx1, zy1 in UI_ZONES.values():
            g.rectangle([zx0 * W, zy0 * H, zx1 * W, zy1 * H], fill=(255, 40, 40, 38))
        g.rectangle([SAFE_MARGIN["x"] * W, SAFE_MARGIN["top"] * H,
                     (1 - SAFE_MARGIN["x"]) * W, (1 - SAFE_MARGIN["bottom"]) * H],
                    outline=(80, 220, 120, 200), width=4)
        base.alpha_composite(layer)
        ImageDraw.Draw(base).text((20, 20), f"t = {t_s:.2f}s", font=pick_font(44),
                                  fill=(255, 230, 120, 255), stroke_width=3,
                                  stroke_fill=(0, 0, 0, 255))

    return base, sorted(set(findings))


MIN_VISIBLE_S = 0.4   # 短于此时长的覆盖层/文字等于没出现


def audit_timeline(draft):
    """时间线静态检查: 不依赖采样时刻, 抓退化时长等结构性问题。
    典型案例: 图片素材物理时长仅 0.04s, add_media_safe 的 clamp 把图卡压成 1 帧闪过。"""
    out = []
    mats = draft.get("materials", {})
    vids = {m["id"]: m for m in mats.get("videos", [])}
    texts = {m["id"]: m for m in mats.get("texts", [])}
    total_us = draft.get("duration", 0)

    for track in draft["tracks"]:
        ttype, tname = track.get("type"), track.get("name") or track.get("type")
        for seg in track.get("segments", []):
            tr = seg["target_timerange"]
            dur_s = tr["duration"] / 1_000_000
            if ttype in ("video", "text", "effect") and dur_s < MIN_VISIBLE_S:
                if ttype == "text":
                    parsed = text_of(texts.get(seg.get("material_id"), {}))
                    what = f"文字「{(parsed[0] if parsed else '')[:12]}」"
                else:
                    what = vids.get(seg.get("material_id"), {}).get("material_name", "素材")
                out.append(f"[闪帧] {tname} 轨 {tr['start']/1e6:.2f}s 处 {what} 仅 {dur_s:.3f}s"
                           f"，画面上等于没出现（图片素材常被 clamp 成 0.04s）")
            if ttype == "effect" and 0.4 <= dur_s < 3.0:
                out.append(f"[特效过短] {tname} 轨 {tr['start']/1e6:.2f}s 处特效仅 {dur_s:.2f}s，"
                           f"观众看不出意图，氛围特效建议 ≥ 3s")
            if tr["start"] + tr["duration"] > total_us + 10_000:
                out.append(f"[越界] {tname} 轨片段超出成片时长 "
                           f"({(tr['start']+tr['duration'])/1e6:.2f}s > {total_us/1e6:.2f}s)")
            if ttype == "video" and track is not draft["tracks"][0] and seg.get("volume", 1.0) > 0:
                name = vids.get(seg.get("material_id"), {}).get("material_name", "素材")
                if os.path.splitext(name)[1].lower() in (".mp4", ".mov", ".m4v"):
                    out.append(f"[声音冲突] {tname} 轨「{name}」未静音，"
                               f"自带声音会和旁白/BGM 打架 (建议 volume=0)")
    return sorted(set(out))


def auto_times(draft):
    """关键时刻: 每个插卡/文字片段出现后 0.6s, 加上均匀采样"""
    ts = set()
    for track in draft["tracks"]:
        if track.get("type") not in ("video", "text"):
            continue
        for s in track.get("segments", []):
            tr = s["target_timerange"]
            ts.add(round((tr["start"] + min(600_000, tr["duration"] // 2)) / 1_000_000, 2))
    total = draft.get("duration", 0) / 1_000_000
    for i in range(1, 6):
        ts.add(round(total * i / 6, 2))
    return sorted(t for t in ts if 0 <= t < total)


def contact_sheet(images, out_path, cols=5, tile_w=360):
    if not images:
        return None
    tiles = []
    for label, im in images:
        th = int(im.height * tile_w / im.width)
        t = im.convert("RGB").resize((tile_w, th), Image.LANCZOS)
        tiles.append((label, t))
    tw, th = tiles[0][1].size
    rows = math.ceil(len(tiles) / cols)
    sheet = Image.new("RGB", (cols * tw, rows * (th + 34)), (30, 30, 30))
    d = ImageDraw.Draw(sheet)
    f = pick_font(20)
    for i, (label, t) in enumerate(tiles):
        r, c = divmod(i, cols)
        x, y = c * tw, r * (th + 34)
        sheet.paste(t, (x, y))
        d.text((x + 6, y + th + 7), label, font=f, fill=(235, 235, 235))
    sheet.save(out_path, quality=90)
    return out_path


def main():
    ap = argparse.ArgumentParser(description="剪映草稿版式预览与审片器")
    ap.add_argument("--draft", required=True, help="草稿名或草稿目录路径")
    ap.add_argument("--at", nargs="*", type=float, help="采样时间点 (秒)")
    ap.add_argument("--auto", action="store_true", help="自动选取关键时刻")
    ap.add_argument("--out", default=None, help="输出目录")
    ap.add_argument("--cols", type=int, default=5)
    ap.add_argument("--px-per-size", type=float, default=PX_PER_SIZE_AT_1920,
                    help="字号换算系数 (默认 6.0 px / size @1920)")
    ap.add_argument("--no-guides", action="store_true", help="不画安全区参考线")
    ap.add_argument("--report-only", action="store_true", help="只输出审查报告")
    args = ap.parse_args()

    draft_dir = find_draft(args.draft)
    draft = load_draft(draft_dir)
    out_dir = args.out or os.path.join(tempfile.gettempdir(), "jy_layout_preview",
                                       os.path.basename(draft_dir))
    os.makedirs(out_dir, exist_ok=True)

    if not HAS_PIL and not args.report_only:
        print("⚠️ 未安装 Pillow，跳过画面伪渲染，只做时间线结构检查。")
        print("   安装后可看版式: pip install pillow")
        args.report_only = True

    times = args.at or (auto_times(draft) if args.auto else [])
    if not times and not args.report_only:
        raise SystemExit("请指定 --at 时间点 或使用 --auto")

    canvas = draft.get("canvas_config") or {}
    print(f"草稿: {os.path.basename(draft_dir)}  画布: {canvas.get('width')}x{canvas.get('height')}"
          f"  时长: {draft.get('duration', 0)/1e6:.2f}s  采样 {len(times)} 帧")

    images, all_findings = [], {}
    if HAS_PIL:
        for t in times:
            img, findings = build_frame(draft, t, args.px_per_size, not args.no_guides)
            if not args.report_only:
                path = os.path.join(out_dir, f"t{t:07.2f}.png")
                img.convert("RGB").save(path, quality=90)
                images.append((f"{t:.2f}s", img))
            if findings:
                all_findings[t] = findings

    if not args.report_only and images:
        sheet = contact_sheet(images, os.path.join(out_dir, "sheet.jpg"), cols=args.cols)
        print(f"\n对比图: {sheet}\n单帧目录: {out_dir}")

    timeline_issues = audit_timeline(draft)
    print("\n===== 时间线结构检查 =====")
    if timeline_issues:
        for f in timeline_issues:
            print(f"   • {f}")
    else:
        print("✅ 无闪帧 / 越界 / 未静音覆盖层")

    print("\n===== 版式审片报告 =====")
    if not all_findings:
        print("✅ 未发现安全区 / 字号 / 重叠问题")
    else:
        counts = {}
        for t, fs in sorted(all_findings.items()):
            print(f"\n⏱  {t:.2f}s")
            for f in fs:
                print(f"   • {f}")
                counts[f.split("]")[0] + "]"] = counts.get(f.split("]")[0] + "]", 0) + 1
        print("\n汇总:", "  ".join(f"{k} x{v}" for k, v in sorted(counts.items())))
    return 0 if (not all_findings and not timeline_issues) else 1


if __name__ == "__main__":
    sys.exit(main())
