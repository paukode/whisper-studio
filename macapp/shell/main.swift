// Whisper Studio — thin native macOS shell.
//
// Single-file AppKit + WebKit app compiled with swiftc (no xcodeproj).
// Responsibilities:
//   * spawn the bundled Python backend (Resources/backend, Resources/python)
//   * wait for /health, then show the SPA in a WKWebView
//   * forward mic-capture permission to the OS TCC prompt
//   * shut the backend down cleanly on quit (SIGTERM, then SIGKILL)

import AppKit
import Darwin
import Foundation
import WebKit

// MARK: - Free port discovery

/// Bind an IPv4 socket to 127.0.0.1:0, read back the kernel-assigned port,
/// close the socket, and return the port.
func findFreePort() -> UInt16? {
    let sock = socket(AF_INET, SOCK_STREAM, 0)
    guard sock >= 0 else { return nil }
    defer { Darwin.close(sock) }

    var reuse: Int32 = 1
    setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout<Int32>.size))

    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = 0
    addr.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))

    let bindResult = withUnsafePointer(to: &addr) { ptr -> Int32 in
        ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { saPtr in
            Darwin.bind(sock, saPtr, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    guard bindResult == 0 else { return nil }

    var bound = sockaddr_in()
    var len = socklen_t(MemoryLayout<sockaddr_in>.size)
    let nameResult = withUnsafeMutablePointer(to: &bound) { ptr -> Int32 in
        ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { saPtr in
            getsockname(sock, saPtr, &len)
        }
    }
    guard nameResult == 0 else { return nil }
    return UInt16(bigEndian: bound.sin_port)
}

// MARK: - App delegate

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate {

    // Backend process state
    private var backend: Process?
    private var expectingBackendExit = false
    private var logFileHandle: FileHandle?

    // UI state
    private var splashWindow: NSWindow?
    private var mainWindow: NSWindow?
    private var webView: WKWebView?

    // Server location
    private var port: UInt16 = 0
    private var baseURL: URL?
    private var whisperHome = ""
    private var logsDir = ""
    private var logPath = ""
    private var healthDeadline = Date()

    // Retained so the sources stay armed for the app's lifetime.
    private var signalSources: [DispatchSourceSignal] = []

    // MARK: Lifecycle

    /// A raw SIGTERM/SIGINT (logout, `kill`) skips AppKit's terminate flow, so
    /// applicationWillTerminate would never run and the backend (plus any
    /// llama-server under it) would be orphaned. Route both signals into the
    /// normal quit path instead.
    private func installSignalHandlers() {
        for sig in [SIGTERM, SIGINT] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { NSApp.terminate(nil) }
            source.resume()
            signalSources.append(source)
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        installSignalHandlers()
        buildMainMenu()

        guard let resourcesURL = Bundle.main.resourceURL else {
            fatalStartupAlert("The application bundle has no Resources directory.")
            return
        }
        let pythonBin = resourcesURL.appendingPathComponent("python/bin/python3")
        let backendDir = resourcesURL.appendingPathComponent("backend")
        let binDir = resourcesURL.appendingPathComponent("bin")

        // WHISPER_HOME: honour a pre-set env var (testing), else Application Support.
        let env = ProcessInfo.processInfo.environment
        if let preset = env["WHISPER_HOME"], !preset.isEmpty {
            whisperHome = (preset as NSString).expandingTildeInPath
        } else {
            whisperHome = NSHomeDirectory() + "/Library/Application Support/WhisperStudio"
        }
        logsDir = whisperHome + "/logs"
        logPath = logsDir + "/backend.log"
        do {
            try FileManager.default.createDirectory(
                atPath: logsDir, withIntermediateDirectories: true)
        } catch {
            fatalStartupAlert("Could not create \(logsDir): \(error.localizedDescription)")
            return
        }

        guard FileManager.default.isExecutableFile(atPath: pythonBin.path) else {
            fatalStartupAlert("Bundled Python runtime is missing at:\n\(pythonBin.path)")
            return
        }
        guard FileManager.default.fileExists(atPath: backendDir.path) else {
            fatalStartupAlert("Bundled backend is missing at:\n\(backendDir.path)")
            return
        }

        guard let freePort = findFreePort() else {
            fatalStartupAlert("Could not find a free TCP port on 127.0.0.1.")
            return
        }
        port = freePort
        baseURL = URL(string: "http://127.0.0.1:\(port)/")

        showSplash()

        do {
            try startBackend(pythonBin: pythonBin, backendDir: backendDir, binDir: binDir)
        } catch {
            fatalStartupAlert("Failed to launch the backend: \(error.localizedDescription)")
            return
        }

        healthDeadline = Date().addingTimeInterval(120)
        pollHealth()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        shutdownBackend()
    }

    // MARK: Backend process

    private func startBackend(pythonBin: URL, backendDir: URL, binDir: URL) throws {
        // Log file: append, with a launch timestamp line.
        if !FileManager.default.fileExists(atPath: logPath) {
            FileManager.default.createFile(atPath: logPath, contents: nil)
        }
        let handle = FileHandle(forWritingAtPath: logPath)
        if let handle = handle {
            handle.seekToEndOfFile()
            let stamp = ISO8601DateFormatter().string(from: Date())
            if let line = "\n===== Whisper Studio launch \(stamp) — port \(port) =====\n"
                .data(using: .utf8)
            {
                handle.write(line)
            }
        }
        logFileHandle = handle

        var procEnv = ProcessInfo.processInfo.environment
        procEnv["HOST"] = "127.0.0.1"
        procEnv["PORT"] = String(port)
        procEnv["WHISPER_HOME"] = whisperHome
        procEnv["WHISPER_BIN_DIR"] = binDir.path
        procEnv["WHISPER_LLAMA_SERVER_PATH"] = binDir.appendingPathComponent("llama-server").path
        procEnv["WHISPER_FFMPEG_PATH"] = binDir.appendingPathComponent("ffmpeg").path
        procEnv["WHISPER_FFPROBE_PATH"] = binDir.appendingPathComponent("ffprobe").path
        procEnv["WHISPER_NODE_PATH"] = binDir.appendingPathComponent("node").path
        // Lets the backend notice when the shell dies without any signal at
        // all (SIGKILL, crash) and shut itself down instead of lingering.
        procEnv["WHISPER_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        let inheritedPath = procEnv["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        procEnv["PATH"] = binDir.path + ":" + inheritedPath
        procEnv["PYTHONUNBUFFERED"] = "1"
        // Bytecode caches must never land inside the signed bundle — a single
        // .pyc write after signing invalidates the whole app seal.
        procEnv["PYTHONPYCACHEPREFIX"] = whisperHome + "/pycache"

        let proc = Process()
        proc.executableURL = pythonBin
        proc.arguments = ["-m", "server.main"]
        proc.currentDirectoryURL = backendDir
        proc.environment = procEnv
        if let handle = handle {
            proc.standardOutput = handle
            proc.standardError = handle
        }
        proc.terminationHandler = { [weak self] finished in
            let status = finished.terminationStatus
            DispatchQueue.main.async {
                guard let self = self, !self.expectingBackendExit else { return }
                self.backendDied(status: status)
            }
        }

        try proc.run()
        backend = proc
    }

    private func shutdownBackend() {
        guard let proc = backend, proc.isRunning else { return }
        expectingBackendExit = true
        proc.terminate()  // SIGTERM
        let deadline = Date().addingTimeInterval(15)
        while proc.isRunning && Date() < deadline {
            usleep(100_000)
        }
        if proc.isRunning {
            kill(proc.processIdentifier, SIGKILL)
        }
        logFileHandle?.closeFile()
        logFileHandle = nil
    }

    private func backendDied(status: Int32) {
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = "Whisper Studio backend quit unexpectedly"
        alert.informativeText =
            "The backend process exited with status \(status).\n\nLog file:\n\(logPath)"
        alert.addButton(withTitle: "Quit")
        alert.addButton(withTitle: "Show Logs")
        let response = alert.runModal()
        if response == .alertSecondButtonReturn {
            revealLogs()
        }
        expectingBackendExit = true
        NSApp.terminate(nil)
    }

    // MARK: Health polling

    private func pollHealth() {
        guard let baseURL = baseURL else { return }
        var request = URLRequest(url: baseURL.appendingPathComponent("health"))
        request.timeoutInterval = 2
        let task = URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            DispatchQueue.main.async {
                guard let self = self else { return }
                if let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) {
                    self.showMainWindow()
                } else if Date() >= self.healthDeadline {
                    self.startupTimedOut()
                } else {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                        self.pollHealth()
                    }
                }
            }
        }
        task.resume()
    }

    private func startupTimedOut() {
        splashWindow?.close()
        splashWindow = nil
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = "Whisper Studio did not start"
        alert.informativeText =
            "The backend did not answer on http://127.0.0.1:\(port)/health within 120 seconds."
            + "\n\nLog file:\n\(logPath)"
        alert.addButton(withTitle: "Quit")
        alert.addButton(withTitle: "Show Logs")
        let response = alert.runModal()
        if response == .alertSecondButtonReturn {
            revealLogs()
        }
        NSApp.terminate(nil)
    }

    // MARK: Windows

    private func showSplash() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 380, height: 110),
            styleMask: [.titled],
            backing: .buffered,
            defer: false)
        window.isReleasedWhenClosed = false
        window.title = "Whisper Studio"

        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .regular
        spinner.translatesAutoresizingMaskIntoConstraints = false
        spinner.widthAnchor.constraint(equalToConstant: 28).isActive = true
        spinner.heightAnchor.constraint(equalToConstant: 28).isActive = true
        spinner.startAnimation(nil)

        let label = NSTextField(labelWithString: "Starting Whisper Studio…")
        label.font = NSFont.systemFont(ofSize: 14)

        let stack = NSStackView(views: [spinner, label])
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = 12
        stack.edgeInsets = NSEdgeInsets(top: 30, left: 30, bottom: 30, right: 30)
        window.contentView = stack

        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        splashWindow = window
    }

    private func showMainWindow() {
        guard mainWindow == nil, let baseURL = baseURL else { return }

        let config = WKWebViewConfiguration()
        config.mediaTypesRequiringUserActionForPlayback = []

        let view = WKWebView(
            frame: NSRect(x: 0, y: 0, width: 1440, height: 900), configuration: config)
        view.uiDelegate = self
        view.navigationDelegate = self
        view.allowsBackForwardNavigationGestures = true

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false)
        window.isReleasedWhenClosed = false
        window.title = "Whisper Studio"
        window.contentMinSize = NSSize(width: 1000, height: 700)
        window.contentView = view
        window.center()
        window.setFrameAutosaveName("WhisperStudioMainWindow")

        view.load(URLRequest(url: baseURL))

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        mainWindow = window
        webView = view

        splashWindow?.close()
        splashWindow = nil
    }

    // MARK: Menu

    private func buildMainMenu() {
        let mainMenu = NSMenu()

        // Application menu
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(
            withTitle: "About Whisper Studio",
            action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
            keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(
            withTitle: "Hide Whisper Studio",
            action: #selector(NSApplication.hide(_:)),
            keyEquivalent: "h")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(
            withTitle: "Quit Whisper Studio",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q")
        appItem.submenu = appMenu
        mainMenu.addItem(appItem)

        // Edit menu — required so the web view keeps standard keyboard shortcuts.
        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(
            withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(
            withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        mainMenu.addItem(editItem)

        // View menu
        let viewItem = NSMenuItem()
        let viewMenu = NSMenu(title: "View")
        viewMenu.addItem(
            withTitle: "Reload", action: #selector(AppDelegate.reloadPage(_:)), keyEquivalent: "r")
        viewItem.submenu = viewMenu
        mainMenu.addItem(viewItem)

        // Whisper Studio menu
        let studioItem = NSMenuItem()
        let studioMenu = NSMenu(title: "Whisper Studio")
        studioMenu.addItem(
            withTitle: "Open in Browser",
            action: #selector(AppDelegate.openInBrowser(_:)),
            keyEquivalent: "")
        studioMenu.addItem(
            withTitle: "Show Logs",
            action: #selector(AppDelegate.showLogs(_:)),
            keyEquivalent: "")
        studioItem.submenu = studioMenu
        mainMenu.addItem(studioItem)

        NSApp.mainMenu = mainMenu
    }

    @objc func reloadPage(_ sender: Any?) {
        webView?.reload()
    }

    @objc func openInBrowser(_ sender: Any?) {
        guard let url = webView?.url ?? baseURL else { return }
        NSWorkspace.shared.open(url)
    }

    @objc func showLogs(_ sender: Any?) {
        revealLogs()
    }

    private func revealLogs() {
        let logURL = URL(fileURLWithPath: logPath)
        if FileManager.default.fileExists(atPath: logPath) {
            NSWorkspace.shared.activateFileViewerSelecting([logURL])
        } else {
            NSWorkspace.shared.open(URL(fileURLWithPath: logsDir))
        }
    }

    // MARK: WKUIDelegate

    func webView(
        _ webView: WKWebView,
        requestMediaCapturePermissionFor origin: WKSecurityOrigin,
        initiatedByFrame frame: WKFrameInfo,
        type: WKMediaCaptureType,
        decisionHandler: @escaping (WKPermissionDecision) -> Void
    ) {
        // Grant at the WebKit layer; the OS-level TCC microphone prompt still applies.
        decisionHandler(.grant)
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        // target=_blank and window.open: hand the URL to the default browser.
        if let url = navigationAction.request.url {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping () -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = "Whisper Studio"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.runModal()
        completionHandler()
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (Bool) -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = "Whisper Studio"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptTextInputPanelWithPrompt prompt: String,
        defaultText: String?,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (String?) -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = "Whisper Studio"
        alert.informativeText = prompt
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        field.stringValue = defaultText ?? ""
        alert.accessoryView = field
        if alert.runModal() == .alertFirstButtonReturn {
            completionHandler(field.stringValue)
        } else {
            completionHandler(nil)
        }
    }

    // MARK: WKNavigationDelegate

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }
        let scheme = url.scheme?.lowercased() ?? ""
        if scheme == "http" || scheme == "https" {
            let host = url.host?.lowercased() ?? ""
            let localHosts: Set<String> = ["127.0.0.1", "localhost", "::1", "[::1]"]
            if !localHosts.contains(host) {
                NSWorkspace.shared.open(url)
                decisionHandler(.cancel)
                return
            }
        } else if scheme == "mailto" || scheme == "tel" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    // MARK: Fatal startup errors

    private func fatalStartupAlert(_ message: String) {
        splashWindow?.close()
        splashWindow = nil
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = "Whisper Studio failed to start"
        alert.informativeText = message
        alert.addButton(withTitle: "Quit")
        alert.runModal()
        NSApp.terminate(nil)
    }
}

// MARK: - Entry point

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
