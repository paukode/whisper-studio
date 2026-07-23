import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent } from '@testing-library/react';

// Hoisted so the vi.mock factory below can close over the same spy the test
// asserts against.
const { branchSessionMock, clearChatMock } = vi.hoisted(() => ({
  branchSessionMock: vi.fn(() => Promise.resolve()),
  clearChatMock: vi.fn(),
}));

const CURRENT_SESSION_ID = 'sess-1';

// --- Store mocks: each zustand hook is invoked as useX((s) => s.field), so
// the mock is a plain function that applies the caller's selector to a fake
// state snapshot. This keeps the test hermetic (no real stores / network).
vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: (selector: (s: unknown) => unknown) =>
    selector({
      currentSessionId: CURRENT_SESSION_ID,
      clearChat: clearChatMock,
      branchSession: branchSessionMock,
    }),
}));

vi.mock('@/stores/sessionRuntimes', () => ({
  useActiveChatStore: (selector: (s: unknown) => unknown) =>
    selector({
      messages: [{ role: 'assistant', content: 'hi', timestamp: 1 }],
      isStreaming: false,
      currentStreamContent: '',
    }),
  getActiveChatStore: () => ({
    getState: () => ({ clearMessages: vi.fn(), resetSessionApprovals: vi.fn() }),
  }),
  getChatStore: () => ({ getState: () => ({ isStreaming: false }) }),
  getActiveTranscriptionStore: () => ({
    getState: () => ({ segments: [], speakerNames: {} }),
  }),
}));

vi.mock('@/stores/settingsStore', () => ({
  useSettingsStore: (selector: (s: unknown) => unknown) =>
    selector({ config: { permissionMode: 'default' } }),
}));

vi.mock('@/stores/uiStore', () => ({
  useUIStore: Object.assign(
    (selector: (s: unknown) => unknown) => selector({ wsConnected: false }),
    { getState: () => ({ addToast: vi.fn(), openWorkspaceConnect: vi.fn(), openSettings: vi.fn() }) },
  ),
}));

vi.mock('@/stores/taskStore', () => ({
  useTaskStore: Object.assign(
    (selector: (s: unknown) => unknown) => selector({ tasksBySession: {} }),
    { getState: () => ({ setTasks: vi.fn() }) },
  ),
}));

// Reject so the hydrate effect's success branch never runs (no session-task
// wiring needed); the .catch in the component swallows it.
vi.mock('@/api/tasks', () => ({
  fetchSessionTasks: vi.fn(() => Promise.reject(new Error('noop'))),
}));

// Child components are irrelevant to the Branch wiring — stub them out.
vi.mock('./ChatMessage', () => ({ ChatMessage: () => null }));
vi.mock('./StreamingMessage', () => ({ StreamingMessage: () => null }));
vi.mock('./ChatInput', () => ({ ChatInput: () => null }));
vi.mock('./ApprovalBanner', () => ({ ApprovalBanner: () => null }));
vi.mock('./TaskCard', () => ({ computeTaskCheckpoints: () => new Map() }));

import { ChatPanel } from './ChatPanel';

describe('ChatPanel — Branch button wiring', () => {
  beforeEach(() => {
    branchSessionMock.mockClear();
  });

  it('branches the current session when the header Branch button is clicked', () => {
    render(<ChatPanel />);
    const btn = document.getElementById('chatBranchBtn') as HTMLButtonElement | null;
    expect(btn).not.toBeNull();
    expect(btn!.disabled).toBe(false);

    fireEvent.click(btn!);

    expect(branchSessionMock).toHaveBeenCalledTimes(1);
    expect(branchSessionMock).toHaveBeenCalledWith(CURRENT_SESSION_ID);
  });
});
