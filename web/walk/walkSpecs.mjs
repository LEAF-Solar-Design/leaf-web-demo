export const SWEEP_SPECS = Object.freeze([
  'features.spec.mjs',
  'control-inventory.spec.mjs',
  'journeys/**/*.spec.mjs',
])

export function walkTestMatch(env = process.env) {
  return env.LEAF_WALK_PROOF === '1' ? '**/*.spec.mjs' : SWEEP_SPECS
}
