// 剪映截图/操作用的 macOS 小工具 (由 scripts/jy_preview.py 编译调用):
//   jy_macos windows                 列出剪映窗口: <窗口号>\t<在屏幕上>\t<x>\t<y>\t<宽>\t<高>\t<pid>\t<标题>
//   jy_macos click <x> <y> [次数]    在屏幕坐标 (点) 处点击鼠标左键
//   jy_macos key <pid> <键码> [shift] [次数]   向剪映进程发送按键 (不依赖前台窗口)
// windows 只读窗口信息; click / key 需要「辅助功能」权限。
import CoreGraphics
import Foundation

let a = CommandLine.arguments
func fail(_ s: String) -> Never { FileHandle.standardError.write((s + "\n").data(using: .utf8)!); exit(2) }
guard a.count >= 2 else { fail("usage: jy_macos windows | click x y [n] | key pid code [shift] [n]") }
let src = CGEventSource(stateID: .hidSystemState)

switch a[1] {
case "windows":
    let list = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
    for w in list {
        let owner = w[kCGWindowOwnerName as String] as? String ?? ""
        guard owner.contains("剪映") || owner.lowercased().contains("videofusion"),
              (w[kCGWindowLayer as String] as? Int ?? 1) == 0 else { continue }
        let b = w[kCGWindowBounds as String] as? [String: Double] ?? [:]
        let on = (w[kCGWindowIsOnscreen as String] as? Bool ?? false) ? 1 : 0
        print("\(w[kCGWindowNumber as String] ?? 0)\t\(on)\t\(Int(b["X"] ?? 0))\t\(Int(b["Y"] ?? 0))\t\(Int(b["Width"] ?? 0))\t\(Int(b["Height"] ?? 0))\t\(w[kCGWindowOwnerPID as String] ?? 0)\t\(w[kCGWindowName as String] as? String ?? "")")
    }
case "click":
    guard a.count >= 4, let x = Double(a[2]), let y = Double(a[3]) else { fail("click x y [n]") }
    let n = a.count > 4 ? Int(a[4]) ?? 1 : 1
    let p = CGPoint(x: x, y: y)
    CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(80_000)
    for i in 1...max(n, 1) {
        for t in [CGEventType.leftMouseDown, .leftMouseUp] {
            let e = CGEvent(mouseEventSource: src, mouseType: t, mouseCursorPosition: p, mouseButton: .left)
            e?.setIntegerValueField(.mouseEventClickState, value: Int64(i))
            e?.post(tap: .cghidEventTap)
            usleep(40_000)
        }
        usleep(60_000)
    }
case "key":
    guard a.count >= 4, let pid = Int32(a[2]), let code = UInt16(a[3]) else { fail("key pid code [shift] [n]") }
    let shift = a.count > 4 && a[4] == "shift"
    let n = a.count > 5 ? Int(a[5]) ?? 1 : (a.count > 4 && a[4] != "shift" ? Int(a[4]) ?? 1 : 1)
    for _ in 0..<max(n, 1) {
        for down in [true, false] {
            let e = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: down)
            if shift { e?.flags = .maskShift }
            e?.postToPid(pid)
            usleep(12_000)
        }
    }
default:
    fail("unknown command \(a[1])")
}
