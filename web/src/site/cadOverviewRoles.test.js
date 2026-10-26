// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('./cadOverview.css', import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')
const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([, selector, body]) => ({
  selector: selector.trim(),
  declarations: [...body.matchAll(/(?:^|;)\s*([\w-]+)\s*:\s*([^;]+)(?=;|$)/g)]
    .map(([, property, value]) => [property, value.trim()]),
}))

function stripVarFallbacks(source) {
  const stack = []
  let result = ''
  for (let index = 0; index < source.length; index += 1) {
    const character = source[index]
    if (character === '(') {
      stack.push({ isVar: /\bvar$/i.test(source.slice(0, index)), fallback: false })
    } else if (character === ')') {
      stack.pop()
    } else if (character === ',' && stack.at(-1)?.isVar) {
      stack.at(-1).fallback = true
    }
    if (!stack.some(({ fallback }) => fallback)) result += character
  }
  return result
}

describe('CAD overview v1 roles', () => {
  it.each([
    ['.cad-overview-outline', 'fill', '--leaf-cad-canvas'],
    ['.cad-overview-outline', 'stroke', '--leaf-cad-ink-dim'],
    ['.cad-overview-context', 'fill', '--leaf-cad-ink-dim'],
    ['.cad-overview-context', 'stroke', '--leaf-cad-ink-dim'],
    ['.cad-overview-selected', 'stroke', '--leaf-cad-selection'],
    ['.cad-overview-focus path', 'fill', '--leaf-cad-canvas'],
  ])('%s %s reads %s', (selector, property, role) => {
    const matches = rules.filter((rule) => rule.selector === selector)
    expect(matches).toHaveLength(1)
    expect(Object.fromEntries(matches[0].declarations)[property]).toBe(`var(${role})`)
  })

  it('removes exact CAD color literals outside var fallbacks', () => {
    expect(stripVarFallbacks(css)).not.toMatch(/#(?:212830|b8b8b8|00b4ff)\b/i)
  })

  it('keeps the focus accent literal', () => {
    expect(css).toContain('#f0c97b')
  })
})
