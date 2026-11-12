import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { manifests, registry } from '../canvas/registry.js';
import { transformManifests } from '../host/hostProvider.js';
import { HOST_BLOCK_IDS, PLACEMENTS, SETTINGS_DIGEST_ID, TRUSTED_BLOCK_IDS, placement } from './placement.js';

const fixture = JSON.parse(readFileSync(`${process.cwd()}/../server/tests/fixtures/lab-placement-cases.v1.json`, 'utf8'));

describe('shared placement conformance', () => {
  it('uses the versioned fixture and the snapshot id set', () => {
    expect(fixture.schema).toBe('leaf.lab-placement-cases.v1');
    expect(new Set(fixture.cases.map(item => item.name)).size).toBe(fixture.cases.length);
    expect(PLACEMENTS).toEqual(['here', 'browser', 'server', 'bot', 'host']);
    expect(SETTINGS_DIGEST_ID).toBe('kashbot2-config');
    expect([...HOST_BLOCK_IDS]).toEqual(['studio-jobs', 'studio-capabilities', 'host-source', 'host-intent']);
    expect(TRUSTED_BLOCK_IDS).toEqual(new Set(manifests.map(manifest => manifest.id)));
    for (const value of [PLACEMENTS, HOST_BLOCK_IDS, TRUSTED_BLOCK_IDS]) expect(Object.isFrozen(value)).toBe(true);
  });
  it.each(fixture.cases)('$name', item => {
    const manifest = item.manifest || registry[item.manifest_ref];
    const before = structuredClone(manifest);
    const options = item.trusted_ids === null ? {} : { trustedIds: new Set(item.trusted_ids) };
    expect(placement(manifest, options)).toBe(item.expect);
    expect(manifest).toEqual(before);
  });
  it('places the 14 strategy snapshot blocks on the bot, the 5 arithmetic builtins here and all 11 generated transforms in the browser', () => {
    expect(manifests).toHaveLength(19);
    const here = new Set(manifests.filter(manifest => placement(manifest) === 'here').map(manifest => manifest.id));
    expect(here).toEqual(new Set(['constant', 'add', 'subtract', 'multiply', 'divide']));
    for (const manifest of manifests) if (!here.has(manifest.id)) expect(placement(manifest)).toBe('bot');
    const transforms = Object.values(transformManifests());
    expect(transforms).toHaveLength(11);
    for (const manifest of transforms) expect(placement(manifest)).toBe('browser');
  });
});
