// Smoke CLI: print the native audio source list as JSON and exit, and (with
// --assert) verify the app-grouping / self-exclusion logic directly.
//
// Compiled ONLY with -D SMOKE_CLI, together with NativeAudioCapture.swift:
//
//   swiftc -D SMOKE_CLI -target arm64-apple-macos14 \
//       macapp/shell/NativeAudioCapture.swift macapp/shell/smoke_list_sources.swift \
//       -o /tmp/smoke_list_sources \
//       -framework AppKit -framework WebKit -framework CoreAudio \
//       -framework AudioToolbox -framework AVFoundation
//
//   /tmp/smoke_list_sources            # JSON source list (grouped app rows)
//   /tmp/smoke_list_sources --assert   # run grouping/exclusion assertions
//
// The normal shell build (build_shell.sh) compiles this file too, but without
// SMOKE_CLI the whole file is empty, so no duplicate entry point exists.
//
// Enumerating audio processes does NOT trigger the TCC prompt — only starting
// a tap does — so this is safe to run headlessly. An empty app list (just the
// System audio entry) is normal when nothing is playing audio.

#if SMOKE_CLI

import Foundation

@main
struct SmokeListSources {
    static func main() {
        guard #available(macOS 14.4, *) else {
            FileHandle.standardError.write(Data("requires macOS 14.4\n".utf8))
            print("[]")
            return
        }
        if CommandLine.arguments.contains("--assert") {
            runAssertions()
            return
        }
        let sources = NativeAudioEnumeration.listSources()
        guard
            let data = try? JSONSerialization.data(
                withJSONObject: sources, options: [.prettyPrinted, .sortedKeys]),
            let json = String(data: data, encoding: .utf8)
        else {
            FileHandle.standardError.write(Data("could not serialize source list\n".utf8))
            print("[]")
            return
        }
        print(json)
    }

    // MARK: Assertions (pure logic + live pid-tree exclusion)

    @available(macOS 14.4, *)
    static func runAssertions() {
        var failures = 0
        func check(_ ok: Bool, _ label: String) {
            print("\(ok ? "PASS" : "FAIL")  \(label)")
            if !ok { failures += 1 }
        }

        // 1. Helper bundles collapse into the OUTERMOST .app ancestor.
        let chromeHelper =
            "/Applications/Google Chrome.app/Contents/Frameworks/"
            + "Google Chrome Framework.framework/Versions/139.0.7258.67/Helpers/"
            + "Google Chrome Helper (GPU).app/Contents/MacOS/Google Chrome Helper (GPU)"
        check(
            NativeAudioEnumeration.outermostAppBundlePath(forExecutablePath: chromeHelper)
                == "/Applications/Google Chrome.app",
            "Chrome GPU helper resolves to /Applications/Google Chrome.app")

        let plainApp = "/Applications/zoom.us.app/Contents/MacOS/zoom.us"
        check(
            NativeAudioEnumeration.outermostAppBundlePath(forExecutablePath: plainApp)
                == "/Applications/zoom.us.app",
            "plain app binary resolves to its own bundle")

        // 2. Framework XPC services (WebKit helpers) have NO .app ancestor —
        //    they group via the responsible pid instead.
        let webkitGPU =
            "/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/"
            + "com.apple.WebKit.GPU.xpc/Contents/MacOS/com.apple.WebKit.GPU"
        check(
            NativeAudioEnumeration.outermostAppBundlePath(forExecutablePath: webkitGPU) == nil,
            "WebKit XPC service has no .app ancestor (grouped by responsible pid)")

        // 3. Non-app binaries keep no bundle (the row keeps the process name).
        check(
            NativeAudioEnumeration.outermostAppBundlePath(forExecutablePath: "/usr/bin/afplay")
                == nil,
            "afplay has no .app ancestor")

        // 3b. Chrome's translocated main binary ("code_sign_clone") has no
        //     .app ancestor either — the shared bundle id is what merges it
        //     with the helper rows into one "Google Chrome" entry.
        let chromeClone =
            "/private/var/folders/xx/T/com.google.Chrome.code_sign_clone/"
            + "code_sign_clone.abc/Google Chrome.app.bundle/Contents/MacOS/Google Chrome"
        check(
            NativeAudioEnumeration.outermostAppBundlePath(forExecutablePath: chromeClone) == nil,
            "code-sign-clone path is grouped by bundle id, not path")

        // 4. Own-bundle exclusion: any executable inside our bundle is ours.
        let ownBundle = "/Applications/Whisper Studio.app"
        check(
            NativeAudioEnumeration.belongsToBundle(
                execPath: ownBundle + "/Contents/MacOS/WhisperStudio", bundlePath: ownBundle),
            "shell binary belongs to own bundle")
        check(
            NativeAudioEnumeration.belongsToBundle(
                execPath: ownBundle + "/Contents/Resources/python/bin/python3",
                bundlePath: ownBundle),
            "bundled backend python belongs to own bundle")
        check(
            !NativeAudioEnumeration.belongsToBundle(
                execPath: "/Applications/Whisper Studio Helper.app/Contents/MacOS/x",
                bundlePath: ownBundle),
            "sibling bundle with a shared prefix is NOT ours")

        // 5. Responsible-pid exclusion: we are responsible for ourselves, so
        //    isOwnProcess(ownPid) is true by both the pid and rpid rules.
        let ownPid = pid_t(ProcessInfo.processInfo.processIdentifier)
        check(responsiblePid(for: ownPid) > 0, "responsibility API resolves (or degrades to pid)")
        check(
            NativeAudioEnumeration.isOwnProcess(pid: ownPid, ownPid: ownPid),
            "own pid is excluded")

        // 6. Pid-tree exclusion: a live child process of ours is ours too
        //    (this is how the spawned backend tree is kept out of the list).
        let child = Process()
        child.executableURL = URL(fileURLWithPath: "/bin/sleep")
        child.arguments = ["5"]
        do {
            try child.run()
            let childPid = pid_t(child.processIdentifier)
            check(
                NativeAudioEnumeration.isOwnProcess(pid: childPid, ownPid: ownPid),
                "live child process (pid \(childPid)) is excluded via the pid tree")
            child.terminate()
        } catch {
            check(false, "could not spawn child for pid-tree assertion: \(error)")
        }

        // 7. Live list: no row may contain any of OUR pids, and every row
        //    carries a non-empty pids array.
        let rows = NativeAudioEnumeration.listSources()
        let appRows = rows.filter { ($0["pid"] as? Int ?? -1) >= 0 }
        let containsSelf = appRows.contains { row in
            ((row["pids"] as? [Int]) ?? []).contains(Int(ownPid))
        }
        check(!containsSelf, "live source list never contains our own pid")
        let allHavePids = appRows.allSatisfy { !((($0["pids"] as? [Int]) ?? []).isEmpty) }
        check(allHavePids, "every app row carries a non-empty pids array")

        if failures > 0 {
            FileHandle.standardError.write(Data("\(failures) assertion(s) failed\n".utf8))
            exit(1)
        }
        print("all assertions passed")
    }
}

#endif
