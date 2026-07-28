// ── LSP Protocol Interfaces ──
// Typed representations of JSON-RPC 2.0 messages and LSP protocol structures.
// Replaces inline Record<string, unknown> casts in useLSP.ts.

export interface LspPosition {
  line: number;
  character: number;
}

export interface LspRange {
  start: LspPosition;
  end: LspPosition;
}

// ── JSON-RPC 2.0 ──

export interface JsonRpcError {
  code: number;
  message: string;
  data?: unknown;
}

/** Incoming message can be a response (has id, no method) or a notification (has method). */
export interface JsonRpcMessage {
  jsonrpc?: string;
  id?: number;
  method?: string;
  params?: unknown;
  result?: unknown;
  error?: JsonRpcError;
}

// ── Completion ──

export interface LspCompletionItem {
  label: string;
  kind?: number;
  insertText?: string;
  detail?: string;
  documentation?: string;
}

export interface LspCompletionList {
  isIncomplete?: boolean;
  items: LspCompletionItem[];
}

export type LspCompletionResult = LspCompletionList | LspCompletionItem[] | null;

// ── Hover ──

export interface LspMarkupContent {
  kind: string;
  value: string;
}

export type LspHoverContents = string | LspMarkupContent | Array<string | LspMarkupContent>;

export interface LspHoverResult {
  contents: LspHoverContents;
  range?: LspRange;
}

// ── Diagnostics ──

export interface LspDiagnostic {
  range: LspRange;
  severity?: number;
  message: string;
  source?: string;
  code?: string | number;
}

export interface LspPublishDiagnosticsParams {
  uri: string;
  diagnostics: LspDiagnostic[];
}
