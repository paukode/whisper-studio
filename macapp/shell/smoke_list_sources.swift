// Smoke CLI: print the native audio source list as JSON and exit.
//
// Compiled ONLY with -D SMOKE_CLI, together with NativeAudioCapture.swift:
//
//   swiftc -D SMOKE_CLI -target arm64-apple-macos14 \
//       macapp/shell/NativeAudioCapture.swift macapp/shell/smoke_list_sources.swift \
//       -o /tmp/smoke_list_sources \
//       -framework AppKit -framework WebKit -framework CoreAudio \
//       -framework AudioToolbox -framework AVFoundation
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
}

#endif
