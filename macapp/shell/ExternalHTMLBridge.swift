// Whisper Studio — bridges the artifact "Open" action to the user's actual
// default browser.
//
// window.open()/target="_blank" only escapes a WKWebView to the OS default
// browser for real http(s)/mailto/tel URLs (see main.swift's
// createWebViewWith / decidePolicyFor). src/utils/openHtmlSandboxed.ts opens
// model-authored HTML via `window.open('about:blank')` plus a sandboxed
// iframe so the artifact never runs same-origin with the app — but that
// about:blank popup has no navigable URL, so inside the native shell there is
// nothing for createWebViewWith to hand off, and the click is a silent no-op.
//
// This bridge gives the page an explicit escape hatch instead: hand over the
// HTML, and the shell writes it to a throwaway file and opens it with
// NSWorkspace — exactly like double-clicking the file in Finder, which uses
// the user's real default browser.
//
// Bridge contract:
//   page → shell: window.webkit.messageHandlers.openExternalHtml.postMessage(html)
//   shell → page marker (documentStart, main frame only):
//       window.__WHISPER_NATIVE_SHELL = true

import Foundation
import WebKit

final class ExternalHTMLBridge: NSObject, WKScriptMessageHandler {

    static let messageHandlerName = "openExternalHtml"

    /// Register the availability marker and the message handler. Call before
    /// creating the WKWebView.
    func install(into configuration: WKWebViewConfiguration) {
        let marker = "window.__WHISPER_NATIVE_SHELL = true;"
        let script = WKUserScript(
            source: marker, injectionTime: .atDocumentStart, forMainFrameOnly: true)
        configuration.userContentController.addUserScript(script)
        configuration.userContentController.add(self, name: Self.messageHandlerName)
    }

    func userContentController(
        _ userContentController: WKUserContentController, didReceive message: WKScriptMessage
    ) {
        guard message.name == Self.messageHandlerName, message.frameInfo.isMainFrame,
            let html = message.body as? String
        else { return }
        writeAndOpen(html)
    }

    private func writeAndOpen(_ html: String) {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("WhisperStudioArtifacts", isDirectory: true)
        let file = dir.appendingPathComponent("artifact-\(UUID().uuidString).html")
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            try html.write(to: file, atomically: true, encoding: .utf8)
            NSWorkspace.shared.open(file)
        } catch {
            FileHandle.standardError.write(
                Data("[openExternalHtml] failed: \(error.localizedDescription)\n".utf8))
        }
    }
}
