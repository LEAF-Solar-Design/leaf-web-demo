// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('./landing.css', import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')
// Match declaration blocks, including rules nested inside media queries.
const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  .map(([, selector, body]) => ({ selector: selector.trim(), body }))
const stageRules = rules.filter(({ selector }) => !/ground-board|data-leaf-theme|leaf-light/.test(selector))
const declarations = stageRules.flatMap(({ body }) => [...body.matchAll(/(?:^|;)\s*([\w-]+)\s*:\s*([^;]+)/g)]
  .map(([, property, value]) => [property, value.trim()]))

describe('front door exact Stage color roles (LS-T1)', () => {
  it('replaces the matching literal colors outside Paper scopes', () => {
    expect(stageRules.length).toBeGreaterThan(0)
    expect(declarations.length).toBeGreaterThan(0)
    for (const [property, value] of declarations) {
      expect(value, `landing.css ${property}: ${value}`)
        .not.toMatch(/#(?:a1a1aa|f4f4f5|f87171|e7b643)\b|rgba\(\s*(?:248\s*,\s*113\s*,\s*113|231\s*,\s*182\s*,\s*67)\s*,/i)
    }
  })

  it('inherits the four roles without local declarations or alias cycles', () => {
    expect(css).not.toMatch(/(?:^|[;{}])\s*--leaf-(?:ink|ink-muted|error|cad-select-warm)\s*:/)
  })

  it('preserves the light alias block', () => {
    const lightRules = rules.filter(({ selector }) => selector === '.leaf-light')
    expect(lightRules).toHaveLength(1)
    expect(lightRules[0].body).toMatch(/(?:^|;)\s*--background\s*:\s*#e9e7e0\s*;/)
    expect(lightRules[0].body).toMatch(/(?:^|;)\s*--card\s*:\s*#ffffff\s*;/)
  })

  it('reads the muted ink role in at least forty declarations', () => {
    const muted = declarations.filter(([, value]) => /var\(\s*--leaf-ink-muted\s*\)/.test(value))
    expect(muted.length).toBeGreaterThanOrEqual(40)
  })
})
