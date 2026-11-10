
"""
智能网页录屏工具 (Smart Web Recorder)

依赖:
    pip install playwright
    playwright install chromium

功能:
    1. 启动无头浏览器并开启视频录制。
    2. 加载目标 URL/HTML。
    3. 智能等待 JS 信号 (window.animationFinished) 或固定时长。
    4. 导出为高清 MP4 素材文件。
"""
import os
import shutil
import time

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("❌ Error: 'playwright' module not found.")
    print("Please install dependencies:")
    print("  pip install playwright")
    print("  playwright install chromium")
    # 为了演示兼容性，如果用户没安装 playwright，我们定义一个假的录制器
    sync_playwright = None

def record_web_animation(url_or_path: str, output_path: str, max_duration=30):
    """
    录制网页动画直至完成信号触发。
    """
    if not sync_playwright:
        print("⚠️ Playwright not detected. Skipping real recording.")
        return False

    # 确保路径是绝对路径
    if not url_or_path.startswith('http'):
        url_or_path = 'file://' + os.path.abspath(url_or_path)
    
    output_dir = os.path.dirname(os.path.abspath(output_path))
    temp_video_dir = os.path.join(output_dir, "temp_rec")
    
    if os.path.exists(temp_video_dir):
        shutil.rmtree(temp_video_dir)

    print(f"🎥 Starting Recorder for: {url_or_path}")
    
    with sync_playwright() as p:
        # Launch browser with recording enabled
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            record_video_dir=temp_video_dir,
            record_video_size={"width": 1920, "height": 1080},
            viewport={"width": 1920, "height": 1080}
        )
        
        page = context.new_page()

        try:
            page.goto(url_or_path)
            
            # 智能等待 JavaScript 信号
            # 这里的 predicate 会在浏览器上下文中反复执行，直到返回 true
            print("⏳ Waiting for 'window.animationFinished' signal...", end="", flush=True)
            page.wait_for_function("() => window.animationFinished === true", timeout=max_duration * 1000)
            print(" ✅ Signal Received!")
            
            # 稍微多录一点点 (0.5s) 作为缓冲，防止截断
            time.sleep(0.5)

        except Exception as e:
            print(f"\n❌ Timeout or Error waiting for animation: {e}")
            # 即使超时，也保存已录制的内容
        
        # Close context to save video
        context.close()
        browser.close()
        
        # Playwright 保存的文件名是随机的，我们需要找到它并重命名
        video_files = [f for f in os.listdir(temp_video_dir) if f.endswith('.webm')]
        if video_files:
            src_video = os.path.join(temp_video_dir, video_files[0])
            # Playwright 默认录制的是 .webm (Chromium)，剪映通常也支持 webm，
            # 若需 mp4 可能需要 ffmpeg 转码，但这里简单起见直接改后缀或保留
            # 注意：Chromium record_video 产出通常是 webm
            final_ext = os.path.splitext(output_path)[1]
            if not final_ext: final_ext = ".webm"
            
            shutil.move(src_video, output_path)
            print(f"💾 Video saved to: {output_path}")
            
            # 清理临时目录
            shutil.rmtree(temp_video_dir)
            return True
        else:
            print("❌ No video file generated.")
            return False

# ---------------------------------------------------------------------------
# 高清网页录屏 (产品演示 / App 网页版功能录屏)
# ---------------------------------------------------------------------------
# 2026-11-10 实测的坑:
#   1. Playwright 的 record_video 按 CSS 像素出画面: deviceScaleFactor=2.5 + size=1080x1920 时, 432x768 的页面
#      只画在左上角, 其余是灰底; 不开 DSF 又会把 432 宽放大成 1080, 文字发糊。
#      解决: 用 DevTools 截屏流 (Page.startScreencast) 按设备像素逐帧取图, 再按帧时间戳合成 30fps MP4。
#   2. isMobile=True 时 mouse.wheel 不滚动页面; 用 mouse 拖动模拟滑动会选中文字 (蓝色高亮)。
#      解决: 在页面里按缓动曲线改 scrollTop (见 smooth_scroll), 并注入 user-select:none。
#   3. 录屏开头是加载白屏: 每段记录「画面就绪」时刻 (ready), 剪辑时用 source_start 从这里开始截取。
#   4. 无头浏览器录不到光标: 注入触控圆点 (TOUCH_JS), 观众能看到点在哪里。

TOUCH_JS = """(() => {
  const css = document.createElement('style');
  css.textContent = '*{-webkit-user-select:none!important;user-select:none!important}.jy-tap{position:fixed;z-index:2147483647;pointer-events:none;width:44px;height:44px;margin:-22px 0 0 -22px;border-radius:50%;background:rgba(232,114,42,.35);border:3px solid rgba(232,114,42,.9);transition:transform .45s ease-out,opacity .45s ease-out}';
  const add = () => document.head && document.head.appendChild(css);
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', add) : add();
  let dot = null;
  addEventListener('pointerdown', (e) => { dot = document.createElement('div'); dot.className = 'jy-tap'; dot.style.left = e.clientX + 'px'; dot.style.top = e.clientY + 'px'; document.body.appendChild(dot); }, true);
  addEventListener('pointermove', (e) => { if (dot && e.buttons) { dot.style.left = e.clientX + 'px'; dot.style.top = e.clientY + 'px'; } }, true);
  addEventListener('pointerup', () => { const d = dot; dot = null; if (!d) return; d.style.transform = 'scale(1.8)'; d.style.opacity = '0'; setTimeout(() => d.remove(), 500); }, true);
})();"""


def smooth_scroll(page, dy: float, steps: int = 24, delay_ms: int = 30):
    """平滑滚动页面里面积最大的可滚动区域 (移动端模拟下 mouse.wheel 不生效)"""
    page.evaluate("""async ({dy, steps, delay}) => {
      const els = [...document.querySelectorAll('div,main,section,body')].filter((d) => { const s = getComputedStyle(d);
        return /(auto|scroll)/.test(s.overflowY) && d.scrollHeight > d.clientHeight + 10 && d.getBoundingClientRect().height > 0; });
      const el = els.sort((a, b) => b.clientHeight * b.clientWidth - a.clientHeight * a.clientWidth)[0] || document.scrollingElement;
      for (let i = 1; i <= steps; i++) {
        const k = 0.5 - Math.cos(Math.PI * i / steps) / 2, prev = 0.5 - Math.cos(Math.PI * (i - 1) / steps) / 2;
        el.scrollTop += dy * (k - prev);
        await new Promise((r) => setTimeout(r, delay));
      }
    }""", {"dy": dy, "steps": steps, "delay": delay_ms})


def record_web_hidpi(url: str, output_path: str, actions, *, viewport=(432, 768), dpr: float = 2.5,
                     mobile: bool = True, storage_state: str = None, fps: int = 30) -> dict:
    """
    用 DevTools 截屏流录一段高清网页视频 (默认 432x768 x2.5 = 1080x1920 竖屏)。
    actions(page, ready): 你的操作脚本; 画面准备好可以开始看时调用 ready() 记下时刻。
    返回 {"path": MP4 路径, "ready": 就绪时刻(秒), "end": 结束时刻(秒)}, 时间都相对视频第一帧。
    """
    import base64
    import subprocess
    import tempfile

    if not sync_playwright:
        raise RuntimeError("需要 playwright: pip install playwright && playwright install chromium")
    tmp = tempfile.mkdtemp(prefix="jy_screencast_")
    frames, marks = [], {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]}, device_scale_factor=dpr,
                                  is_mobile=mobile, locale="zh-CN", **({"storage_state": storage_state} if storage_state else {}))
        ctx.add_init_script(TOUCH_JS)
        page = ctx.new_page()
        cdp = ctx.new_cdp_session(page)

        def on_frame(f):
            path = os.path.join(tmp, f"f{len(frames):05d}.jpg")
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(f["data"]))
            frames.append((path, f["metadata"]["timestamp"]))
            try:
                cdp.send("Page.screencastFrameAck", {"sessionId": f["sessionId"]})
            except Exception:
                pass

        cdp.on("Page.screencastFrame", on_frame)
        w, h = int(viewport[0] * dpr), int(viewport[1] * dpr)
        cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": w, "maxHeight": h, "everyNthFrame": 1})
        t0 = time.time()
        try:
            actions(page, lambda: marks.setdefault("ready", time.time() - t0))
        finally:
            end = time.time() - t0
            try:
                cdp.send("Page.stopScreencast")
            except Exception:
                pass
            ctx.close()
            browser.close()
    if not frames:
        raise RuntimeError("没有采到任何画面")
    # 截屏流只在画面变化时出帧: 每帧按到下一帧的时间差写时长, 最后一帧停到结束
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w") as fh:
        fh.write("ffconcat version 1.0\n")
        for i, (path, ts) in enumerate(frames):
            nxt = frames[i + 1][1] if i + 1 < len(frames) else t0 + end
            fh.write(f"file '{path}'\nduration {max(0.001, nxt - ts):.4f}\n")
        fh.write(f"file '{frames[-1][0]}'\n")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-vf",
                    f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=white,fps={fps},format=yuv420p",
                    "-c:v", "libx264", "-crf", "18", output_path], check=True)
    first = frames[0][1] - t0
    shutil.rmtree(tmp, ignore_errors=True)
    return {"path": output_path, "ready": max(0.0, marks.get("ready", first) - first), "end": end - first}


if __name__ == "__main__":
    # 测试代码
    skill_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html_path = os.path.join(skill_root, "assets", "web_vfx_demo.html")
    output_video = os.path.join(skill_root, "assets", "generated_vfx.webm")
    
    if os.path.exists(html_path):
        record_web_animation(html_path, output_video)
    else:
        print("HTML file not found.")
