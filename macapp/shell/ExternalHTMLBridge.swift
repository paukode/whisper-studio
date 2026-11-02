// Whisper Studio: bridges the artifact "Open" action to the user's actual
// default browser.
//
// window.open()/target="_blank" only escapes a WKWebView to the OS default
// browser for real http(s)/mailto/tel URLs (see main.swift's
// createWebViewWith / decidePolicyFor). src/utils/openHtmlSandboxed.ts opens
// model-authored HTML via `window.open('about:blank')` plus a sandboxed
// iframe so the artifact never runs same-origin with the app, but that
// about:blank popup has no navigable URL, so inside the native shell there is
// nothing for createWebViewWith to hand off, and the click is a silent no-op.
//
// This bridge gives the page an explicit escape hatch instead: hand over the
// HTML, and the shell writes it to a throwaway file and opens it with
// NSWorkspace, exactly like double-clicking the file in Finder, which uses
// the user's real default browser.
//
// Bridge contract:
//   page → shell: window.webkit.messageHandlers.openExternalHtml.postMessage(html)
//   The page detects the bridge by that message handler.

import AppKit
import Foundation
import WebKit

final class ExternalHTMLBridge: NSObject, WKScriptMessageHandler {

    static let messageHandlerName = "openExternalHtml"

    /// Register the message handler. Call before creating the WKWebView.
    func install(into configuration: WKWebViewConfiguration) {
        configuration.userContentController.add(self, name: Self.messageHandlerName)
    }

    func userContentController(
        _ userContentController: WKUserContentController, didReceive message: WKScriptMessage
    ) {
        guard message.name == Self.messageHandlerName, message.frameInfo.isMainFrame,
            let html = message.body as? String
        else { return }
        if let failure = writeAndOpen(html) {
            FileHandle.standardError.write(Data("[openExternalHtml] \(failure)\n".utf8))
            showError(failure, in: message.webView)
        }
    }

    /// Write the HTML to a throwaway file and open it. Returns the failure
    /// text, or nil when the browser was asked to open it.
    private func writeAndOpen(_ html: String) -> String? {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("WhisperStudioArtifacts", isDirectory: true)
        let file = dir.appendingPathComponent("artifact-\(UUID().uuidString).html")
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            try html.write(to: file, atomically: true, encoding: .utf8)
        } catch {
            return "Could not write the page to open: \(error.localizedDescription)"
        }
        guard NSWorkspace.shared.open(file) else {
            return "macOS could not open the page in the default browser."
        }
        return nil
    }

    /// Show the failure as an error toast through the page's
    /// window.__whisperShellToast hook (src/services/shellToastBridge.ts).
    private func showError(_ text: String, in webView: WKWebView?) {
        let js =
            "window.__whisperShellToast(\(jsStringLiteral(text)), { type: \(jsStringLiteral("error")) });"
        webView?.evaluateJavaScript(js) { _, error in
            if let error {
                FileHandle.standardError.write(
                    Data("[openExternalHtml] toast failed: \(error.localizedDescription)\n".utf8))
            }
        }
    }
}
