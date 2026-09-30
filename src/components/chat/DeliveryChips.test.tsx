import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';

const post = vi.fn();
vi.mock('@/api/client', () => ({
  get: vi.fn(),
  put: vi.fn(),
  post: (...args: unknown[]) => post(...args),
  del: vi.fn(),
  patch: vi.fn(),
}));

import { DeliveryChips } from './DeliveryChips';
import { ChatMessage } from './ChatMessage';
import { StreamingMessage } from './StreamingMessage';
import { dropRuntime, getChatStore } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import type { ChatMessage as ChatMessageType, Delivery } from '@/types/chat';

/**
 * The "Verified" row under a reply: one chip per delivery the server
 * confirmed, drawn with its kind's glyph, label and detail. A file chip opens
 * the file through the chat's own #wsfile handler, a web chip opens its page
 * in a new browser tab, and a chip without a link is a record, never a
 * control.
 */
const REPORT: Delivery = {
  kind: 'file',
  target: '/Users/me/Downloads/report.html',
  label: 'report.html',
  detail: '12.4 KB, saved 19:31',
  href: '#wsfile=%2FUsers%2Fme%2FDownloads%2Freport.html&open=os',
};
const PR: Delivery = {
  kind: 'pr',
  target: 'https://github.com/acme/app/pull/12',
  label: 'PR #12',
  detail: 'Add the export button',
  href: 'https://github.com/acme/app/pull/12',
};
const PUSH: Delivery = { kind: 'push', target: 'main', label: 'main', detail: 'to origin/main' };

const chipOf = (label: string) => screen.getByText(label).closest('.delivery-chip') as HTMLElement;
const glyphOf = (label: string) => chipOf(label).querySelector('.delivery-glyph')?.innerHTML;

beforeEach(() => {
  post.mockReset();
  post.mockResolvedValue({});
});

describe('DeliveryChips', () => {
  it('opens a file chip through the #wsfile handler: a click opens it, Cmd-click reveals it', () => {
    render(<DeliveryChips deliveries={[REPORT]} />);
    const link = screen.getByRole('link', { name: /report\.html/ });
    expect(link).toHaveAttribute('href', REPORT.href);
    expect(link).not.toHaveAttribute('target');

    fireEvent.click(link);
    expect(post).toHaveBeenLastCalledWith('/api/workspace/open-with', { path: REPORT.target });

    fireEvent.click(link, { metaKey: true });
    expect(post).toHaveBeenLastCalledWith('/api/workspace/reveal', { path: REPORT.target });
  });

  it('opens a web chip in a new browser tab, never in the app window', () => {
    render(<DeliveryChips deliveries={[PR]} />);
    const link = screen.getByRole('link', { name: /PR #12/ });
    expect(link).toHaveAttribute('href', PR.href);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link.getAttribute('rel')?.split(' ')).toEqual(expect.arrayContaining(['noopener', 'noreferrer']));
  });

  it('renders a chip without a link as a record: label and detail, no link, no button', () => {
    render(<DeliveryChips deliveries={[PUSH]} />);
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
    const chip = chipOf('main');
    expect(within(chip).getByText('to origin/main')).toHaveClass('delivery-detail');
  });

  it('never follows an href that is neither a file link nor a web URL', () => {
    const odd: Delivery = { kind: 'link', target: 'share', label: 'Share link', href: 'javascript:alert(1)' };
    render(<DeliveryChips deliveries={[odd]} />);
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.getByText('Share link')).toBeInTheDocument();
  });

  it('draws each known kind with its own glyph and gives an unknown kind the generic one', () => {
    render(
      <DeliveryChips
        deliveries={[
          REPORT,
          PUSH,
          { kind: 'fax', target: '+48 22 555 01 01', label: 'Fax to the office' },
          { kind: 'telex', target: 'TLX 1234', label: 'Telex to the port' },
        ]}
      />,
    );
    expect(glyphOf('report.html')).not.toBe(glyphOf('main'));
    expect(glyphOf('Fax to the office')).toBeTruthy();
    expect(glyphOf('Fax to the office')).toBe(glyphOf('Telex to the port'));
    expect(glyphOf('Fax to the office')).not.toBe(glyphOf('report.html'));
  });

  it('keeps the chips in order under one Verified caption', () => {
    render(<DeliveryChips deliveries={[REPORT, PR, PUSH]} />);
    const row = screen.getByRole('group', { name: 'Verified deliveries' });
    expect(within(row).getAllByText('Verified')).toHaveLength(1);
    const labels = [...row.querySelectorAll('.delivery-label')].map((el) => el.textContent);
    expect(labels).toEqual(['report.html', 'PR #12', 'main']);
  });

  it('names the whole target and where a click goes on hover', () => {
    render(<DeliveryChips deliveries={[REPORT, PUSH]} />);
    const title = chipOf('report.html').getAttribute('title') ?? '';
    expect(title).toContain(REPORT.target);
    expect(title).toMatch(/default app/);
    // A label that already is the whole target repeats nothing.
    expect(chipOf('main')).not.toHaveAttribute('title');
  });

  it('renders nothing when nothing real was verified', () => {
    const junk = [{ kind: 'file' }, null] as unknown as Delivery[];
    const { container } = render(<DeliveryChips deliveries={junk} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('the Verified row in a conversation', () => {
  const reply: ChatMessageType = {
    role: 'assistant',
    content: 'Saved report.html to Downloads.',
    timestamp: '2026-09-29T19:31:05.000Z',
    deliveries: [REPORT],
  };

  it('sits under the reply text, above its timestamp', () => {
    const { container } = render(<ChatMessage message={reply} index={1} />);
    const text = container.querySelector('.markdown-content');
    const row = container.querySelector('.delivery-row');
    const time = container.querySelector('.msg-timestamp');
    expect(text && row && time).toBeTruthy();
    expect(text!.compareDocumentPosition(row!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(row!.compareDocumentPosition(time!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('never shows on a user message', () => {
    const asked: ChatMessageType = { ...reply, role: 'user', content: 'Save the report' };
    const { container } = render(<ChatMessage message={asked} index={0} />);
    expect(container.querySelector('.delivery-row')).toBeNull();
  });

  describe('while the reply streams', () => {
    const sid = 'sess-live-chips';
    afterEach(() => {
      dropRuntime(sid);
      useSessionStore.setState({ currentSessionId: null });
    });

    it('shows each delivery as soon as it is verified', () => {
      useSessionStore.setState({ currentSessionId: sid });
      const chat = getChatStore(sid);
      chat.getState().setStreaming(true);
      chat.getState().addLiveDeliveries([REPORT]);
      render(<StreamingMessage content="Saved report.html to Downloads." isStreaming />);
      expect(screen.getByRole('link', { name: /report\.html/ })).toHaveAttribute('href', REPORT.href);
    });
  });
});
