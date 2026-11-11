import { get } from './client';

/** An agent type the composer offers as `@agent-<name>`. */
export interface AgentTypeInfo {
  name: string;
  description: string;
}

/** The agent types for the connected workspace (server/agents/routes.py):
 *  the built-in presets and the custom types under `.whisper/agents/`. */
export async function fetchAgentTypes(): Promise<AgentTypeInfo[]> {
  const data = await get<{ types: AgentTypeInfo[] }>('/api/agents/types');
  return Array.isArray(data.types) ? data.types : [];
}
