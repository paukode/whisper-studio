// Native translation bridge — Apple's on-device Translation framework,
// exposed to the page over the same WKWebView bridge pattern as
// NativeAudioCapture (contract pinned in src/services/nativeTranslation.ts):
//
//   * page marker (documentStart, main frame only):
//       window.__WHISPER_NATIVE_TRANSLATE = { available: bool }
//   * page → native: window.webkit.messageHandlers.nativeTranslate.postMessage
//       {id, text, source, target}
//   * native → page: window.__whisperNativeTranslate.onResult(id, text|null, error|null)
//
// TranslationSession can only be obtained through SwiftUI's .translationTask
// modifier (no programmatic init as of macOS 26), so the bridge parks an
// invisible zero-sized NSHostingView on the web view and drives it with a
// published TranslationSession.Configuration: each request (re)arms the
// configuration, SwiftUI re-runs the task closure with a live session, and
// the closure drains the queued requests for that language pair. The OS
// handles language-pack downloads (one-time consent sheet on first use);
// everything after is offline.

import AppKit
import Foundation
import SwiftUI
import WebKit

#if canImport(Translation)
    import Translation
#endif

func nativeTranslateLog(_ message: String) {
    FileHandle.standardError.write(Data("[native-translate] \(message)\n".utf8))
}

@available(macOS 15.0, *)
@MainActor
final class TranslationCoordinator: ObservableObject {
    struct Request {
        let id: String
        let text: String
        let source: String
        let target: String
        var pair: String { "\(source)>\(target)" }
    }

    @Published var configuration: TranslationSession.Configuration?
    /// Called with (id, translated text or nil, error or nil).
    var reply: ((String, String?, String?) -> Void)?

    private var queue: [Request] = []
    private var currentPair: String?

    func submit(_ request: Request) {
        queue.append(request)
        arm(for: request.pair)
    }

    /// Point the configuration at `pair`, re-triggering the translationTask.
    /// A same-pair re-arm uses invalidate() (new session, same languages);
    /// a different pair replaces the configuration wholesale.
    private func arm(for pair: String) {
        guard let request = queue.first(where: { $0.pair == pair }) else { return }
        if configuration != nil, currentPair == pair {
            configuration?.invalidate()
            return
        }
        currentPair = pair
        configuration = TranslationSession.Configuration(
            source: Locale.Language(identifier: request.source),
            target: Locale.Language(identifier: request.target)
        )
    }

    /// Runs inside the translationTask closure with a live session for
    /// `currentPair`. Requests for another pair are left queued and re-armed
    /// afterwards (rare — a recording keeps one language pair).
    func drain(session: TranslationSession) async {
        guard let pair = currentPair else { return }
        while let index = queue.firstIndex(where: { $0.pair == pair }) {
            let request = queue.remove(at: index)
            do {
                let response = try await session.translate(request.text)
                reply?(request.id, response.targetText, nil)
            } catch {
                nativeTranslateLog("translate failed: \(error)")
                reply?(request.id, nil, String(describing: error))
            }
        }
        if let next = queue.first {
            arm(for: next.pair)
        }
    }
}

@available(macOS 15.0, *)
private struct TranslationHostView: View {
    @ObservedObject var coordinator: TranslationCoordinator

    var body: some View {
        Color.clear
            .frame(width: 0, height: 0)
            .translationTask(coordinator.configuration) { session in
                await coordinator.drain(session: session)
            }
    }
}

final class NativeTranslationBridge: NSObject, WKScriptMessageHandler {
    static let messageHandlerName = "nativeTranslate"

    weak var webView: WKWebView?

    /// TranslationCoordinator, stored untyped so this class compiles for the
    /// macOS 14 deployment target (the framework is 15+).
    private var coordinatorStorage: Any?
    private var hostView: NSView?

    static var isSupported: Bool {
        if #available(macOS 15.0, *) { return true }
        return false
    }

    /// Register the documentStart marker and the message handler. Call before
    /// creating the WKWebView; assign `webView` right after (mirrors
    /// NativeAudioBridge.install).
    func install(into configuration: WKWebViewConfiguration) {
        let marker =
            "window.__WHISPER_NATIVE_TRANSLATE = { available: \(Self.isSupported) };"
        let script = WKUserScript(
            source: marker, injectionTime: .atDocumentStart, forMainFrameOnly: true)
        configuration.userContentController.addUserScript(script)
        configuration.userContentController.add(self, name: Self.messageHandlerName)
    }

    func userContentController(
        _ userContentController: WKUserContentController, didReceive message: WKScriptMessage
    ) {
        guard message.name == Self.messageHandlerName, message.frameInfo.isMainFrame,
            let body = message.body as? [String: Any],
            let id = body["id"] as? String,
            let text = body["text"] as? String
        else { return }

        guard #available(macOS 15.0, *) else {
            emitResult(id: id, text: nil, error: "Translation requires macOS 15 or later.")
            return
        }

        let source = (body["source"] as? String) ?? "en"
        let target = (body["target"] as? String) ?? "en"
        DispatchQueue.main.async { [self] in
            coordinator().submit(
                TranslationCoordinator.Request(id: id, text: text, source: source, target: target))
        }
    }

    @available(macOS 15.0, *)
    @MainActor
    private func coordinator() -> TranslationCoordinator {
        if let existing = coordinatorStorage as? TranslationCoordinator { return existing }
        let coordinator = TranslationCoordinator()
        coordinator.reply = { [weak self] id, text, error in
            self?.emitResult(id: id, text: text, error: error)
        }
        coordinatorStorage = coordinator
        // Park the invisible SwiftUI host on the web view so translationTask
        // has a live view hierarchy to run in.
        if hostView == nil, let webView {
            let host = NSHostingView(rootView: TranslationHostView(coordinator: coordinator))
            host.frame = .zero
            host.isHidden = true
            webView.addSubview(host)
            hostView = host
        }
        return coordinator
    }

    private func emitResult(id: String, text: String?, error: String?) {
        func js(_ value: String?) -> String {
            guard let value else { return "null" }
            let data = (try? JSONSerialization.data(
                withJSONObject: value, options: .fragmentsAllowed)) ?? Data("\"\"".utf8)
            return String(data: data, encoding: .utf8) ?? "\"\""
        }
        let call =
            "window.__whisperNativeTranslate && "
            + "window.__whisperNativeTranslate.onResult(\(js(id)), \(js(text)), \(js(error)));"
        DispatchQueue.main.async { [weak self] in
            self?.webView?.evaluateJavaScript(call, completionHandler: nil)
        }
    }
}
