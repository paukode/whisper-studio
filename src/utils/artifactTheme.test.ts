/**
 * The theme block a model-authored HTML page gets: where it goes in the
 * page, and which app tokens it carries.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { ARTIFACT_THEME_STYLE_ID, readArtifactVars, withArtifactTheme } from './artifactTheme';

const CSS = ':root{--ws-accent:#123456}';
const BLOCK = `<style id="${ARTIFACT_THEME_STYLE_ID}">${CSS}</style>`;

describe('withArtifactTheme', () => {
  it('goes after <meta charset>, so the charset stays at the top of a saved file', () => {
    const html = '<!DOCTYPE html><html><head><meta charset="utf-8"><title>t</title></head><body></body></html>';
    const out = withArtifactTheme(html, CSS);
    expect(out.indexOf('charset')).toBeLessThan(out.indexOf(BLOCK));
    expect(out.indexOf(BLOCK)).toBeLessThan(out.indexOf('<title>'));
  });

  it('comes before the page styles, so a page that redefines a variable wins', () => {
    const html = '<html><head><style>:root{--ws-accent:red}</style></head></html>';
    const out = withArtifactTheme(html, CSS);
    expect(out.indexOf(BLOCK)).toBeLessThan(out.indexOf(':root{--ws-accent:red}'));
  });

  it('falls back to <head>, then <html>, then the doctype, then the start', () => {
    expect(withArtifactTheme('<head lang="en"></head>', CSS)).toBe(`<head lang="en">${BLOCK}</head>`);
    expect(withArtifactTheme('<html><body></body></html>', CSS)).toBe(`<html>${BLOCK}<body></body></html>`);
    expect(withArtifactTheme('<!DOCTYPE html><p>x</p>', CSS)).toBe(`<!DOCTYPE html>${BLOCK}<p>x</p>`);
    expect(withArtifactTheme('<p>x</p>', CSS)).toBe(`${BLOCK}<p>x</p>`);
  });

  it('ignores a charset meta and a <header> in the body', () => {
    const html = '<html><head></head><body><header>h</header><meta charset="x"></body></html>';
    expect(withArtifactTheme(html, CSS)).toBe(
      `<html><head>${BLOCK}</head><body><header>h</header><meta charset="x"></body></html>`,
    );
  });

  it('adds the block and changes nothing else in the page', () => {
    const html = '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>é</body></html>';
    expect(withArtifactTheme(html, CSS).replace(BLOCK, '')).toBe(html);
  });
});

describe('readArtifactVars', () => {
  const root = document.documentElement;

  afterEach(() => {
    root.removeAttribute('style');
  });

  it('carries the live app tokens under their page names', () => {
    root.style.setProperty('--accent', '#9a82ff');
    root.style.setProperty('--bg-primary', '#14132a');
    root.style.setProperty('--speaker-3', '#7fd3a0');
    const css = readArtifactVars();
    expect(css.startsWith(':root{')).toBe(true);
    expect(css).toContain('--ws-accent:#9a82ff');
    expect(css).toContain('--ws-bg:#14132a');
    expect(css).toContain('--ws-c3:#7fd3a0');
  });

  it('follows a token change on the next read', () => {
    root.style.setProperty('--accent', '#9a82ff');
    expect(readArtifactVars()).toContain('--ws-accent:#9a82ff');
    root.style.setProperty('--accent', '#166e8c');
    expect(readArtifactVars()).toContain('--ws-accent:#166e8c');
  });
});
