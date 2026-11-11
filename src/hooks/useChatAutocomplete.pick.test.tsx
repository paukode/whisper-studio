/**
 * A pick from the `@` menu: a workspace file or folder becomes a chip (the
 * `@query` text goes away), and any other mention is inserted as text.
 */
import { renderHook, act } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('@/api/client', () => ({ get: vi.fn() }));

import { useChatAutocomplete } from './useChatAutocomplete';

function setup(text: string) {
  const textarea = document.createElement('textarea');
  textarea.value = text;
  document.body.appendChild(textarea);
  textarea.setSelectionRange(text.length, text.length);
  const setText = vi.fn();
  const onAttachWorkspacePath = vi.fn();
  const { result } = renderHook(() => useChatAutocomplete({
    text,
    setText,
    textareaRef: { current: textarea },
    slashCommands: [],
    skills: [],
    mcpPrompts: [],
    mcpResources: [],
    sessions: [],
    agents: [{ name: 'explore', description: 'Read-only' }],
    workspace: true,
    onAttachWorkspacePath,
  }));
  return { result, setText, onAttachWorkspacePath };
}

describe('picking from the @ menu', () => {
  it('attaches a folder as a chip and removes the typed @query', () => {
    const { result, setText, onAttachWorkspacePath } = setup('look at @comp');

    act(() => { result.current.computeAutocomplete('look at @comp', 13); });
    act(() => {
      result.current.selectAcItem({ icon: 'folder', name: 'src/components/', desc: '', insert: '', kind: 'folder', path: 'src/components' });
    });

    expect(onAttachWorkspacePath).toHaveBeenCalledWith('src/components', true);
    expect(setText).toHaveBeenCalledWith('look at ');
    expect(result.current.acVisible).toBe(false);
  });

  it('inserts an agent mention as text', () => {
    const { result, setText, onAttachWorkspacePath } = setup('ask @ag');

    act(() => { result.current.computeAutocomplete('ask @ag', 7); });
    const agent = result.current.acItems.find((i) => i.kind === 'agent')!;
    act(() => { result.current.selectAcItem(agent); });

    expect(setText).toHaveBeenCalledWith('ask @agent-explore ');
    expect(onAttachWorkspacePath).not.toHaveBeenCalled();
  });

  it('keeps the menu open on the file hint', () => {
    const { result, onAttachWorkspacePath } = setup('@');

    act(() => { result.current.computeAutocomplete('@', 1); });
    act(() => { result.current.selectAcItem(result.current.acItems[0]); });

    expect(result.current.acItems[0].kind).toBe('hint');
    expect(result.current.acVisible).toBe(true);
    expect(onAttachWorkspacePath).not.toHaveBeenCalled();
  });
});
