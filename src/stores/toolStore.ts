import { create } from 'zustand';
import { get } from '@/api/client';
import type { Skill, MCPTool } from '@/types/tools';
import { useUIStore } from './uiStore';

export interface ToolState {
  skills: Skill[];
  mcpTools: MCPTool[];
  isLoading: boolean;
  // Actions
  fetchSkills: () => Promise<void>;
  fetchMCPTools: () => Promise<void>;
}

export const useToolStore = create<ToolState>()((set) => ({
  skills: [],
  mcpTools: [],
  isLoading: false,

  fetchSkills: async () => {
    set({ isLoading: true });
    try {
      const data = await get<{ skills: Array<{ name: string; description: string; content: string; enabled: boolean }> }>('/api/skills');
      const skills: Skill[] = (data.skills ?? []).map(s => ({
        name: s.name,
        description: s.description ?? '',
        content: s.content ?? '',
        enabled: s.enabled ?? true,
      }));
      set({ skills });
    } catch (err) {
      console.warn('Failed to fetch skills:', err);
      useUIStore.getState().addToast({
        type: 'error',
        message: 'Failed to fetch skills',
        duration: 4000,
      });
    } finally {
      set({ isLoading: false });
    }
  },

  fetchMCPTools: async () => {
    set({ isLoading: true });
    try {
      const data = await get<{ skills: unknown[]; mcpTools: Array<{ name: string; description: string; server: string }> }>('/api/skills');
      const mcpTools: MCPTool[] = (data.mcpTools ?? []).map(t => ({
        name: t.name,
        description: t.description ?? '',
        serverName: t.server ?? '',
        inputSchema: {},
      }));
      set({ mcpTools });
    } catch (err) {
      console.warn('Failed to fetch MCP tools:', err);
      useUIStore.getState().addToast({
        type: 'error',
        message: 'Failed to fetch MCP tools',
        duration: 4000,
      });
    } finally {
      set({ isLoading: false });
    }
  },
}));
