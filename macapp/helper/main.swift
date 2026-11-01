// Whisper Studio capture helper: the Mac app's native mic and system/app
// audio capture for a source install, which runs in a browser and has no
// Swift shell. The server starts this program per page connection and relays
// its stdin/stdout to the page (server/capture_helper.py), which then talks
// to it with the same commands and callbacks it uses inside the app window.
//
// The capture code is the app's own (macapp/shell/NativeAudioCapture.swift
// and NativeMicCapture.swift, compiled in unchanged); only the transport
// differs: one JSON object per line instead of a WKWebView bridge.
//
//   * stdin:  {"channel":"audio","cmd":"list"|"start"|"stop","pid":-1|N,"bundleID":...}
//             {"channel":"mic","cmd":"start"|"stop"}
//   * stdout: {"channel":"audio"|"mic","event":"<callback name>","args":[...]}
//     where the callback is a method of window.__whisperNativeAudio or
//     window.__whisperNativeMic (onSources, onStarted, onAudio, onWarning,
//     onError, onStopped).
//
// macOS asks for the microphone and System Audio Recording permissions on
// the first start, for the terminal app that started setup.sh. The helper
// exits when stdin closes (the page disconnected or the server stopped).

import Foundation

let outLock = NSLock()

func send(_ channel: String, _ event: String, _ args: [Any]) {
    guard
        let data = try? JSONSerialization.data(
            withJSONObject: ["channel": channel, "event": event, "args": args])
    else { return }
    outLock.lock()
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
    outLock.unlock()
}

final class Helper {
    private var audio: NativeAudioCaptureSession?
    private var mic: NativeMicCaptureSession?

    func handle(_ line: String) {
        guard let data = line.data(using: .utf8),
            let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
            let channel = body["channel"] as? String,
            let cmd = body["cmd"] as? String
        else {
            send("audio", "onError", ["The capture helper got a message it cannot read."])
            return
        }
        switch (channel, cmd) {
        case ("audio", "list"):
            DispatchQueue.global(qos: .userInitiated).async {
                let json = NativeAudioEnumeration.sourcesJSON()
                let sources = (try? JSONSerialization.jsonObject(with: Data(json.utf8))) ?? []
                send("audio", "onSources", [sources])
            }
        case ("audio", "start"):
            let pid = pid_t((body["pid"] as? NSNumber)?.int32Value ?? -1)
            startAudio(pid: pid, bundleID: body["bundleID"] as? String)
        case ("audio", "stop"):
            guard let session = audio else { return send("audio", "onStopped", []) }
            audio = nil
            session.stop { send("audio", "onStopped", []) }
        case ("mic", "start"):
            startMic()
        case ("mic", "stop"):
            guard let session = mic else { return send("mic", "onStopped", []) }
            mic = nil
            session.stop { send("mic", "onStopped", []) }
        default:
            send(channel == "mic" ? "mic" : "audio", "onError", ["Unknown capture command: \(channel) \(cmd)"])
        }
    }

    private func startAudio(pid: pid_t, bundleID: String?) {
        audio?.stop()
        let session = NativeAudioCaptureSession(pid: pid, bundleID: bundleID)
        session.onChunk = { base64, count, rms in send("audio", "onAudio", [base64, count, rms]) }
        session.onError = { send("audio", "onError", [$0]) }
        session.onWarning = { send("audio", "onWarning", [$0]) }
        audio = session
        session.start { [weak self, weak session] error in
            DispatchQueue.main.async {
                if let error {
                    if self?.audio === session { self?.audio = nil }
                    send("audio", "onError", [error])
                } else {
                    send("audio", "onStarted", [])
                }
            }
        }
    }

    private func startMic() {
        mic?.stop()
        let session = NativeMicCaptureSession()
        session.onChunk = { base64, count, rms in send("mic", "onAudio", [base64, count, rms]) }
        session.onError = { [weak self, weak session] message in
            DispatchQueue.main.async {
                if self?.mic === session { self?.mic = nil }
                send("mic", "onError", [message])
            }
        }
        mic = session
        session.start { [weak self, weak session] device, error in
            DispatchQueue.main.async {
                if let error {
                    if self?.mic === session { self?.mic = nil }
                    send("mic", "onError", [error])
                } else {
                    send("mic", "onStarted", [device ?? ""])
                }
            }
        }
    }

    func shutdown() {
        audio?.stop()
        mic?.stop()
    }
}

let helper = Helper()
DispatchQueue.global(qos: .userInitiated).async {
    while let line = readLine() {
        DispatchQueue.main.async { helper.handle(line) }
    }
    DispatchQueue.main.async {
        helper.shutdown()
        exit(0)
    }
}
RunLoop.main.run()
