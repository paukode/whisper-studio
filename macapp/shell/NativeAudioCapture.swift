// Whisper Studio — native system/app audio capture for the macOS shell.
//
// Captures the audio OUTPUT of the Mac (all of it, or one chosen app) with
// Core Audio process taps (macOS 14.4+): CATapDescription →
// AudioHardwareCreateProcessTap → private TAP-ONLY aggregate device →
// AudioDeviceCreateIOProcID reading the tap's input stream. Captured audio
// (typically 48 kHz stereo Float32) is converted to 16 kHz mono Int16 with
// AVAudioConverter and pushed to the SPA over a WKWebView bridge as base64
// chunks of 1600–3200 samples (100–200 ms).
//
// The aggregate device deliberately contains NO physical sub-devices: taps
// don't need one, and including the real output device opens an IO path to
// the user's speakers/headphones (Bluetooth headsets flip into call mode,
// the OS shows the app in the audio chain, and the tap's input buffers get
// interleaved with the headset's own mic streams). Only if tap-only aggregate
// creation/start fails does the code fall back to including the default
// output device — and then every output buffer is zero-filled in the IOProc
// and the tap's input buffers are located by format, never assumed at index 0.
//
// Source rows are grouped by USER-FACING APP: helper processes (Chrome
// Helper, WebKit GPU/media XPC services) collapse into their owning app via
// the responsible-pid + outermost-".app"-ancestor resolution below, and every
// process belonging to Whisper Studio itself (shell, its WebKit helpers, the
// spawned backend tree) is excluded from both the list and global-tap capture.
//
// Bridge contract (pinned — the frontend mirrors it in
// src/services/nativeAudioSource.ts):
//   * injected at documentStart, main frame only:
//       window.__WHISPER_NATIVE_AUDIO = { platform: "macos", available: bool }
//   * page → native: window.webkit.messageHandlers.nativeAudio.postMessage
//       {cmd:"list"} | {cmd:"start", pid:-1|N, bundleID?} | {cmd:"stop"}
//   * native → page (all via evaluateJavaScript on the main thread):
//       window.__whisperNativeAudio.onSources(json)   // rows: {pid, pids, name, bundleID?}
//       window.__whisperNativeAudio.onStarted()
//       window.__whisperNativeAudio.onAudio(base64, sampleCount, rms)
//       window.__whisperNativeAudio.onWarning(message) // capture running but silent
//       window.__whisperNativeAudio.onError(message)
//       window.__whisperNativeAudio.onStopped()
//
// The first start triggers the OS "System Audio Recording" TCC prompt
// (NSAudioCaptureUsageDescription in Info.plist); a denial surfaces as
// onError. A tap that starts but produces silence for 5 s surfaces as
// onWarning. Failures are logged to stderr, which the shell inherits.

import AppKit
import AVFoundation
import CoreAudio
import Darwin
import Foundation
import WebKit

// MARK: - Logging

func nativeAudioLog(_ message: String) {
    FileHandle.standardError.write(Data("[native-audio] \(message)\n".utf8))
}

/// Render an OSStatus as its four-char code (plus decimal) for error text.
func fourCC(_ status: OSStatus) -> String {
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
enum CAQuery {
    static func address(
        _ selector: AudioObjectPropertySelector,
        scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
        element: AudioObjectPropertyElement = kAudioObjectPropertyElementMain
    ) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: element)
    }

    static func readObjectList(
        _ objectID: AudioObjectID, _ selector: AudioObjectPropertySelector,
        scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal
    ) -> [AudioObjectID] {
        var addr = address(selector, scope: scope)
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

    /// UID string of the default output device. READ-ONLY — used only for the
    /// legacy fallback aggregate; the default devices are never modified.
    static func defaultOutputDeviceUID() -> String? {
        var addr = address(kAudioHardwarePropertyDefaultOutputDevice)
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

// MARK: - Process → user-facing app resolution (shared with the smoke CLI)

/// BSD process name via libproc — the fallback for audio clients that are not
/// regular apps (no NSRunningApplication, no bundle id), e.g. CLI players.
func processName(_ pid: pid_t) -> String? {
    var buf = [CChar](repeating: 0, count: 1024)
    let n = proc_name(pid, &buf, UInt32(buf.count))
    guard n > 0 else { return nil }
    let name = String(cString: buf)
    return name.isEmpty ? nil : name
}

/// Absolute executable path of a live process via libproc, nil when gone.
func executablePath(for pid: pid_t) -> String? {
    guard pid > 0 else { return nil }
    var buf = [CChar](repeating: 0, count: 4096)
    let n = proc_pidpath(pid, &buf, UInt32(buf.count))
    guard n > 0 else { return nil }
    let path = String(cString: buf)
    return path.isEmpty ? nil : path
}

/// Parent pid via libproc (0 when unavailable).
func parentPid(of pid: pid_t) -> pid_t {
    var info = proc_bsdinfo()
    let size = Int32(MemoryLayout<proc_bsdinfo>.size)
    let n = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size)
    guard n == size else { return 0 }
    return pid_t(info.pbi_ppid)
}

/// The pid TCC holds responsible for a process — maps XPC helpers (WebKit
/// GPU/media services, which do NOT live inside their host's .app bundle) to
/// their host app. Resolved via dlsym so a missing symbol degrades gracefully.
private typealias ResponsiblePidFn = @convention(c) (pid_t) -> pid_t
private let _responsiblePidFn: ResponsiblePidFn? = {
    guard let sym = dlsym(UnsafeMutableRawPointer(bitPattern: -2) /* RTLD_DEFAULT */,
                          "responsibility_get_pid_responsible_for_pid")
    else { return nil }
    return unsafeBitCast(sym, to: ResponsiblePidFn.self)
}()

func responsiblePid(for pid: pid_t) -> pid_t {
    guard let fn = _responsiblePidFn else { return pid }
    let r = fn(pid)
    return r > 0 ? r : pid
}

@available(macOS 14.4, *)
enum NativeAudioEnumeration {

    /// One user-facing app that currently has audio-capable processes.
    struct SourceApp {
        var name: String
        var bundleID: String?
        var pids: [pid_t]
    }

    /// Resolved identity of one audio process: which app row it belongs to.
    struct ResolvedApp {
        /// Grouping key — same key ⇒ same row.
        let key: String
        let name: String
        let bundleID: String?
    }

    /// OUTERMOST ".app" ancestor of an executable path, e.g.
    /// ".../Google Chrome.app/.../Google Chrome Helper (GPU).app/.../helper"
    /// → "/Applications/Google Chrome.app". Nil for non-app binaries (afplay)
    /// and framework XPC services (WebKit helpers live in WebKit.framework).
    static func outermostAppBundlePath(forExecutablePath path: String) -> String? {
        let comps = (path as NSString).pathComponents
        guard comps.count > 1 else { return nil }
        // Never treat the executable component itself as a bundle directory.
        guard
            let idx = comps.dropLast().firstIndex(where: {
                $0.hasSuffix(".app") && $0.count > 4
            })
        else { return nil }
        return NSString.path(withComponents: Array(comps[0...idx]))
    }

    /// True when `execPath` lives inside (or is) `bundlePath`. Pure — the
    /// smoke CLI asserts our own-bundle exclusion through this.
    static func belongsToBundle(execPath: String, bundlePath: String?) -> Bool {
        guard let bundlePath, !bundlePath.isEmpty else { return false }
        return execPath == bundlePath || execPath.hasPrefix(bundlePath + "/")
    }

    /// Our own outermost .app path (nil for non-bundled builds like the
    /// smoke CLI, where only pid-based exclusion applies).
    static let ownBundlePath: String? = {
        let url = Bundle.main.bundleURL
        if url.pathExtension == "app" { return url.path }
        if let exec = Bundle.main.executablePath {
            return outermostAppBundlePath(forExecutablePath: exec)
        }
        return nil
    }()

    /// Does this pid belong to Whisper Studio itself? True for the shell pid,
    /// anything TCC holds us responsible for (our WebKit GPU/media helpers),
    /// anything running from inside our own bundle, and every descendant of
    /// the shell (the spawned backend python, llama-server, ffmpeg, ...).
    static func isOwnProcess(pid: pid_t, ownPid: pid_t) -> Bool {
        if pid == ownPid { return true }
        if responsiblePid(for: pid) == ownPid { return true }
        if let exec = executablePath(for: pid),
            belongsToBundle(execPath: exec, bundlePath: ownBundlePath)
        {
            return true
        }
        var p = pid
        var hops = 0
        while p > 1 && hops < 32 {
            let parent = parentPid(of: p)
            if parent == ownPid { return true }
            if parent <= 0 || parent == p { break }
            p = parent
            hops += 1
        }
        return false
    }

    /// Resolve the user-facing app a process belongs to.
    ///
    /// Order matters (verified against a live process dump):
    ///  1. Outermost .app ancestor of the process's OWN executable path —
    ///     catches Chrome/Slack/Teams-style nested helper bundles.
    ///  2. Framework XPC services only (path contains ".xpc/"): hop to the
    ///     RESPONSIBLE process and use its app — WebKit GPU/media helpers
    ///     run from WebKit.framework, never from the host app's bundle.
    ///  3. Otherwise keep the process name (afplay) — deliberately NOT the
    ///     responsible app, or a CLI player would show as its terminal.
    ///
    /// Grouping keys on the resolved bundle id first: Chrome's main process
    /// can run from a translocated "code_sign_clone" path with no .app
    /// ancestor while its helpers resolve to /Applications/Google Chrome.app,
    /// and only the shared bundle id ties those rows together.
    static func resolveApp(pid: pid_t, coreAudioBundleID: String?) -> ResolvedApp {
        let ownExec = executablePath(for: pid)
        var appPath = ownExec.flatMap { outermostAppBundlePath(forExecutablePath: $0) }
        if appPath == nil, let exec = ownExec, exec.contains(".xpc/") {
            let rpid = responsiblePid(for: pid)
            if rpid != pid, let rexec = executablePath(for: rpid) {
                appPath = outermostAppBundlePath(forExecutablePath: rexec)
            }
        }
        if let appPath {
            var name = FileManager.default.displayName(atPath: appPath)
            if name.hasSuffix(".app") { name = String(name.dropLast(4)) }
            let bundleID = Bundle(path: appPath)?.bundleIdentifier ?? coreAudioBundleID
            let key = bundleID.map { "bundle:\($0)" } ?? "app:\(appPath)"
            return ResolvedApp(key: key, name: name, bundleID: bundleID)
        }
        // No .app anywhere: standalone binary (afplay) or a bare service.
        let name =
            NSRunningApplication(processIdentifier: pid)?.localizedName
            ?? processName(pid)
            ?? coreAudioBundleID?.components(separatedBy: ".").last
            ?? "PID \(pid)"
        let key = coreAudioBundleID.map { "bundle:\($0)" } ?? "proc:\(name)"
        return ResolvedApp(key: key, name: name, bundleID: coreAudioBundleID)
    }

    /// Audio-capable processes grouped into user-facing apps, our own
    /// processes excluded. `onlyRunningOutput` filters to apps currently
    /// producing audio (the list UI); start-time re-resolution passes false
    /// so a momentarily quiet app can still be tapped.
    static func listGroupedApps(onlyRunningOutput: Bool) -> [SourceApp] {
        let ownPid = pid_t(ProcessInfo.processInfo.processIdentifier)
        var groups: [String: SourceApp] = [:]

        let processObjects = CAQuery.readObjectList(
            AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList)
        for obj in processObjects {
            guard let pid = CAQuery.readPID(obj, kAudioProcessPropertyPID), pid > 0
            else { continue }
            if onlyRunningOutput {
                // "Producing audio" = running output; fall back to the generic
                // is-running flag on systems where the output selector errors.
                let running =
                    CAQuery.readUInt32(obj, kAudioProcessPropertyIsRunningOutput)
                    ?? CAQuery.readUInt32(obj, kAudioProcessPropertyIsRunning) ?? 0
                guard running != 0 else { continue }
            }
            guard !isOwnProcess(pid: pid, ownPid: ownPid) else { continue }

            let bundleID = CAQuery.readString(obj, kAudioProcessPropertyBundleID)
            let resolved = resolveApp(pid: pid, coreAudioBundleID: bundleID)
            if var existing = groups[resolved.key] {
                if !existing.pids.contains(pid) { existing.pids.append(pid) }
                groups[resolved.key] = existing
            } else {
                groups[resolved.key] = SourceApp(
                    name: resolved.name, bundleID: resolved.bundleID, pids: [pid])
            }
        }

        return groups.values.sorted {
            $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending
        }
    }

    /// Rows for the picker: the fixed {pid:-1, "System audio"} entry plus one
    /// row per app. Each app row carries ALL its audio pids ("pids"); "pid"
    /// stays as the first pid for bridge back-compat. Enumeration does NOT
    /// trigger the TCC prompt (only starting a tap does).
    static func listSources() -> [[String: Any]] {
        var out: [[String: Any]] = [["pid": -1, "name": "System audio"]]
        for app in listGroupedApps(onlyRunningOutput: true) {
            guard let first = app.pids.first else { continue }
            var entry: [String: Any] = [
                "pid": Int(first),
                "pids": app.pids.map(Int.init),
                "name": app.name,
            ]
            if let bundleID = app.bundleID { entry["bundleID"] = bundleID }
            out.append(entry)
        }
        return out
    }

    /// Every Core Audio process object belonging to Whisper Studio itself —
    /// the exclusion list for the global tap (shell + WebKit helpers +
    /// backend process tree, not just the shell pid).
    static func ownProcessObjects() -> (objects: [AudioObjectID], pids: [pid_t]) {
        let ownPid = pid_t(ProcessInfo.processInfo.processIdentifier)
        var objects: [AudioObjectID] = []
        var pids: [pid_t] = []
        let processObjects = CAQuery.readObjectList(
            AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList)
        for obj in processObjects {
            guard let pid = CAQuery.readPID(obj, kAudioProcessPropertyPID), pid > 0
            else { continue }
            if isOwnProcess(pid: pid, ownPid: ownPid) {
                objects.append(obj)
                pids.append(pid)
            }
        }
        return (objects, pids)
    }

    /// Re-resolve, at start time, ALL audio process objects of the app the
    /// user picked. The armed row may be stale (relaunch changed pids, more
    /// helpers spawned since), so match by the app identity of the given pid
    /// when it is still alive, and by bundle id otherwise.
    static func processObjects(
        forAppWithPid pid: pid_t, bundleID: String?
    ) -> (objects: [AudioObjectID], pids: [pid_t]) {
        let ownPid = pid_t(ProcessInfo.processInfo.processIdentifier)
        let processObjects = CAQuery.readObjectList(
            AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList)

        // Identity of the requested pid, when that process still exists.
        var targetKey: String?
        for obj in processObjects {
            guard let opid = CAQuery.readPID(obj, kAudioProcessPropertyPID), opid == pid
            else { continue }
            let caBundle = CAQuery.readString(obj, kAudioProcessPropertyBundleID)
            targetKey = resolveApp(pid: opid, coreAudioBundleID: caBundle).key
            break
        }

        var objects: [AudioObjectID] = []
        var pids: [pid_t] = []
        for obj in processObjects {
            guard let opid = CAQuery.readPID(obj, kAudioProcessPropertyPID), opid > 0
            else { continue }
            guard !isOwnProcess(pid: opid, ownPid: ownPid) else { continue }
            let caBundle = CAQuery.readString(obj, kAudioProcessPropertyBundleID)
            let resolved = resolveApp(pid: opid, coreAudioBundleID: caBundle)
            let matches =
                (targetKey != nil && resolved.key == targetKey)
                || (bundleID != nil && resolved.bundleID == bundleID)
                || opid == pid
            if matches {
                objects.append(obj)
                pids.append(opid)
            }
        }
        return (objects, pids)
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

/// tap → private TAP-ONLY auto-die aggregate device → IOProc reading the
/// tap's input stream → AVAudioConverter (tap format → 16 kHz mono Int16) →
/// base64 chunks.
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
    /// Warn when a started capture has produced nothing but silence this long.
    private static let silenceWarningSeconds: TimeInterval = 5

    static let silenceWarningMessage =
        "System audio capture is producing no sound. Check System Settings > "
        + "Privacy & Security > Screen & System Audio Recording, then stop and "
        + "re-start the source."

    private let targetPid: pid_t
    private let targetBundleID: String?
    private let queue = DispatchQueue(label: "whisper.native-audio.capture", qos: .userInitiated)

    // queue-confined state
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var ioProcID: AudioDeviceIOProcID?
    private var converter: AVAudioConverter?
    private var inputFormat: AVAudioFormat?
    private var pending: [Int16] = []
    private var running = false
    /// True when the tap-only aggregate failed and the fallback aggregate
    /// (which includes the default output device) is in use.
    private var usedOutputFallback = false
    /// Resolved index of the tap's first buffer inside the IOProc input ABL
    /// (only ever non-zero in fallback mode). Nil until located.
    private var tapBufferStart: Int?
    private var tapLayoutLogged = false

    // Silent-capture watchdog (queue-confined).
    private var watchdog: DispatchSourceTimer?
    private var startedAt = Date()
    private var sawCallback = false
    private var sawNonZero = false
    private var warned = false

    private let outputFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!

    /// (base64, sampleCount, rms) — called on the capture queue. rms is the
    /// root-mean-square of the chunk in [0, 1] (a live level for the UI).
    var onChunk: ((String, Int, Double) -> Void)?
    /// Mid-capture failure — called on the capture queue, after teardown.
    var onError: ((String) -> Void)?
    /// Capture is running but silent (no callbacks, or only zero samples,
    /// for 5 s) — called once per session on the capture queue.
    var onWarning: ((String) -> Void)?

    init(pid: pid_t, bundleID: String? = nil) {
        self.targetPid = pid
        self.targetBundleID = bundleID
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
                self.startSilenceWatchdogLocked()
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
            // Global tap = "System audio", excluding EVERY process that
            // belongs to Whisper Studio (shell, WebKit GPU/media helpers,
            // the backend tree) so our own playback never feeds back into
            // transcription.
            let own = NativeAudioEnumeration.ownProcessObjects()
            nativeAudioLog(
                "global tap excluding \(own.objects.count) own process(es): pids \(own.pids)")
            description = CATapDescription(stereoGlobalTapButExcludeProcesses: own.objects)
        } else {
            // Re-resolve the picked app at start time: tap ALL of its audio
            // processes (helpers included), not just the possibly stale pid
            // the row carried when the menu was opened.
            let target = NativeAudioEnumeration.processObjects(
                forAppWithPid: targetPid, bundleID: targetBundleID)
            guard !target.objects.isEmpty else {
                throw NativeAudioError(
                    message:
                        "The selected app (pid \(targetPid)) is not registered as an audio process. Make sure it is running and has played audio, then pick it again.")
            }
            nativeAudioLog("tapping app pids \(target.pids) (requested pid \(targetPid))")
            description = CATapDescription(stereoMixdownOfProcesses: target.objects)
        }
        description.name = "Whisper Studio Tap"
        description.uuid = UUID()
        description.isPrivate = true
        description.muteBehavior = .unmuted  // the user keeps hearing the audio

        // 2. Create the tap. This is the call gated by the "System Audio
        //    Recording" TCC permission — denial comes back as an error here.
        var newTapID = AudioObjectID(kAudioObjectUnknown)
        var status = AudioHardwareCreateProcessTap(description, &newTapID)
        nativeAudioLog("AudioHardwareCreateProcessTap → OSStatus \(status) \(fourCC(status))")
        guard status == noErr, newTapID != kAudioObjectUnknown else {
            // 'nope' (1852797029, kAudioHardwareIllegalOperationError) is what
            // a TCC denial typically comes back as.
            let hint =
                status == kAudioHardwareIllegalOperationError
                ? " This usually means the System Audio Recording permission is denied."
                : ""
            throw NativeAudioError(
                message:
                    "Could not create the audio tap (error \(fourCC(status))).\(hint) If macOS asked for permission and it was denied, enable Whisper Studio under System Settings > Privacy & Security > Screen & System Audio Recording and try again.")
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

        // 4. Converter tap format → 16 kHz mono Int16 (rate + channel mixdown).
        guard let newConverter = AVAudioConverter(from: tapFormat, to: outputFormat) else {
            throw NativeAudioError(
                message:
                    "Could not build an audio converter from \(tapFormat.sampleRate) Hz/\(tapFormat.channelCount)ch to 16 kHz mono.")
        }
        converter = newConverter

        // 5+6. Aggregate + IOProc + start. Tap-only first (no physical
        //      sub-devices — no IO path to the user's output); fall back to
        //      including the default output device only if that fails, with
        //      output buffers zero-filled in the IOProc.
        do {
            try setUpAggregateAndIOLocked(tapUUID: description.uuid, includeOutputDevice: false)
        } catch {
            let message = (error as? NativeAudioError)?.message ?? error.localizedDescription
            nativeAudioLog(
                "tap-only aggregate path failed (\(message)); retrying with the default "
                    + "output device as a sub-device (output buffers stay zero-filled)")
            tearDownAggregateLocked()
            usedOutputFallback = true
            tapBufferStart = nil
            try setUpAggregateAndIOLocked(tapUUID: description.uuid, includeOutputDevice: true)
        }

        nativeAudioLog(
            "capture started (pid \(targetPid), tap \(tapID), aggregate \(aggregateID), "
                + "format \(tapFormat.sampleRate) Hz/\(tapFormat.channelCount)ch, "
                + (usedOutputFallback ? "fallback aggregate with output device)" : "tap-only aggregate)"))
    }

    /// Create the aggregate (optionally with the default output device as a
    /// sub-device), install the IOProc and start the device. Throws with the
    /// failing step's OSStatus; the caller decides whether to fall back.
    private func setUpAggregateAndIOLocked(tapUUID: UUID, includeOutputDevice: Bool) throws {
        var composition: [String: Any] = [
            kAudioAggregateDeviceNameKey: "Whisper Studio Capture",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceTapListKey: [
                [
                    kAudioSubTapUIDKey: tapUUID.uuidString,
                    kAudioSubTapDriftCompensationKey: true,
                ]
            ],
        ]
        if includeOutputDevice {
            guard let outputUID = CAQuery.defaultOutputDeviceUID() else {
                throw NativeAudioError(
                    message: "Could not resolve the default output device for the fallback aggregate.")
            }
            composition[kAudioAggregateDeviceMainSubDeviceKey] = outputUID
            composition[kAudioAggregateDeviceSubDeviceListKey] = [
                [kAudioSubDeviceUIDKey: outputUID]
            ]
        }

        var newAggregateID = AudioObjectID(kAudioObjectUnknown)
        var status = AudioHardwareCreateAggregateDevice(composition as CFDictionary, &newAggregateID)
        nativeAudioLog(
            "AudioHardwareCreateAggregateDevice(\(includeOutputDevice ? "with output" : "tap-only")) "
                + "→ OSStatus \(status) \(fourCC(status))")
        guard status == noErr, newAggregateID != kAudioObjectUnknown else {
            throw NativeAudioError(
                message: "Could not create the capture aggregate device (error \(fourCC(status))).")
        }
        aggregateID = newAggregateID

        var newProcID: AudioDeviceIOProcID?
        status = AudioDeviceCreateIOProcIDWithBlock(&newProcID, aggregateID, queue) {
            [weak self] _, inInputData, _, outOutputData, _ in
            self?.handleIO(input: inInputData, output: outOutputData)
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
    }

    /// IOProc payload — runs on `queue`; buffers are only valid inside the call.
    private func handleIO(
        input inInputData: UnsafePointer<AudioBufferList>,
        output outOutputData: UnsafeMutablePointer<AudioBufferList>
    ) {
        // The capture must NEVER write audio to a physical output: zero-fill
        // every output buffer first (a no-op for the tap-only aggregate,
        // which has no output streams; load-bearing in fallback mode).
        let outABL = UnsafeMutableAudioBufferListPointer(outOutputData)
        for buf in outABL {
            if let data = buf.mData, buf.mDataByteSize > 0 {
                memset(data, 0, Int(buf.mDataByteSize))
            }
        }

        guard running, let inputFormat, let converter else { return }
        sawCallback = true

        let asbd = inputFormat.streamDescription.pointee
        let ablPointer = UnsafeMutableAudioBufferListPointer(
            UnsafeMutablePointer(mutating: inInputData))
        guard ablPointer.count > 0 else { return }

        // How many buffers the TAP contributes: 1 when interleaved, one per
        // channel when deinterleaved.
        let tapBufferCount = inputFormat.isInterleaved ? 1 : Int(inputFormat.channelCount)
        let chansPerBuffer = inputFormat.isInterleaved ? Int(inputFormat.channelCount) : 1

        // Locate the tap's buffers inside the input ABL. Tap-only aggregate:
        // the tap is the only input, index 0. Fallback aggregate: the output
        // sub-device may contribute its own input streams (Bluetooth headsets
        // expose their mic!), so find the run of buffers matching the tap's
        // channel layout — searching from the END, where taps are appended.
        var start = 0
        if usedOutputFallback {
            if let cached = tapBufferStart {
                start = cached
            } else {
                var found = -1
                var candidate = ablPointer.count - tapBufferCount
                while candidate >= 0 {
                    var matches = true
                    for i in 0..<tapBufferCount
                    where Int(ablPointer[candidate + i].mNumberChannels) != chansPerBuffer {
                        matches = false
                        break
                    }
                    if matches {
                        found = candidate
                        break
                    }
                    candidate -= 1
                }
                if found < 0 {
                    if !tapLayoutLogged {
                        tapLayoutLogged = true
                        let layout = ablPointer.map { Int($0.mNumberChannels) }
                        nativeAudioLog(
                            "could not locate the tap in the fallback aggregate's input "
                                + "buffers (channel layout \(layout), expected \(tapBufferCount) "
                                + "buffer(s) of \(chansPerBuffer)ch)")
                    }
                    return
                }
                tapBufferStart = found
                start = found
                if !tapLayoutLogged {
                    tapLayoutLogged = true
                    let layout = ablPointer.map { Int($0.mNumberChannels) }
                    nativeAudioLog(
                        "fallback aggregate input layout \(layout); tap buffers at "
                            + "index \(found)..<\(found + tapBufferCount)")
                }
            }
        }
        guard start + tapBufferCount <= ablPointer.count else { return }

        let bytesPerFrame = Int(asbd.mBytesPerFrame)
        guard bytesPerFrame > 0 else { return }
        let frames = AVAudioFrameCount(Int(ablPointer[start].mDataByteSize) / bytesPerFrame)
        guard frames > 0 else { return }

        // Copy the tap's buffers into an AVAudioPCMBuffer of its own format.
        guard let inBuffer = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: frames)
        else { return }
        inBuffer.frameLength = frames
        let dst = UnsafeMutableAudioBufferListPointer(inBuffer.mutableAudioBufferList)
        for i in 0..<min(tapBufferCount, dst.count) {
            let src = ablPointer[start + i]
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
            let samples = UnsafeBufferPointer(start: int16Data[0], count: outFrames)
            if !sawNonZero, samples.contains(where: { $0 != 0 }) {
                sawNonZero = true
            }
            pending.append(contentsOf: samples)
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
            var sumSquares = 0.0
            for s in chunk {
                let v = Double(s) / 32768.0
                sumSquares += v * v
            }
            let rms = (sumSquares / Double(n)).squareRoot()
            let data = chunk.withUnsafeBufferPointer { Data(buffer: $0) }
            onChunk?(data.base64EncodedString(), n, rms)
        }
    }

    // MARK: Silent-capture watchdog

    /// After a successful start, check once per second whether the capture is
    /// actually producing sound. If 5 s pass with either no IO callbacks at
    /// all or nothing but zero samples, emit one onWarning. The watchdog
    /// disarms itself the moment a non-zero sample is seen.
    private func startSilenceWatchdogLocked() {
        startedAt = Date()
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 1.0, repeating: 1.0)
        timer.setEventHandler { [weak self] in
            self?.checkSilenceLocked()
        }
        timer.resume()
        watchdog = timer
    }

    private func checkSilenceLocked() {
        guard running, !warned else { return }
        if sawNonZero {
            watchdog?.cancel()
            watchdog = nil
            return
        }
        guard Date().timeIntervalSince(startedAt) >= Self.silenceWarningSeconds else { return }
        warned = true
        watchdog?.cancel()
        watchdog = nil
        nativeAudioLog(
            "silent capture: "
                + (sawCallback
                    ? "IO callbacks arriving but every sample is zero"
                    : "no IO callbacks at all since start"))
        onWarning?(Self.silenceWarningMessage)
    }

    // MARK: Teardown

    private func tearDownAggregateLocked() {
        if let procID = ioProcID, aggregateID != kAudioObjectUnknown {
            AudioDeviceStop(aggregateID, procID)
            AudioDeviceDestroyIOProcID(aggregateID, procID)
        }
        ioProcID = nil
        if aggregateID != kAudioObjectUnknown {
            AudioHardwareDestroyAggregateDevice(aggregateID)
            aggregateID = AudioObjectID(kAudioObjectUnknown)
        }
    }

    private func teardownLocked() {
        running = false
        watchdog?.cancel()
        watchdog = nil
        tearDownAggregateLocked()
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = AudioObjectID(kAudioObjectUnknown)
        }
        converter = nil
        inputFormat = nil
        pending.removeAll()
        tapBufferStart = nil
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
            let bundleID = body["bundleID"] as? String
            handleStart(pid: pid, bundleID: bundleID)
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
    private func handleStart(pid: pid_t, bundleID: String?) {
        // Replace any previous capture — one native source at a time.
        if let old = session {
            old.stop()
            session = nil
        }
        let newSession = NativeAudioCaptureSession(pid: pid, bundleID: bundleID)
        newSession.onChunk = { [weak self] base64, count, rms in
            let rmsLiteral = String(format: "%.6f", rms)
            self?.emit(
                "window.__whisperNativeAudio && window.__whisperNativeAudio.onAudio(\"\(base64)\", \(count), \(rmsLiteral));"
            )
        }
        newSession.onError = { [weak self] message in
            self?.emitError(message)
        }
        newSession.onWarning = { [weak self] message in
            self?.emitWarning(message)
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

    private func emitWarning(_ message: String) {
        nativeAudioLog("warning → page: \(message)")
        // onWarning is newer than onError — guard the callback itself so an
        // older page bundle is a silent no-op, never a JS exception.
        emit(
            "window.__whisperNativeAudio && window.__whisperNativeAudio.onWarning && window.__whisperNativeAudio.onWarning(\(Self.jsStringLiteral(message)));"
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
