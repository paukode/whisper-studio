import { create } from 'zustand';

const MIC_SAMPLE_RATE = 16000;

/** getUserMedia constraints used by every consumer of the shared mic.
 *
 *  Kept permissive (echoCancellation/noiseSuppression OFF) so the
 *  documented system-audio aggregate-device workflow (see comment at
 *  the top of Header.tsx) keeps working. The Web Audio API's processed
 *  flags would re-route the stream through the browser's voice-isolation
 *  pipeline, which strips the loopback audio that aggregate devices feed
 *  in. The chat-input mic is happy with this same setting — voice
 *  recognition tolerates raw audio fine. */
const MIC_CONSTRAINTS: MediaStreamConstraints = {
  audio: {
    channelCount: 1,
    sampleRate: MIC_SAMPLE_RATE,
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: true,
  },
};

export interface RecordingState {
  isRecording: boolean;
  isConnected: boolean;
  /** Which session OWNS the live recording. Recording is decoupled from
   *  the viewed session: transcripts route here, the header shows a
   *  jump chip when this differs from currentSessionId, and stop/save
   *  always target this session. Null when idle. */
  recordingSessionId: string | null;
  micStream: MediaStream | null;
  audioContext: AudioContext | null;
  /** Optional Chrome/Edge tab-audio stream, armed from the source picker
   *  (CaptureSourceMenu) before recording via getDisplayMedia. The
   *  recording controller mixes it into the same mono worklet input as
   *  the mic at start(). Null when only the mic is captured. */
  tabStream: MediaStream | null;
  // Actions
  setRecording: (recording: boolean) => void;
  setRecordingSession: (sessionId: string | null) => void;
  setConnected: (connected: boolean) => void;
  /** Capture a browser tab's audio (Chrome/Edge only). Requests display
   *  media WITH video so Chrome offers the "Also share tab audio"
   *  checkbox, then keeps only the audio track. Rejects with
   *  `Error('unsupported')` when getDisplayMedia is missing, or
   *  `Error('no-audio')` when the user shared a tab but not its audio. */
  acquireTabAudio: () => Promise<MediaStream>;
  /** Stop and drop the armed tab-audio stream. No-op when none is armed. */
  releaseTabAudio: () => void;
  cleanup: () => void;
  /** Refcounted acquisition of the shared mic stream + AudioContext.
   *
   *  Pass a stable owner key (e.g. ``"header-recorder"`` or
   *  ``"chat-input-mic"``) — each owner adds 1 to the refcount. Calling
   *  twice with the same owner is a no-op (returns the same instances).
   *  The first call lazily opens the device with `getUserMedia`; later
   *  calls return the same instances synchronously after the first
   *  promise resolves.
   *
   *  Each consumer should construct its own AudioWorkletNode hanging
   *  off the shared MediaStream — the source can have multiple
   *  connections, so two transcribers can read the same audio without
   *  fighting the OS for device exclusivity. */
  acquireMic: (owner: string) => Promise<{ stream: MediaStream; context: AudioContext }>;
  /** Drops the owner's refcount. When the count hits zero the tracks
   *  are stopped and the AudioContext is closed. */
  releaseMic: (owner: string) => Promise<void>;
}

// Module-level shared state. Lives outside the zustand `set` because
// it's reference-counted and we want a single instance no matter how
// many `useRecordingStore` subscribers React mounts.
let _micPromise: Promise<{ stream: MediaStream; context: AudioContext }> | null = null;
const _owners = new Set<string>();

export const useRecordingStore = create<RecordingState>()((set, get) => ({
  isRecording: false,
  isConnected: false,
  recordingSessionId: null,
  micStream: null,
  audioContext: null,
  tabStream: null,

  setRecording: (recording: boolean) => {
    set({ isRecording: recording });
  },

  setRecordingSession: (sessionId: string | null) => {
    set({ recordingSessionId: sessionId });
  },

  setConnected: (connected: boolean) => {
    set({ isConnected: connected });
  },

  acquireTabAudio: async () => {
    const md = navigator.mediaDevices;
    if (!md || typeof md.getDisplayMedia !== 'function') {
      throw new Error('unsupported');
    }
    // video:true is required for Chrome/Edge to offer the "Also share tab
    // audio" checkbox — audio-only getDisplayMedia is not accepted. We
    // drop the video track the moment we have the stream and keep audio.
    const display = await md.getDisplayMedia({ video: true, audio: true });
    display.getVideoTracks().forEach((track) => track.stop());
    const audioTracks = display.getAudioTracks();
    if (audioTracks.length === 0) {
      display.getTracks().forEach((track) => track.stop());
      throw new Error('no-audio');
    }
    const tabStream = new MediaStream(audioTracks);
    // Chrome's "Stop sharing" toolbar (or the picker's own stop) ends the
    // track — mirror that into state so the picker updates and any live
    // recording mix drops the source.
    audioTracks[0].addEventListener('ended', () => {
      if (get().tabStream === tabStream) set({ tabStream: null });
    });
    set({ tabStream });
    return tabStream;
  },

  releaseTabAudio: () => {
    const { tabStream } = get();
    if (tabStream) {
      tabStream.getTracks().forEach((track) => track.stop());
      set({ tabStream: null });
    }
  },

  cleanup: () => {
    const { micStream, audioContext, tabStream } = get();

    // Stop all tracks on the mic stream
    if (micStream) {
      micStream.getTracks().forEach((track) => track.stop());
    }

    // Stop the optional tab-audio stream too.
    if (tabStream) {
      tabStream.getTracks().forEach((track) => track.stop());
    }

    // Close the audio context
    if (audioContext && audioContext.state !== 'closed') {
      audioContext.close().catch(() => {
        // Ignore errors when closing audio context (may already be closed)
      });
    }

    // cleanup() is a "force-tear-down everything" operation, so it also
    // empties the owner set. Otherwise a stale owner could prevent the
    // next acquireMic() from re-initializing on the next start.
    _owners.clear();
    _micPromise = null;

    set({
      isRecording: false,
      isConnected: false,
      recordingSessionId: null,
      micStream: null,
      audioContext: null,
      tabStream: null,
    });
  },

  acquireMic: async (owner: string) => {
    _owners.add(owner);

    if (_micPromise) {
      // Already opening (or already open). Await and return the shared
      // pair. If the in-flight open is still resolving, subsequent
      // owners share the same promise so getUserMedia is only called
      // once.
      try {
        return await _micPromise;
      } catch (err) {
        // The original opener failed — drop our owner so the refcount
        // doesn't lock the store into a broken state.
        _owners.delete(owner);
        throw err;
      }
    }

    _micPromise = (async () => {
      const stream = await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS);
      const context = new AudioContext({ sampleRate: MIC_SAMPLE_RATE });
      set({ micStream: stream, audioContext: context });
      return { stream, context };
    })();

    try {
      return await _micPromise;
    } catch (err) {
      // Open failed — wipe state so the next acquire retries cleanly.
      _owners.delete(owner);
      _micPromise = null;
      set({ micStream: null, audioContext: null });
      throw err;
    }
  },

  releaseMic: async (owner: string) => {
    if (!_owners.has(owner)) return;
    _owners.delete(owner);
    if (_owners.size > 0) {
      // Other consumers still need the stream — leave it running.
      return;
    }

    const { micStream, audioContext } = get();

    // Last owner released while the device is still opening: state has no
    // stream yet (micStream/audioContext are only populated once the
    // getUserMedia promise resolves). The synchronous teardown below would
    // find nothing to stop and then null `_micPromise`, orphaning the
    // MediaStream that getUserMedia is about to hand back — a permanently
    // live mic (the recording indicator never turns off). Instead, chain
    // the teardown onto the pending open and only stop if no new owner has
    // re-acquired by the time it resolves.
    if (!micStream && _micPromise) {
      const pending = _micPromise;
      _micPromise = null;
      void pending
        .then(({ stream, context }) => {
          // A new owner may have acquired mid-open — if so, keep it live.
          if (_owners.size > 0) return;
          stream.getTracks().forEach((track) => track.stop());
          if (context && context.state !== 'closed') {
            void context.close().catch(() => {
              // Already closed elsewhere — ignore.
            });
          }
          set({ micStream: null, audioContext: null });
        })
        .catch(() => {
          // The in-flight open failed; acquireMic already reset state.
        });
      return;
    }

    if (micStream) {
      micStream.getTracks().forEach((track) => track.stop());
    }
    if (audioContext && audioContext.state !== 'closed') {
      try {
        await audioContext.close();
      } catch {
        // Already closed elsewhere — ignore.
      }
    }
    _micPromise = null;
    set({ micStream: null, audioContext: null });
  },
}));
