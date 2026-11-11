import { post } from './client';

/** Run an MCP prompt, `/<command> <args>` typed in the composer, and get the
 *  attachment that carries its text (server/mcp_routes.py). Rejects with the
 *  server's reason, for example a missing argument. */
export function runMcpPrompt(command: string, args: string): Promise<{ id: string; filename: string }> {
  return post<{ id: string; filename: string }>('/api/mcp/prompt', { command, arguments: args });
}
