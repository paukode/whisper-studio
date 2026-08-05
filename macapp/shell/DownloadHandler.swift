// Whisper Studio — WKDownload handling for the native shell.
//
// WKWebView implements NO default download behavior: an anchor with a
// `download` attribute or a non-renderable response just *navigates*, which
// used to replace the SPA with raw exported markdown. main.swift routes such
// navigations here via the `.download` navigation policy; this delegate
// saves the payload into ~/Downloads (never overwriting — name collisions
// get " (2)", " (3)", … suffixes) and reports the outcome back to the page
// through the `window.__whisperShellToast` hook registered by the frontend
// (src/services/shellToastBridge.ts). If the hook is missing (older cached
// page), the report degrades to a stderr log — never an alert, never a crash.
//
// The first file written into ~/Downloads triggers the standard macOS
// "wants to access your Downloads folder" consent prompt; for a
// non-sandboxed app that needs no Info.plist key (see macapp/README.md).
//
// Smoke CLI: the destination-dedupe logic can be unit-run standalone:
//
//   swiftc -D DOWNLOAD_SMOKE_CLI -parse-as-library \
//       -target arm64-apple-macos14 \
//       macapp/shell/DownloadHandler.swift -o /tmp/smoke_download \
//       -framework WebKit
//   /tmp/smoke_download            # runs assertions against a temp dir
//
// (A flag distinct from SMOKE_CLI so the two @main smoke entry points can
// never collide in one compile.) The normal shell build (build_shell.sh)
// compiles this file without the flag, so the smoke body contributes
// nothing to the app binary.

import Foundation
import WebKit

// MARK: - Pure helpers (exercised by the smoke CLI)

/// Make a suggested filename safe as a single path component. Path
/// separators and control characters are replaced; an empty result falls
/// back to "download".
func sanitizedFilename(_ name: String) -> String {
    var out = ""
    for scalar in name.unicodeScalars {
        switch scalar {
        case "/", ":":
            out.append("-")
        default:
            if scalar.value < 0x20 || scalar.value == 0x7F {
                out.append("-")
            } else {
                out.unicodeScalars.append(scalar)
            }
        }
    }
    let trimmed = out.trimmingCharacters(in: .whitespaces)
    return trimmed.isEmpty ? "download" : trimmed
}

/// Pick a destination inside `directory` that does not collide with an
/// existing file: "report.md" → "report (2).md" → "report (3).md" (the
/// extension is preserved; extensionless names get the plain " (n)" suffix).
/// `fileExists` is injectable so the logic can be tested against any state.
func dedupedDestination(
    directory: URL,
    suggestedFilename: String,
    fileExists: (URL) -> Bool = { FileManager.default.fileExists(atPath: $0.path) }
) -> URL {
    let name = sanitizedFilename(suggestedFilename)
    let base = (name as NSString).deletingPathExtension
    let ext = (name as NSString).pathExtension

    var candidate = directory.appendingPathComponent(name)
    var counter = 2
    while fileExists(candidate) {
        let next = ext.isEmpty ? "\(base) (\(counter))" : "\(base) (\(counter)).\(ext)"
        candidate = directory.appendingPathComponent(next)
        counter += 1
    }
    return candidate
}

/// Encode a Swift string as a double-quoted JavaScript string literal,
/// escaping quotes, backslashes, control characters, and the JS line
/// terminators U+2028/U+2029 — safe to splice into evaluateJavaScript.
func jsStringLiteral(_ s: String) -> String {
    var out = "\""
    for scalar in s.unicodeScalars {
        switch scalar {
        case "\"": out += "\\\""
        case "\\": out += "\\\\"
        case "\n": out += "\\n"
        case "\r": out += "\\r"
        case "\t": out += "\\t"
        case "\u{2028}": out += "\\u2028"
        case "\u{2029}": out += "\\u2029"
        default:
            if scalar.value < 0x20 {
                out += String(format: "\\u%04x", scalar.value)
            } else {
                out.unicodeScalars.append(scalar)
            }
        }
    }
    out += "\""
    return out
}

// MARK: - Download delegate

#if !DOWNLOAD_SMOKE_CLI

final class DownloadHandler: NSObject, WKDownloadDelegate {

    /// Set by the app delegate once the main web view exists; used to surface
    /// completion toasts inside the page.
    weak var webView: WKWebView?

    /// Destination chosen per in-flight download, so the finish/fail
    /// callbacks can name the file. Also doubles as a reservation set: two
    /// rapid downloads of the same suggested name must not race into the
    /// same destination before the first file hits disk. WKDownload delegate
    /// callbacks arrive on the main thread, so plain dictionary access is safe.
    private var inFlight: [ObjectIdentifier: URL] = [:]

    private var downloadsDirectory: URL {
        FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory() + "/Downloads")
    }

    /// Called from the navigation delegate's `didBecome download` hooks.
    func attach(_ download: WKDownload) {
        download.delegate = self
    }

    // MARK: WKDownloadDelegate

    func download(
        _ download: WKDownload,
        decideDestinationUsing response: URLResponse,
        suggestedFilename: String,
        completionHandler: @escaping (URL?) -> Void
    ) {
        let reserved = Set(inFlight.values.map { $0.path })
        let destination = dedupedDestination(
            directory: downloadsDirectory,
            suggestedFilename: suggestedFilename,
            fileExists: { url in
                reserved.contains(url.path) || FileManager.default.fileExists(atPath: url.path)
            }
        )
        inFlight[ObjectIdentifier(download)] = destination
        completionHandler(destination)
    }

    func downloadDidFinish(_ download: WKDownload) {
        let destination = inFlight.removeValue(forKey: ObjectIdentifier(download))
        let name = destination?.lastPathComponent ?? "file"
        notifyPage(message: "Saved to Downloads: \(name)", type: "success")
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        if let destination = inFlight.removeValue(forKey: ObjectIdentifier(download)) {
            // Best effort: don't leave a partial file behind.
            try? FileManager.default.removeItem(at: destination)
        }
        notifyPage(
            message: "Download failed: \(error.localizedDescription)", type: "error")
    }

    // MARK: Page feedback

    /// Call the page's `window.__whisperShellToast(message, {type})` hook.
    /// Missing hook (older page) or JS failure degrades to a stderr log.
    private func notifyPage(message: String, type: String) {
        let log = { FileHandle.standardError.write(Data("[download] \(message)\n".utf8)) }
        DispatchQueue.main.async { [weak self] in
            guard let webView = self?.webView else {
                log()
                return
            }
            let js = """
                (function () {
                  if (typeof window.__whisperShellToast === 'function') {
                    window.__whisperShellToast(\(jsStringLiteral(message)), \
                { type: \(jsStringLiteral(type)) });
                    return true;
                  }
                  return false;
                })();
                """
            webView.evaluateJavaScript(js) { result, error in
                if error != nil || (result as? Bool) != true {
                    log()
                }
            }
        }
    }
}

#endif

// MARK: - Smoke CLI

#if DOWNLOAD_SMOKE_CLI

@main
struct SmokeDownloadDedupe {
    static func main() {
        var failures = 0
        func check(_ ok: Bool, _ label: String) {
            print("\(ok ? "PASS" : "FAIL")  \(label)")
            if !ok { failures += 1 }
        }

        // Real temp dir with pre-existing files, exercising the default
        // FileManager-backed fileExists closure.
        let dir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("whisper-download-smoke-\(ProcessInfo.processInfo.processIdentifier)")
        try! FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }

        func touch(_ name: String) {
            FileManager.default.createFile(
                atPath: dir.appendingPathComponent(name).path, contents: Data())
        }
        touch("report.md")
        touch("report (2).md")
        touch("data")
        touch("archive.tar.gz")

        func dest(_ suggested: String) -> String {
            dedupedDestination(directory: dir, suggestedFilename: suggested).lastPathComponent
        }

        check(dest("notes.txt") == "notes.txt", "fresh name is used as-is")
        check(dest("report.md") == "report (3).md", "collision skips existing ' (2)' to ' (3)'")
        check(dest("data") == "data (2)", "extensionless collision gets ' (2)'")
        check(dest("archive.tar.gz") == "archive.tar (2).gz", "multi-dot name suffixes before the last extension")
        check(dest("") == "download", "empty suggestion falls back to 'download'")
        check(dest("a/b:c.txt") == "a-b-c.txt", "path separators are sanitized out")

        // The dedupe walk actually terminates on a fresh candidate: create
        // the deduped file and re-run.
        touch("report (3).md")
        check(dest("report.md") == "report (4).md", "re-run after creating ' (3)' yields ' (4)'")

        // Injectable fileExists (what the in-flight reservation set uses).
        let everything = dedupedDestination(
            directory: dir, suggestedFilename: "x.txt",
            fileExists: { $0.lastPathComponent != "x (4).txt" })
        check(everything.lastPathComponent == "x (4).txt", "injected fileExists drives the walk")

        // JS string literal escaping — quotes, backslashes, newlines, and
        // JS line terminators must all be neutralized.
        check(
            jsStringLiteral("Saved to Downloads: a.md") == "\"Saved to Downloads: a.md\"",
            "plain string passes through quoted")
        check(
            jsStringLiteral("a\"b\\c\nd\u{2028}e") == "\"a\\\"b\\\\c\\nd\\u2028e\"",
            "quotes, backslashes, newlines, U+2028 are escaped")

        if failures > 0 {
            FileHandle.standardError.write(Data("\(failures) assertion(s) failed\n".utf8))
            exit(1)
        }
        print("all assertions passed")
    }
}

#endif
