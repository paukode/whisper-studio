/**
 * Pure conversions between LSP protocol shapes and Monaco editor shapes.
 *
 * The two coordinate systems differ: LSP positions are 0-based (line/character)
 * while Monaco positions are 1-based (lineNumber/column). The enum numberings
 * for diagnostic severity and completion kind differ too. Everything here is a
 * pure function so it can be unit-tested without a Monaco or LSP runtime — the
 * enum objects Monaco owns are injected by the caller.
 */
import type {
  LspPosition,
  LspRange,
  LspDiagnostic,
  LspCompletionResult,
  LspHoverResult,
  LspHoverContents,
  LspMarkupContent,
} from '@/types/lsp';

/** A Monaco 1-based range (subset of monaco IRange). */
export interface MonacoRange {
  startLineNumber: number;
  startColumn: number;
  endLineNumber: number;
  endColumn: number;
}

/** Monaco marker shape (subset of monaco editor.IMarkerData). */
export interface MonacoMarker extends MonacoRange {
  severity: number;
  message: string;
  source?: string;
  code?: string;
}

/** Monaco completion item shape (subset of monaco languages.CompletionItem). */
export interface MonacoCompletion {
  label: string;
  kind: number;
  insertText: string;
  detail?: string;
  documentation?: string;
  range: MonacoRange;
}

/** Monaco hover shape (subset of monaco languages.Hover). */
export interface MonacoHover {
  contents: Array<{ value: string }>;
  range?: MonacoRange;
}

/** monaco.MarkerSeverity — {Hint:1, Info:2, Warning:4, Error:8}. */
export interface MarkerSeverityEnum {
  Error: number;
  Warning: number;
  Info: number;
  Hint: number;
}

/** monaco.languages.CompletionItemKind — full member set. */
export type CompletionItemKindEnum = Record<string, number>;

// ── Positions & ranges (0-based LSP <-> 1-based Monaco) ──

/** LSP position (0-based) -> Monaco position (1-based). */
export function lspToMonacoPosition(pos: LspPosition): { lineNumber: number; column: number } {
  return { lineNumber: pos.line + 1, column: pos.character + 1 };
}

/** Monaco position (1-based) -> LSP position (0-based). */
export function monacoToLspPosition(lineNumber: number, column: number): LspPosition {
  return { line: lineNumber - 1, character: column - 1 };
}

/** LSP range (0-based) -> Monaco range (1-based). */
export function lspToMonacoRange(range: LspRange): MonacoRange {
  return {
    startLineNumber: range.start.line + 1,
    startColumn: range.start.character + 1,
    endLineNumber: range.end.line + 1,
    endColumn: range.end.character + 1,
  };
}

// ── Diagnostics ──

/**
 * Map an LSP diagnostic severity (1 Error, 2 Warning, 3 Info, 4 Hint) to a
 * Monaco MarkerSeverity value. An absent severity is treated as Error, matching
 * how editors surface unqualified diagnostics.
 */
export function lspToMonacoSeverity(severity: number | undefined, sev: MarkerSeverityEnum): number {
  switch (severity) {
    case 1:
      return sev.Error;
    case 2:
      return sev.Warning;
    case 3:
      return sev.Info;
    case 4:
      return sev.Hint;
    default:
      return sev.Error;
  }
}

/** Map LSP diagnostics to Monaco markers for `setModelMarkers`. */
export function diagnosticsToMarkers(
  diagnostics: LspDiagnostic[],
  sev: MarkerSeverityEnum,
): MonacoMarker[] {
  return diagnostics.map((d) => ({
    ...lspToMonacoRange(d.range),
    severity: lspToMonacoSeverity(d.severity, sev),
    message: d.message,
    source: d.source,
    code: d.code !== undefined ? String(d.code) : undefined,
  }));
}

// ── Completion ──

/**
 * Map an LSP CompletionItemKind (1..25) to a Monaco CompletionItemKind. The two
 * enums use different numeric values, so we translate through the injected
 * Monaco enum by name.
 */
export function lspToMonacoCompletionKind(
  kind: number | undefined,
  k: CompletionItemKindEnum,
): number {
  const byLspKind: Record<number, string> = {
    1: 'Text',
    2: 'Method',
    3: 'Function',
    4: 'Constructor',
    5: 'Field',
    6: 'Variable',
    7: 'Class',
    8: 'Interface',
    9: 'Module',
    10: 'Property',
    11: 'Unit',
    12: 'Value',
    13: 'Enum',
    14: 'Keyword',
    15: 'Snippet',
    16: 'Color',
    17: 'File',
    18: 'Reference',
    19: 'Folder',
    20: 'EnumMember',
    21: 'Constant',
    22: 'Struct',
    23: 'Event',
    24: 'Operator',
    25: 'TypeParameter',
  };
  const name = kind !== undefined ? byLspKind[kind] : undefined;
  if (name && k[name] !== undefined) return k[name];
  return k.Text ?? 0;
}

/**
 * Map an LSP completion result (a list, a bare item array, or null) to Monaco
 * completion items. `range` is the word range the suggestions replace.
 */
export function mapCompletionResult(
  result: LspCompletionResult,
  kindEnum: CompletionItemKindEnum,
  range: MonacoRange,
): MonacoCompletion[] {
  if (!result) return [];
  const items = Array.isArray(result) ? result : result.items;
  if (!items) return [];
  return items.map((item) => ({
    label: item.label,
    kind: lspToMonacoCompletionKind(item.kind, kindEnum),
    insertText: item.insertText ?? item.label,
    detail: item.detail,
    documentation: item.documentation,
    range,
  }));
}

// ── Hover ──

function contentToString(content: string | LspMarkupContent): string {
  return typeof content === 'string' ? content : content.value;
}

/** Flatten LSP hover contents (string | MarkupContent | array) into markdown. */
export function hoverContentsToMarkdown(contents: LspHoverContents): string {
  if (Array.isArray(contents)) {
    return contents.map(contentToString).filter(Boolean).join('\n\n');
  }
  return contentToString(contents);
}

/** Map an LSP hover result to a Monaco Hover, or null when there's nothing to show. */
export function mapHoverResult(result: LspHoverResult | null | undefined): MonacoHover | null {
  if (!result || result.contents == null) return null;
  const value = hoverContentsToMarkdown(result.contents);
  if (!value) return null;
  return {
    contents: [{ value }],
    range: result.range ? lspToMonacoRange(result.range) : undefined,
  };
}
