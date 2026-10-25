// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('./ops-workspace.css', import.meta.url), 'utf8')
  .replace(/\r\n/g, '\n')
  .replace(/\/\*[\s\S]*?\*\//g, '')
const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([, selector, body]) => ({
  selector: selector.trim(),
  declarations: [...body.matchAll(/(?:^|;)\s*([\w-]+)\s*:\s*([^;]+)(?=;|$)/g)]
    .map(([, property, value]) => [property, value.trim()]),
}))

describe('Ops dark shell roles', () => {
  it('pins the cockpit ground to the dark v1 roles exactly once', () => {
    const matches = rules.filter(({ selector }) =>
      selector === '.studio-shell .studio-ground-ops[data-studio-shell="cockpit"]',
    )
    expect(matches).toHaveLength(1)
    expect(Object.fromEntries(matches[0].declarations)).toMatchObject({
      '--leaf-card': 'var(--leaf-raised)',
      '--leaf-foreground': 'var(--leaf-ink)',
      '--leaf-border': 'var(--leaf-line)',
      '--leaf-ring': 'var(--leaf-focus)',
    })
  })

  it('uses dark role fallbacks for the workspace background and ink', () => {
    const matches = rules.filter(({ selector }) => selector === '.ops-workspace')
    expect(matches).toHaveLength(1)
    expect(Object.fromEntries(matches[0].declarations)).toMatchObject({
      background: 'var(--leaf-card, var(--leaf-raised))',
      color: 'var(--leaf-foreground, var(--leaf-ink))',
    })
  })

  it('uses tokens for colors, font sizes and border radii', () => {
    const literalColor = /#(?:[\da-f]{8}|[\da-f]{6}|[\da-f]{4}|[\da-f]{3})(?![\da-f])|\b(?:rgba?|hsla?)\(/i
    expect(rules.length).toBeGreaterThan(0)
    for (const { selector, declarations } of rules) {
      for (const [property, value] of declarations) {
        const context = `${selector} ${property}: ${value}`
        if (!property.startsWith('--leaf-')) {
          expect(value, context).not.toMatch(literalColor)
        }
        if (property === 'font-size') {
          expect(!/\d/.test(value) || value.includes('var(--leaf-size-'), context).toBe(true)
        }
        if (property === 'border-radius') {
          expect(value === '0' || !/\d/.test(value) || value.includes('var(--leaf-radius-'), context).toBe(true)
        }
      }
    }
  })
})
