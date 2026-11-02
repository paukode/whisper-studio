import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { WordViewer } from './WordViewer';

/**
 * The Word viewer offers no in-app editor: its old Edit button opened an
 * empty text editor, and Cmd+S then saved plain text over the .docx. It
 * opens the document in its default app instead.
 */

vi.mock('mammoth', () => ({
  convertToHtml: async () => ({ value: '<p>Hello from the document</p>' }),
}));

function renderViewer(onOpenInDefaultApp?: () => void) {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(new ArrayBuffer(4), { status: 200 }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <WordViewer filePath="docs/report.docx" onOpenInDefaultApp={onOpenInDefaultApp} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('WordViewer', () => {
  it('offers Open in default app and no Edit', async () => {
    const open = vi.fn();
    renderViewer(open);

    expect(await screen.findByText('Hello from the document')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Open in default app' }));
    expect(open).toHaveBeenCalledTimes(1);
  });

  it('shows no open button where no handler is given (the dock preview)', async () => {
    renderViewer();

    expect(await screen.findByText('Hello from the document')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Open in default app' })).toBeNull();
  });
});
