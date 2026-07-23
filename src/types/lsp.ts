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

export interface JsonRpcRequest {
  jsonrpc: '2.0';
  id: number;
  method: string;
  params?: unknown;
}

export interface JsonRpcNotification {
  jsonrpc: '2.0';
  method: string;
  params?: unknown;
}

export interface JsonRpcResponse {
  jsonrpc: '2.0';
  id: number;
  result?: unknown;
  error?: JsonRpcError;
}

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

// ── Location (definition, references) ──

export interface LspLocation {
  uri: string;
  range: LspRange;
}

export interface LspLocationLink {
  targetUri: string;
  targetRange: LspRange;
  targetSelectionRange?: LspRange;
  originSelectionRange?: LspRange;
}

export type LspDefinitionResult = LspLocation | LspLocation[] | LspLocationLink[] | null;

// ── Signature Help ──

export interface LspParameterInfo {
  label: string;
  documentation?: string;
}

export interface LspSignatureInfo {
  label: string;
  documentation?: string;
  parameters?: LspParameterInfo[];
}

export interface LspSignatureHelp {
  signatures: LspSignatureInfo[];
  activeSignature?: number;
  activeParameter?: number;
}

// ── Document Symbols ──

export interface LspDocumentSymbol {
  name: string;
  detail?: string;
  kind: number;
  range: LspRange;
  selectionRange?: LspRange;
  children?: LspDocumentSymbol[];
}

// ── Text Edit / Formatting ──

export interface LspTextEdit {
  range: LspRange;
  newText: string;
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

// ── Initialize ──

export interface LspInitializeResult {
  capabilities: Record<string, unknown>;
}
