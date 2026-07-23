import { describe, it, expect } from 'vitest';
import { toError } from './toError';

describe('toError', () => {
  it('passes through an Error instance unchanged', () => {
    const original = new Error('original');
    const result = toError(original);
    expect(result).toBe(original);
    expect(result.message).toBe('original');
  });

  it('wraps a string in an Error', () => {
    const result = toError('something failed');
    expect(result).toBeInstanceOf(Error);
    expect(result.message).toBe('something failed');
  });

  it('wraps an object with a message property', () => {
    const result = toError({ message: 'object error', code: 42 });
    expect(result).toBeInstanceOf(Error);
    expect(result.message).toBe('object error');
  });

  it('handles null', () => {
    const result = toError(null);
    expect(result).toBeInstanceOf(Error);
    expect(result.message).toBe('null');
  });

  it('handles undefined', () => {
    const result = toError(undefined);
    expect(result).toBeInstanceOf(Error);
    expect(result.message).toBe('undefined');
  });

  it('handles a number', () => {
    const result = toError(404);
    expect(result).toBeInstanceOf(Error);
    expect(result.message).toBe('404');
  });

  it('handles an object without a message property', () => {
    const result = toError({ code: 'ERR_TIMEOUT' });
    expect(result).toBeInstanceOf(Error);
    expect(result.message).toBe('[object Object]');
  });

  it('preserves Error subclass identity', () => {
    const original = new TypeError('type mismatch');
    const result = toError(original);
    expect(result).toBe(original);
    expect(result).toBeInstanceOf(TypeError);
  });
});
