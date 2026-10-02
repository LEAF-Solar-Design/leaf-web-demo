// Python re's whitespace set, including the four information separators.
export const MODEL_WHITESPACE = Object.freeze([
  0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x001C, 0x001D, 0x001E, 0x001F,
  0x0020, 0x0085, 0x00A0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004,
  0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200A, 0x2028, 0x2029, 0x202F,
  0x205F, 0x3000,
].map((point) => String.fromCodePoint(point)))

const DECIMAL = /^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$(?![\s\S])/

function rating(raw) {
  if (typeof raw !== 'number' && (typeof raw !== 'string' || !DECIMAL.test(raw))) return null
  const value = typeof raw === 'number' ? raw : Number(raw)
  return Number.isFinite(value) && value > 0 && value <= 1_000_000 ? value : null
}

export function validateHardware(raw) {
  const model = raw?.model
  if (typeof model !== 'string' || [...model].length < 1 || [...model].length > 128
    || ![...model].some((point) => !MODEL_WHITESPACE.includes(point))) {
    return { ok: false, field: 'model', reason: 'Enter a model with 1 to 128 code points and at least one non-whitespace character.' }
  }
  const hardware = { model }
  for (const field of ['max_dc_voltage', 'max_ac_power_kw']) {
    const value = rating(raw?.[field])
    if (value === null) return { ok: false, field, reason: 'Enter a decimal number greater than 0 and at most 1000000.' }
    hardware[field] = value
  }
  return { ok: true, hardware }
}

export function placementParams(graphRev, hardware) {
  if (!Number.isInteger(graphRev) || graphRev < 0 || graphRev > 2_147_483_647) return null
  const validated = validateHardware(hardware)
  return validated.ok ? { expected_rev: graphRev, hardware: validated.hardware } : null
}
