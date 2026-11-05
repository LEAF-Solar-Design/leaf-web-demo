import { manifests } from '../canvas/registry.js';
import { hasOperator } from './ops.js';

export const SETTINGS_DIGEST_ID = 'kashbot2-config';
export const PLACEMENTS = Object.freeze(['here', 'browser', 'server', 'bot', 'host']);
export const HOST_BLOCK_IDS = Object.freeze(new Set(['studio-jobs', 'studio-capabilities', 'host-source', 'host-intent']));
export const TRUSTED_BLOCK_IDS = Object.freeze(new Set(manifests.map(manifest => manifest.id)));

export function placement(manifest, { trustedIds = TRUSTED_BLOCK_IDS } = {}) {
  const { id, kind, effect, residency } = manifest;
  if (residency === 'host' || HOST_BLOCK_IDS.has(id)) return 'host';
  const operator = kind === 'builtin' && effect === 'pure' && hasOperator(id);
  if ((operator && trustedIds.has(id))
      || (id === SETTINGS_DIGEST_ID && kind === 'settings' && effect === 'pure')) return 'here';
  if (operator) return 'browser';
  return trustedIds.has(id) ? 'bot' : 'server';
}
