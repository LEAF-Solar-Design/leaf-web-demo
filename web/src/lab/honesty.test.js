// @vitest-environment node
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { describe, expect, it, vi } from 'vitest'
import fixture from './contract/sources/leaf-lab-honesty.v1.json'
import { HONESTY_CODES, sentence, withholds, scopeOf } from './honesty.js'

describe('STD-9 honesty', () => {
  it('pins identical fixture bytes to the merged upstream source', () => {
    const server = readFileSync(new URL('../../../contract/fixtures/lab/leaf-lab-honesty.v1.json', import.meta.url))
    const browser = readFileSync(new URL('./contract/sources/leaf-lab-honesty.v1.json', import.meta.url))
    expect(browser.equals(server)).toBe(true)
    const sources = JSON.parse(readFileSync(new URL('../../../contract/fixtures/lab/SOURCES.json', import.meta.url), 'utf8'))
    const rows = sources.sources.filter(row => row.standard === 'STD-9')
    expect(rows).toHaveLength(1)
    expect(rows[0]).toEqual({
      repo: 'mushy-code', head: 'f960f9bdd8fcd7abd19089af685d11d4049b0386',
      upstream_path: 'contract/fixtures/leaf-lab-honesty.v1.json',
      frozen_path: 'contract/fixtures/lab/leaf-lab-honesty.v1.json',
      sha256: '209c2558759ca90a9328d3e3400bb16bafb067c6cd6a7229376984c099ac4831',
      bytes: 20749, standard: 'STD-9',
    })
    expect(browser.length).toBe(rows[0].bytes)
    expect(createHash('sha256').update(browser).digest('hex')).toBe(rows[0].sha256)
  })

  it('exports frozen codes in fixture order', () => {
    expect(HONESTY_CODES).toEqual(fixture.codes.map(row => row.code))
    expect(new Set(HONESTY_CODES).size).toBe(13)
    expect(Object.isFrozen(HONESTY_CODES)).toBe(true)
  })

  it.each(fixture.codes)('matches every field for $code', row => {
    expect(sentence(row.code)).toBe(row.sentence)
    expect(withholds(row.code)).toBe(row.withholds_result)
    expect(scopeOf(row.code)).toBe(row.scope)
  })

  it('preserves the required sentences exactly', () => {
    expect(sentence('runs_on_bot')).toBe('Runs on the bot. Not run here.')
    expect(sentence('replay_is_direction')).toBe('Direction only. Never evidence and never a gate.')
    expect(sentence('browser_only_block')).toBe("Runs here as you edit. The Lab server's Run does not accept it yet.")
  })

  it.each([sentence, withholds, scopeOf])('rejects unknown codes in every lookup', lookup => {
    for (const code of ['nope', '__proto__', 'constructor', null]) {
      expect(() => lookup(code)).toThrow(/^unknown_code$/)
    }
  })

  it.each(['null', 'schema', 'missing_codes', 'not_array', 'count', 'duplicate', 'code', 'sentence', 'flag'])(
    'fails closed for an invalid fixture: %s', async invalid => {
      let data = structuredClone(fixture)
      if (invalid === 'null') data = null
      else if (invalid === 'schema') data.schema = 'wrong'
      else if (invalid === 'missing_codes') delete data.codes
      else if (invalid === 'not_array') data.codes = {}
      else if (invalid === 'count') data.codes.pop()
      else if (invalid === 'duplicate') data.codes[12] = data.codes[0]
      else data.codes[0][invalid === 'flag' ? 'withholds_result' : invalid] = null
      vi.resetModules()
      vi.doMock('./contract/sources/leaf-lab-honesty.v1.json', () => ({ default: data }))
      try {
        await expect(import('./honesty.js')).rejects.toThrow(/^lab_honesty_invalid$/)
      } finally {
        vi.doUnmock('./contract/sources/leaf-lab-honesty.v1.json')
        vi.resetModules()
      }
    },
  )
})
