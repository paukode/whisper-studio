import { marked } from 'marked';
import type { ChatMessage as ChatMessageType, CronEventPayload } from '@/types/chat';
import { downloadFile } from '@/utils/downloadFile';

/** Rescue cron_event rows persisted under the older flat shape. Returns
 *  null if the row doesn't carry the minimum cron fields. */
export function extractFlatCronPayload(m: ChatMessageType): CronEventPayload | null {
  const r = m as unknown as Record<string, unknown>;
  const ev = r['event_type'];
  if (ev !== 'cron_created' && ev !== 'cron_fired' && ev !== 'cron_deleted') {
    return null;
  }
  return {
    event_type: ev,
    cron_id: String(r['cron_id'] ?? ''),
    cron_name: String(r['cron_name'] ?? ''),
    run_id: typeof r['run_id'] === 'string' ? (r['run_id'] as string) : undefined,
    status: r['status'] === 'ok' || r['status'] === 'failed'
      ? (r['status'] as 'ok' | 'failed')
      : undefined,
    text: typeof r['text'] === 'string' ? (r['text'] as string) : undefined,
    interval_minutes: typeof r['interval_minutes'] === 'number'
      ? (r['interval_minutes'] as number)
      : undefined,
    timestamp: String(r['timestamp'] ?? m.timestamp ?? new Date().toISOString()),
  };
}

/** The prose of an assistant turn: every row that has any, a blank line
 *  between them. What Copy takes and what an export calls the response. */
export function turnText(rows: readonly ChatMessageType[]): string {
  return rows
    .map((r) => r.content ?? '')
    .filter((c) => c.trim())
    .join('\n\n');
}

/**
 * Export one assistant turn as markdown with full metadata: the thinking,
 * the tools and the prose of every row the turn committed.
 */
export function exportTurn(rows: readonly ChatMessageType[]): void {
  if (rows.length === 0) return;
  let md = `# Assistant Response\n\n`;
  md += `**Time:** ${rows[rows.length - 1].timestamp}\n\n`;

  for (const message of rows) {
    if (message._thinkingText) {
      md += `## Thinking (${((message._thinkingMs ?? 0) / 1000).toFixed(1)}s)\n\n`;
      md += `${message._thinkingText}\n\n`;
    }
  }

  const tools = rows.flatMap((m) => m.toolUse ?? []);
  if (tools.length) {
    md += `## Tools Used\n\n`;
    for (const tool of tools) {
      md += `- **${tool.toolName}** (${tool.status})\n`;
      if (tool.result) md += `  \`\`\`\n  ${tool.result.slice(0, 500)}\n  \`\`\`\n`;
    }
    md += '\n';
  }

  for (const message of rows) {
    if (message.userQuestion) {
      md += `## User Question\n\n`;
      md += `**Q:** ${message.userQuestion.question}\n`;
      md += `**Options:** ${message.userQuestion.options.join(', ')}\n\n`;
    }
  }

  md += `## Response\n\n${turnText(rows)}\n\n`;

  const usage = rows.reduce(
    (acc, m) => (m._usage
      ? { input: acc.input + m._usage.input_tokens, output: acc.output + m._usage.output_tokens, seen: true }
      : acc),
    { input: 0, output: 0, seen: false },
  );
  if (usage.seen) {
    md += `---\n*Tokens: ${usage.input.toLocaleString()} in / ${usage.output.toLocaleString()} out*\n`;
  }

  downloadFile(md, `message-${Date.now()}.md`, 'text/markdown');
}

/**
 * Copy assistant message content as rich text (HTML) to clipboard.
 *
 * Defensive against undefined/null content: legacy chat_history rows or
 * upstream bugs (e.g. a cron_event row that lost its payload) can leave
 * `content` unset, and `marked.parse(undefined)` throws. Treat null/undef
 * as empty string so the click never crashes the page.
 */
export async function copyRichText(content: string | undefined | null): Promise<void> {
  const safe = content ?? '';
  const html = marked.parse(safe, { async: false }) as string;
  const htmlBlob = new Blob([html], { type: 'text/html' });
  const textBlob = new Blob([safe], { type: 'text/plain' });
  await navigator.clipboard.write([
    new ClipboardItem({
      'text/html': htmlBlob,
      'text/plain': textBlob,
    }),
  ]);
}
