import { vi } from 'vitest';

/** A copy event whose clipboardData records what a listener sets. jsdom has
 *  no DataTransfer, so the event carries a minimal stand-in. */
export function copyEvent() {
  const data = new Map<string, string>();
  const event = new Event('copy', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'clipboardData', {
    value: { setData: (type: string, value: string) => data.set(type, value) },
  });
  return { event, data };
}

/** document.execCommand as a browser runs 'copy': it fires the copy event
 *  synchronously and returns true, or refuses and returns false. jsdom has
 *  no execCommand at all; delete the stub with Reflect.deleteProperty. */
export function stubExecCommand(allowed: boolean) {
  const copy = copyEvent();
  const exec = vi.fn((command: string) => {
    if (command !== 'copy' || !allowed) return false;
    document.dispatchEvent(copy.event);
    return true;
  });
  Object.defineProperty(document, 'execCommand', { value: exec, configurable: true });
  return { ...copy, exec };
}
