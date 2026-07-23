import { describe, it, expect } from 'vitest';
import { stripMarkdownTitle } from './stripMarkdownTitle';

describe('stripMarkdownTitle', () => {
  it('strips leading heading markers', () => {
    expect(stripMarkdownTitle('# Your choice matters')).toBe('Your choice matters');
    expect(stripMarkdownTitle('### AI/ML pipeline design')).toBe('AI/ML pipeline design');
  });

  it('strips dangling / paired bold and italic markers', () => {
    expect(stripMarkdownTitle('**I cannot')).toBe('I cannot');
    expect(stripMarkdownTitle('**Important** note')).toBe('Important note');
    expect(stripMarkdownTitle('*emphasis*')).toBe('emphasis');
  });

  it('strips blockquote, bullet and ordered-list markers', () => {
    expect(stripMarkdownTitle('> Note to self')).toBe('Note to self');
    expect(stripMarkdownTitle('- todo item')).toBe('todo item');
    expect(stripMarkdownTitle('1. first step')).toBe('first step');
  });

  it('strips inline code / strikethrough markers', () => {
    expect(stripMarkdownTitle('`useEffect` cleanup')).toBe('useEffect cleanup');
    expect(stripMarkdownTitle('~~deprecated~~ approach')).toBe('deprecated approach');
  });

  it('reduces links and images to their text', () => {
    expect(stripMarkdownTitle('[the docs](https://x.com)')).toBe('the docs');
    expect(stripMarkdownTitle('![diagram](a.png) overview')).toBe('diagram overview');
  });

  it('leaves clean titles and snake_case untouched', () => {
    expect(stripMarkdownTitle('Quarterly planning')).toBe('Quarterly planning');
    expect(stripMarkdownTitle('refactor my_helper module')).toBe('refactor my_helper module');
  });

  it('falls back to the trimmed input rather than returning empty', () => {
    expect(stripMarkdownTitle('***')).toBe('***');
    expect(stripMarkdownTitle('  spaced  ')).toBe('spaced');
    expect(stripMarkdownTitle('')).toBe('');
  });
});
