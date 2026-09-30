import React, { useEffect, useRef } from 'react';
import type { Delivery } from '@/types/chat';
import { toDelivery } from '@/hooks/chatStream/deliveries';
import { attachWsFileHandlers, chatLinkHint, parseWsFileHref } from '@/utils/wsFileLinks';
import { ICON } from '@/components/chat/ActivityRow';

/** The Activity row's stroke style, for the few kinds it has no glyph for. */
const glyph = (children: React.ReactNode) => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {children}
  </svg>
);

const CHECK = glyph(<polyline points="20 6 9 17 4 12" />);

/** Each kind drawn the way the Activity row draws the tool that delivers it:
 *  a push with git_push's arrow, a schedule with cron's clock, a removal with
 *  the delete cross. A Map, so a kind named like an Object property can never
 *  resolve to one. */
const KIND_GLYPHS = new Map<string, React.ReactNode>([
  ['file', ICON.file],
  ['folder', ICON.folder],
  ['removed', ICON.x],
  ['artifact', ICON.sparkle],
  ['push', ICON.arrowUp],
  ['commit', ICON.gitCommit],
  ['pr', glyph(<><circle cx="18" cy="18" r="3" /><circle cx="6" cy="6" r="3" /><path d="M13 6h3a2 2 0 0 1 2 2v7" /><line x1="6" y1="9" x2="6" y2="21" /></>)],
  ['merge', ICON.gitBranch],
  ['issue', glyph(<><circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="1.5" /></>)],
  ['upload', glyph(<><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="17 8 12 3 7 8" /><line x1="12" y1="3" x2="12" y2="15" /></>)],
  ['message', ICON.message],
  ['publish', ICON.globe],
  ['schedule', ICON.clock],
  ['link', ICON.link],
]);

/** A kind this client does not know yet still gets a chip, as a generic
 *  delivery. */
const GENERIC_GLYPH = ICON.box;

/** Where a chip leads. A `#wsfile=` link goes through the chat's own link
 *  handler (a click opens the file, Cmd-click reveals it in Finder); an
 *  http(s) URL opens in the browser. Any other href is shown, never followed. */
function linkOf(href: string | undefined): { href: string; external: boolean } | null {
  if (!href) return null;
  if (parseWsFileHref(href)) return { href, external: false };
  if (/^https?:\/\//i.test(href)) return { href, external: true };
  return null;
}

const DeliveryChip: React.FC<{ delivery: Delivery }> = ({ delivery }) => {
  const label = delivery.label || delivery.target;
  const link = linkOf(delivery.href);
  // The label is short: hovering names the whole target (the full path, the
  // URL) and, for a routed link, where a click goes.
  const title = [delivery.target !== label ? delivery.target : '', link ? chatLinkHint(link.href) : '']
    .filter(Boolean)
    .join('\n') || undefined;
  const body = (
    <>
      <span className="delivery-glyph">{KIND_GLYPHS.get(delivery.kind) ?? GENERIC_GLYPH}</span>
      <span className="delivery-label">{label}</span>
      {delivery.detail && <span className="delivery-detail">{delivery.detail}</span>}
    </>
  );
  if (!link) {
    return <span className="delivery-chip" title={title}>{body}</span>;
  }
  if (link.external) {
    return (
      <a className="delivery-chip" href={link.href} target="_blank" rel="noopener noreferrer" title={title}>
        {body}
      </a>
    );
  }
  return <a className="delivery-chip" href={link.href} title={title}>{body}</a>;
};

export interface DeliveryChipsProps {
  deliveries: readonly Delivery[];
}

/**
 * "Verified": what the server confirmed a reply delivered (a file saved, a
 * push, a PR opened, a message sent), one quiet chip each under the text that
 * claimed it: the kind's glyph, the label, then the detail. A chip with a
 * link opens it the way the same link in the text would; one without is a
 * record only. Renders nothing when nothing was verified.
 */
const DeliveryChipsView: React.FC<DeliveryChipsProps> = ({ deliveries }) => {
  const rowRef = useRef<HTMLDivElement>(null);
  // A stored row outlives the code that wrote it: show real deliveries only.
  const shown = (Array.isArray(deliveries) ? deliveries : [])
    .map(toDelivery)
    .filter((d): d is Delivery => d !== null);
  const visible = shown.length > 0;

  useEffect(() => {
    if (!visible || !rowRef.current) return;
    return attachWsFileHandlers(rowRef.current);
  }, [visible]);

  if (!visible) return null;
  return (
    <div ref={rowRef} className="delivery-row" role="group" aria-label="Verified deliveries">
      <span className="delivery-caption">{CHECK}Verified</span>
      {shown.map((d, i) => (
        <DeliveryChip key={`${i}-${d.kind}-${d.target}`} delivery={d} />
      ))}
    </div>
  );
};

/** Memoized: the streaming bubble re-renders on every painted token, and its
 *  live deliveries only change when a frame arrives. */
export const DeliveryChips = React.memo(DeliveryChipsView);
