// Whisper Studio — native system/app audio capture for the macOS shell.
//
// Captures the audio OUTPUT of the Mac (all of it, or one chosen process)
// with Core Audio process taps (macOS 14.4+): CATapDescription →
// AudioHardwareCreateProcessTap → private aggregate device →
// AudioDeviceCreateIOProcID reading the tap's input stream. Captured audio
// (typically 48 kHz stereo Float32) is converted to 16 kHz mono Int16 with
// AVAudioConverter and pushed to the SPA over a WKWebView bridge as base64
// chunks of 1600–3200 samples (100–200 ms).
//
// Bridge contract (pinned — the frontend mirrors it in
// src/services/nativeAudioSource.ts):
//   * injected at documentStart, main frame only:
//       window.__WHISPER_NATIVE_AUDIO = { platform: "macos", available: bool }
//   * page → native: window.webkit.messageHandlers.nativeAudio.postMessage
//       {cmd:"list"} | {cmd:"start", pid:-1|N} | {cmd:"stop"}
//   * native → page (all via evaluateJavaScript on the main thread):
//       window.__whisperNativeAudio.onSources(json)
//       window.__whisperNativeAudio.onStarted()
//       window.__whisperNativeAudio.onAudio(base64, sampleCount)
//       window.__whisperNativeAudio.onError(message)
//       window.__whisperNativeAudio.onStopped()
//
// The first start triggers the OS "System Audio Recording" TCC prompt
// (NSAudioCaptureUsageDescription in Info.plist); a denial surfaces as
// onError. Failures are logged to stderr, which the shell inherits.

import AppKit
import AVFoundation
import CoreAudio
import Foundation
import WebKit

// MARK: - Logging

func nativeAudioLog(_ message: String) {
    FileHandle.standardError.write(Data("[native-audio] \(message)\n".utf8))
}

/// Render an OSStatus as its four-char code (plus decimal) for error text.
private func fourCC(_ status: OSStatus) -> String {
    let n = UInt32(bitPattern: status)
    let bytes = [
        UInt8((n >> 24) & 0xFF), UInt8((n >> 16) & 0xFF),
        UInt8((n >> 8) & 0xFF), UInt8(n & 0xFF),
    ]
    if bytes.allSatisfy({ $0 >= 0x20 && $0 < 0x7F }) {
        return "'\(String(bytes: bytes, encoding: .ascii) ?? "")' (\(status))"
    }
    return "\(status)"
}

private struct NativeAudioError: Error {
    let message: String
}

// MARK: - Core Audio property helpers (macOS 14.4+)

@available(macOS 14.4, *)
private enum CAQuery {
    static func address(
        _ selector: AudioObjectPropertySelector,
        scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
        element: AudioObjectPropertyElement = kAudioObjectPropertyElementMain
    ) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: element)
    }

    static func readObjectList(
        _ objectID: AudioObjectID, _ selector: AudioObjectPropertySelector
    ) -> [AudioObjectID] {
        var addr = address(selector)
        var dataSize: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(objectID, &addr, 0, nil, &dataSize) == noErr,
            dataSize > 0
        else { return [] }
        let count = Int(dataSize) / MemoryLayout<AudioObjectID>.size
        var list = [AudioObjectID](repeating: AudioObjectID(kAudioObjectUnknown), count: count)
        guard AudioObjectGetPropertyData(objectID, &addr, 0, nil, &dataSize, &list) == noErr
        else { return [] }
        return list
    }

    static func readUInt32(
        _ objectID: AudioObjectID, _ selector: AudioObjectPropertySelector
    ) -> UInt32? {
        var addr = address(selector)
        var value: UInt32 = 0
        var dataSize = UInt32(MemoryLayout<UInt32>.size)
        guard AudioObjectGetPropertyData(objectID, &addr, 0, nil, &dataSize, &value) == noErr
        else { return nil }
        return value
    }

    static func readPID(
        _ objectID: AudioObjectID, _ selector: AudioObjectPropertySelector
    ) -> pid_t? {
        var addr = address(selector)
        var value: pid_t = -1
        var dataSize = UInt32(MemoryLayout<pid_t>.size)
        guard AudioObjectGetPropertyData(objectID, &addr, 0, nil, &dataSize, &value) == noErr
        else { return nil }
        return value
    }

    static func readString(
        _ objectID: AudioObjectID, _ selector: AudioObjectPropertySelector
    ) -> String? {
        var addr = address(selector)
        var value: Unmanaged<CFString>?
        var dataSize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        let status = withUnsafeMutablePointer(to: &value) { ptr in
            AudioObjectGetPropertyData(objectID, &addr, 0, nil, &dataSize, ptr)
        }
        guard status == noErr, let cf = value?.takeRetainedValue() else { return nil }
        let str = cf as String
        return str.isEmpty ? nil : str
    }

    /// PID → Core Audio process object. Returns kAudioObjectUnknown when the
    /// process has never been an audio client (nothing to tap).
    static func translatePIDToProcessObject(_ pid: pid_t) -> AudioObjectID {
        var addr = address(kAudioHardwarePropertyTranslatePIDToProcessObject)
        var qualifier = pid
        var objectID = AudioObjectID(kAudioObjectUnknown)
        var dataSize = UInt32(MemoryLayout<AudioObjectID>.size)
        let status = withUnsafeMutablePointer(to: &qualifier) { qptr in
            AudioObjectGetPropertyData(
                AudioObjectID(kAudioObjectSystemObject), &addr,
                UInt32(MemoryLayout<pid_t>.size), qptr, &dataSize, &objectID)
        }
        guard status == noErr else { return AudioObjectID(kAudioObjectUnknown) }
        return objectID
    }

    /// UID string of the default system output device (used as the aggregate's
    /// clock master). Nil when it can't be resolved — the tap-only aggregate
    /// still works, so callers treat this as optional.
    static func defaultSystemOutputUID() -> String? {
        var addr = address(kAudioHardwarePropertyDefaultSystemOutputDevice)
        var deviceID = AudioObjectID(kAudioObjectUnknown)
        var dataSize = UInt32(MemoryLayout<AudioObjectID>.size)
        guard
            AudioObjectGetPropertyData(
                AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &dataSize, &deviceID)
                == noErr,
            deviceID != kAudioObjectUnknown
        else { return nil }
        return readString(deviceID, kAudioDevicePropertyDeviceUID)
    }
}

// MARK: - Source enumeration (shared with the smoke CLI)

/// BSD process name via libproc — the fallback for audio clients that are not
/// regular apps (no NSRunningApplication, no bundle id), e.g. CLI players.
private func processName(_ pid: pid_t) -> String? {
    var buf = [CChar](repeating: 0, count: 1024)
    let n = proc_name(pid, &buf, UInt32(buf.count))
    guard n > 0 else { return nil }
    let name = String(cString: buf)
    return name.isEmpty ? nil : name
}

@available(macOS 14.4, *)
enum NativeAudioEnumeration {

    /// Processes currently producing audio output, deduped by bundle id,
    /// with human app names — plus the fixed {pid:-1, "System audio"} entry.
    /// Enumeration does NOT trigger the TCC prompt (only starting a tap does).
    static func listSources() -> [[String: Any]] {
        var out: [[String: Any]] = [["pid": -1, "name": "System audio"]]
        let ownPid = pid_t(ProcessInfo.processInfo.processIdentifier)
        var seen = Set<String>()
        var apps: [[String: Any]] = []

        let processObjects = CAQuery.readObjectList(
            AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList)
        for obj in processObjects {
            guard let pid = CAQuery.readPID(obj, kAudioProcessPropertyPID), pid != ownPid
            else { continue }
            // "Producing audio" = running output; fall back to the generic
            // is-running flag on systems where the output selector errors.
            let running =
                CAQuery.readUInt32(obj, kAudioProcessPropertyIsRunningOutput)
                ?? CAQuery.readUInt32(obj, kAudioProcessPropertyIsRunning) ?? 0
            guard running != 0 else { continue }

            let bundleID = CAQuery.readString(obj, kAudioProcessPropertyBundleID)
            let runningApp = NSRunningApplication(processIdentifier: pid)
            let name =
                runningApp?.localizedName
                ?? bundleID?.components(separatedBy: ".").last
                ?? processName(pid)
                ?? "PID \(pid)"
            let dedupeKey = bundleID ?? "pid-\(pid)"
            guard !seen.contains(dedupeKey) else { continue }
            seen.insert(dedupeKey)

            var entry: [String: Any] = ["pid": Int(pid), "name": name]
            if let bundleID { entry["bundleID"] = bundleID }
            apps.append(entry)
        }

        apps.sort {
            (($0["name"] as? String) ?? "").localizedCaseInsensitiveCompare(
                ($1["name"] as? String) ?? "") == .orderedAscending
        }
        out.append(contentsOf: apps)
        return out
    }

    static func sourcesJSON() -> String {
        guard
            let data = try? JSONSerialization.data(
                withJSONObject: listSources(), options: [.sortedKeys]),
            let json = String(data: data, encoding: .utf8)
        else { return "[]" }
        // JSON is a valid JS literal except for U+2028/U+2029 — escape them
        // since this string is interpolated into evaluateJavaScript source.
        return
            json
            .replacingOccurrences(of: "\u{2028}", with: "\\u2028")
            .replacingOccurrences(of: "\u{2029}", with: "\\u2029")
    }
}

// MARK: - Capture session (one start..stop)

/// tap → private auto-die aggregate device → IOProc reading the tap's input
/// stream → AVAudioConverter (tap format → 16 kHz mono Int16) → base64 chunks.
///
/// All mutable state is confined to `queue`, which is also the dispatch queue
/// the IOProc block runs on — CoreAudio never invokes the block on its
/// realtime thread when a queue is supplied, and the block only sees buffers
/// valid for the duration of the call, so everything is copied before use.
@available(macOS 14.4, *)
final class NativeAudioCaptureSession {

    /// Emit chunks of 1600–3200 samples (100–200 ms at 16 kHz).
    private static let minChunkSamples = 1600
    private static let maxChunkSamples = 3200

    private let targetPid: pid_t
    private let queue = DispatchQueue(label: "whisper.native-audio.capture", qos: .userInitiated)

    // queue-confined state
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var ioProcID: AudioDeviceIOProcID?
    private var converter: AVAudioConverter?
    private var inputFormat: AVAudioFormat?
    private var pending: [Int16] = []
    private var running = false

    private let outputFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!

    /// (base64, sampleCount) — called on the capture queue.
    var onChunk: ((String, Int) -> Void)?
    /// Mid-capture failure — called on the capture queue, after teardown.
    var onError: ((String) -> Void)?

    init(pid: pid_t) {
        self.targetPid = pid
    }

    deinit {
        // Safety net: normal shutdown goes through stop(). If the session is
        // dropped without it, tear the tap/aggregate down synchronously.
        if running {
            queue.sync { self.teardownLocked() }
        }
    }

    /// Create tap + aggregate + IOProc and start the device. Runs on the
    /// capture queue (AudioHardwareCreateProcessTap can block on the TCC
    /// prompt — never call from the main thread). Exactly one of
    /// completion(nil) / completion(message) is invoked, on the queue.
    func start(completion: @escaping (String?) -> Void) {
        queue.async {
            do {
                try self.startLocked()
                self.running = true
                completion(nil)
            } catch {
                self.teardownLocked()
                let message =
                    (error as? NativeAudioError)?.message ?? error.localizedDescription
                nativeAudioLog("start failed: \(message)")
                completion(message)
            }
        }
    }

    /// Drain pending samples, then destroy IOProc, aggregate and tap.
    /// Asynchronous so a stop during a TCC-blocked start can't deadlock.
    func stop(completion: (() -> Void)? = nil) {
        queue.async {
            self.emitPending(flush: true)
            self.teardownLocked()
            completion?()
        }
    }

    // MARK: queue-confined internals

    private func startLocked() throws {
        // 1. Tap description: one code path for both modes.
        let description: CATapDescription
        if targetPid < 0 {
            // Global tap = "System audio", excluding our own process so the
            // SPA's own playback (if any) never feeds back into transcription.
            var exclude: [AudioObjectID] = []
            let own = CAQuery.translatePIDToProcessObject(
                pid_t(ProcessInfo.processInfo.processIdentifier))
            if own != kAudioObjectUnknown { exclude.append(own) }
            description = CATapDescription(stereoGlobalTapButExcludeProcesses: exclude)
        } else {
            let processObject = CAQuery.translatePIDToProcessObject(targetPid)
            guard processObject != kAudioObjectUnknown else {
                throw NativeAudioError(
                    message:
                        "The selected app (pid \(targetPid)) is not registered as an audio process. Make sure it is running and has played audio, then pick it again.")
            }
            description = CATapDescription(stereoMixdownOfProcesses: [processObject])
        }
        description.name = "Whisper Studio Tap"
        description.uuid = UUID()
        description.isPrivate = true
        description.muteBehavior = .unmuted  // the user keeps hearing the audio

        // 2. Create the tap. This is the call gated by the "System Audio
        //    Recording" TCC permission — denial comes back as an error here.
        var newTapID = AudioObjectID(kAudioObjectUnknown)
        var status = AudioHardwareCreateProcessTap(description, &newTapID)
        guard status == noErr, newTapID != kAudioObjectUnknown else {
            throw NativeAudioError(
                message:
                    "Could not create the audio tap (error \(fourCC(status))). If macOS asked for permission and it was denied, enable Whisper Studio under System Settings > Privacy & Security > Screen & System Audio Recording and try again.")
        }
        tapID = newTapID

        // 3. The tap's stream format (typically 48 kHz stereo Float32).
        var formatAddr = CAQuery.address(kAudioTapPropertyFormat)
        var asbd = AudioStreamBasicDescription()
        var asbdSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        status = AudioObjectGetPropertyData(tapID, &formatAddr, 0, nil, &asbdSize, &asbd)
        guard status == noErr, let tapFormat = AVAudioFormat(streamDescription: &asbd) else {
            throw NativeAudioError(
                message: "Could not read the tap's audio format (error \(fourCC(status))).")
        }
        inputFormat = tapFormat

        // 4. Private, auto-destroyed aggregate device that hosts the tap.
        //    The default system output is included as clock master when
        //    resolvable; the tap alone still captures without it.
        var composition: [String: Any] = [
            kAudioAggregateDeviceNameKey: "Whisper Studio Capture",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceTapListKey: [
                [
                    kAudioSubTapUIDKey: description.uuid.uuidString,
                    kAudioSubTapDriftCompensationKey: true,
                ]
            ],
        ]
        if let outputUID = CAQuery.defaultSystemOutputUID() {
            composition[kAudioAggregateDeviceMainSubDeviceKey] = outputUID
            composition[kAudioAggregateDeviceSubDeviceListKey] = [
                [kAudioSubDeviceUIDKey: outputUID]
            ]
        }
        var newAggregateID = AudioObjectID(kAudioObjectUnknown)
        status = AudioHardwareCreateAggregateDevice(composition as CFDictionary, &newAggregateID)
        guard status == noErr, newAggregateID != kAudioObjectUnknown else {
            throw NativeAudioError(
                message: "Could not create the capture aggregate device (error \(fourCC(status))).")
        }
        aggregateID = newAggregateID

        // 5. Converter tap format → 16 kHz mono Int16 (rate + channel mixdown).
        guard let newConverter = AVAudioConverter(from: tapFormat, to: outputFormat) else {
            throw NativeAudioError(
                message:
                    "Could not build an audio converter from \(tapFormat.sampleRate) Hz/\(tapFormat.channelCount)ch to 16 kHz mono.")
        }
        converter = newConverter

        // 6. IOProc on our serial queue reading the tap's input stream.
        var newProcID: AudioDeviceIOProcID?
        status = AudioDeviceCreateIOProcIDWithBlock(&newProcID, aggregateID, queue) {
            [weak self] _, inInputData, _, _, _ in
            self?.handleInput(inInputData)
        }
        guard status == noErr, let procID = newProcID else {
            throw NativeAudioError(
                message: "Could not install the audio IO proc (error \(fourCC(status))).")
        }
        ioProcID = procID

        status = AudioDeviceStart(aggregateID, procID)
        guard status == noErr else {
            throw NativeAudioError(
                message: "Could not start the capture device (error \(fourCC(status))).")
        }
        nativeAudioLog(
            "capture started (pid \(targetPid), tap \(tapID), aggregate \(aggregateID), "
                + "format \(tapFormat.sampleRate) Hz/\(tapFormat.channelCount)ch)")
    }

    /// IOProc payload — runs on `queue`; buffers are only valid inside the call.
    private func handleInput(_ inInputData: UnsafePointer<AudioBufferList>) {
        guard running, let inputFormat, let converter else { return }

        let asbd = inputFormat.streamDescription.pointee
        let ablPointer = UnsafeMutableAudioBufferListPointer(
            UnsafeMutablePointer(mutating: inInputData))
        guard ablPointer.count > 0 else { return }
        let bytesPerFrame = Int(asbd.mBytesPerFrame)
        guard bytesPerFrame > 0 else { return }
        let frames = AVAudioFrameCount(Int(ablPointer[0].mDataByteSize) / bytesPerFrame)
        guard frames > 0 else { return }

        // Copy the ABL into an AVAudioPCMBuffer of the tap's own format.
        guard let inBuffer = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: frames)
        else { return }
        inBuffer.frameLength = frames
        let dst = UnsafeMutableAudioBufferListPointer(inBuffer.mutableAudioBufferList)
        for (i, src) in ablPointer.enumerated() where i < dst.count {
            guard let srcData = src.mData, let dstData = dst[i].mData else { continue }
            let bytes = min(Int(src.mDataByteSize), Int(dst[i].mDataByteSize))
            memcpy(dstData, srcData, bytes)
        }

        // Convert. The converter keeps resampler state across calls, so each
        // buffer is fed exactly once (.haveData then .noDataNow).
        let outCapacity =
            AVAudioFrameCount(
                Double(frames) * outputFormat.sampleRate / inputFormat.sampleRate) + 64
        guard
            let outBuffer = AVAudioPCMBuffer(
                pcmFormat: outputFormat, frameCapacity: outCapacity)
        else { return }
        var fed = false
        var convError: NSError?
        let result = converter.convert(to: outBuffer, error: &convError) { _, outStatus in
            if fed {
                outStatus.pointee = .noDataNow
                return nil
            }
            fed = true
            outStatus.pointee = .haveData
            return inBuffer
        }
        if result == .error {
            nativeAudioLog("conversion failed: \(convError?.localizedDescription ?? "unknown")")
            return
        }

        let outFrames = Int(outBuffer.frameLength)
        if outFrames > 0, let int16Data = outBuffer.int16ChannelData {
            pending.append(
                contentsOf: UnsafeBufferPointer(start: int16Data[0], count: outFrames))
        }
        emitPending(flush: false)
    }

    /// Push accumulated 16 kHz mono Int16 samples to the page in chunks of
    /// 1600–3200 samples. flush=true also emits a final sub-minimum tail.
    private func emitPending(flush: Bool) {
        while pending.count >= Self.minChunkSamples || (flush && !pending.isEmpty) {
            let n = min(pending.count, Self.maxChunkSamples)
            let chunk = Array(pending.prefix(n))
            pending.removeFirst(n)
            let data = chunk.withUnsafeBufferPointer { Data(buffer: $0) }
            onChunk?(data.base64EncodedString(), n)
        }
    }

    private func teardownLocked() {
        running = false
        if let procID = ioProcID, aggregateID != kAudioObjectUnknown {
            AudioDeviceStop(aggregateID, procID)
            AudioDeviceDestroyIOProcID(aggregateID, procID)
        }
        ioProcID = nil
        if aggregateID != kAudioObjectUnknown {
            AudioHardwareDestroyAggregateDevice(aggregateID)
            aggregateID = AudioObjectID(kAudioObjectUnknown)
        }
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = AudioObjectID(kAudioObjectUnknown)
        }
        converter = nil
        inputFormat = nil
        pending.removeAll()
    }
}

// MARK: - WKWebView bridge

/// Owns the page-facing contract: injects the availability marker, receives
/// {cmd} messages, forwards to a NativeAudioCaptureSession, and marshals every
/// native → page callback through evaluateJavaScript on the main thread.
final class NativeAudioBridge: NSObject, WKScriptMessageHandler {

    static let messageHandlerName = "nativeAudio"

    /// Process taps need macOS 14.4+ (runtime check, not just compile-time).
    static var isSupported: Bool {
        if #available(macOS 14.4, *) { return true }
        return false
    }

    weak var webView: WKWebView?

    private var _session: AnyObject?
    @available(macOS 14.4, *)
    private var session: NativeAudioCaptureSession? {
        get { _session as? NativeAudioCaptureSession }
        set { _session = newValue }
    }

    /// Register the documentStart user script (main frame only) and the
    /// "nativeAudio" message handler on the given configuration. Call before
    /// creating the WKWebView; assign `webView` right after.
    func install(into configuration: WKWebViewConfiguration) {
        let marker =
            "window.__WHISPER_NATIVE_AUDIO = { platform: \"macos\", available: \(Self.isSupported) };"
        let script = WKUserScript(
            source: marker, injectionTime: .atDocumentStart, forMainFrameOnly: true)
        configuration.userContentController.addUserScript(script)
        configuration.userContentController.add(self, name: Self.messageHandlerName)
    }

    /// Stop any live capture (app quit / window teardown).
    func shutdown() {
        guard #available(macOS 14.4, *) else { return }
        if let session {
            session.stop()
            self.session = nil
        }
    }

    // MARK: WKScriptMessageHandler

    func userContentController(
        _ userContentController: WKUserContentController, didReceive message: WKScriptMessage
    ) {
        guard message.name == Self.messageHandlerName, message.frameInfo.isMainFrame,
            let body = message.body as? [String: Any],
            let cmd = body["cmd"] as? String
        else { return }

        guard #available(macOS 14.4, *) else {
            emitError("Native audio capture requires macOS 14.4 or later.")
            return
        }

        switch cmd {
        case "list":
            handleList()
        case "start":
            let pid = pid_t((body["pid"] as? NSNumber)?.int32Value ?? -1)
            handleStart(pid: pid)
        case "stop":
            handleStop()
        default:
            emitError("Unknown native audio command: \(cmd)")
        }
    }

    // MARK: Command handlers (main thread)

    @available(macOS 14.4, *)
    private func handleList() {
        // Enumeration walks Core Audio process objects — cheap, but keep it
        // off the main thread anyway; the reply hops back via emit().
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let json = NativeAudioEnumeration.sourcesJSON()
            self?.emit("window.__whisperNativeAudio && window.__whisperNativeAudio.onSources(\(json));")
        }
    }

    @available(macOS 14.4, *)
    private func handleStart(pid: pid_t) {
        // Replace any previous capture — one native source at a time.
        if let old = session {
            old.stop()
            session = nil
        }
        let newSession = NativeAudioCaptureSession(pid: pid)
        newSession.onChunk = { [weak self] base64, count in
            self?.emit(
                "window.__whisperNativeAudio && window.__whisperNativeAudio.onAudio(\"\(base64)\", \(count));"
            )
        }
        newSession.onError = { [weak self] message in
            self?.emitError(message)
        }
        session = newSession
        newSession.start { [weak self, weak newSession] errorMessage in
            DispatchQueue.main.async {
                guard let self else { return }
                if let errorMessage {
                    if self.session === newSession {
                        self.session = nil
                    }
                    self.emitError(errorMessage)
                } else {
                    self.emit(
                        "window.__whisperNativeAudio && window.__whisperNativeAudio.onStarted();")
                }
            }
        }
    }

    @available(macOS 14.4, *)
    private func handleStop() {
        guard let session else {
            // Nothing running — still acknowledge so the page never hangs.
            emit("window.__whisperNativeAudio && window.__whisperNativeAudio.onStopped();")
            return
        }
        self.session = nil
        session.stop { [weak self] in
            self?.emit("window.__whisperNativeAudio && window.__whisperNativeAudio.onStopped();")
        }
    }

    // MARK: Native → page marshaling

    /// All native → page calls funnel through here: evaluateJavaScript must
    /// run on the main thread, and callers may be on capture/background queues.
    private func emit(_ js: String) {
        DispatchQueue.main.async { [weak self] in
            self?.webView?.evaluateJavaScript(js, completionHandler: nil)
        }
    }

    private func emitError(_ message: String) {
        nativeAudioLog("error → page: \(message)")
        emit(
            "window.__whisperNativeAudio && window.__whisperNativeAudio.onError(\(Self.jsStringLiteral(message)));"
        )
    }

    /// JSON-encode a Swift string into a safe JS string literal.
    private static func jsStringLiteral(_ s: String) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: [s]),
            let json = String(data: data, encoding: .utf8),
            json.count >= 2
        else { return "\"native audio error\"" }
        // [ "..." ] → "..."
        return String(json.dropFirst().dropLast())
            .replacingOccurrences(of: "\u{2028}", with: "\\u2028")
            .replacingOccurrences(of: "\u{2029}", with: "\\u2029")
    }
}
