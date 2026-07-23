/**
 * MediaRecorder mock for jsdom test environment.
 *
 * jsdom does not implement MediaRecorder. This stub provides the
 * constructor and lifecycle methods so transcription recording
 * components can be tested.
 */

class MediaRecorderMock {
  static isTypeSupported(_mimeType: string): boolean {
    return true;
  }

  stream: MediaStream;
  mimeType: string;
  state: 'inactive' | 'recording' | 'paused' = 'inactive';

  ondataavailable: ((event: BlobEvent) => void) | null = null;
  onstart: ((event: Event) => void) | null = null;
  onstop: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onpause: ((event: Event) => void) | null = null;
  onresume: ((event: Event) => void) | null = null;

  constructor(stream: MediaStream, options?: MediaRecorderOptions) {
    this.stream = stream;
    this.mimeType = options?.mimeType ?? 'audio/webm';
  }

  start(timeslice?: number): void {
    this.state = 'recording';
    this.onstart?.(new Event('start'));

    // If timeslice is provided, simulate periodic data events
    if (timeslice && timeslice > 0) {
      // No-op in mock — tests can call _emitData() manually
    }
  }

  stop(): void {
    this.state = 'inactive';
    this.onstop?.(new Event('stop'));
  }

  pause(): void {
    this.state = 'paused';
    this.onpause?.(new Event('pause'));
  }

  resume(): void {
    this.state = 'recording';
    this.onresume?.(new Event('resume'));
  }

  requestData(): void {
    this._emitData(new Blob([], { type: this.mimeType }));
  }

  addEventListener(_type: string, _listener: EventListener): void {
    // Stub
  }

  removeEventListener(_type: string, _listener: EventListener): void {
    // Stub
  }

  dispatchEvent(_event: Event): boolean {
    return true;
  }

  /** Test helper: simulate a dataavailable event */
  _emitData(blob: Blob): void {
    const event = new Event('dataavailable') as BlobEvent;
    Object.defineProperty(event, 'data', { value: blob });
    this.ondataavailable?.(event);
  }
}

Object.defineProperty(globalThis, 'MediaRecorder', {
  value: MediaRecorderMock,
  writable: true,
});

export { MediaRecorderMock };
