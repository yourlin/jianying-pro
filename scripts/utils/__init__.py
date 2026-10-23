"""skill 内部工具包。导入时让控制台输出不因编码崩溃。"""
import sys


def _safe_console():
    """
    Windows 控制台常是 cp1252 / GBK, print 里的 ✅ ⛔ 等字符会抛 UnicodeEncodeError, 让整个调用失败。
    这里把 stdout / stderr 改成「编码不了的字符用 ? 代替」, 不改变原来的编码。
    """
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if enc and enc != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


_safe_console()
