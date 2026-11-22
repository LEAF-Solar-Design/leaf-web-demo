// Time budgets are ceilings, not waits: default 3, range 1 to 10, fails closed.
export function parseTestTimeScale(raw) {
  if (raw === undefined || raw === '') return 3
  const scale = Number(raw)
  if (!Number.isFinite(scale) || scale < 1 || scale > 10) {
    throw new Error('LEAF_TEST_TIME_SCALE must be a number from 1 to 10')
  }
  return scale
}

export const TEST_TIME_SCALE = parseTestTimeScale(globalThis.process?.env?.LEAF_TEST_TIME_SCALE)

export function scaleTimeout(ms) {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms <= 0) {
    throw new TypeError('Timeout must be a finite number greater than 0')
  }
  return Math.round(ms * TEST_TIME_SCALE)
}
