// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('./studioShell.css', import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

const declarations = (selector) => {
  const rule = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .find(([, candidate]) => candidate.trim() === selector)
  if (!rule) throw new Error(`Missing CSS rule: ${selector}`)
  return Object.fromEntries(rule[2].split(';').filter(part => part.trim()).map(part => {
    const colon = part.indexOf(':')
    return [part.slice(0, colon).trim(), part.slice(colon + 1).trim()]
  }))
}

const cockpit = declarations('.studio-shell .app[data-studio-shell="cockpit"][data-surface]')
const ground = declarations('.studio-shell:has(.app[data-studio-shell="cockpit"]) .studio-ground')
const legacyNames = [
  '--background', '--card', '--border', '--primary', '--primary-hover', '--on-accent',
  '--foreground', '--muted', '--muted-2', '--card-grad', '--accent-tint', '--accent-tint-hover',
  '--rule-strong', '--hover-tint', '--hover-tint-strong', '--well', '--well-border',
  '--well-chip', '--well-text', '--well-dim', '--well-faint', '--well-accent',
  '--well-accent-tint', '--ink', '--ink-2', '--rule',
]

describe('surface ground legacy palette', () => {
  it('uses the dark ink role for inherited text', () => {
    expect(ground.color).toBe('var(--leaf-ink)')
  })

  it.each(legacyNames)('maps %s like the cockpit with one hop through cockpit tokens', (name) => {
    expect(cockpit[name]).toBeDefined()
    const expected = cockpit[name].replace(/var\((--ck-[\w-]+)\)/g, (_, token) => {
      expect(cockpit[token], token).toBeDefined()
      return cockpit[token]
    })
    expect(ground[name], name).toBe(expected)
  })

  it('does not depend on cockpit tokens outside the app', () => {
    for (const [name, value] of Object.entries(ground)) {
      expect(value, name).not.toContain('var(--ck-')
    }
  })
})
