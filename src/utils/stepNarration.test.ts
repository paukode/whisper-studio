import { describe, expect, it } from 'vitest';
import { marked } from 'marked';
import { toStepNarration } from './stepNarration';

function render(md: string): string {
  return marked.parse(toStepNarration(md), { async: false }) as string;
}

describe('toStepNarration', () => {
  it('breaks each action cue onto its own step and mutes the cue', () => {
    const out = toStepNarration('Let me read the file. Now edit it. Finally, verify.');
    // three cue-led steps, separated by blank lines
    expect(out.split('\n\n').length).toBe(3);
    expect(out).toContain('<span class="agent-step-cue">Let me</span>');
    expect(out).toContain('<span class="agent-step-cue">Now</span>');
  });

  it('keeps non-cue follow-on sentences with their step', () => {
    const steps = toStepNarration('Let me read it. The read was deduped. Now edit it.').split('\n\n');
    expect(steps.length).toBe(2);
    expect(steps[0]).toContain('The read was deduped.');
  });

  it('preserves inline code even when the filename contains a dot', () => {
    const html = render('Let me read `sec-cross-account.html` now. Now edit `diagram.css` too.');
    expect(html).toContain('<code>sec-cross-account.html</code>');
    expect(html).toContain('<code>diagram.css</code>');
  });

  it('inserts a missing space after sentence punctuation', () => {
    expect(toStepNarration('Let me do it precisely.The read was deduped. Now go.')).toContain(
      'precisely. The',
    );
  });

  it('does not corrupt numbers in the prose', () => {
    const html = render('Let me check item 2. Now verify all 9 configs pass. Then finish.');
    expect(html).toContain('item 2');
    expect(html).toContain('all 9 configs');
  });

  it('leaves a normal single-sentence answer untouched', () => {
    const md = 'Here is the answer with some `code`.';
    expect(toStepNarration(md)).toBe(md);
  });

  it('leaves markdown tables untouched', () => {
    const md = 'Summary:\n\n| a | b |\n| - | - |\n| 1 | 2 |';
    expect(toStepNarration(md)).toBe(md);
  });

  it('leaves lists untouched', () => {
    const md = '- Let me one\n- Now two\n- Then three';
    expect(toStepNarration(md)).toBe(md);
  });

  it('bails entirely on fenced code blocks', () => {
    const md = 'Let me show it. Now run:\n\n```js\nconst x = 1;\n```\n\nThen done.';
    expect(toStepNarration(md)).toBe(md);
  });

  it('is a no-op on empty input', () => {
    expect(toStepNarration('')).toBe('');
  });

  it('does not leak private-use sentinel characters', () => {
    const out = toStepNarration('Let me read `a.b`. Now read `c.d`. Then stop.');
    expect(out.includes(String.fromCharCode(0xe000))).toBe(false);
    expect(out.includes(String.fromCharCode(0xe001))).toBe(false);
  });
});
