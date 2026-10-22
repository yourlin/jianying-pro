"""
在剪映里打开草稿, 定位到指定时间, 截取播放器画面, 用于生成草稿后检查效果 (macOS, 剪映 11.x)。

    python jy_preview.py 草稿名 --at 1 --at 3.5 [-o 输出目录] [--full] [--keep-open]
    python jy_preview.py --snapshot [-o out.png]        # 只截当前剪映窗口

需要两项系统权限 (系统设置 → 隐私与安全性, 勾选运行脚本的终端/应用):
  - 屏幕录制: 截图。只截剪映窗口, 不截其他屏幕内容; 窗口被遮挡也能截到, 最小化时截不到。
  - 辅助功能: 打开草稿 (点击首页草稿封面), 定位时间 (向剪映发送 Home / ←→ 按键), 回到首页 (菜单「返回首页」)。
操作时会把剪映切到前台并点击鼠标, 请不要同时操作电脑。剪映需要已打开并停在首页或编辑页。
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
SWIFT_SRC = os.path.join(HERE, "..", "tools", "macos", "jy_macos.swift")
BIN = os.path.join(os.path.expanduser("~/.cache/jianying-pro"), "jy_macos")
PROCESS = "VideoFusion-macOS"
TITLE_PREFIX = "HomePageDraftTitle:"
KEY_HOME, KEY_LEFT, KEY_RIGHT = 115, 123, 124
PANEL_GAP = (20, 20, 20)  # 剪映 11.x 面板之间的缝隙颜色
PANEL_BGS = ((38, 38, 38), (48, 48, 48), (20, 20, 20))  # 面板背景 / 标题栏 / 缝隙


class PreviewError(RuntimeError):
    pass


def _helper() -> str:
    """编译 (首次约 10 秒) 并返回 macOS 小工具路径。"""
    if not os.path.exists(BIN) or os.path.getmtime(BIN) < os.path.getmtime(SWIFT_SRC):
        os.makedirs(os.path.dirname(BIN), exist_ok=True)
        r = subprocess.run(["swiftc", "-O", SWIFT_SRC, "-o", BIN], capture_output=True, text=True)
        if r.returncode != 0:
            raise PreviewError(f"编译 macOS 小工具失败 (需要 Xcode 命令行工具): {r.stderr.strip()[:300]}")
    return BIN


def _tool(*args) -> str:
    return subprocess.run([_helper(), *map(str, args)], capture_output=True, text=True, check=True).stdout


def _osa(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        if "辅助访问" in r.stderr or "assistive" in r.stderr.lower():
            raise PreviewError("需要辅助功能权限: 系统设置 → 隐私与安全性 → 辅助功能, 勾选运行脚本的终端/应用")
        raise PreviewError(f"AppleScript 失败: {r.stderr.strip()}")
    return r.stdout.strip()


def _tell(body: str) -> str:
    return _osa(f'tell application "System Events" to tell process "{PROCESS}"\n{body}\nend tell')


# ---------- 窗口 ----------

def list_windows() -> List[dict]:
    wins = []
    for line in _tool("windows").splitlines():
        wid, on, x, y, w, h, pid, *title = line.split("\t")
        wins.append({"id": int(wid), "onscreen": on == "1", "x": int(x), "y": int(y), "w": int(w), "h": int(h),
                     "pid": int(pid), "title": title[0] if title else ""})
    return wins


def main_window() -> Optional[dict]:
    """剪映主窗口 (首页或编辑页): 在屏幕上、面积最大的窗口。"""
    wins = [w for w in list_windows() if w["onscreen"] and w["w"] >= 600 and w["h"] >= 400]
    return max(wins, key=lambda w: w["w"] * w["h"]) if wins else None


def snapshot(out: Optional[str] = None) -> str:
    """截取剪映主窗口 (Retina 下为 2 倍像素), 返回 PNG 路径。"""
    win = main_window()
    if win is None:
        raise PreviewError("没找到剪映窗口: 请先打开剪映 (窗口不能最小化)")
    out = out or os.path.join(tempfile.gettempdir(), f"jy_snapshot_{int(time.time() * 1000)}.png")
    r = subprocess.run(["screencapture", "-x", "-o", f"-l{win['id']}", out], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        raise PreviewError(f"截图失败 (需要屏幕录制权限): {r.stderr.strip()}")
    return out


# ---------- 首页 / 编辑页 ----------

WIN = 'first window whose name is "剪映专业版"'


def _center(name: str) -> Optional[Tuple[int, int, int, int]]:
    """主窗口中名为 name 的 static text 的 (x, y, w, h) (屏幕坐标, 点); 不存在返回 None。"""
    out = _tell(f'''try
set e to static text "{name}" of ({WIN})
set {{px, py}} to position of e
set {{sx, sy}} to size of e
return (px as text) & "," & (py as text) & "," & (sx as text) & "," & (sy as text)
on error
return ""
end try''')
    return tuple(int(float(v)) for v in out.split(",")) if out else None


def _first_title_box() -> Optional[Tuple[int, int, int, int]]:
    """首页列表里第一个可见草稿标题的位置, 用作滚动的落点。"""
    out = _tell(f'''try
set e to first static text of ({WIN}) whose name starts with "{TITLE_PREFIX}"
set {{px, py}} to position of e
set {{sx, sy}} to size of e
return (px as text) & "," & (py as text) & "," & (sx as text) & "," & (sy as text)
on error
return ""
end try''')
    return tuple(int(float(v)) for v in out.split(",")) if out else None


def in_editor() -> bool:
    return _center("currentProgress") is not None


def _activate():
    _tell("set frontmost to true")
    time.sleep(0.3)


def _pid() -> int:
    win = main_window()
    if win is None:
        raise PreviewError("没找到剪映窗口: 请先打开剪映 (窗口不能最小化)")
    return win["pid"]


def _key(code: int, shift: bool = False, count: int = 1):
    if count > 0:
        _tool("key", _pid(), code, *(["shift"] if shift else []), count)


def go_home(timeout: float = 20):
    """编辑页 → 首页 (菜单「剪映专业版 → 返回首页」, 不行再按 Cmd+W; 剪映会自动保存草稿)。已在首页时不操作。"""
    if not in_editor():
        return
    _activate()
    try:  # 剪映前面常浮着一个小窗口, 按键可能被它接走, 菜单更可靠
        _tell('click menu item "返回首页" of menu 1 of menu bar item "剪映专业版" of menu bar 1')
    except PreviewError:
        _tell('keystroke "w" using command down')
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.5)
        if not in_editor():
            time.sleep(1)
            return
    raise PreviewError("回到首页超时")


def refresh_home():
    """首页草稿列表不会自动发现新草稿: 切到「模板」再切回「首页」让剪映重新扫描。"""
    for tab in ("模板", "首页"):
        box = _center(tab)
        if box:
            _tool("click", box[0] + box[2] // 2, box[1] + box[3] // 2)
            time.sleep(1.5)


def open_draft(name: str, timeout: float = 40, settle: float = 3, close_editor: bool = False):
    """
    在剪映里打开草稿。剪映停在编辑页时默认报错: 剪映关闭草稿时会保存它内存里的版本,
    如果那正是 skill 刚改过的草稿, 修改会被覆盖。确认无妨时传 close_editor=True。
    """
    _activate()
    if in_editor():
        if not close_editor:
            raise PreviewError("剪映正停在编辑页: 请先回到首页再预览 (剪映关闭草稿时可能覆盖 skill 刚保存的修改)")
        go_home()
    title = TITLE_PREFIX + name
    box = _center(title)
    if box is None:
        refresh_home()
        box = _center(title)
    scrolled = 0
    while box is None and scrolled < 40:  # 不在第一屏: 在草稿列表上往下滚动查找
        anchor = _first_title_box()
        if anchor is None:
            break
        _tool("scroll", anchor[0] + 200, anchor[1], -5)
        scrolled += 5
        time.sleep(0.6)
        box = _center(title)
    if box is None:
        raise PreviewError(f"首页列表里没找到草稿「{name}」")
    # 点击标题左侧的封面 (标题本身只选中不打开)
    _tool("click", box[0] - 42, box[1] + box[3] // 2)
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.5)
        if in_editor():
            time.sleep(settle)  # 等素材/特效加载
            return
    raise PreviewError(f"打开草稿「{name}」超时")


def draft_fps(name: str) -> float:
    try:
        from utils.formatters import get_default_drafts_root

        path = os.path.join(get_default_drafts_root(), name, "draft_info.skill.json")
        with open(path, "r", encoding="utf-8") as f:
            return float(json.load(f).get("fps") or 30)
    except Exception:
        return 30.0


def current_frame(fps: float = 30.0) -> Optional[int]:
    """播放头当前位置 (帧), 读自播放器时间码的辅助功能描述 (currentProgress|HH:MM:SS:FF)。"""
    try:
        desc = _tell(f'return description of static text "currentProgress" of ({WIN})')
    except PreviewError:
        return None
    tc = desc.split("|")[-1].strip()
    parts = tc.split(":")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return None
    h, m, sec, ff = map(int, parts)
    return int(round((h * 3600 + m * 60 + sec) * fps)) + ff


def _step(frames: int):
    """按帧移动播放头: Shift+←/→ 每次 10 帧, ←/→ 每次 1 帧。"""
    code = KEY_RIGHT if frames > 0 else KEY_LEFT
    n = abs(frames)
    _key(code, shift=True, count=n // 10)
    _key(code, count=n % 10)


def seek(seconds: float, fps: float = 30.0, settle: float = 1.0, retries: int = 4):
    """
    播放头定位到 seconds 秒: Home 回到开头, 再按 Shift+→ (10 帧) / → (1 帧)。
    剪映忙 (刚打开草稿、加载素材) 时会丢按键, 所以按完读时间码校验, 不对就补按差值。
    """
    target = max(0, int(round(seconds * fps)))
    _activate()
    _key(KEY_HOME)
    time.sleep(0.2)
    _step(target)
    for _ in range(retries):
        time.sleep(0.4)
        cur = current_frame(fps)
        if cur is None or cur == target:
            break
        _step(target - cur)
    time.sleep(settle)


# ---------- 播放器区域 ----------

def _near(c, ref, tol=4) -> bool:
    return all(abs(int(a) - b) <= tol for a, b in zip(c[:3], ref))


def player_box(img, win: dict) -> Tuple[int, int, int, int]:
    """播放器画布在截图中的像素区域: 由时间码 (currentProgress) 找到播放器面板, 再取面板里的画布。"""
    tc = _center("currentProgress")
    if tc is None:
        raise PreviewError("不在编辑页, 无法定位播放器")
    s = img.width / win["w"]
    px = img.convert("RGB").load()
    x0, y0 = int((tc[0] - win["x"] - 4) * s), int((tc[1] - win["y"] + tc[3] // 2) * s)

    def walk(x, y, dx, dy):
        while 0 < x < img.width - 1 and 0 < y < img.height - 1 and not _near(px[x, y], PANEL_GAP, 2):
            x, y = x + dx, y + dy
        return x, y

    left, _ = walk(x0, y0, -1, 0)
    right, _ = walk(x0, y0, 1, 0)
    _, top = walk(x0, y0, 0, -1)
    # 画布: 面板内、标题栏以下、时间码以上, 与面板背景不同的区域
    ys, ye = top + int(30 * s), y0 - int(14 * s)
    xs, xe = left + 2, right - 2
    def content(x, y):
        return not any(_near(px[x, y], bg, 3) for bg in PANEL_BGS)

    cols = [x for x in range(xs, xe, 2) if any(content(x, y) for y in range(ys, ye, 8))]
    rows = [y for y in range(ys, ye, 2) if any(content(x, y) for x in range(xs, xe, 8))]
    if not cols or not rows:
        return xs, ys, xe, ye
    return cols[0], rows[0], cols[-1] + 2, rows[-1] + 2


def capture_player(out: str, full: bool = False) -> str:
    path = snapshot(out)
    if full:
        return path
    from PIL import Image

    img = Image.open(path)
    img.crop(player_box(img, main_window())).save(path)
    return path


def preview(name: str, times: List[float], out_dir: Optional[str] = None, full: bool = False,
            keep_open: bool = False, close_editor: bool = False) -> List[str]:
    """
    打开草稿, 依次截取各时间点的播放器画面, 返回 PNG 路径列表; 截完回到首页 (keep_open=True 时留在编辑页,
    之后再改这个草稿前要先回首页)。
    """
    out_dir = out_dir or os.path.join(tempfile.gettempdir(), "jy_preview")
    os.makedirs(out_dir, exist_ok=True)
    open_draft(name, close_editor=close_editor)
    fps = draft_fps(name)
    paths = []
    try:
        for t in times or [0]:
            seek(t, fps)
            paths.append(capture_player(os.path.join(out_dir, f"{name}_{t:g}s.png"), full))
    finally:
        if not keep_open:
            go_home()
    return paths


def _discard_draft(name: str):
    """把草稿目录移到废纸篓 (可恢复)。只用于 skill 生成的预热草稿。"""
    from utils.formatters import get_default_drafts_root

    src = os.path.join(get_default_drafts_root(), name)
    if os.path.isdir(src):
        dst = os.path.join(os.path.expanduser("~/.Trash"), f"{name}.{int(time.time())}")
        os.rename(src, dst)


def open_and_wait(names: List[str], wait: float = 60, close_editor: bool = False,
                  discard: bool = False) -> List[str]:
    """
    依次在剪映里打开草稿, 停留 wait 秒 (让剪映下载素材 / 模板包) 后回到首页。用于模板预热。
    discard=True 时每个草稿用完就移到废纸篓, 免得首页列表越来越长、后面的草稿找不到。
    返回成功打开的草稿名; 单个失败不影响后面的。
    """
    done = []
    for i, name in enumerate(names, 1):
        try:
            open_draft(name, close_editor=close_editor or i > 1)
            print(f"⏳ [{i}/{len(names)}] 已打开「{name}」, 等待 {wait:.0f} 秒", flush=True)
            time.sleep(wait)
            go_home()
            done.append(name)
            if discard:
                _discard_draft(name)
        except PreviewError as e:
            print(f"⚠️ [{i}/{len(names)}] 「{name}」: {e}", flush=True)
            try:
                go_home()
            except PreviewError:
                pass
    return done


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="在剪映里打开草稿并截取播放器画面 (macOS)")
    ap.add_argument("name", nargs="?", help="草稿名")
    ap.add_argument("--at", type=float, action="append", help="截图时间点 (秒), 可重复; 默认 0")
    ap.add_argument("-o", "--output", help="输出目录 (--snapshot 时为文件路径)")
    ap.add_argument("--full", action="store_true", help="截整个剪映窗口, 不裁剪播放器")
    ap.add_argument("--keep-open", action="store_true", help="截完不回首页")
    ap.add_argument("--snapshot", action="store_true", help="只截当前剪映窗口")
    ap.add_argument("--close-editor", action="store_true", help="剪映停在编辑页时先关闭当前草稿 (Cmd+W)")
    args = ap.parse_args(argv)
    try:
        if args.snapshot:
            print(snapshot(args.output))
        elif args.name:
            for p in preview(args.name, args.at or [0], args.output, args.full, args.keep_open,
                             args.close_editor):
                print(p)
        else:
            ap.error("需要草稿名或 --snapshot")
    except (PreviewError, subprocess.CalledProcessError) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
