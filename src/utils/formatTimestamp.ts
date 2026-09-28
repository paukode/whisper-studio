/**
 * Timestamp formatting utilities for chat messages and transcript segments.
 */

// Built once: toLocale*String constructs a new Intl formatter on every call,
// and a chat renders one timestamp per message.
const TIME_FORMAT = new Intl.DateTimeFormat([], { hour: 'numeric', minute: '2-digit' });
const WEEKDAY_FORMAT = new Intl.DateTimeFormat([], { weekday: 'short' });
const MONTH_DAY_FORMAT = new Intl.DateTimeFormat([], { month: 'short', day: 'numeric' });

/**
 * Format an ISO timestamp for display on chat messages.
 * - Today: "2:34 PM"
 * - Yesterday: "Yesterday 2:34 PM"
 * - This week: "Mon 2:34 PM"
 * - Older: "Jan 5 2:34 PM"
 */
export function formatMessageTimestamp(isoString: string): string {
  const date = new Date(isoString);
  if (isNaN(date.getTime())) return '';

  const now = new Date();
  const timeStr = TIME_FORMAT.format(date);

  // Same calendar day
  if (
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate()
  ) {
    return timeStr;
  }

  // Yesterday
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (
    date.getFullYear() === yesterday.getFullYear() &&
    date.getMonth() === yesterday.getMonth() &&
    date.getDate() === yesterday.getDate()
  ) {
    return `Yesterday ${timeStr}`;
  }

  // Within past 7 days
  const diffMs = now.getTime() - date.getTime();
  if (diffMs < 7 * 24 * 60 * 60 * 1000 && diffMs > 0) {
    const dayName = WEEKDAY_FORMAT.format(date);
    return `${dayName} ${timeStr}`;
  }

  // Older
  const dateStr = MONTH_DAY_FORMAT.format(date);
  return `${dateStr} ${timeStr}`;
}

/**
 * Format an epoch-ms timestamp for display on transcript segments.
 * Shows wall-clock time like "2:34:15 PM".
 *
 * Accepts legacy epoch-seconds values too. Any value below ~1e11 is treated
 * as seconds and promoted to milliseconds — this auto-heals segments saved
 * by an earlier version of Header.tsx that wrote `Date.now() / 1000`, which
 * otherwise rendered as a fixed 1970 timestamp.
 */
export function formatSegmentTimestamp(epochMs: number): string {
  if (!epochMs || epochMs <= 0) return '';
  const ms = epochMs < 1e11 ? epochMs * 1000 : epochMs;
  const date = new Date(ms);
  if (isNaN(date.getTime())) return '';
  return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' });
}
