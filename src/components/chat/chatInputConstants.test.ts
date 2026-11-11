import { describe, expect, it } from 'vitest';
import { ICON } from './activityIcons';
import {
  BASE_SLASH_COMMANDS,
  matchSubmitTrigger,
  optLabel,
  optValue,
  resolveDictationFinal,
} from './chatInputConstants';
import { atItems } from './composerMenus';

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

describe('command and mention menu icons', () => {
  const EMOJI = /\p{Extended_Pictographic}/u;
  const mentions = atItems('', { sessions: [], agents: [], resources: [], workspace: true });
  const rows = [...BASE_SLASH_COMMANDS, ...mentions];

  it('name an SVG icon, never an emoji', () => {
    for (const row of rows) {
      expect(ICON[row.icon], row.icon).toBeDefined();
      expect(EMOJI.test(row.icon)).toBe(false);
    }
  });

  it('carry no emoji in their text', () => {
    for (const row of BASE_SLASH_COMMANDS) expect(EMOJI.test(row.cmd + row.desc), row.cmd).toBe(false);
    for (const row of mentions) expect(EMOJI.test(row.name + row.desc), row.name).toBe(false);
  });
});
