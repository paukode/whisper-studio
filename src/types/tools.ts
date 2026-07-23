export interface Skill {
  name: string;
  description: string;
  content: string;
  enabled: boolean;
  isFolder?: boolean;
  hasScripts?: boolean;
  trusted?: boolean;
}

export interface MCPTool {
  name: string;
  description: string;
  serverName: string;
  inputSchema: Record<string, unknown>;
}
