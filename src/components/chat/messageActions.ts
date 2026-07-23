import { marked } from 'marked';
import type { ChatMessage as ChatMessageType, CronEventPayload } from '@/types/chat';

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

/**
 * Export a single assistant message as markdown with full metadata.
 */
export function exportSingleMessage(message: ChatMessageType): void {
  let md = `# Assistant Response\n\n`;
  md += `**Time:** ${message.timestamp}\n\n`;

  if (message._thinkingText) {
    md += `## Thinking (${((message._thinkingMs ?? 0) / 1000).toFixed(1)}s)\n\n`;
    md += `${message._thinkingText}\n\n`;
  }

  if (message.toolUse?.length) {
    md += `## Tools Used\n\n`;
    for (const tool of message.toolUse) {
      md += `- **${tool.toolName}** (${tool.status})\n`;
      if (tool.result) md += `  \`\`\`\n  ${tool.result.slice(0, 500)}\n  \`\`\`\n`;
    }
    md += '\n';
  }

  if (message.userQuestion) {
    md += `## User Question\n\n`;
    md += `**Q:** ${message.userQuestion.question}\n`;
    md += `**Options:** ${message.userQuestion.options.join(', ')}\n\n`;
  }

  md += `## Response\n\n${message.content}\n\n`;

  if (message._usage) {
    md += `---\n*Tokens: ${message._usage.input_tokens.toLocaleString()} in / ${message._usage.output_tokens.toLocaleString()} out*\n`;
  }

  const blob = new Blob([md], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `message-${Date.now()}.md`;
  a.click();
  URL.revokeObjectURL(url);
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
