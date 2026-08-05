/**
 * Native-capture activity indicators: the 3-bar level meter on the ACTIVE
 * source row (CaptureSourceMenu) driven by recordingStore.nativeLevel, and
 * its threshold behavior. The store level is fed by the shell's per-chunk
 * rms via the recording controller.
 */
import { render, screen, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { CaptureSourceMenu, NativeLevelMeter } from './CaptureSourceMenu';
import { useRecordingStore, NATIVE_LEVEL_ACTIVE_THRESHOLD } from '@/stores/recordingStore';

describe('NativeLevelMeter', () => {
  it('renders dimmed with zero lit bars when the level is below threshold', () => {
    render(<NativeLevelMeter level={0} />);
    const meter = screen.getByTestId('native-level-meter');
    expect(meter.className).not.toContain('active');
    expect(meter.getAttribute('data-lit')).toBe('0');
  });

  it('exactly at the threshold still counts as silent', () => {
    render(<NativeLevelMeter level={NATIVE_LEVEL_ACTIVE_THRESHOLD} />);
    expect(screen.getByTestId('native-level-meter').getAttribute('data-lit')).toBe('0');
  });

  it('lights more bars as the level rises', () => {
    const { rerender } = render(<NativeLevelMeter level={0.01} />);
    const lit = () => screen.getByTestId('native-level-meter').getAttribute('data-lit');
    expect(lit()).toBe('1');
    rerender(<NativeLevelMeter level={0.05} />);
    expect(lit()).toBe('2');
    rerender(<NativeLevelMeter level={0.3} />);
    expect(lit()).toBe('3');
    expect(screen.getByTestId('native-level-meter').className).toContain('active');
  });
});

describe('CaptureSourceMenu — active source activity indicator', () => {
  beforeEach(() => {
    window.__WHISPER_NATIVE_AUDIO = { platform: 'macos', available: true };
    useRecordingStore.setState({
      isRecording: true,
      nativeSource: { pid: -1, name: 'System audio' },
      micEnabled: true,
      tabStream: null,
      nativeLevel: 0,
    });
  });

  afterEach(() => {
    delete window.__WHISPER_NATIVE_AUDIO;
    useRecordingStore.setState({
      isRecording: false,
      nativeSource: null,
      micEnabled: true,
      nativeLevel: 0,
    });
  });

  it('shows the meter on the active System audio row while recording', () => {
    render(<CaptureSourceMenu />);
    const meter = screen.getByTestId('native-level-meter');
    expect(meter).toBeInTheDocument();
    expect(meter.getAttribute('data-lit')).toBe('0');
  });

  it('reacts live to store level updates (shell rms → store → meter)', () => {
    render(<CaptureSourceMenu />);
    act(() => {
      useRecordingStore.getState().setNativeLevel(0.31);
    });
    expect(screen.getByTestId('native-level-meter').getAttribute('data-lit')).toBe('3');
    act(() => {
      useRecordingStore.getState().setNativeLevel(0);
    });
    expect(screen.getByTestId('native-level-meter').getAttribute('data-lit')).toBe('0');
  });

  it('hides the meter when not recording', () => {
    act(() => {
      useRecordingStore.setState({ isRecording: false });
    });
    render(<CaptureSourceMenu />);
    expect(screen.queryByTestId('native-level-meter')).toBeNull();
  });

  it('hides the meter when no native source is armed', () => {
    act(() => {
      useRecordingStore.setState({ nativeSource: null });
    });
    render(<CaptureSourceMenu />);
    expect(screen.queryByTestId('native-level-meter')).toBeNull();
  });
});
