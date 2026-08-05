/**
 * Regression test for the assistant-message hanging-indent bug.
 *
 * Every assistant message is rendered with `stepFormat` on, so its container
 * carries the `.step-narration` class. The step-narration hanging-indent rule
 * (`padding-left: 1.1em; text-indent: -1.1em`) must ONLY apply to paragraphs
 * the pre-processor actually rewrote into steps — the ones that contain a
 * `.agent-step-cue` chevron span. `toStepNarration` is a no-op for ordinary
 * prose, so a normal multi-paragraph answer has NO chevron spans and must NOT
 * pick up the hanging indent (first line flush, wrapped lines/paragraphs
 * indented ~1em was the reported symptom).
 *
 * Two guards:
 *  1. DOM: normal prose emits plain <p> with zero `.agent-step-cue`; a real
 *     step-narration message emits <p> that DO carry `.agent-step-cue`.
 *  2. CSS: the hanging-indent rule in markdown.css is scoped to
 *     `p:has(.agent-step-cue)`, never bare `.step-narration p`.
 */
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { MarkdownRenderer } from './MarkdownRenderer';
// Load the real stylesheet as a string (vite `?raw`) so the regression guard
// checks the shipped CSS, not a copy.
import markdownCss from '../../../static/modules/markdown.css?raw';

describe('MarkdownRenderer step-narration scoping', () => {
  it('normal multi-paragraph prose renders plain <p> with no step-cue (no hanging indent)', () => {
    // Mirrors the reported message: flowing prose across paragraphs, no action
    // cues, so `toStepNarration` leaves it byte-for-byte alone.
    const content =
      'The repository was cloned and the initial checks passed while continuing ' +
      'the visual inspection, which the reviewer will report on once complete.\n\n' +
      'A second paragraph continues the explanation across several sentences so ' +
      'that its wrapped lines are what a hanging indent would visibly break.';

    const { container } = render(<MarkdownRenderer content={content} stepFormat />);

    // The container still gets the class (stepFormat is always on for assistant
    // messages) — that is fine; the CSS is scoped so the class alone is inert.
    const root = container.querySelector('.markdown-content');
    expect(root).not.toBeNull();
    expect(root?.classList.contains('step-narration')).toBe(true);

    const paragraphs = container.querySelectorAll('p');
    expect(paragraphs.length).toBe(2);

    // The tell: NO chevron spans anywhere, so `p:has(.agent-step-cue)` can
    // never match a normal paragraph -> no hanging indent leaks onto prose.
    expect(container.querySelectorAll('.agent-step-cue').length).toBe(0);
    paragraphs.forEach((p) => {
      expect(p.querySelector('.agent-step-cue')).toBeNull();
    });
  });

  it('genuine step narration still emits chevron cues on its step paragraphs', () => {
    const content = 'Let me read the file. Now edit it. Finally, verify.';
    const { container } = render(<MarkdownRenderer content={content} stepFormat />);

    const cues = container.querySelectorAll('.agent-step-cue');
    expect(cues.length).toBeGreaterThan(0);

    // Every step line lives inside a <p> that a `p:has(.agent-step-cue)`
    // selector will match — so genuine steps keep their chevron + indent.
    const steppedParagraphs = Array.from(container.querySelectorAll('p')).filter(
      (p) => p.querySelector('.agent-step-cue') !== null,
    );
    expect(steppedParagraphs.length).toBeGreaterThan(0);
  });

  it('markdown.css scopes the hanging indent to p:has(.agent-step-cue)', () => {
    const css = markdownCss;

    // The negative text-indent hanging rule must exist and be scoped to the
    // step lines only.
    expect(css).toContain('.markdown-content.step-narration p:has(.agent-step-cue)');
    expect(css).toContain('text-indent: -1.1em');

    // The over-broad selector that caused the bug must be gone: no rule targets
    // every <p> inside a step-narration container.
    expect(css).not.toMatch(/\.markdown-content\.step-narration p\s*\{/);
    expect(css).not.toMatch(/\.markdown-content\.step-narration p:first-child\s*\{/);
    expect(css).not.toMatch(/\.markdown-content\.step-narration p:last-child\s*\{/);
  });
});
