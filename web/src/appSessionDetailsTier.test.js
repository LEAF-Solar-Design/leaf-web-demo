import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

describe('session details tier', () => {
  it('an unknown tier reads not available instead of a bare separator', () => {
    const source = readFileSync(resolve(process.cwd(), 'src/App.jsx'), 'utf8')

    expect(source).toContain("const tierDisplay = tier || 'not available'")
    expect(source).not.toContain("tier || '\u00b7'")
  })
})
