import { describe, expect, it } from 'vitest';
import {
  matchColonSubmenu,
  matchSubmitTrigger,
  optLabel,
  optValue,
  parseSkillMention,
  resolveDictationFinal,
  SKILL_MENTION_PLACEHOLDER,
} from './chatInputConstants';

describe('optLabel / optValue', () => {
  it('treats a plain string option as both label and value', () => {
    expect(optLabel('low')).toBe('low');
    expect(optValue('low')).toBe('low');
  });

  it('splits label (shown) from value (inserted) for object options', () => {
    // The /model case: the picker shows the version label, the command inserts
    // the model key the /model handler validates against.
    const opt = { label: 'Sonnet 4.6', value: 'sonnet' };
    expect(optLabel(opt)).toBe('Sonnet 4.6');
    expect(optValue(opt)).toBe('sonnet');
  });
});

describe('matchSubmitTrigger', () => {
  it.each([
    ['okay send', 'okay send'],
    ['okay submit', 'okay submit'],
    ['send now', 'send now'],
    ['submit now', 'submit now'],
    ['send it now', 'send it now'],
    ['submit it now', 'submit it now'], // the natural blend the user actually said
    ['send away', 'send away'],
    ['fire away', 'fire away'],
    ['send the message', 'send the message'],
  ])('fires on the bare command %j with empty body', (input, hit) => {
    expect(matchSubmitTrigger(input)).toEqual({ hit, body: '' });
  });

  it('strips the command and returns the preceding message body', () => {
    expect(matchSubmitTrigger('add a dark mode toggle send now')).toEqual({
      hit: 'send now',
      body: 'add a dark mode toggle',
    });
  });

  it('tolerates a comma the speech engine inserts inside the command', () => {
    // Speech-to-text returns "okay send" as "Okay, send".
    expect(matchSubmitTrigger('Okay, send')).toEqual({ hit: 'okay send', body: '' });
    expect(matchSubmitTrigger('okay, submit')).toEqual({ hit: 'okay submit', body: '' });
  });

  it('still fires when a stray filler word trails the command', () => {
    // Exactly the screenshot: "...working Submit it now. Okay."
    expect(matchSubmitTrigger("It seems it's working submit it now. Okay.")).toEqual({
      hit: 'submit it now',
      body: "It seems it's working",
    });
  });

  it('handles the full screenshot transcript', () => {
    const r = matchSubmitTrigger("Okay, that sounds good. It seems it's working Submit it now. Okay.");
    expect(r?.body).toBe("Okay, that sounds good. It seems it's working");
  });

  it('reads the command at the very end of the running dictation', () => {
    expect(matchSubmitTrigger('please send it now')).toEqual({ hit: 'send it now', body: 'please' });
  });

  it.each([
    'send',                               // bare verb: too easy to mishear
    'submit',
    'okay',                               // bare lead
    'go ahead',
    'add the button and implement it',
    'can you implement this',
    'can you send it',                    // "send it" ends real requests
    'please submit this',                 // "submit this" ends real requests
    'send me an email about this',        // command word mid-sentence
    'tell him the build is done',
    'hello world',
  ])('does NOT fire on %j', (input) => {
    expect(matchSubmitTrigger(input)).toBeNull();
  });
});

describe('resolveDictationFinal', () => {
  it('appends a non-command fragment to the running text', () => {
    expect(resolveDictationFinal('fix the parser', 'and add a test')).toEqual({
      text: 'fix the parser and add a test',
      submit: null,
    });
  });

  it('submits the accumulated message when a command ends the fragment', () => {
    expect(resolveDictationFinal('fix the parser bug', 'okay send')).toEqual({
      text: '',
      submit: 'fix the parser bug',
    });
  });

  it('submits when the command lands in its own final fragment', () => {
    // base already holds the whole message; the command arrives separately.
    expect(resolveDictationFinal('add a dark mode toggle', 'submit now')).toEqual({
      text: '',
      submit: 'add a dark mode toggle',
    });
  });

  it('does not submit an empty message when only the command was spoken', () => {
    expect(resolveDictationFinal('', 'okay send')).toEqual({ text: '', submit: null });
  });

  it('starts a fresh message from empty base', () => {
    expect(resolveDictationFinal('', 'hello there')).toEqual({ text: 'hello there', submit: null });
  });

  it('keeps the text unchanged for an empty fragment', () => {
    expect(resolveDictationFinal('half a sentence', '')).toEqual({ text: 'half a sentence', submit: null });
  });
});

describe('matchColonSubmenu', () => {
  it('matches the bare submenu prefix with an empty query', () => {
    expect(matchColonSubmenu('/file:')).toEqual({ type: 'file', query: '' });
  });

  it('matches a filename query typed directly after the colon', () => {
    expect(matchColonSubmenu('/file:report.pdf')).toEqual({ type: 'file', query: 'report.pdf' });
  });

  it('allows a single leading space before the query', () => {
    expect(matchColonSubmenu('/file: report.pdf')).toEqual({ type: 'file', query: 'report.pdf' });
  });

  it('dismisses once a space starts the message body', () => {
    // The whole point of the fix: a question after the filename must NOT keep
    // the file-search popup open.
    expect(matchColonSubmenu('/file: report.pdf what is this about?')).toBeNull();
    expect(matchColonSubmenu('/file:report.pdf summarize it')).toBeNull();
  });

  it('also covers /skills: and /mcp: submenus', () => {
    expect(matchColonSubmenu('/skills:web')).toEqual({ type: 'skills', query: 'web' });
    expect(matchColonSubmenu('/mcp:')).toEqual({ type: 'mcp', query: '' });
  });

  it('returns null for non-submenu text', () => {
    expect(matchColonSubmenu('/model haiku')).toBeNull();
    expect(matchColonSubmenu('hello world')).toBeNull();
    expect(matchColonSubmenu('not /file: at start')).toBeNull();
  });
});

describe('parseSkillMention', () => {
  it('substitutes the placeholder payload for a bare mention, with empty displayText', () => {
    expect(parseSkillMention('@summarize')).toEqual({
      forceSkill: 'summarize',
      messageToSend: SKILL_MENTION_PLACEHOLDER,
      displayText: '',
    });
  });

  it('supports the autocomplete @skills: form', () => {
    expect(parseSkillMention('@skills:meeting_notes')).toEqual({
      forceSkill: 'meeting_notes',
      messageToSend: SKILL_MENTION_PLACEHOLDER,
      displayText: '',
    });
  });

  it('keeps the user text as both payload and displayText when present', () => {
    expect(parseSkillMention('@meeting_notes with action items')).toEqual({
      forceSkill: 'meeting_notes',
      messageToSend: 'with action items',
      displayText: 'with action items',
    });
  });

  it('passes a mention-less message through untouched', () => {
    expect(parseSkillMention('just a normal question')).toEqual({
      messageToSend: 'just a normal question',
    });
  });
});
