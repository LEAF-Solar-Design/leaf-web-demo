// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('./sheets.css', import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

describe('Sheets v1 Stage color roles', () => {
  it('replaces the P5f opaque palette literals', () => {
    expect(css).not.toMatch(/#(?:0a0a0a|0c0c0e|101013|111113|141416|17171a|18181b|1c1c20|1f1f23|27272a|fff(?:fff)?|a1a1aa|71717a|22c55e|4ade80|04140a|e7b643|e24947|43b2e1)\b/i)
  })

  it('replaces the accent, ink, warning and danger rgba families', () => {
    expect(css).not.toMatch(/rgba\(\s*(?:34\s*,\s*197\s*,\s*94|255\s*,\s*255\s*,\s*255|231\s*,\s*182\s*,\s*67|226\s*,\s*73\s*,\s*71)\s*,/i)
  })

  it('preserves the excluded hint and hero colors', () => {
    expect(css).toMatch(/#3f3f46\b/i)
    expect(css).toMatch(/#0b0d10\b/i)
  })

  it('re-pins the root accent, success and muted roles', () => {
    const rootBlocks = [...css.matchAll(/(?:^|})\s*\.sheets-root\s*\{([^{}]*)\}/g)]
      .map(([, body]) => body)
    const repin = rootBlocks.find(body => /--primary\s*:/.test(body))
    expect(repin).toBeDefined()
    for (const [property, role] of [
      ['primary', 'accent'],
      ['primary-hover', 'accent-hover'],
      ['status-success', 'ok'],
      ['muted-2', 'ink-muted'],
    ]) {
      expect(repin).toMatch(new RegExp(`--${property}\\s*:\\s*var\\(--leaf-${role}\\)\\s*;`))
    }
  })

  it('does not redeclare the shared leaf roles', () => {
    expect(css).not.toMatch(/--leaf-[\w-]+\s*:/i)
  })
})
