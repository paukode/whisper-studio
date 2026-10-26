import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

// settingsStore imports the api client; stub so rendering is inert.
vi.mock('@/api/client', () => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() }));

import { ThinkGroup, WorkGroup } from './ComposerGroups';
import { useSettingsStore } from '@/stores/settingsStore';
import { EFFORT_NOTES } from '@/utils/effortNotes';

const CLOUD_TIERS = ['low', 'medium', 'high', 'extra', 'max', 'ultracode'];

function setCloudModel(effortLevel = 'max', tiers: string[] = CLOUD_TIERS) {
  useSettingsStore.setState({
    models: [{ key: 'luna', name: 'GPT-6 Luna', effort_levels: tiers }] as never,
    selectedModel: 'luna',
    effortLevel,
    verbosity: 'medium',
  });
}

function renderThink(open = true) {
  return render(
    <ThinkGroup
      open={open}
      onToggle={vi.fn()}
      onClose={vi.fn()}
      modelOpen={false}
      onToggleModel={vi.fn()}
      onSelectModel={vi.fn()}
    />,
  );
}

function renderWork(props: Partial<React.ComponentProps<typeof WorkGroup>> = {}) {
  return render(
    <WorkGroup
      open
      onToggle={vi.fn()}
      onClose={vi.fn()}
      connected
      wsPath="/Users/me/code/whisper-studio"
      recentPaths={['/Users/me/code/whisper-studio', '/Users/me/code/other']}
      onBrowse={vi.fn()}
      onConnect={vi.fn()}
      permissionMode="default"
      onSelectMode={vi.fn()}
      onManage={vi.fn()}
      {...props}
    />,
  );
}

describe('ThinkGroup capsule', () => {
  beforeEach(() => setCloudModel());

  it('names the model, the thinking level and the answer length, each with its setting', () => {
    renderThink(false);
    expect(screen.getByTitle('Model: GPT-6 Luna')).toHaveTextContent('GPT-6 Luna');
    expect(screen.getByTitle('Thinking: Max')).toHaveTextContent('Max');
    expect(screen.getByTitle('Answers: Normal')).toHaveTextContent('Normal');
    expect(screen.queryByTestId('think-panel')).toBeNull();
  });

  it('drops the thinking segment for a model with no levels', () => {
    setCloudModel('high', []);
    renderThink(false);
    expect(screen.queryByTitle(/^Thinking:/)).toBeNull();
  });

  it('shows Off, and no answer length, for an on-device model', () => {
    useSettingsStore.setState({
      models: [{ key: 'gemma', name: 'Gemma 4', is_local: true, effort_levels: ['none', 'low', 'max'] }] as never,
      selectedModel: 'gemma',
      localThinkingLevel: 'none',
    });
    renderThink(false);
    expect(screen.getByTitle('Thinking: Off')).toBeTruthy();
    expect(screen.queryByTitle(/^Answers:/)).toBeNull();
  });
});

describe('ThinkGroup panel', () => {
  beforeEach(() => setCloudModel('max'));

  it('labels every row with what it controls', () => {
    renderThink();
    const panel = screen.getByTestId('think-panel');
    for (const text of ['Model', 'who answers', 'Thinking', 'how hard it works', 'Answers', 'reply length']) {
      expect(panel).toHaveTextContent(text);
    }
  });

  it('sets the effort level and explains it', () => {
    renderThink();
    expect(screen.getByTestId('effort-note')).toHaveTextContent(EFFORT_NOTES.max);
    fireEvent.click(screen.getByTestId('effort-option-low'));
    expect(useSettingsStore.getState().effortLevel).toBe('low');
    expect(screen.getByTestId('effort-option-low')).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByTestId('effort-note')).toHaveTextContent(/Light reasoning\. Fast and cheap/);
  });

  it('mentions speed or cost in every effort note', () => {
    for (const note of Object.values(EFFORT_NOTES)) expect(note).toMatch(/fast|slow|speed/i);
  });

  it('sets the answer length', () => {
    renderThink();
    fireEvent.click(screen.getByTestId('verbosity-option-high'));
    expect(useSettingsStore.getState().verbosity).toBe('high');
    fireEvent.click(screen.getByTestId('verbosity-option-low'));
    expect(useSettingsStore.getState().verbosity).toBe('low');
  });
});

describe('WorkGroup capsule', () => {
  it('shows the workspace folder name and the friendly mode label', () => {
    renderWork({ open: false });
    expect(screen.getByTitle('/Users/me/code/whisper-studio')).toHaveTextContent('whisper-studio');
    expect(screen.getByTitle('Permission mode: Asks first')).toHaveTextContent('Asks first');
  });

  it('says so when no workspace is connected', () => {
    renderWork({ open: false, connected: false, wsPath: '' });
    expect(screen.getByTitle('No workspace connected')).toHaveTextContent('No workspace');
  });

  it('tints risky modes amber and red, and safe ones green', () => {
    const { rerender } = renderWork({ open: false, permissionMode: 'acceptEdits' });
    expect(screen.getByTitle('Permission mode: Accept edits').className).toContain('tone-warn');
    rerender(<WorkGroup open={false} onToggle={vi.fn()} onClose={vi.fn()} connected={false} wsPath="" recentPaths={[]}
      onBrowse={vi.fn()} onConnect={vi.fn()} permissionMode="bypassPermissions" onSelectMode={vi.fn()} onManage={vi.fn()} />);
    expect(screen.getByTitle('Permission mode: Bypass').className).toContain('tone-danger');
    rerender(<WorkGroup open={false} onToggle={vi.fn()} onClose={vi.fn()} connected={false} wsPath="" recentPaths={[]}
      onBrowse={vi.fn()} onConnect={vi.fn()} permissionMode="default" onSelectMode={vi.fn()} onManage={vi.fn()} />);
    expect(screen.getByTitle('Permission mode: Asks first').className).toContain('tone-safe');
  });
});

describe('WorkGroup panel', () => {
  it('lists every mode with its explanation, keyed by value', () => {
    renderWork();
    for (const value of ['default', 'auto', 'plan', 'acceptEdits', 'bypassPermissions', 'dontAsk']) {
      expect(screen.getByTestId(`mode-option-${value}`)).toBeTruthy();
    }
    expect(screen.getByTestId('mode-option-plan')).toHaveTextContent(/changes come back as a proposal/);
    expect(screen.getByTestId('mode-option-default')).toHaveAttribute('aria-pressed', 'true');
  });

  it('selecting Plan calls onSelectMode with "plan" once', () => {
    const onSelectMode = vi.fn();
    renderWork({ onSelectMode });
    fireEvent.click(screen.getByTestId('mode-option-plan'));
    expect(onSelectMode).toHaveBeenCalledWith('plan');
    expect(onSelectMode).toHaveBeenCalledTimes(1);
  });

  it('offers Browse and the other recent folders, not the connected one', () => {
    const onConnect = vi.fn();
    const onBrowse = vi.fn();
    renderWork({ onConnect, onBrowse });
    expect(screen.queryByTestId('workspace-option-/Users/me/code/whisper-studio')).toBeNull();
    fireEvent.click(screen.getByTestId('workspace-option-/Users/me/code/other'));
    expect(onConnect).toHaveBeenCalledWith('/Users/me/code/other');
    fireEvent.click(screen.getByTestId('workspace-option-browse'));
    expect(onBrowse).toHaveBeenCalledTimes(1);
  });

  it('keeps Manage rules as its own link', () => {
    const onManage = vi.fn();
    renderWork({ onManage });
    fireEvent.click(screen.getByTestId('mode-option-manage'));
    expect(onManage).toHaveBeenCalledTimes(1);
  });
});
