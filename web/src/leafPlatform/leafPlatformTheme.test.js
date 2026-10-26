// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, '')
const css = stripComments(read('./leafPlatform.css'))
const scene = read('./LeafPlatformScene.jsx')
const standard = stripComments(read('../standard/leaf-standard.css'))
const normalize = (selector) => selector.replace(/\s+/g, ' ').trim()
const rules = new Map([...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  .map(([, selector, body]) => [normalize(selector), Object.fromEntries(
    [...body.matchAll(/([\w-]+)\s*:\s*([^;]+);/g)]
      .map(([, property, value]) => [property, value.trim()]),
  )]))

function luminance(hex) {
  expect(hex).toMatch(/^#[\da-f]{6}$/i)
  const channels = hex.slice(1).match(/../g).map((channel) => {
    const value = parseInt(channel, 16) / 255
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
  })
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722
}

describe('Studio in AutoCAD Paper theme', () => {
  it('scopes the scene main element to Paper', () => {
    const main = scene.match(/<main\s+className="leaf-platform"(?=\s|>)[^>]*>/)?.[0]
    expect(main).toBeDefined()
    expect(main).toMatch(/\sdata-leaf-theme="paper"/)
  })

  it('contains no literal hex or RGB colors', () => {
    expect(css).not.toMatch(/#[\da-f]{3,8}\b|\brgba?\s*\(/i)
  })

  it.each([
    ['.leaf-platform', 'background', 'var(--leaf-surface)'],
    ['.leaf-platform', 'color', 'var(--leaf-ink)'],
    ['.leaf-platform select, .leaf-platform button', 'border', '1px solid var(--leaf-ink-muted)'],
    ['.leaf-platform select', 'background', 'var(--leaf-surface)'],
    ['.leaf-platform select', 'color', 'var(--leaf-ink)'],
    ['.leaf-platform button', 'background', 'var(--leaf-accent-text)'],
    ['.leaf-platform button', 'color', 'var(--leaf-on-accent)'],
    ['.leaf-platform button:disabled', 'background', 'var(--leaf-raised)'],
    ['.leaf-platform button:disabled', 'color', 'var(--leaf-ink-muted)'],
    ['.leaf-platform a', 'color', 'var(--leaf-accent-text)'],
    ['.leaf-platform :is(h1, button, select, a):focus-visible', 'outline', '3px solid var(--leaf-focus)'],
  ])('%s %s reads %s', (selector, property, value) => {
    expect(rules.get(selector)?.[property]).toBe(value)
  })

  it.each([
    ['ink', 'surface', 4.5],
    ['on-accent', 'accent-text', 4.5],
    ['accent-text', 'surface', 4.5],
    ['ink-muted', 'surface', 3],
    ['focus', 'surface', 3],
  ])('Paper %s on %s meets %s:1 contrast', (foreground, background, minimum) => {
    const paper = standard.match(/\[data-leaf-theme="paper"\]\s*\{([^{}]*)\}/)?.[1]
    expect(paper).toBeDefined()
    const roles = Object.fromEntries([...paper.matchAll(/--leaf-([\w-]+)\s*:\s*([^;]+);/g)]
      .map(([, role, value]) => [role, value.trim()]))
    const values = [foreground, background].map((role) => luminance(roles[role]))
    const contrast = (Math.max(...values) + 0.05) / (Math.min(...values) + 0.05)
    expect(contrast).toBeGreaterThanOrEqual(minimum)
  })
})
