import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFile } from './workspace';

describe('workspace API', () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = vi.fn();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  describe('readFile', () => {
    it('returns a string for a JSON-typed response instead of a parsed object', async () => {
      // A `.json` file served with raw=true still carries an application/json
      // content-type. The shared client would `response.json()`-parse it into an
      // object; readFile must bypass that and hand back the raw text.
      const jsonText = '{"key": "value"}';
      vi.mocked(globalThis.fetch).mockResolvedValue(
        new Response(jsonText, {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
      );

      const result = await readFile('data/config.json');

      expect(typeof result).toBe('string');
      expect(result).toBe(jsonText);
      expect(globalThis.fetch).toHaveBeenCalledWith(
        '/api/workspace/file?path=data%2Fconfig.json&raw=true',
      );
    });

    it('throws when the response is not ok', async () => {
      vi.mocked(globalThis.fetch).mockResolvedValue(
        new Response('nope', { status: 404, statusText: 'Not Found' }),
      );

      await expect(readFile('missing.md')).rejects.toThrow(/Failed to read file: 404/);
    });
  });
});
