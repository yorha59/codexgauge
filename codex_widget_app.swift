// Codex Widget 悬浮窗 —— 无边框 NSPanel + WKWebView 壳 v5
// 无关闭按钮 · 顶部把手拖拽 · 原生毛玻璃底(亮背景自适应压暗) · 右键菜单 · 位置记忆
// v5: 点击归还网页(柱状图下钻/按钮交互), 拖拽收敛到顶部把手; 删除"单击任意处切换"
// 编译: swiftc -O -framework WebKit -framework Cocoa -o codex_widget codex_widget_app.swift
import Cocoa
import WebKit

/// 拖拽把手 + 右键菜单（WKWebView 会吞鼠标事件，isMovableByWindowBackground 无效）。
/// 点击策略: 顶部标题条左侧 = 拖拽把手; 其余区域一律透传给网页
/// (按钮点击/柱状图下钻等 DOM 交互)。删除"单击任意处切换展开"。
final class DraggableWebView: WKWebView {
    var onSingleClick: (() -> Void)?   // 仅右键菜单"展开/收起"项使用
    weak var host: PanelDelegate?

    // 把手区: 顶部标题条(前 30pt), 右侧留 150pt 给 acts 热区(历史综合统计⤢+时钟整块, x≈172 起)。
    // 注意 isFlipped=true: loc.y 从顶部算起, 顶部 = loc.y < 30。
    // 60pt 时期按钮文字左半(x 194~262)被把手吞掉, 点击开不了窗——实测修正。
    private func isDragzone(_ loc: NSPoint) -> Bool {
        loc.y < 30 && loc.x < bounds.width - 150
    }

    override func mouseDown(with event: NSEvent) {
        let loc = convert(event.locationInWindow, from: nil)
        if isDragzone(loc) {
            window?.performDrag(with: event)
        } else {
            super.mouseDown(with: event)   // 透传给 DOM: 按钮/柱状图/滚动
        }
    }

    override func rightMouseDown(with event: NSEvent) {
        let menu = NSMenu()
        let toggle = NSMenuItem(title: "打开历史综合统计",
                                action: #selector(toggleAction), keyEquivalent: "")
        toggle.target = self
        let reload = NSMenuItem(title: "重新加载", action: #selector(reloadAction), keyEquivalent: "")
        reload.target = self
        let quit = NSMenuItem(title: "退出悬浮窗", action: #selector(quitAction), keyEquivalent: "q")
        quit.target = self
        menu.addItem(toggle)
        menu.addItem(reload)
        menu.addItem(NSMenuItem.separator())
        menu.addItem(quit)
        NSApp.activate(ignoringOtherApps: true)
        menu.popUp(positioning: nil, at: event.locationInWindow, in: self)
        NSApp.deactivate()
    }

    @objc func reloadAction() { reload() }
    @objc func quitAction() { NSApp.terminate(nil) }
    @objc func toggleAction() { onSingleClick?() }
}

/// 拆分理由浮窗: 点击黄标弹出, 鼠标移入保活/移出关闭(hover 语义), 8s 未进入兜底关闭。
/// 玻璃底与主悬浮窗一致(hudWindow+黑tint)。单例复用, 不叠多窗。
final class ReasonPop: NSObject, WKNavigationDelegate {
    static let shared = ReasonPop()
    var panel: NSPanel?
    var web: WKWebView?
    var hoverTimer: Timer?
    var entered = false          // 鼠标是否进入过浮窗
    var anchor = NSRect.zero     // 主悬浮窗 frame(移出判定豁免区)

    var shownAt = Date()        // show() 时重置, 8s 兜底用

    func show(near host: NSRect, ctx: String, page: String = "reason") {
        anchor = host
        close()
        let size = NSSize(width: 330, height: 420)
        // 出现位置: 主窗左侧; 左缘放不下则右侧; 再兜底当前屏内贴边
        var origin = NSPoint(x: host.minX - size.width - 8, y: host.maxY - size.height)
        if let scr = NSScreen.screens.first(where: { $0.frame.intersects(host) }) {
            if origin.x < scr.visibleFrame.minX {
                origin.x = min(host.maxX + 8, scr.visibleFrame.maxX - size.width)
            }
            origin.x = max(origin.x, scr.visibleFrame.minX)
            origin.y = min(max(origin.y, scr.visibleFrame.minY),
                           scr.visibleFrame.maxY - size.height)
        }
        let p = NSPanel(contentRect: NSRect(origin: origin, size: size),
                        styleMask: [.nonactivatingPanel, .borderless],
                        backing: .buffered, defer: false)
        p.isFloatingPanel = true
        p.level = .floating
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true
        p.hidesOnDeactivate = false
        let effect = NSVisualEffectView(frame: NSRect(origin: .zero, size: size))
        effect.material = .hudWindow
        effect.blendingMode = .behindWindow
        effect.state = .active
        effect.wantsLayer = true
        effect.layer?.cornerRadius = 12
        effect.layer?.borderWidth = 1
        effect.layer?.borderColor = NSColor.white.withAlphaComponent(0.12).cgColor
        let tint = NSView(frame: NSRect(origin: .zero, size: size))
        tint.wantsLayer = true
        tint.layer?.backgroundColor = NSColor.black.withAlphaComponent(0.5).cgColor
        tint.layer?.cornerRadius = 12
        tint.autoresizingMask = [.width, .height]
        effect.addSubview(tint)
        effect.autoresizingMask = [.width, .height]
        p.contentView = effect
        let wv = WKWebView(frame: NSRect(origin: .zero, size: size))
        wv.navigationDelegate = self
        wv.setValue(false, forKey: "drawsBackground")
        wv.underPageBackgroundColor = .clear
        wv.autoresizingMask = [.width, .height]
        effect.addSubview(wv)
        let url = URL(string: "http://127.0.0.1:8790/\(page)?ctx=\(ctx)")!
        wv.load(URLRequest(url: url))
        panel = p
        web = wv
        entered = false
        shownAt = Date()
        p.orderFrontRegardless()
        // 0.25s 轮询全局鼠标位置: 进入过浮窗后, 移出(且不在主窗上)即关
        hoverTimer?.invalidate()
        hoverTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            DispatchQueue.main.async { self?.poll() }
        }
    }

    private func poll() {
        guard let p = panel else { return }
        let m = NSEvent.mouseLocation          // 全局坐标(左下原点)
        let f = p.frame
        let inPop = f.insetBy(dx: -6, dy: -6).contains(m)   // 6pt 容差
        let inHost = anchor.insetBy(dx: -6, dy: -6).contains(m)
        if inPop { entered = true }
        if entered && !inPop && !inHost {
            close()
            return
        }
        // 从未进入过: 8s 兜底关闭
        if !entered && Date().timeIntervalSince(shownAt) > 8 { close() }
    }

    func close() {
        hoverTimer?.invalidate()
        hoverTimer = nil
        panel?.orderOut(nil)
        panel = nil
        web = nil
    }

    func webView(_ w: WKWebView, didFinish n: WKNavigation!) {
        // 内容高度自适应: 高度按 body 撑, 宽固定 330
        w.evaluateJavaScript("document.body.offsetWidth + ',' + document.body.offsetHeight") { r, _ in
            guard let s = r as? String, s.contains(","),
                  let wv = Double(s.split(separator: ",")[0]),
                  let hv = Double(s.split(separator: ",")[1]) else { return }
            let newH = min(max(hv + 16, 200), 520)
            let newW = max(wv + 4, 330)
            guard let p = self.panel else { return }
            var f = p.frame
            guard abs(f.height - newH) > 1.5 || abs(f.width - newW) > 1.5 else { return }
            f.origin.y = f.maxY - newH
            f.size = NSSize(width: newW, height: newH)
            p.setFrame(f, display: true)
        }
    }
}

/// 大窗拖拽把手: titlebar 已隐藏(透明+fullSizeContentView), 需自管拖拽。
/// 策略与悬浮窗把手一致: 顶部 30pt 全宽 = 拖拽, 其余透传 DOM。
/// 顶部 30pt 区域内容 = 红绿灯(左 0~70pt) + 页面标题行(padding-left:78px 起)——标题文字区域可拖, 不与 KPI/图表/按钮冲突。
final class DashDragWebView: WKWebView {
    private func isDragzone(_ loc: NSPoint) -> Bool {
        loc.y < 30   // isFlipped 语义: y 从顶算
    }
    override func mouseDown(with event: NSEvent) {
        let loc = convert(event.locationInWindow, from: nil)
        if isDragzone(loc) {
            window?.performDrag(with: event)
        } else {
            super.mouseDown(with: event)
        }
    }
}

/// 历史综合统计窗口: 标准 titled 窗口(红绿灯/缩放), 从 widget"历史综合统计 ⤢"打开。
/// 主悬浮窗保持收起态不动; 单例复用, 关闭后可再开。
final class DashWindowController: NSObject, NSWindowDelegate, WKNavigationDelegate {
    static let shared = DashWindowController()
    var win: NSWindow?
    var web: WKWebView?
    var loadView: NSView?
    static let frameKey = "codexDashFrame"

    func open(near host: NSRect? = nil) {
        if let w = win {
            w.makeKeyAndOrderFront(nil)   // 已开/曾开: 前置复用, 不重建
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        // 开在 widget 所在屏(点它的悬浮窗就近), 无 host 兜底键窗屏
        let scrFrame: NSRect
        if let host = host, host != .zero,
           let s = NSScreen.screens.first(where: { $0.frame.intersects(host) }) {
            scrFrame = s.visibleFrame
        } else {
            scrFrame = NSScreen.main?.visibleFrame ?? NSRect(x: 100, y: 100, width: 1200, height: 700)
        }
        var frame = NSRect(x: scrFrame.midX - 540, y: scrFrame.midY - 350, width: 1080, height: 700)
        // 位置/大小记忆: 仅当记忆位置与 widget 同屏时才复用, 否则在新屏居中
        // 注意用中心点 contains 判定——intersects 对仅共享屏幕分界边的窗口也返回 true
        if let saved = UserDefaults.standard.string(forKey: Self.frameKey) {
            let f = NSRectFromString(saved)
            let mid = NSPoint(x: f.midX, y: f.midY)
            if f.width > 400 && f.height > 300 && scrFrame.contains(mid) { frame = f }
        }
        let w = NSWindow(contentRect: frame, styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = "Codex 历史综合统计"
        w.isReleasedWhenClosed = false   // 关闭只 orderOut 不销毁: 锁毁会撞上关窗动画(崩溃实测)
        w.delegate = self
        // 大面板用实底深色(玻璃透壁纸在大窗上显虚,用户反馈)
        // 玻璃与悬浮窗完全同配方: hudWindow+behindWindow+50%黑tint(亮度兜底)
        w.titlebarAppearsTransparent = true
        w.styleMask.insert(.fullSizeContentView)
        w.titleVisibility = .hidden
        w.isOpaque = false
        w.backgroundColor = .clear
        let wv = DashDragWebView(frame: NSRect(origin: .zero, size: frame.size))
        wv.setValue(false, forKey: "drawsBackground")  // 透出玻璃
        wv.underPageBackgroundColor = .clear
        wv.autoresizingMask = [.width, .height]
        wv.navigationDelegate = self
        let effect = NSVisualEffectView(frame: NSRect(origin: .zero, size: frame.size))
        effect.material = .hudWindow
        effect.blendingMode = .behindWindow
        effect.state = .active
        effect.wantsLayer = true
        effect.autoresizingMask = [.width, .height]
        // 与悬浮窗同款 50% 黑 tint(白壁纸兜底) — 直角, 大窗不圆角
        let tint = NSView(frame: NSRect(origin: .zero, size: frame.size))
        tint.wantsLayer = true
        tint.layer?.backgroundColor = NSColor.black.withAlphaComponent(0.5).cgColor
        tint.autoresizingMask = [.width, .height]
        effect.addSubview(tint)
        effect.addSubview(wv)   // wv 必须挂进层级(在 tint 之上)
        w.contentView = effect
        // loading 占位: 页面就绪前避免空白窗口 (实测首载 ~300ms) — 同款玻璃+tint
        let load = NSVisualEffectView(frame: frame)
        load.material = .hudWindow
        load.blendingMode = .behindWindow
        load.state = .active
        load.wantsLayer = true
        load.autoresizingMask = [.width, .height]
        let tip = NSTextField(labelWithString: "加载中…")
        tip.font = NSFont.systemFont(ofSize: 13, weight: .medium)
        tip.textColor = NSColor.labelColor
        tip.alignment = .center
        tip.translatesAutoresizingMaskIntoConstraints = false
        load.addSubview(tip)
        NSLayoutConstraint.activate([
            tip.centerXAnchor.constraint(equalTo: load.centerXAnchor),
            tip.centerYAnchor.constraint(equalTo: load.centerYAnchor),
        ])
        wv.addSubview(load)
        loadView = load
        wv.load(URLRequest(url: URL(string: "http://127.0.0.1:8790/")!))
        win = w
        web = wv
        w.makeKeyAndOrderFront(nil)
        // macOS 窗口放置引擎会按主屏重排新窗(T1/T2 插桩实证), orderFront 后须重设 frame 断言目标屏
        var f2 = frame
        f2.size.height += 32   // 构造后实测 frame 高度含标题栏, 对齐之
        w.setFrame(f2, display: true)
        NSApp.activate(ignoringOtherApps: true)
    }

    func webView(_ w: WKWebView, didFinish n: WKNavigation!) {
        loadView?.removeFromSuperview()
        loadView = nil
    }

    func close() {
        win?.close()
        win = nil
        web = nil
    }

    func windowDidMove(_ n: Notification) {
        saveFrame()
    }
    func windowDidEndLiveResize(_ n: Notification) {
        saveFrame()
    }
    func windowWillClose(_ n: Notification) {
        // 窗口已 isReleasedWhenClosed=false, 只 orderOut; win/web 保留供下次复用
    }
    private func saveFrame() {
        if let w = win {
            UserDefaults.standard.set(NSStringFromRect(w.frame), forKey: Self.frameKey)
        }
    }
}

final class PanelDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, NSWindowDelegate, WKScriptMessageHandler {
    var panel: NSPanel!
    var web: DraggableWebView!
    var fitTimer: Timer?
    var expanded = false
    // 两种模式各自记忆位置, 互不覆盖
    static let originKey = "codexWidgetOrigin"
    static let expandedOriginKey = "codexWidgetExpandedOrigin"
    let widgetURL = URL(string: "http://127.0.0.1:8790/widget")!
    let dashURL   = URL(string: "http://127.0.0.1:8790/")!

    func applicationDidFinishLaunching(_ n: Notification) {
        let size = NSSize(width: 324, height: 480)   // 页面加载后按内容自适应

        panel = NSPanel(contentRect: NSRect(origin: .zero, size: size),
                        styleMask: [.nonactivatingPanel, .borderless],
                        backing: .buffered, defer: false)
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.hidesOnDeactivate = false
        panel.delegate = self

        // 原生毛玻璃底: behindWindow 由系统合成器采样窗口背后真实内容——
        // 亮背景自动压暗托住浅色文字, 暗背景近无感。CSS backdrop-filter 在
        // 透明 WKWebView 里采不到窗口背后内容(已实测), 唯一可行的是这层原生效果。
        let effect = NSVisualEffectView(frame: NSRect(origin: .zero, size: size))
        effect.material = .hudWindow
        effect.blendingMode = .behindWindow
        effect.state = .active
        effect.wantsLayer = true
        effect.layer?.cornerRadius = 12
        effect.layer?.borderWidth = 1
        effect.layer?.borderColor = NSColor.white.withAlphaComponent(0.12).cgColor
        // 亮度兜底 tint: 白底上 hudWindow 材质只压到中灰(~134), 叠 50% 黑
        // 保证亮背景下文字对比度; 暗背景下视觉近无感
        let tint = NSView(frame: NSRect(origin: .zero, size: size))
        tint.wantsLayer = true
        tint.layer?.backgroundColor = NSColor.black.withAlphaComponent(0.5).cgColor
        tint.layer?.cornerRadius = 12
        tint.autoresizingMask = [.width, .height]
        effect.addSubview(tint)
        effect.autoresizingMask = [.width, .height]
        panel.contentView = effect

        web = DraggableWebView(frame: NSRect(origin: .zero, size: size))
        web.navigationDelegate = self
        web.host = self
        web.onSingleClick = { [weak self] in self?.toggleMode() }
        // JS → 原生: widget 页"图表 ⤢"按钮 / 面板页"收起"按钮
        web.configuration.userContentController.add(self, name: "expand")
        // JS → 原生: 点击黄标"建议拆分"弹拆分理由浮窗
        web.configuration.userContentController.add(self, name: "popreason")
        web.setValue(false, forKey: "drawsBackground")
        web.underPageBackgroundColor = .clear
        web.autoresizingMask = [.width, .height]
        effect.addSubview(web)
        load()

        restoreOrigin(forExpanded: false, fallbackSize: size)
        panel.orderFrontRegardless()
    }

    // MARK: 模式切换

    // 网页按钮消息: "expand" = 开历史统计新窗口(主悬浮窗不动), "closedash" = 关新窗口
    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.name == "expand" {
            if message.body as? String == "expand" {
                DashWindowController.shared.open(near: panel?.frame)
            } else if message.body as? String == "closedash" {
                DashWindowController.shared.close()
            }
        }
        if message.name == "popreason" {
            let body = (message.body as? String) ?? "?"
            if body.hasPrefix("ck:") {
                // 绿标: ck:ctxK:idleSec → 缓存依据页
                let parts = body.split(separator: ":")
                let ctxK = parts.count > 1 ? parts[1] : "?"
                let idle = parts.count > 2 ? parts[2] : "0"
                ReasonPop.shared.show(near: panel.frame, ctx: ctxK + "&idle=" + idle, page: "cache")
            } else {
                ReasonPop.shared.show(near: panel.frame, ctx: body)
            }
        }
    }

    func toggleMode() {
        // 窗口化改造: 主悬浮窗不再切换展开——统一开历史统计新窗口(开在 widget 同屏)
        DashWindowController.shared.open(near: panel?.frame)
    }

    private func restoreOrigin(forExpanded: Bool, fallbackSize: NSSize) {
        let key = forExpanded ? Self.expandedOriginKey : Self.originKey
        if let saved = UserDefaults.standard.string(forKey: key) {
            let pt = NSPointFromString(saved) ?? NSPoint(x: -1, y: -1)
            if isVisibleScreenPoint(pt, size: fallbackSize) {
                panel.setFrameOrigin(pt)
                return
            }
        }
        if let screen = NSScreen.main {
            let v = screen.visibleFrame
            panel.setFrameOrigin(NSPoint(x: v.maxX - fallbackSize.width - 12, y: v.maxY - fallbackSize.height - 12))
        }
    }

    // MARK: 窗口

    func windowDidMove(_ n: Notification) {
        let key = expanded ? Self.expandedOriginKey : Self.originKey
        UserDefaults.standard.set(NSStringFromPoint(panel.frame.origin), forKey: key)
    }

    private func isVisibleScreenPoint(_ p: NSPoint, size: NSSize) -> Bool {
        NSScreen.screens.contains { $0.visibleFrame.intersects(
            NSRect(origin: p, size: size).insetBy(dx: -40, dy: -40)) }
    }

    func load() {
        var req = URLRequest(url: widgetURL)
        req.timeoutInterval = 10
        web.load(req)
    }

    // MARK: 高度自适应

    func webView(_ w: WKWebView, didFinish navigation: WKNavigation!) {
        fitContentSize()
        fitTimer?.invalidate()
        fitTimer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { _ in
            DispatchQueue.main.async { self.fitContentSize() }
        }
    }

    func fitContentSize() {
        // 展开面板: 固定宽 442(=body max-width 420 + padding/边框), body 宽被视口
        // 反向锁死不能自报告更宽; 收起态: body offset 即内容真实尺寸。
        // 都不能用 documentElement.scrollHeight(视口比内容高时返回视口高度)。
        web.evaluateJavaScript("document.body.offsetWidth + ',' + document.body.offsetHeight") { r, _ in
            guard let s = r as? String, s.contains(","),
                  let w = Double(s.split(separator: ",")[0]),
                  let h = Double(s.split(separator: ",")[1]) else { return }
            let newW = self.expanded ? 1080.0 : min(max(w + 2, 300), 420.0)
            // 展开态: 高度封顶屏幕 80%(超出内容靠页面内滚动); 收起态: 完全自适应
            let maxH = self.expanded ? min(1200.0, Double((self.panel.screen?.visibleFrame.height ?? 900) * 0.8)) : 1100.0
            let newH = min(max(h + 2, 160), maxH)
            let gW = CGFloat(newW), gH = CGFloat(newH)
            var f = self.panel.frame
            guard abs(f.width - gW) > 1.5 || abs(f.height - gH) > 1.5 else { return }
            f.origin.y = f.maxY - gH            // 顶边不动, 向下生长
            // 右缘防溢出: 展开变宽时向左生长, 保证右缘不超所在屏
            if let scr = self.panel.screen {
                let right = f.origin.x + gW
                if right > scr.visibleFrame.maxX {
                    f.origin.x = scr.visibleFrame.maxX - gW
                }
                if f.origin.x < scr.visibleFrame.minX {
                    f.origin.x = scr.visibleFrame.minX
                }
            }
            f.size = NSSize(width: gW, height: gH)
            self.panel.setFrame(f, display: false)
            // autoresizing 在部分场景不跟随, 显式同步 webview 宽高到面板
            DispatchQueue.main.async {
                self.web.frame = self.panel.contentView?.bounds ?? .zero
            }
        }
    }

    func webView(_ w: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { self.load() }
    }
    func webView(_ w: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { self.load() }
    }
}

let app = NSApplication.shared
let delegate = PanelDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
