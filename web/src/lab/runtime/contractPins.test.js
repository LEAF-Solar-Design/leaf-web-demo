import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// npm --prefix web runs Vitest from web/. Filesystem paths avoid Vite's
// asset-URL transform and keep canonical files outside the module graph.
const root = resolve(process.cwd(), '..');
const vendor = resolve(process.cwd(), 'src/lab/contract');
const files = {
  'lab-block.v1.schema.json': 'contract/lab-block.v1.schema.json',
  'lab-pin.v1.schema.json': 'contract/lab-pin.v1.schema.json',
  'lab-graph.v1.schema.json': 'contract/lab-graph.v1.schema.json',
  'lab-embed.v1.schema.json': 'contract/lab-embed.v1.schema.json',
  'leaf-block-manifest.v1.schema.json': 'contract/fixtures/lab/leaf-block-manifest.v1.schema.json',
  'fixtures/ops-conformance.json': 'contract/fixtures/lab/ops-conformance.json',
};
const pins = JSON.parse(readFileSync(resolve(vendor, 'PIN.json'), 'utf8'));

describe('vendored Lab contracts', () => {
  it('pins exactly the required files', () => {
    expect(Object.keys(pins.files).sort()).toEqual(Object.keys(files).sort());
  });
  it.each(Object.entries(files))('%s matches its canonical bytes and sha256 pin', (name, source) => {
    const original = readFileSync(resolve(root, source));
    const vendored = readFileSync(resolve(vendor, name));
    expect(vendored.equals(original)).toBe(true);
    expect(pins.files[name]).toEqual({ source, sha256: createHash('sha256').update(original).digest('hex') });
    expect(createHash('sha256').update(vendored).digest('hex')).toBe(pins.files[name].sha256);
  });
});
