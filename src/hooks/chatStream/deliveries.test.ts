import { describe, expect, it } from 'vitest';
import { mergeDeliveries, toDelivery } from './deliveries';
import type { Delivery } from '@/types/chat';

const report: Delivery = {
  kind: 'file',
  target: '/Users/me/Downloads/report.html',
  label: 'report.html',
  detail: '12.4 KB, saved 19:31',
  href: '#wsfile=%2FUsers%2Fme%2FDownloads%2Freport.html&open=os',
};
const push: Delivery = { kind: 'push', target: 'main', label: 'main', detail: 'to origin/main' };
const pr: Delivery = {
  kind: 'pr',
  target: 'https://github.com/acme/app/pull/12',
  label: 'PR #12',
  href: 'https://github.com/acme/app/pull/12',
};

describe('mergeDeliveries', () => {
  it('appends deliveries in the order they first appear', () => {
    const merged = mergeDeliveries(mergeDeliveries(undefined, [report]), [push, pr]);
    expect(merged).toEqual([report, push, pr]);
  });

  it('lets a later report of the same kind and target replace the earlier one where it stands', () => {
    const resaved = { ...report, detail: '13.0 KB, saved 19:40' };
    const merged = mergeDeliveries([report, push], [resaved]);
    expect(merged).toEqual([resaved, push]);
  });

  it('dedupes within one frame too, the later item winning', () => {
    const resaved = { ...report, detail: '13.0 KB, saved 19:40' };
    expect(mergeDeliveries([], [report, push, resaved])).toEqual([resaved, push]);
  });

  it('treats the same target under another kind as another delivery', () => {
    const merged: Delivery = { ...pr, kind: 'merge', label: 'PR #12 merged' };
    expect(mergeDeliveries([pr], [merged])).toEqual([pr, merged]);
  });

  it('keeps a kind it does not know, as a delivery like any other', () => {
    const fax = { kind: 'fax', target: '+48 22 555 01 01', label: 'Fax to the office' };
    expect(mergeDeliveries([report], [fax])).toEqual([report, fax]);
  });

  it('skips what is not a delivery and keeps what was already merged', () => {
    const merged = mergeDeliveries([report], [
      null,
      'report.html',
      { kind: 'file', target: '/tmp/a.txt' },
      { kind: 'file', target: 42, label: 'a.txt' },
      { kind: 'file', target: '', label: '' },
      push,
    ]);
    expect(merged).toEqual([report, push]);
    expect(mergeDeliveries([report], { items: [push] })).toEqual([report]);
  });
});

describe('toDelivery', () => {
  it('reads a null, empty or non-string optional field as absent', () => {
    const d = toDelivery({ kind: 'message', target: 'ana@example.com', label: 'Email to Ana', detail: null, href: '', at: 7 });
    expect(d).toEqual({ kind: 'message', target: 'ana@example.com', label: 'Email to Ana' });
    expect(d && 'detail' in d).toBe(false);
  });

  it('keeps every field the frame declares and nothing else', () => {
    const d = toDelivery({ ...report, at: '2026-09-29T19:31:02+00:00', verifiedBy: 'stat' });
    expect(d).toEqual({ ...report, at: '2026-09-29T19:31:02+00:00' });
  });
});
