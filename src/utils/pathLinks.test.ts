import { describe, expect, it } from 'vitest';
import { linkifyPaths } from './pathLinks';
import { parseWsFileHref } from './wsFileLinks';

const WS = '/Users/me/repo';

describe('linkifyPaths: a path mention becomes an editor link', () => {
  it('links a path with a line to that line', () => {
    expect(linkifyPaths('See src/hooks/chatStream/sseStream.ts:282 for the leg.', WS)).toBe(
      'See [src/hooks/chatStream/sseStream.ts:282]' +
        '(#wsfile=/Users/me/repo/src/hooks/chatStream/sseStream.ts&L=282-282) for the leg.',
    );
  });

  it('links a path with a line range to those lines', () => {
    expect(linkifyPaths('The check is server/goals/gate.py:40-44.', WS)).toBe(
      'The check is [server/goals/gate.py:40-44](#wsfile=/Users/me/repo/server/goals/gate.py&L=40-44).',
    );
  });

  it('links a path with no line to the top of the file', () => {
    expect(linkifyPaths('Read src/utils/pathLinks.ts first', WS)).toBe(
      'Read [src/utils/pathLinks.ts](#wsfile=/Users/me/repo/src/utils/pathLinks.ts) first',
    );
  });

  it('reads ./ and a dot folder against the workspace root', () => {
    expect(linkifyPaths('./src/a.ts and .github/workflows/ci.yml', WS)).toBe(
      '[./src/a.ts](#wsfile=/Users/me/repo/src/a.ts) and ' +
        '[.github/workflows/ci.yml](#wsfile=/Users/me/repo/.github/workflows/ci.yml)',
    );
  });

  it('quotes the absolute path the way the server does and keeps the link parseable', () => {
    // The root may hold anything a folder name can; the mention cannot.
    const out = linkifyPaths('Edit docs/notes.md:3 now.', '/Users/me/A&B (old)/');
    expect(out).toBe(
      'Edit [docs/notes.md:3](#wsfile=/Users/me/A%26B%20%28old%29/docs/notes.md&L=3-3) now.',
    );
    const href = /\]\((#wsfile=[^)]+)\)/.exec(out)?.[1] ?? '';
    expect(parseWsFileHref(href)).toEqual({
      path: '/Users/me/A&B (old)/docs/notes.md',
      startLine: 3,
      endLine: 3,
    });
  });

  it('keeps the underscores of a name from reading as emphasis', () => {
    expect(linkifyPaths('server/prompts/__init__.py', WS)).toBe(
      '[server/prompts/\\_\\_init\\_\\_.py](#wsfile=/Users/me/repo/server/prompts/__init__.py)',
    );
  });

  it('links a path in bold, in parentheses, in quotes and in a table cell', () => {
    const out = linkifyPaths('**src/a.ts** (src/b.ts) "src/c.ts" | src/d.ts |', WS);
    for (const name of ['a', 'b', 'c', 'd']) {
      expect(out).toContain(`[src/${name}.ts](#wsfile=/Users/me/repo/src/${name}.ts)`);
    }
  });

  it('links a path with folders in any script as one path', () => {
    const out = linkifyPaths('See docs/über/a.md and Документы/plan.md.', WS);
    expect(out).toBe(
      'See [docs/über/a.md](#wsfile=/Users/me/repo/docs/%C3%BCber/a.md) and ' +
        '[Документы/plan.md](#wsfile=/Users/me/repo/' +
        '%D0%94%D0%BE%D0%BA%D1%83%D0%BC%D0%B5%D0%BD%D1%82%D1%8B/plan.md).',
    );
    const href = /\]\((#wsfile=[^)]+)\)/.exec(out)?.[1] ?? '';
    expect(parseWsFileHref(href)?.path).toBe('/Users/me/repo/docs/über/a.md');
  });

  it('links a name with several dots by its last extension', () => {
    expect(linkifyPaths('src/app.module.ts and src/a.test.ts.', WS)).toBe(
      '[src/app.module.ts](#wsfile=/Users/me/repo/src/app.module.ts) and ' +
        '[src/a.test.ts](#wsfile=/Users/me/repo/src/a.test.ts).',
    );
  });
});

describe('linkifyPaths: what stays text', () => {
  it('leaves everything alone with no workspace connected', () => {
    const text = 'See src/hooks/chatStream/sseStream.ts:282.';
    expect(linkifyPaths(text, '')).toBe(text);
  });

  it.each([
    ['a web address', 'Open https://github.com/org/repo/blob/main/src/a.ts or s3://bucket/q3/a.csv.'],
    ['a bare domain', 'The guide is at example.com/docs/a.html and docs.python.org/3/re.html.'],
    ['"and/or"', 'Use one and/or the other, input/output, TCP/IP.'],
    ['a date', 'Due 2026/10/18 or 18/10/2026.'],
    ['a version or a fraction', 'Needs node/24.1 or react/18.3.1, about 1/2.5 of it.'],
    ['a first folder with a dot', 'Under python3.12/site.py and v1.2/notes.md.'],
    ['an absolute or home path', 'Wrote /Users/me/repo/src/a.ts and ~/notes/a.md.'],
    ['Windows-style text', 'C:\\Users\\me\\a.txt, src\\hooks\\a.ts and C:/Users/me/a.txt'],
    ['the tail of another token', 'git@github.com:org/repo.git, $HOME/bin/run.sh, key=src/a.ts'],
    ['a name with no folder or no extension', 'README.md, src/utils/ and src/Makefile'],
  ])('leaves %s alone', (_what, text) => {
    expect(linkifyPaths(text, WS)).toBe(text);
  });

  it('leaves paths inside inline code alone', () => {
    const text = 'Run `src/a.ts` and ``x `src/b.ts` y`` here.';
    expect(linkifyPaths(text, WS)).toBe(text);
  });

  it('leaves paths inside fenced code alone, and links the prose after it', () => {
    const fenced = '```ts\nimport x from "src/c.ts";\n```\nThen src/d.ts';
    expect(linkifyPaths(fenced, WS)).toBe(
      '```ts\nimport x from "src/c.ts";\n```\nThen [src/d.ts](#wsfile=/Users/me/repo/src/d.ts)',
    );
    const tilde = '~~~\nsrc/e.ts\n~~~';
    expect(linkifyPaths(tilde, WS)).toBe(tilde);
    const inList = '1. Step\n\n   ```bash\n   cat src/o.ts\n   ```\n';
    expect(linkifyPaths(inList, WS)).toBe(inList);
  });

  it('leaves an unclosed fence alone while it streams', () => {
    const streaming = 'Here:\n```python\nopen("src/a.py")\nprint(src/b.py';
    expect(linkifyPaths(streaming, WS)).toBe(streaming);
  });

  it('leaves paths already inside a link alone, the write result link included', () => {
    const text =
      'Wrote [src/a.ts](#wsfile=/Users/me/repo/src/a.ts&open=os), see [the src/b.ts notes](https://x.y), ' +
      '![src/c.png](src/c.png), [src/d.ts][1] and <https://x.y/src/e.ts>.\n\n[1]: src/d.ts';
    expect(linkifyPaths(text, WS)).toBe(text);
  });

  it('leaves the text of a raw HTML anchor alone', () => {
    const text = '<a href="https://x.y">src/a.ts</a>';
    expect(linkifyPaths(text, WS)).toBe(text);
  });

  it.each([
    ['a pre block', '<pre>\nsrc/a.ts\n</pre>'],
    ['inline code', 'Run <code>src/a.ts</code> now.'],
    ['a key or a sample', '<kbd>src/a.ts</kbd> and <samp class="x">src/b.ts</samp>'],
    ['a pre block still streaming', 'Here:\n<pre>\nsrc/a.ts\nsrc/b.ts'],
  ])('leaves paths inside raw HTML code alone: %s', (_what, text) => {
    expect(linkifyPaths(text, WS)).toBe(text);
  });

  it('links the prose after raw HTML code', () => {
    expect(linkifyPaths('<code>src/a.ts</code> calls src/b.ts', WS)).toBe(
      '<code>src/a.ts</code> calls [src/b.ts](#wsfile=/Users/me/repo/src/b.ts)',
    );
  });
});

describe('linkifyPaths: one pass in time linear to the text', () => {
  // The streaming view re-parses on every token. Unbounded, each of these
  // took 0.4 to 2.6 s at this size; bounded, each takes a few ms.
  it.each([
    ['a run of hyphens', 'a-'.repeat(25_000)],
    ['a run of dots', 'a.'.repeat(25_000)],
    ['anchors never closed', '<a x '.repeat(10_000)],
    ['tags never closed', '<b a="'.repeat(10_000)],
    ['raw code never closed', '<code> x '.repeat(6_000)],
  ])('reads %s quickly', (_what, text) => {
    linkifyPaths('warm up src/a.ts', WS);
    const input = `${text} src/x.ts`;
    const t0 = performance.now();
    linkifyPaths(input, WS);
    expect(input.length).toBeGreaterThan(48_000);
    expect(performance.now() - t0).toBeLessThan(100);
  });
});
