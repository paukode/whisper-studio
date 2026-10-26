import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MoreMenu } from './MoreMenu';
import { useSettingsStore } from '@/stores/settingsStore';

function renderMenu() {
  render(
    <MoreMenu
      open
      section={null}
      setSection={() => {}}
      onToggle={() => {}}
      onClose={() => {}}
      indexes={[]}
      selectedIndexes={[]}
      toggleIndex={() => {}}
      wsConnected={false}
      onInsertSkill={() => {}}
    />,
  );
}

describe('MoreMenu Memory row', () => {
  beforeEach(() => {
    useSettingsStore.setState({ autoMemory: true, autoMemoryNote: null });
  });

  it('reads On when memories are recorded and recalled', () => {
    renderMenu();
    const row = screen.getByText('Memory').closest('button')!;
    expect(row).toHaveTextContent('On');
  });

  it('reads Recall only, with the reason, when the mode records nothing', () => {
    const note = 'Local mode recalls saved memories but records no new ones.';
    useSettingsStore.setState({ autoMemoryNote: note });
    renderMenu();
    const row = screen.getByText('Memory').closest('button')!;
    expect(row).toHaveTextContent('Recall only');
    expect(row.getAttribute('title')).toContain(note);
  });
});

describe('MoreMenu count badge', () => {
  it('counts the hidden settings that are switched on', () => {
    useSettingsStore.setState({ autoMemory: true, autoMemoryNote: null });
    render(
      <MoreMenu
        open={false}
        section={null}
        setSection={() => {}}
        onToggle={() => {}}
        onClose={() => {}}
        indexes={[{ path: '/a', name: 'a', files: 1, chunks: 1 }, { path: '/b', name: 'b', files: 1, chunks: 1 }] as never}
        selectedIndexes={['/a']}
        toggleIndex={() => {}}
        wsConnected={false}
        onInsertSkill={() => {}}
      />,
    );
    expect(screen.getByTestId('more-badge')).toHaveTextContent('2');
    expect(screen.getByRole('button', { name: /More controls, 2 on/ })).toBeTruthy();
  });

  it('shows no badge when nothing hidden is on', () => {
    useSettingsStore.setState({ autoMemory: false, autoMemoryNote: null });
    renderMenu();
    expect(screen.queryByTestId('more-badge')).toBeNull();
  });
});
