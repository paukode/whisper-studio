/**
 * Deliveries the server verified (`deliveries` SSE frames): a file saved, a
 * push, a PR opened, a message sent. A delivery is identified by kind +
 * target, so a later report of the same one (the file saved again, with a
 * new size) replaces the earlier report where it stands, and the chips keep
 * the order they first appeared in.
 */
import type { Delivery } from '@/types/chat';

const OPTIONAL_FIELDS = ['detail', 'href', 'at'] as const;

/** One frame item or stored row as a Delivery, or null when it is not one.
 *  Nothing here trusts the shape: a frame that fails validation still
 *  reaches the client raw, and a stored row outlives the code that wrote
 *  it. An optional field that is null, empty or not a string is left out. */
export function toDelivery(raw: unknown): Delivery | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.kind !== 'string' || typeof r.target !== 'string' || typeof r.label !== 'string') {
    return null;
  }
  // Nothing to show: a chip reads its label, or the target when unlabelled.
  if (!r.label && !r.target) return null;
  const delivery: Delivery = { kind: r.kind, target: r.target, label: r.label };
  for (const field of OPTIONAL_FIELDS) {
    const value = r[field];
    if (typeof value === 'string' && value) delivery[field] = value;
  }
  return delivery;
}

function deliveryKey(d: Delivery): string {
  return `${d.kind}\n${d.target}`;
}

/** `existing` with `incoming` merged in: a new kind + target is appended, a
 *  known one is replaced in place. Items that are not deliveries are
 *  skipped, so a malformed frame never costs the chips already shown. */
export function mergeDeliveries(
  existing: readonly Delivery[] | undefined,
  incoming: unknown,
): Delivery[] {
  const byKey = new Map<string, Delivery>();
  const add = (raw: unknown) => {
    const delivery = toDelivery(raw);
    if (delivery) byKey.set(deliveryKey(delivery), delivery);
  };
  (existing ?? []).forEach(add);
  if (Array.isArray(incoming)) incoming.forEach(add);
  return [...byKey.values()];
}
