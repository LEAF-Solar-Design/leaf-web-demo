// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const readCss = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

function declarations(css, selector) {
  const rule = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .find(([, candidate]) => candidate.trim() === selector)
  expect(rule, `Missing CSS rule: ${selector}`).toBeTruthy()
  return Object.fromEntries(rule[2].split(';').filter((entry) => entry.trim()).map((entry) => {
    const colon = entry.indexOf(':')
    return [entry.slice(0, colon).trim(), entry.slice(colon + 1).trim()]
  }))
}

function luminance(hex) {
  expect(hex).toMatch(/^#[\da-f]{6}$/i)
  const channels = hex.slice(1).match(/../g).map((channel) => {
    const value = parseInt(channel, 16) / 255
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
  })
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722
}

function contrast(a, b) {
  const [lighter, darker] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (lighter + 0.05) / (darker + 0.05)
}

describe('operator console v1 Stage roles', () => {
  it('paints its background with the Stage surface role', () => {
    const css = readCss('./operator.css')
    expect(css).not.toContain('--panel-bg')
    expect(declarations(css, '.operator-console').background).toBe('var(--leaf-surface)')
  })

  it('maps all seven shared tokens to v1 roles without color literals', () => {
    const tokens = declarations(readCss('../../styles.css'), '.operator-console')
    const expected = {
      '--muted': 'var(--leaf-ink-muted)',
      '--muted-2': 'var(--leaf-ink-muted)',
      '--primary': 'var(--leaf-accent)',
      '--accent-tint': 'var(--leaf-accent-tint)',
      '--status-warning': 'var(--leaf-warn)',
      '--hairline': 'var(--leaf-line)',
      '--hover-tint': 'var(--leaf-hover-tint)',
    }
    for (const [property, value] of Object.entries(expected)) {
      expect(tokens[property], property).toBe(value)
      expect(tokens[property], property).not.toMatch(/#[\da-f]+|rgba?\s*\(/i)
    }
  })

  it('pins the console text with the ink role at doubled-class specificity', () => {
    const rule = declarations(readCss('../../styles.css'), '.operator-console.operator-console')
    expect(rule.color).toBe('var(--leaf-ink)')
  })

  it('keeps both muted ledger text and normal ink at 4.5:1 on the Stage surface', () => {
    const stage = declarations(readCss('../../standard/leaf-standard.css'), '[data-leaf-theme="stage"]')
    for (const ink of ['--leaf-ink-muted', '--leaf-ink']) {
      expect(contrast(stage[ink], stage['--leaf-surface']), ink).toBeGreaterThanOrEqual(4.5)
    }
  })

  it('names v1 roles in the cost panel fallbacks', () => {
    const css = readCss('../CostTransparencyPanel.css')
    expect(css).toContain('var(--card, var(--leaf-surface))')
    expect(css).toContain('var(--foreground, var(--leaf-ink))')
    expect(css.match(/var\(--border, var\(--leaf-line\)\)/g)).toHaveLength(2)
    expect(css).not.toMatch(/#fff|#222|#aaa/i)
  })
})
