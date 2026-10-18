/**
 * A plain path mention in rendered markdown is an anchor to the file.
 *
 * utils/pathLinks.ts turns the mention into a #wsfile= link before marked
 * runs. DOMPurify must keep the href, so wsFileLinks.ts can open the file at
 * the cited lines; code stays code, a write result's link keeps opening with
 * the OS app, and with no workspace connected, or in a file's own preview,
 * nothing changes.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { MarkdownRenderer } from './MarkdownRenderer';
import { StreamingMarkdown } from './StreamingMarkdown';
import { useUIStore } from '@/stores/uiStore';
import { parseWsFileHref } from '@/utils/wsFileLinks';

const WS = '/Users/me/repo';

afterEach(() => {
  useUIStore.setState({ wsPath: '' });
});

describe('path mentions in rendered markdown', () => {
  it('renders a path mention as an anchor whose href survives DOMPurify', () => {
    useUIStore.setState({ wsPath: WS });
    const { container } = render(
      <MarkdownRenderer content="The leg is in src/hooks/chatStream/sseStream.ts:282, not `src/a.ts`." />,
    );
    const anchors = container.querySelectorAll('a');
    expect(anchors).toHaveLength(1);
    expect(anchors[0].textContent).toBe('src/hooks/chatStream/sseStream.ts:282');
    const href = anchors[0].getAttribute('href') ?? '';
    expect(href).toBe(`#wsfile=${WS}/src/hooks/chatStream/sseStream.ts&L=282-282`);
    expect(parseWsFileHref(href)).toEqual({
      path: `${WS}/src/hooks/chatStream/sseStream.ts`,
      startLine: 282,
      endLine: 282,
    });
    expect(container.querySelector('code')?.textContent).toBe('src/a.ts');
  });

  it('shows a name with underscores as written', () => {
    useUIStore.setState({ wsPath: WS });
    const { container } = render(<MarkdownRenderer content="See server/prompts/__init__.py." />);
    const anchor = container.querySelector('a');
    expect(anchor?.textContent).toBe('server/prompts/__init__.py');
    expect(container.querySelector('strong')).toBeNull();
  });

  it('keeps the write result link opening with the OS app', () => {
    useUIStore.setState({ wsPath: WS });
    const link = `[src/a.ts](#wsfile=${WS}/src/a.ts&open=os)`;
    const { container } = render(<MarkdownRenderer content={`| File |\n|---|\n| ${link} |`} />);
    const anchors = container.querySelectorAll('a');
    expect(anchors).toHaveLength(1);
    expect(anchors[0].getAttribute('href')).toBe(`#wsfile=${WS}/src/a.ts&open=os`);
  });

  it('leaves the paths of a file preview as text, since they read against its folder', () => {
    useUIStore.setState({ wsPath: WS });
    const { container } = render(
      <MarkdownRenderer content="See images/diagram.png and src/a.ts:3." linkPaths={false} />,
    );
    expect(container.querySelector('a')).toBeNull();
    expect(container.textContent).toContain('images/diagram.png and src/a.ts:3');
  });

  it('renders the text as before with no workspace connected', () => {
    const { container } = render(<MarkdownRenderer content="The leg is in src/hooks/a.ts:282." />);
    expect(container.querySelector('a')).toBeNull();
    expect(container.textContent).toContain('src/hooks/a.ts:282');
  });

  it('links the mention while the reply streams', () => {
    useUIStore.setState({ wsPath: WS });
    const { container } = render(
      <StreamingMarkdown content="Reading server/goals/gate.py:40-44 to see how" isStreaming />,
    );
    const anchor = container.querySelector('a');
    expect(anchor?.getAttribute('href')).toBe(`#wsfile=${WS}/server/goals/gate.py&L=40-44`);
  });
});
