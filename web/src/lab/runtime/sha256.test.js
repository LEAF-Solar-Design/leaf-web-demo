import { createHash, webcrypto } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { digest } from '../contract.js';
import { sha256Hex } from './sha256.js';

const fixture = JSON.parse(readFileSync(`${process.cwd()}/../contract/fixtures/lab/digest-vectors.json`, 'utf8'));
afterEach(() => vi.unstubAllGlobals());

describe('synchronous SHA-256', () => {
  it.each(fixture.vectors)('$name matches the canonical digest and Web Crypto', async vector => {
    vi.stubGlobal('crypto', webcrypto);
    expect(sha256Hex(vector.canonical)).toBe(vector.sha256);
    expect(sha256Hex(vector.canonical)).toBe(await digest(vector.input));
  });
  it.each([0, 1, 55, 56, 63, 64, 65, 119, 120, 127, 128, 10000])('handles padding at %i bytes', length => {
    const text = 'a'.repeat(length);
    expect(sha256Hex(text)).toBe(createHash('sha256').update(text).digest('hex'));
  });
  it.each(['é', '😀', '\ud800', '\udc00', 'a\ud800b', '😀é'.repeat(100)])('encodes UTF-8 text %j', text => {
    expect(sha256Hex(text)).toBe(createHash('sha256').update(text).digest('hex'));
  });
  it.each([undefined, null, 1, true, {}, [], new String('text')])('refuses non-string input %j', value => {
    expect(() => sha256Hex(value)).toThrow('sha256_text_required');
  });
});
