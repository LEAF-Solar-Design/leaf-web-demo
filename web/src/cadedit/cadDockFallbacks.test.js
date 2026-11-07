// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

function readCss(path) {
  return readFileSync(new URL(path, import.meta.url), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')
}

function declarations(body) {
  return [...body.matchAll(/(?:^|;)\s*([\w-]+)\s*:\s*([^;]+)(?=;|$)/g)]
    .map(([, property, value]) => [property, value.trim()])
}

const stylesheets = [
  ['ObjectSnapMenu.css', './ObjectSnapMenu.css'],
  ['drawingObjectList.css', './drawingObjectList.css'],
  ['cadOverview.css', '../site/cadOverview.css'],
].map(([file, path]) => ({ file, css: readCss(path) }))
const shells = [
  ['../site/cockpit.css', '.studio-shell .app:is([data-surface="cad"], [data-surface="solar"])'],
  ['../site/studioShell.css', '.studio-shell .app[data-studio-shell="cockpit"][data-surface]'],
].map(([path, selector]) => {
  // Later surface and responsive overrides intentionally use different roles.
  // The fallback contract follows the first shared palette block in each file.
  const matches = [...readCss(path).matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .filter(([, candidate]) => candidate.trim() === selector)
  expect(matches.length, `${path} shared palette block`).toBeGreaterThan(0)
  return matches[0][2]
})
const rules = stylesheets.flatMap(({ file, css }) =>
  [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([, selector, body]) => ({
    file, selector: selector.trim(), declarations: declarations(body),
  })))

// These existing geometry/font fallbacks are outside the color-role contract.
const nonColorTokens = new Set([
  '--ck-pane', '--ck-canvas-top', '--ck-canvas-top-live', '--ck-dock', '--ck-drawer', '--ck-font',
])

function varFallbacks(css) {
  return [...css.matchAll(/\bvar\(/g)].flatMap((match) => {
    const start = match.index + match[0].length
    let depth = 1
    let comma = -1
    let end = start
    for (; end < css.length; end += 1) {
      if (css[end] === '(') depth += 1
      if (css[end] === ')') depth -= 1
      if (depth === 0) break
      if (css[end] === ',' && depth === 1 && comma === -1) comma = end
    }
    expect(depth, `balanced var() at ${match.index}`).toBe(0)
    if (comma === -1) return []
    return [{ token: css.slice(start, comma).trim(), fallback: css.slice(comma + 1, end).trim() }]
  })
}

function shellRole(body, token) {
  const values = declarations(body)
    .filter(([property]) => property === token).map(([, value]) => value)
  expect(values.length, `${token} must be declared in each shell`).toBeGreaterThan(0)
  for (const value of values) expect(value).toMatch(/^var\(--leaf-[\w-]+\)$/)
  expect(new Set(values).size, `${token} must have one role`).toBe(1)
  return values[0]
}

function hexRgb(value) {
  expect(value).toMatch(/^#(?:[\da-f]{3}|[\da-f]{6})$/i)
  const hex = value.slice(1)
  const expanded = hex.length === 3 ? [...hex].map((digit) => digit + digit).join('') : hex
  return [0, 2, 4].map((offset) => parseInt(expanded.slice(offset, offset + 2), 16))
}

function luminance(rgb) {
  const linear = rgb.map((channel) => {
    const srgb = channel / 255
    return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4
  })
  return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722
}

describe('CAD dock color fallbacks', () => {
  it.each(stylesheets)('$file fallbacks match the roles declared by both shells', ({ css }) => {
    const colors = varFallbacks(css).filter(({ token }) => token.startsWith('--ck-') && !nonColorTokens.has(token))
    expect(colors.length).toBeGreaterThan(0)
    for (const { token, fallback } of colors) {
      const roles = shells.map((shell) => shellRole(shell, token))
      expect(roles[0], `${token} shell roles agree`).toBe(roles[1])
      expect(fallback, `${token} fallback`).toBe(roles[0])
    }
  })

  it('keeps exactly the four recorded color literals, including those inside fallbacks', () => {
    const literals = rules.flatMap(({ file, selector, declarations: entries }) =>
      entries.flatMap(([property, value]) =>
        [...value.matchAll(/#[\da-f]+\b|\b(?:rgba?|hsla?)\([^)]*\)/gi)]
          .map(([literal]) => [file, selector, property, literal])))
    expect(literals).toEqual([
      ['ObjectSnapMenu.css', '.object-snap-popup', 'box-shadow', 'rgba(0, 0, 0, 0.45)'],
      ['cadOverview.css', '.cad-overview-viewport', 'fill', '#fff'],
      ['cadOverview.css', '.cad-overview-viewport', 'stroke', '#fff'],
      ['cadOverview.css', '.cad-overview-focus', 'stroke', '#f0c97b'],
    ])
  })

  it('uses muted body ink for snap help', () => {
    const help = rules.filter(({ file, selector }) => file === 'ObjectSnapMenu.css' && selector === '.object-snap-help')
    expect(help).toHaveLength(1)
    expect(Object.fromEntries(help[0].declarations).color).toBe('var(--ck-ink-dim, var(--leaf-ink-muted))')
  })

  it('keeps dock text at WCAG AA over ground and CAD canvas', () => {
    const standard = readCss('../standard/leaf-standard.css')
    const root = standard.match(/:root\s*\{([^}]*)\}/)
    expect(root).not.toBeNull()
    const roles = Object.fromEntries(declarations(root[1]))
    const glass = roles['--leaf-glass-dock'].match(/^rgba\(\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)\s*\)$/)
    expect(glass).not.toBeNull()
    const [red, green, blue, alpha] = glass.slice(1).map(Number)
    expect(alpha).toBeGreaterThanOrEqual(0)
    expect(alpha).toBeLessThanOrEqual(1)
    for (const background of ['--leaf-ground', '--leaf-cad-canvas']) {
      const base = hexRgb(roles[background])
      const composite = [red, green, blue].map((channel, index) => channel * alpha + base[index] * (1 - alpha))
      const backdrop = luminance(composite)
      for (const ink of ['--leaf-ink', '--leaf-ink-muted', '--leaf-warn']) {
        const foreground = luminance(hexRgb(roles[ink]))
        const contrast = (Math.max(foreground, backdrop) + 0.05) / (Math.min(foreground, backdrop) + 0.05)
        expect(contrast, `${ink} on dock over ${background}`).toBeGreaterThanOrEqual(4.5)
      }
    }
  })
})
