// Whisper Studio: native microphone capture for the macOS shell.
//
// The recorder used to open the microphone through WebKit (getUserMedia).
// WebKit always opens it in "VideoChat" mode: Apple's voice processing unit,
// with echo cancellation and automatic gain, arbitrated with every other app
// that uses voice processing. Field logs showed what that costs at the start
// of a recording: a mic that took up to 4 s to open, about 3 s of audio
// 18-25 dB too quiet until the voice processor settled, and, during a Teams
// call, a route that flipped between the two apps. The first words of every
// recording were lost or misheard.
//
// This captures the default input device directly with AVAudioEngine and
// voice processing OFF: the device is opened as-is, many apps can read it at
// the same time, and the level is the device's own from the first buffer.
// Audio is converted to 16 kHz mono Int16 and pushed to the page as base64
// chunks of 1600-3200 samples, the same shape as the system audio tap
// (NativeAudioCapture.swift), so the page's recording pipeline treats both
// alike. The microphone opens only on {cmd:"start"} (the press of Record).
//
// Bridge contract (pinned; src/services/nativeMicSource.ts mirrors it):
//   * injected at documentStart, main frame only:
//       window.__WHISPER_NATIVE_MIC = { platform: "macos", available: true }
//   * page -> native: window.webkit.messageHandlers.nativeMic.postMessage
//       {cmd:"start"} | {cmd:"stop"}
//   * native -> page (evaluateJavaScript on the main thread):
//       window.__whisperNativeMic.onStarted(deviceName)
//       window.__whisperNativeMic.onAudio(base64, sampleCount, rms)
//       window.__whisperNativeMic.onError(message)
//       window.__whisperNativeMic.onStopped()

import AVFoundation
import Foundation
import WebKit

func nativeMicLog(_ message: String) {
    FileHandle.standardError.write(Data("[native-mic] \(message)\n".utf8))
}

// MARK: - Capture session (one start..stop)

final class NativeMicCaptureSession {

    /// Emit chunks of 1600-3200 samples (100-200 ms at 16 kHz).
    private static let minChunkSamples = 1600
    private static let maxChunkSamples = 3200

    private let engine = AVAudioEngine()
    private let queue = DispatchQueue(label: "whisper.native-mic.capture", qos: .userInitiated)
    private let outputFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!

    // queue-confined state
    private var converter: AVAudioConverter?
    private var converterInput: AVAudioFormat?
    private var pending: [Int16] = []
    private var running = false
    private var configObserver: NSObjectProtocol?

    /// (base64, sampleCount, rms), called on the capture queue.
    var onChunk: ((String, Int, Double) -> Void)?
    /// A failure after start (the session has already stopped itself).
    var onError: ((String) -> Void)?

    /// Open the microphone: `completion(deviceName, nil)` once the engine
    /// runs (the first buffer follows within one buffer period), or
    /// `completion(nil, message)`. Asks for permission when not yet decided.
    func start(_ completion: @escaping (_ device: String?, _ error: String?) -> Void) {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            queue.async { completion(self.deviceName(), self.openEngine()) }
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                self.queue.async {
                    guard granted else {
                        completion(nil, Self.deniedMessage)
                        return
                    }
                    completion(self.deviceName(), self.openEngine())
                }
            }
        default:
            completion(nil, Self.deniedMessage)
        }
    }

    private static let deniedMessage =
        "Microphone access is blocked. Allow Whisper Studio in System Settings > "
        + "Privacy & Security > Microphone, then start the recording again."

    private func deviceName() -> String {
        AVCaptureDevice.default(for: .audio)?.localizedName ?? "Default microphone"
    }

    /// Install the tap and start the engine. Returns an error message or nil.
    /// Runs on `queue`.
    private func openEngine() -> String? {
        let input = engine.inputNode
        // The point of this capture: the device as-is, no voice processing.
        if input.isVoiceProcessingEnabled {
            try? input.setVoiceProcessingEnabled(false)
        }
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            return "No microphone input is available. Connect or select an input device in System Settings > Sound."
        }
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 2048, format: format) { [weak self] buffer, _ in
            guard let self, let copy = Self.copy(buffer) else { return }
            self.queue.async { self.handle(copy) }
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            return "The microphone could not be started: \(error.localizedDescription)"
        }
        running = true
        if configObserver == nil {
            // The default input changed (headset plugged in, AirPods) or its
            // format did: the engine stops itself; reopen on the new device.
            configObserver = NotificationCenter.default.addObserver(
                forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
            ) { [weak self] _ in
                self?.queue.async { self?.reopen() }
            }
        }
        nativeMicLog("started: \(deviceName()), \(Int(format.sampleRate)) Hz, \(format.channelCount) ch")
        return nil
    }

    private func reopen() {
        guard running else { return }
        nativeMicLog("input device or format changed; reopening")
        engine.stop()
        converter = nil
        converterInput = nil
        if let message = openEngine() {
            teardown()
            onError?(message)
        }
    }

    /// The tap's buffer is only valid during the callback: copy it.
    private static func copy(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        guard
            let out = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: buffer.frameLength)
        else { return nil }
        out.frameLength = buffer.frameLength
        let src = UnsafeMutableAudioBufferListPointer(
            UnsafeMutablePointer(mutating: buffer.audioBufferList))
        let dst = UnsafeMutableAudioBufferListPointer(out.mutableAudioBufferList)
        for (s, d) in zip(src, dst) {
            guard let from = s.mData, let to = d.mData else { continue }
            memcpy(to, from, Int(min(s.mDataByteSize, d.mDataByteSize)))
        }
        return out
    }

    /// Convert one captured buffer to 16 kHz mono Int16 and emit full chunks.
    private func handle(_ buffer: AVAudioPCMBuffer) {
        guard running else { return }
        if converter == nil || converterInput != buffer.format {
            guard let made = AVAudioConverter(from: buffer.format, to: outputFormat) else {
                nativeMicLog("no converter for \(buffer.format)")
                return
            }
            made.downmix = true
            converter = made
            converterInput = buffer.format
        }
        guard let converter else { return }
        let ratio = outputFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
            return
        }
        var supplied = false
        var error: NSError?
        let status = converter.convert(to: out, error: &error) { _, inputStatus in
            if supplied {
                inputStatus.pointee = .noDataNow
                return nil
            }
            supplied = true
            inputStatus.pointee = .haveData
            return buffer
        }
        if status == .error {
            nativeMicLog("conversion failed: \(error?.localizedDescription ?? "unknown")")
            return
        }
        guard let samples = out.int16ChannelData?[0], out.frameLength > 0 else { return }
        pending.append(contentsOf: UnsafeBufferPointer(start: samples, count: Int(out.frameLength)))
        while pending.count >= Self.minChunkSamples {
            let take = min(pending.count, Self.maxChunkSamples)
            emit(Array(pending.prefix(take)))
            pending.removeFirst(take)
        }
    }

    private func emit(_ chunk: [Int16]) {
        var sum = 0.0
        for v in chunk {
            let f = Double(v) / 32768.0
            sum += f * f
        }
        let rms = chunk.isEmpty ? 0 : (sum / Double(chunk.count)).squareRoot()
        let data = chunk.withUnsafeBufferPointer { Data(buffer: $0) }
        onChunk?(data.base64EncodedString(), chunk.count, rms)
    }

    func stop(_ completion: (() -> Void)? = nil) {
        queue.async {
            // Flush the tail so the last words are not cut off.
            if self.running, !self.pending.isEmpty {
                self.emit(self.pending)
                self.pending.removeAll()
            }
            self.teardown()
            completion?()
        }
    }

    /// Runs on `queue`.
    private func teardown() {
        running = false
        if let configObserver {
            NotificationCenter.default.removeObserver(configObserver)
            self.configObserver = nil
        }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        converter = nil
        converterInput = nil
        pending.removeAll()
    }
}

// MARK: - WKWebView bridge

final class NativeMicBridge: NSObject, WKScriptMessageHandler {

    static let messageHandlerName = "nativeMic"

    weak var webView: WKWebView?
    private var session: NativeMicCaptureSession?

    func install(into configuration: WKWebViewConfiguration) {
        let marker = "window.__WHISPER_NATIVE_MIC = { platform: \"macos\", available: true };"
        configuration.userContentController.addUserScript(
            WKUserScript(source: marker, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        configuration.userContentController.add(self, name: Self.messageHandlerName)
    }

    /// Close the microphone (app quit / window teardown).
    func shutdown() {
        session?.stop()
        session = nil
    }

    func userContentController(
        _ userContentController: WKUserContentController, didReceive message: WKScriptMessage
    ) {
        guard message.name == Self.messageHandlerName, message.frameInfo.isMainFrame,
            let body = message.body as? [String: Any],
            let cmd = body["cmd"] as? String
        else { return }
        switch cmd {
        case "start":
            handleStart()
        case "stop":
            handleStop()
        default:
            emitError("Unknown native mic command: \(cmd)")
        }
    }

    private func handleStart() {
        // One microphone session at a time: a new start replaces the old.
        session?.stop()
        let newSession = NativeMicCaptureSession()
        newSession.onChunk = { [weak self] base64, count, rms in
            let level = String(format: "%.6f", rms)
            self?.emit(
                "window.__whisperNativeMic && window.__whisperNativeMic.onAudio(\"\(base64)\", \(count), \(level));"
            )
        }
        newSession.onError = { [weak self, weak newSession] message in
            DispatchQueue.main.async {
                guard let self else { return }
                if self.session === newSession { self.session = nil }
                self.emitError(message)
            }
        }
        session = newSession
        newSession.start { [weak self, weak newSession] device, error in
            DispatchQueue.main.async {
                guard let self else { return }
                if let error {
                    if self.session === newSession { self.session = nil }
                    self.emitError(error)
                    return
                }
                let name = Self.jsStringLiteral(device ?? "")
                self.emit(
                    "window.__whisperNativeMic && window.__whisperNativeMic.onStarted(\(name));")
            }
        }
    }

    private func handleStop() {
        guard let session else {
            emit("window.__whisperNativeMic && window.__whisperNativeMic.onStopped();")
            return
        }
        self.session = nil
        session.stop { [weak self] in
            self?.emit("window.__whisperNativeMic && window.__whisperNativeMic.onStopped();")
        }
    }

    private func emit(_ js: String) {
        DispatchQueue.main.async { [weak self] in
            self?.webView?.evaluateJavaScript(js, completionHandler: nil)
        }
    }

    private func emitError(_ message: String) {
        nativeMicLog("error -> page: \(message)")
        emit(
            "window.__whisperNativeMic && window.__whisperNativeMic.onError(\(Self.jsStringLiteral(message)));"
        )
    }

    /// JSON-encode a Swift string into a safe JS string literal.
    private static func jsStringLiteral(_ s: String) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: [s]),
            let json = String(data: data, encoding: .utf8),
            json.count >= 2
        else { return "\"\"" }
        return String(json.dropFirst().dropLast())
            .replacingOccurrences(of: "\u{2028}", with: "\\u2028")
            .replacingOccurrences(of: "\u{2029}", with: "\\u2029")
    }
}
