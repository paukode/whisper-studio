import { useMemo } from 'react';

/**
 * A stable `dangerouslySetInnerHTML` value for `html`.
 *
 * React 19 compares this prop by object identity and assigns innerHTML
 * whenever the object is new, so an inline `{ __html: html }` rebuilds the
 * element's whole DOM on every re-render, even when the HTML is unchanged.
 * In the chat that meant every past message was torn down and re-parsed on
 * each streamed token (and lost text selection and code block scroll).
 */
export function useHtmlProp(html: string): { __html: string } {
  return useMemo(() => ({ __html: html }), [html]);
}
