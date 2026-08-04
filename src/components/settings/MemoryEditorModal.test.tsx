import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryEditorModal } from './MemoryEditorModal';

vi.mock('@/api/client', () => ({
  get: vi.fn(() => Promise.resolve({ content: '# Project Memory\n' })),
  put: vi.fn(() => Promise.resolve({ saved: true })),
  del: vi.fn(() => Promise.resolve({ deleted: true })),
}));

function renderModal(props: Partial<React.ComponentProps<typeof MemoryEditorModal>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryEditorModal isOpen onClose={vi.fn()} {...props} />
    </QueryClientProvider>,
  );
}

describe('MemoryEditorModal — backdrop dismissal', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders nothing when closed', () => {
    const qc = new QueryClient();
    const { container } = render(
      <QueryClientProvider client={qc}>
        <MemoryEditorModal isOpen={false} onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    expect(container.firstChild).toBeNull();
  });

  it('closes on a genuine backdrop press (mousedown + click on the overlay)', () => {
    const onClose = vi.fn();
    const { container } = renderModal({ onClose });
    const overlay = container.querySelector('.settings-overlay') as HTMLElement;
    expect(overlay).toBeTruthy();

    fireEvent.mouseDown(overlay);
    fireEvent.click(overlay);

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does NOT close when a selection starts inside the content and releases on the backdrop', () => {
    const onClose = vi.fn();
    const { container } = renderModal({ onClose });
    const overlay = container.querySelector('.settings-overlay') as HTMLElement;
    const heading = screen.getByRole('heading', { name: /project memory/i });

    // Press begins on the content; the resulting click (on release outside)
    // targets the overlay — the historical "select text, release outside" bug.
    fireEvent.mouseDown(heading);
    fireEvent.click(overlay);

    expect(onClose).not.toHaveBeenCalled();
  });

  it('closes on the × button and on Escape', () => {
    const onClose = vi.fn();
    renderModal({ onClose });
    fireEvent.click(screen.getByRole('button', { name: '×' }));
    expect(onClose).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
