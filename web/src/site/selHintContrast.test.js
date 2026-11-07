// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('../styles.css', import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

describe('base selection hint contrast', () => {
  it('keeps the CAD dim ink at full opacity', () => {
    const rules = [...css.matchAll(/^\s*\.sel-hint\s*\{([^{}]*)\}/gm)]
    expect(rules).toHaveLength(1)

    const declarations = [...rules[0][1].matchAll(/(?:^|;)\s*([\w-]+)\s*:\s*([^;]+)/g)]
      .map(([, property, value]) => [property, value.trim()])
    expect(declarations.filter(([property]) => property === 'opacity'))
      .toEqual([['opacity', '1']])
    expect(declarations.filter(([property]) => property === 'color'))
      .toEqual([['color', 'var(--leaf-cad-ink-dim)']])
  })
})
