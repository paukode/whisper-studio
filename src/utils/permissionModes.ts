/**
 * Canonical permission-mode metadata, shared by every surface that renders a
 * mode name: the composer Mode dropdown, the app status bar, the chat panel's
 * workspace indicator, the command palette, and Settings → Permissions.
 * `risk` drives the caution tint (amber = writes auto-approved, red = no
 * prompts at all); `help` describes what the backend actually enforces
 * (server/security/permissions.py).
 */
export interface PermissionModeInfo {
  value: string;
  label: string;
  risk?: 'warn' | 'danger';
  help: string;
}

export const PERMISSION_MODES: PermissionModeInfo[] = [
  { value: 'default', label: 'Default',
    help: 'Reads are allowed. Every file write and command asks first.' },
  { value: 'auto', label: 'Auto',
    help: 'Reads are allowed. An AI classifier approves or asks for each write and command.' },
  { value: 'plan', label: 'Plan',
    help: 'Read only. File edits and commands are blocked; changes come back as a proposal.' },
  { value: 'acceptEdits', label: 'Accept edits', risk: 'warn',
    help: 'File writes and creates run without asking. Deletes and commands still ask.' },
  { value: 'bypassPermissions', label: 'Bypass', risk: 'danger',
    help: 'Everything runs immediately. No prompts, no review. Use only when you fully trust the task.' },
  { value: 'dontAsk', label: "Don't ask",
    help: 'Writes are silently denied. Nothing will prompt you; only read-only tools run.' },
];

/** Friendly label for a mode value ("bypassPermissions" → "Bypass"). Falls
 *  back to the raw value so unknown modes stay visible instead of vanishing. */
export const permissionModeLabel = (mode: string): string =>
  PERMISSION_MODES.find((m) => m.value === mode)?.label ?? mode;
