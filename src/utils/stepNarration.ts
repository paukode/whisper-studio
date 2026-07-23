/**
 * Reformat run-together agent/assistant STEP NARRATION into an activity log:
 * each action ("Let me…", "Now…", "First,…") lands on its own line, prefixed
 * with a chevron and with the cue phrase de-emphasized (Option C).
 *
 * This is a markdown PRE-processor: it returns markdown (with a couple of
 * inline `<span class="agent-step-cue">` markers the sanitizer keeps) that the
 * normal renderer then parses — so inline `code`, and everything else, still
 * renders. It is deliberately conservative:
 *   - only prose paragraphs are touched; fenced code, lists, tables, headings,
 *     and blockquotes are left byte-for-byte alone,
 *   - a paragraph is only reformatted when it actually reads as multi-step
 *     narration (>= 2 cue-led steps), so ordinary answers are untouched.
 */

// Action cues that begin a new step. Longest-first so "Let me" wins over "Let".
const CUES = [
  "Let me",
  "Let's",
  "Now the",
  "Now let me",
  "Now",
  "First,",
  "First",
  "Next,",
  "Next",
  "Then,",
  "Then",
  "Finally,",
  "Finally",
  "Also,",
  "Also",
  "Continuing",
  "I'll",
  "I'm",
  "I need",
  "I have",
  "I now",
  "I can",
  "I see",
  "Done.",
];

function matchCue(sentence: string): string | null {
  for (const cue of CUES) {
    if (sentence === cue || sentence.startsWith(cue + " ") || sentence.startsWith(cue + ",")) {
      return cue.replace(/,$/, "");
    }
  }
  return null;
}

interface Step {
  cue: string;
  body: string;
}

function splitSteps(text: string): Step[] {
  const sentences = text.match(/[^.!?]+[.!?]+["')\]]*\s*|[^.!?]+$/g) ?? [text];
  const steps: Step[] = [];
  for (const raw of sentences) {
    const s = raw.trim();
    if (!s) continue;
    const cue = matchCue(s);
    if (steps.length === 0) {
      steps.push({ cue: cue ?? "", body: s });
    } else if (cue) {
      steps.push({ cue, body: s });
    } else {
      steps[steps.length - 1].body += " " + s;
    }
  }
  return steps;
}

const CHEVRON = '<span class="agent-step-cue">›</span> ';

function renderStep({ cue, body }: Step): string {
  if (cue && body.startsWith(cue)) {
    return CHEVRON + '<span class="agent-step-cue">' + cue + "</span>" + body.slice(cue.length);
  }
  return CHEVRON + body;
}

const STRUCTURAL_LINE = /^(\s*)([-*+]\s|\d+\.\s|#{1,6}\s|>\s?|\||```|~~~)/;

function transformBlock(block: string): string {
  const trimmed = block.trim();
  if (!trimmed) return block;
  // Leave any markdown-structural block untouched (lists, tables, headings,
  // blockquotes, fenced/indented code).
  if (STRUCTURAL_LINE.test(trimmed) || /^ {4,}\S/.test(block)) return block;
  if (trimmed.includes("\n") && trimmed.split("\n").some((l) => STRUCTURAL_LINE.test(l))) {
    return block;
  }
  // Normalize a missing space after sentence punctuation ("precisely.The").
  const norm = trimmed.replace(/([.!?])([A-Z])/g, "$1 $2");
  const steps = splitSteps(norm);
  if (steps.length < 2) return block; // not step narration — leave as-is
  return steps.map(renderStep).join("\n\n");
}

// Private-use-area sentinels wrapping a mask index. They never appear in real
// prose, carry no ".!?" (so sentence splitting ignores them) and are distinct
// from the digits already in the text ("item 1", "9 configs").
const MASK_OPEN = String.fromCharCode(0xe000);
const MASK_CLOSE = String.fromCharCode(0xe001);
const MASK_RE = new RegExp(MASK_OPEN + "(\\d+)" + MASK_CLOSE, "g");

/** Reformat step narration; a no-op for content that isn't multi-step prose. */
export function toStepNarration(md: string): string {
  if (!md) return md;
  // Fenced code can span blank lines, so a naive block split would corrupt it.
  // Bail entirely on any fenced code (step narration never has it; a real
  // answer that does is exactly what we must not touch).
  if (md.includes("```") || md.includes("~~~")) return md;
  // Mask inline `code` spans BEFORE any sentence/step splitting so a filename
  // like `sec-cross-account.html` is never split on its dot. Restore at the end.
  const codes: string[] = [];
  const masked = md.replace(/`[^`\n]*`/g, (m) => {
    codes.push(m);
    return MASK_OPEN + (codes.length - 1) + MASK_CLOSE;
  });
  const out = masked
    .split(/\n{2,}/)
    .map(transformBlock)
    .join("\n\n");
  return out.replace(MASK_RE, (_m, i) => codes[Number(i)] ?? "");
}
