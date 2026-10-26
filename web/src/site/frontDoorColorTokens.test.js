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

  it('converges the old neutrals and greens onto Stage roles outside Paper scopes', () => {
    for (const [property, value] of declarations) {
      expect(value, `landing.css ${property}: ${value}`)
        .not.toMatch(/#(?:0a0a0a|050605|0c0c0e|0d100e|0f0f11|101013|111113|141416|151b18|16181d|17171a|18181b|1c1c20|1f1f23|27272a|fff|ffffff|e4e4e7|949494|71717a|22c55e|4ade80)\b/i)
    }
  })

  it('uses the semantic success and primary roles in the Stage alias block', () => {
    const aliasRules = stageRules.filter(({ selector, body }) => selector === '.stage-root'
      && /(?:^|;)\s*--background\s*:/.test(body))
    expect(aliasRules).toHaveLength(1)
    expect(aliasRules[0].body).toMatch(/(?:^|;)\s*--status-success\s*:\s*var\(--leaf-ok\)\s*;/)
    expect(aliasRules[0].body).toMatch(/(?:^|;)\s*--primary\s*:\s*var\(--leaf-accent\)\s*;/)
  })

  it('has no reciprocal custom property aliases anywhere in the file', () => {
    const aliases = [...css.matchAll(/(?:^|[;{}])\s*(--[\w-]+)\s*:\s*var\(\s*(--[\w-]+)\s*\)\s*(?=;|})/g)]
      .map(([, property, target]) => [property, target])
    expect(aliases.length).toBeGreaterThan(0)
    const edges = new Set(aliases.map(([property, target]) => `${property}:${target}`))
    for (const [property, target] of aliases) {
      if (!target.startsWith('--leaf-')) continue
      expect(edges.has(`${target}:${property}`), `landing.css ${property} and ${target} form an alias cycle`).toBe(false)
    }
  })

  it('converges status hues and zinc text onto Stage roles outside Paper scopes', () => {
    expect(declarations.length).toBeGreaterThan(0)
    for (const [property, value] of declarations) {
      expect(value, `landing.css ${property}: ${value}`)
        .not.toMatch(/#(?:facc15|fbbf24|e24947|fca5a5|43b2e1|d4d4d8)\b|rgba\(\s*(?:251\s*,\s*191\s*,\s*36|245\s*,\s*158\s*,\s*11|226\s*,\s*73\s*,\s*71)\s*,/i)
    }
  })

  it('uses the semantic error and info roles in the Stage alias block', () => {
    const aliasRules = stageRules.filter(({ selector, body }) => selector === '.stage-root'
      && /(?:^|;)\s*--background\s*:/.test(body))
    expect(aliasRules).toHaveLength(1)
    expect(aliasRules[0].body).toMatch(/(?:^|;)\s*--status-danger\s*:\s*var\(--leaf-error\)\s*;/)
    expect(aliasRules[0].body).toMatch(/(?:^|;)\s*--status-info\s*:\s*var\(--leaf-info\)\s*;/)
  })

  it('preserves the pinned coral and studio accent colors', () => {
    expect(css).toMatch(/#f0997b\b/i)
    expect(css).toMatch(/rgba\(\s*240\s*,\s*153\s*,\s*123\s*,\s*\.12\s*\)/i)
    expect(css).toMatch(/rgba\(\s*240\s*,\s*153\s*,\s*123\s*,\s*\.35\s*\)/i)
    expect(css).toMatch(/#7fd6a6\b/i)
  })

  it('replaces translucent neutral ink outside Paper scopes', () => {
    expect(declarations.length).toBeGreaterThan(0)
    for (const [property, value] of declarations) {
      expect(value, `landing.css ${property}: ${value}`)
        .not.toMatch(/rgba\(\s*(?:240\s*,\s*242\s*,\s*240|255\s*,\s*255\s*,\s*255)\s*,/i)
    }
  })

  it('mixes every translucent Leaf role with a bounded percentage and transparent', () => {
    const mixes = [...css.matchAll(/color-mix\(\s*in\s+srgb\s*,\s*var\(\s*--leaf-[\w-]+\s*\)([^)]*)\)/g)]
    const starts = [...css.matchAll(/color-mix\(\s*in\s+srgb\s*,\s*var\(\s*--leaf-/g)]
    expect(starts.length).toBeGreaterThan(0)
    expect(mixes.length).toBe(starts.length)
    for (const [mix, tail] of mixes) {
      const percentage = tail.match(/^\s+([+-]?(?:\d+(?:\.\d+)?|\.\d+))%\s*,\s*transparent\s*$/)
      expect(percentage, `landing.css ${mix}`).not.toBeNull()
      expect(Number(percentage[1]), mix).toBeGreaterThanOrEqual(0)
      expect(Number(percentage[1]), mix).toBeLessThanOrEqual(100)
    }
  })

  it('preserves the pinned seven-percent green tint', () => {
    expect(css).toContain('rgba(34, 197, 94, .07)')
  })

  it('preserves the light alias block', () => {
    const lightRules = rules.filter(({ selector }) => selector === '.leaf-light')
    expect(lightRules).toHaveLength(1)
    expect(lightRules[0].body).toMatch(/(?:^|;)\s*--background\s*:\s*#e9e7e0\s*;/)
    expect(lightRules[0].body).toMatch(/(?:^|;)\s*--card\s*:\s*#ffffff\s*;/)
    expect(lightRules[0].body).toMatch(/(?:^|;)\s*--border\s*:\s*#d6d4cb\s*;/)
    expect(lightRules[0].body).toMatch(/(?:^|;)\s*--primary\s*:\s*#1e6b45\s*;/)
  })

  it('reads the muted ink role in at least forty declarations', () => {
    const muted = declarations.filter(([, value]) => /var\(\s*--leaf-ink-muted\s*\)/.test(value))
    expect(muted.length).toBeGreaterThanOrEqual(40)
  })
})
