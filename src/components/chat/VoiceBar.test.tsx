import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';

vi.mock('@/api/client', () => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() }));

// vi.mock factories are hoisted above imports, so the shared mock object must
// be created through vi.hoisted to exist when the factory runs.
const controller = vi.hoisted(() => ({
  stop: vi.fn(),
  resolveRequest: vi.fn(),
  setMuted: vi.fn(),
  start: vi.fn(),
  sendText: vi.fn(),
  loadStatus: vi.fn(),
}));
vi.mock('@/services/voiceController', () => ({ voiceController: controller }));

import { VoiceBar } from './VoiceBar';
import { VoiceLiveMessage } from './VoiceLiveMessage';
import { useSessionStore } from '@/stores/sessionStore';
import { useVoiceStore } from '@/stores/voiceStore';

describe('VoiceBar', () => {
  beforeEach(() => {
    useVoiceStore.getState().reset();
    useVoiceStore.getState().begin('s1');
    useVoiceStore.getState().setAvailability({
      available: true,
      reason: null,
      voices: [{ id: 'tiffany', label: 'Tiffany', locale: 'en-US', polyglot: 'yes' }],
    });
    controller.stop.mockClear();
    controller.setMuted.mockClear();
  });

  it('shows the state label, hint and voice, and reflects speaking', () => {
    useVoiceStore.getState().setReady('amazon.nova-2-sonic-v1:0', 'tiffany');
    const { container } = render(<VoiceBar />);
    expect(screen.getByText('Listening')).toBeInTheDocument();
    expect(screen.getByText(/Speak naturally/)).toBeInTheDocument();
    expect(screen.getByText('Tiffany · en-US')).toBeInTheDocument();
    expect(container.querySelector('.voice-bar')?.className).toContain('voice-listening');
    act(() => useVoiceStore.getState().setStatus('speaking'));
    expect(screen.getByText('Speaking')).toBeInTheDocument();
    expect(container.querySelector('.voice-bar')?.className).toContain('voice-speaking');
  });

  it('End asks the controller to stop; Mute toggles and changes the hint', () => {
    useVoiceStore.getState().setReady('m', 'tiffany');
    render(<VoiceBar />);
    fireEvent.click(screen.getByLabelText('End voice conversation'));
    expect(controller.stop).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByLabelText('Mute microphone'));
    expect(controller.setMuted).toHaveBeenCalledWith(true);
    act(() => useVoiceStore.getState().setMuted(true));
    expect(screen.getByText(/Microphone muted/)).toBeInTheDocument();
    expect(screen.getByLabelText('Unmute microphone')).toBeInTheDocument();
  });

  it('Type instead flips the typing flag so the composer shows the text box', () => {
    useVoiceStore.getState().setReady('m', 'tiffany');
    render(<VoiceBar />);
    fireEvent.click(screen.getByLabelText('Type instead'));
    expect(useVoiceStore.getState().typing).toBe(true);
  });
});

describe('VoiceLiveMessage', () => {
  beforeEach(() => {
    useVoiceStore.getState().reset();
    useSessionStore.setState({ currentSessionId: 's1' });
    useVoiceStore.getState().begin('s1');
    useVoiceStore.getState().setReady('m', 'tiffany');
  });

  it('renders nothing while idle, then the live text and activity while speaking', () => {
    const { container, rerender } = render(<VoiceLiveMessage />);
    expect(container.querySelector('[data-testid="voice-live-message"]')).toBeNull();
    act(() => {
      useVoiceStore.getState().setStatus('speaking');
      useVoiceStore.getState().appendLiveText('Running the tests now.');
      useVoiceStore.getState().upsertStep({ id: 's1', name: 'ws_run_command', status: 'running', detail: 'pytest', source: 'assistant' });
    });
    rerender(<VoiceLiveMessage />);
    expect(screen.getByText('Running the tests now.')).toBeInTheDocument();
    expect(screen.getByText(/Speaking · Tiffany/)).toBeInTheDocument();
    expect(container.querySelector('.activity-row')).not.toBeNull();
  });

  it('shows run work only in the session it belongs to, even after voice is off', () => {
    act(() => {
      useVoiceStore.getState().upsertStep({ id: 'a1', name: 'spawn_agent', status: 'running', detail: 'review', source: 'assistant', runId: 'r1' });
      useVoiceStore.getState().adjustDraining(1);
      useVoiceStore.getState().reset();
      useSessionStore.setState({ currentSessionId: 's1' });
    });
    const { container, rerender } = render(<VoiceLiveMessage />);
    expect(container.querySelector('[data-testid="voice-live-message"]')).not.toBeNull();
    expect(container.textContent).toContain('still finishing this work');
    expect(container.querySelector('.voice-draining-stop')).not.toBeNull();
    // A new session must not show another session's voice work.
    act(() => { useSessionStore.setState({ currentSessionId: 's2' }); });
    rerender(<VoiceLiveMessage />);
    expect(container.querySelector('[data-testid="voice-live-message"]')).toBeNull();
    act(() => {
      useVoiceStore.getState().adjustDraining(-1);
      useVoiceStore.getState().reset();
      useSessionStore.setState({ currentSessionId: 's1' });
    });
  });

  it('shows a Thinking row while a delegated request runs with nothing to say yet', () => {
    useVoiceStore.getState().setStatus('thinking');
    render(<VoiceLiveMessage />);
    expect(screen.getByText('Thinking')).toBeInTheDocument();
  });

  it('shows the pending request card with Yes/No that resolve through the controller', () => {
    act(() =>
      useVoiceStore.getState().setPendingRequest({
        kind: 'approval_request',
        toolUseId: 'tu1',
        action: 'command',
        category: 'cli',
        summary: 'Run: pytest -q',
        question: '',
        options: [],
        riskHint: 'medium',
        detail: 'pytest -q',
      }),
    );
    render(<VoiceLiveMessage />);
    expect(screen.getByText('Approval needed')).toBeInTheDocument();
    expect(screen.getByText('Run: pytest -q')).toBeInTheDocument();
    expect(screen.getByText(/Say yes or no/)).toBeInTheDocument();
    fireEvent.click(screen.getByText('Yes'));
    expect(controller.resolveRequest).toHaveBeenCalledWith('approve');
    fireEvent.click(screen.getByText('No'));
    expect(controller.resolveRequest).toHaveBeenCalledWith('deny');
  });
});
