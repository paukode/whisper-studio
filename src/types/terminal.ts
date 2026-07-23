export interface TerminalSession {
  id: string;
  label: string;
  cwd?: string;
  isConnected: boolean;
}
