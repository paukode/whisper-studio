/**
 * Off-screen messages must stay out of layout and paint.
 *
 * A streaming reply relays out the message column on every frame. Without
 * containment that walked every message in the session: at 300 messages the
 * main thread was busy for longer than the reply took to arrive (measured
 * 2026-09-25, replaying a real Bedrock stream), and the text reached the
 * screen 90 ms late in clumps. With it, a long session streams like a new one.
 */
import { describe, expect, it } from 'vitest';
import styleCss from '../../../static/style.css?raw';

describe('chat history containment', () => {
  it('lets the browser skip older off-screen messages and remember their height', () => {
    const rule = styleCss.match(/\.chat-messages > \.chat-msg-wrap:nth-last-child\(n\+\d+\)\s*\{([^}]*)\}/);
    expect(rule).not.toBeNull();
    expect(rule![1]).toMatch(/content-visibility:\s*auto/);
    expect(rule![1]).toMatch(/contain-intrinsic-size:\s*auto\s+\d+px/);
    // In a flex column a skipped message would otherwise shrink to nothing.
    expect(rule![1]).toMatch(/flex-shrink:\s*0/);
  });
});
