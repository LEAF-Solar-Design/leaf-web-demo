// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
const sheets = read('./sheets.css')
const standard = read('../standard/leaf-standard.css')
const ops = read('../integrations/ops-workspace.css')
const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, '')

describe('Sheets hint contrast and Ops workspace font', () => {
  it('preserves the one recorded hint color declaration from d20fe8ef', () => {
    expect(stripComments(sheets).match(/\bcolor:\s*#3f3f46\s*(?:;|})/g)).toHaveLength(1)
    expect(sheets).toContain('color: #3f3f46;')
  })

  it('ends with a scoped hint rule using the muted ink role', () => {
    expect(stripComments(sheets).trim()).toMatch(
      /\.sheets-root\s+\.sheet-index-row\s+\.hint\s*\{\s*color:\s*var\(--leaf-ink-muted\)\s*;?\s*\}$/,
    )
  })

  it('uses the standard text font in the Ops workspace', () => {
    expect(ops).not.toContain('system-ui')
    expect(ops).toContain('font-family: var(--leaf-font-text)')
  })

  it('computes Stage hint ink as rgb(161, 161, 170)', () => {
    const style = document.createElement('style')
    style.textContent = `${standard}\n${sheets}`
    const root = document.createElement('div')
    root.className = 'sheets-root'
    root.innerHTML = '<div class="sheet-index-row"><span class="hint">Sheet hint</span></div>'
    document.head.append(style)
    document.body.append(root)

    try {
      const hint = root.querySelector('.hint')
      // jsdom 25 does not resolve var() colors. Materialize the role from the
      // injected cascade, preserving each matching rule's selector and order.
      let ink = ''
      for (let node = hint; node && !ink; node = node.parentElement) {
        ink = getComputedStyle(node).getPropertyValue('--leaf-ink-muted').trim()
      }
      expect(ink).toBe('#a1a1aa')
      for (const rule of style.sheet.cssRules) {
        if (rule.selectorText && hint.matches(rule.selectorText)
          && rule.style.getPropertyValue('color').trim() === 'var(--leaf-ink-muted)') {
          rule.style.setProperty('color', ink)
        }
      }
      // Reinsert the rules so jsdom invalidates its computed-style cache.
      style.textContent = Array.from(style.sheet.cssRules, (rule) => rule.cssText).join('\n')
      expect(getComputedStyle(hint).color).toBe('rgb(161, 161, 170)')
    } finally {
      root.remove()
      style.remove()
    }
  })
})
