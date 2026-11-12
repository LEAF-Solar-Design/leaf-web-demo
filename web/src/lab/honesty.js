import fixture from './contract/sources/leaf-lab-honesty.v1.json'

function readCodes() {
  const rows = fixture?.codes
  if (fixture?.schema !== 'leaf.lab-honesty.v1' || !Array.isArray(rows) || rows.length !== 13
      || rows.some(row => !row || typeof row.code !== 'string' || !row.code
        || typeof row.sentence !== 'string' || typeof row.scope !== 'string'
        || typeof row.withholds_result !== 'boolean')
      || new Set(rows.map(row => row.code)).size !== 13) {
    throw Error('lab_honesty_invalid')
  }
  return new Map(rows.map(row => [row.code, Object.freeze({ ...row })]))
}

const codes = readCodes()
export const HONESTY_CODES = Object.freeze([...codes.keys()])

function lookup(code) {
  if (!codes.has(code)) throw Error('unknown_code')
  return codes.get(code)
}

export function sentence(code) { return lookup(code).sentence }
export function withholds(code) { return lookup(code).withholds_result }
export function scopeOf(code) { return lookup(code).scope }
