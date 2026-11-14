// Budgets are ceilings, not waits. LEAF_E2E_TIME_SCALE overrides (1 to 10).
export const TIME_SCALE = (() => {
  const raw = process.env.LEAF_E2E_TIME_SCALE
  if (raw === undefined || raw === '') return 3
  const value = Number(raw)
  if (!Number.isFinite(value) || value < 1 || value > 10) throw new Error('LEAF_E2E_TIME_SCALE must be a number from 1 to 10')
  return value
})()

export const scaled = (ms) => ms * TIME_SCALE
