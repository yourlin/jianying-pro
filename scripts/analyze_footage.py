"""
素材分析 (引导模式第 1 步): 读懂素材再做剪辑决策。

    python analyze_footage.py <主视频> [--assets <素材目录>] [-o analysis.json] [--no-transcribe]
                              [--language zh] [--model small] [--prompt "专有名词提示"] [--transcript 已有转写.json]

输出 analysis.json:
  - video: 时长 / 宽高 / 方向 (portrait/landscape/square) / 帧率 / 有无音轨
  - silences: 静音段 (>= --min-silence 秒, 用于剪气口)
  - faces: 人脸在画面中的位置 (OpenCV), 以及插卡 / 标题可以放的区域
  - scenes: 镜头切换时间点 (ffmpeg scene 检测, 用于判断是单镜头口播还是多镜头素材)
  - transcript: 语音转写 (faster-whisper, 带词级时间戳; 没装就跳过)
  - assets: 素材目录里的图片 / 视频及其方向, 作为插卡 / B-roll 候选
  - hints: 由以上推断的场景建议 (口播 / vlog / 混剪 ...) 和要注意的问题
"""

import argparse
import json
import os
import re
import subprocess
import sys
from typing import List, Optional

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}


def probe(path: str) -> dict:
    """ffprobe 读宽高 / 时长 / 帧率 / 旋转, 返回 {} 表示读不到。"""
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=duration:stream=codec_type,width,height,r_frame_rate:stream_side_data=rotation:stream_tags=rotate",
           "-of", "json", path]
    try:
        out = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    streams = out.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    rot = 0
    for sd in v.get("side_data_list") or []:
        rot = int(float(sd.get("rotation", 0) or 0))
    rot = rot or int(float((v.get("tags") or {}).get("rotate", 0) or 0))
    if abs(rot) in (90, 270):
        w, h = h, w
    fps = 0.0
    if v.get("r_frame_rate") and "/" in v["r_frame_rate"]:
        a, b = v["r_frame_rate"].split("/")
        fps = round(float(a) / float(b), 3) if float(b) else 0.0
    return {"width": w, "height": h, "orientation": orientation(w, h), "fps": fps,
            "duration": round(float((out.get("format") or {}).get("duration") or 0), 3),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


def orientation(w: int, h: int) -> str:
    if not w or not h:
        return "unknown"
    r = w / h
    return "square" if 0.9 <= r <= 1.1 else ("portrait" if r < 1 else "landscape")


def detect_silences(path: str, noise_db: float = -35, min_dur: float = 0.45) -> List[dict]:
    """ffmpeg silencedetect: 返回 [{start, end, duration}] (秒)。"""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vn",
           "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"]
    try:
        err = subprocess.run(cmd, capture_output=True, text=True, timeout=600).stderr
    except (OSError, subprocess.TimeoutExpired):
        return []
    out, start = [], None
    for line in err.splitlines():
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
        m = re.search(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", line)
        if m and start is not None:
            out.append({"start": round(start, 3), "end": round(float(m.group(1)), 3),
                        "duration": round(float(m.group(2)), 3)})
            start = None
    return out


def detect_scenes(path: str, threshold: float = 0.3) -> List[float]:
    """镜头切换时间点 (秒)。低分辨率抽帧检测, 长视频也很快。"""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-an",
           "-vf", f"scale=160:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"]
    try:
        err = subprocess.run(cmd, capture_output=True, text=True, timeout=900).stderr
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [round(float(m.group(1)), 3) for m in re.finditer(r"pts_time:([\d.]+)", err)]


def transcribe(path: str, language: Optional[str] = None, model: str = "small",
               prompt: Optional[str] = None) -> Optional[List[dict]]:
    """faster-whisper 转写, 带词级时间戳: [{start, end, text, words: [{w, s, e}]}]; 没装返回 None。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    m = WhisperModel(model, device="auto", compute_type="int8")
    segments, _info = m.transcribe(path, language=language, word_timestamps=True, vad_filter=True,
                                   initial_prompt=prompt or None)
    out = []
    for seg in segments:
        words = [{"w": w.word, "s": round(w.start, 3), "e": round(w.end, 3)} for w in (seg.words or [])]
        out.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip(), "words": words})
    return out


def detect_faces(path: str, duration: float, samples: int = 24) -> Optional[dict]:
    """
    抽帧做人脸检测 (OpenCV Haar), 返回人脸在画面中的典型位置, 给插卡 / 标题避让用:
      box: 中位人脸框 [x0, y0, x1, y1] (画面比例, 左上为 0); present: 有人脸的帧占比;
      face_y: 人脸中心对应的剪映 transform_y (+ 为上); card_zones: 建议放插卡的 transform_y 区间。
    没装 OpenCV 或 OpenCV 没有 Haar 级联 (5.x) 时返回 None。
    """
    try:
        import cv2
    except ImportError:
        return None
    import tempfile

    # opencv-python 5.x 去掉了 Haar 级联, 此时跳过人脸检测
    if not hasattr(cv2, "CascadeClassifier") or not hasattr(cv2, "data"):
        return None
    cascade = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
    boxes, seen = [], 0
    with tempfile.TemporaryDirectory() as tmp:
        n = max(1, min(samples, int(duration // 2) or 1))
        for i in range(n):
            t = duration * (i + 0.5) / n
            img = os.path.join(tmp, f"{i}.jpg")
            subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
                            "-vf", "scale=360:-2", "-y", img], capture_output=True)
            frame = cv2.imread(img)
            if frame is None:
                continue
            seen += 1
            h, w = frame.shape[:2]
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, 1.1, 5, minSize=(int(w * 0.12), int(w * 0.12)))
            if len(faces):
                x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                boxes.append((x / w, y / h, (x + fw) / w, (y + fh) / h))
    if not seen:
        return None
    if not boxes:
        return {"present": 0.0}

    def med(i):
        v = sorted(b[i] for b in boxes)
        return round(v[len(v) // 2], 3)

    x0, y0, x1, y1 = med(0), med(1), med(2), med(3)
    to_ty = lambda r: round(1 - 2 * r, 3)  # 画面比例 (上为 0) -> 剪映 transform_y (上为 +1)
    # 头部比脸框高出约 40% 脸高 (头发), 下巴往下到肩
    head_top, chin = max(0.0, y0 - 0.4 * (y1 - y0)), y1
    zones = []
    if head_top > 0.22:  # 头顶上方的空间够放卡
        zones.append({"where": "头顶上方", "y_range": [to_ty(0.10), to_ty(head_top - 0.02)]})
    if chin < 0.62:  # 下巴到字幕区 (约 72%) 之间
        zones.append({"where": "下巴与字幕之间", "y_range": [to_ty(chin + 0.03), to_ty(0.66)]})
    return {"present": round(len(boxes) / seen, 2), "box": [x0, y0, x1, y1], "face_y": to_ty((y0 + y1) / 2),
            "head_top_y": to_ty(head_top), "chin_y": to_ty(chin), "card_zones": zones}


def scan_assets(folder: str, exclude: str = "") -> List[dict]:
    """素材目录里的图片 / 视频 (不递归, 跳过隐藏文件和 __jycache__)。"""
    out = []
    if not folder or not os.path.isdir(folder):
        return out
    for name in sorted(os.listdir(folder)):
        p = os.path.join(folder, name)
        ext = os.path.splitext(name)[1].lower()
        if name.startswith(".") or not os.path.isfile(p) or os.path.abspath(p) == os.path.abspath(exclude):
            continue
        if ext in IMAGE_EXTS or ext in VIDEO_EXTS:
            info = probe(p)
            out.append({"path": p, "kind": "image" if ext in IMAGE_EXTS else "video", **info})
    return out


def hints(video: dict, silences: List[dict], scenes: List[float], transcript, assets: List[dict]) -> dict:
    """由分析结果推断场景和要注意的问题 (给引导模式做默认选项, 最终由用户选)。"""
    dur = video.get("duration") or 0
    speech = sum(s["end"] - s["start"] for s in transcript or [])
    talk_ratio = speech / dur if dur else 0
    shots_per_min = len(scenes) / (dur / 60) if dur else 0
    if transcript and talk_ratio > 0.6 and shots_per_min < 4:
        scene = "口播知识"
    elif transcript and shots_per_min >= 4:
        scene = "vlog"
    elif not transcript and shots_per_min >= 8:
        scene = "卡点混剪"
    else:
        scene = "vlog" if shots_per_min >= 2 else "口播知识"
    notes = []
    gaps = [s for s in silences if 0.45 <= s["duration"] <= 3 and 1 < s["start"] < dur - 1]
    if gaps:
        notes.append(f"有 {len(gaps)} 处 0.45~3 秒的静音气口, 共 {sum(g['duration'] for g in gaps):.1f}s, 可以剪掉提节奏")
    tail = [s for s in silences if s["end"] >= dur - 0.2 and s["duration"] > 0.8]
    if tail:
        notes.append(f"片尾 {tail[0]['duration']:.1f}s 静音, 建议裁掉")
    if video.get("orientation") == "portrait":
        notes.append("竖屏素材: 画布用 1080x1920, 横版素材只能做插卡或垫底, 不能当主画面")
    wrong = [a for a in assets if a.get("orientation") not in (video.get("orientation"), "square", "unknown")]
    if wrong:
        notes.append(f"{len(wrong)} 个素材方向和主视频不同 (只适合做插卡 / 画中画)")
    if transcript is None:
        notes.append("没有转写 (未安装 faster-whisper 或用了 --no-transcribe), 字幕和卡点需要另行处理")
    return {"scene_guess": scene, "talk_ratio": round(talk_ratio, 2), "shots_per_min": round(shots_per_min, 1),
            "cuttable_gaps": gaps, "notes": notes}


def analyze(video_path: str, assets_dir: Optional[str] = None, do_transcribe: bool = True,
            language: Optional[str] = None, model: str = "small", prompt: Optional[str] = None,
            min_silence: float = 0.45, transcript_path: Optional[str] = None) -> dict:
    video = probe(video_path)
    if not video:
        raise RuntimeError(f"读不到视频信息 (需要 ffprobe): {video_path}")
    silences = detect_silences(video_path, min_dur=min_silence) if video.get("has_audio") else []
    scenes = detect_scenes(video_path)
    if transcript_path:  # 复用已有转写 (格式同 transcribe 的输出)
        with open(transcript_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        transcript = data.get("transcript") if isinstance(data, dict) else data
    else:
        transcript = transcribe(video_path, language, model, prompt) if do_transcribe and video.get("has_audio") else None
    faces = detect_faces(video_path, video.get("duration") or 0)
    assets = scan_assets(assets_dir if assets_dir is not None else os.path.dirname(os.path.abspath(video_path)),
                         exclude=video_path)
    res = {"source": os.path.abspath(video_path), "video": video, "faces": faces, "silences": silences,
           "scenes": scenes, "transcript": transcript, "assets": assets,
           "hints": hints(video, silences, scenes, transcript, assets)}
    if faces and faces.get("box"):
        f = faces
        res["hints"]["notes"].append(
            f"人脸中心约在 transform_y={f['face_y']} (头顶 {f['head_top_y']} / 下巴 {f['chin_y']}), 插卡和标题要避开; "
            + ("建议插卡区: " + "、".join(f"{z['where']} y∈{z['y_range']}" for z in f["card_zones"])
               if f["card_zones"] else "画面里没有足够的空白, 插卡只能短暂压屏或改成小窗"))
    return res


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="素材分析 (引导模式第 1 步)")
    ap.add_argument("video")
    ap.add_argument("--assets", help="插卡 / B-roll 素材目录 (默认主视频所在目录)")
    ap.add_argument("-o", "--output", help="输出 JSON (默认 <主视频目录>/analysis.json)")
    ap.add_argument("--no-transcribe", action="store_true")
    ap.add_argument("--language", help="语音语言, 如 zh / en (默认自动)")
    ap.add_argument("--model", default="small", help="faster-whisper 模型 (tiny/base/small/medium)")
    ap.add_argument("--prompt", help="转写提示词: 专有名词、人名等, 提高识别准确率")
    ap.add_argument("--min-silence", type=float, default=0.45)
    ap.add_argument("--transcript", help="复用已有转写 JSON (不再重新识别)")
    args = ap.parse_args(argv)
    res = analyze(args.video, args.assets, not args.no_transcribe, args.language, args.model, args.prompt,
                  args.min_silence, args.transcript)
    out = args.output or os.path.join(os.path.dirname(os.path.abspath(args.video)), "analysis.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    v, h = res["video"], res["hints"]
    print(f"🎬 {os.path.basename(args.video)}: {v['width']}x{v['height']} {v['orientation']}, {v['duration']:.1f}s, "
          f"{v['fps']}fps, 音轨 {'有' if v['has_audio'] else '无'}")
    print(f"   镜头切换 {len(res['scenes'])} 次 ({h['shots_per_min']}/分钟), 说话占比 {h['talk_ratio']:.0%}, "
          f"转写 {len(res['transcript'] or [])} 句, 素材 {len(res['assets'])} 个")
    print(f"   场景推断: {h['scene_guess']}")
    for n in h["notes"]:
        print(f"   - {n}")
    print(f"✅ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
