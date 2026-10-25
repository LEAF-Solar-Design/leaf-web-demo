// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8').replace(/\r\n/g, '\n')
const cockpit = read('./cockpit.css').replace(/\/\*[\s\S]*?\*\//g, '')
const scope = '.studio-shell .app[data-studio-shell="cockpit"][data-drawer][data-surface]'
const phoneBlocks = [...cockpit.matchAll(/@media \(max-width: 600px\)\s*\{((?:[^{}]|\{[^{}]*\})*)\}/g)]
const phone = phoneBlocks[0]?.[1] || ''
const rules = [...phone.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
const ruleBody = (selector) => rules
  .filter(([, head]) => head.trim() === `${scope}${selector}`)
  .map(([, , body]) => body).join('\n')

describe('phone shell targets (L8, L9)', () => {
  it('reserves a pinned 140 px dock for the prompt and 96 px command bar', () => {
    expect(phoneBlocks).toHaveLength(1)
    expect(ruleBody('')).toMatch(/--phone-command-height:\s*140px;/)
    expect(ruleBody('')).toMatch(/--ck-dock:\s*calc\(var\(--phone-command-height\) \+ var\(--ck-status\) \+ 4px\);/)
    const dock = ruleBody(' .bar-dock')
    expect(dock).toMatch(/position:\s*fixed;/)
    expect(dock).toMatch(/bottom:\s*calc\(var\(--ck-status\) \+ 4px\);/)
    expect(dock).toMatch(/height:\s*var\(--phone-command-height\);/)
    const bar = ruleBody(' .bar.bar-command-line')
    expect(bar).toMatch(/flex:\s*0 0 96px;/)
    expect(bar).toMatch(/(?:^|;)\s*height:\s*96px;/)
    expect(bar).toMatch(/min-height:\s*96px;/)
    expect(ruleBody(' .bar-command-line .bar-input')).toMatch(/height:\s*52px;/)
  })

  it('keeps a 44 px controls row scrollable without wrapping', () => {
    const controls = ruleBody(' .bar-command-line .bar-controls')
    expect(controls).toMatch(/flex:\s*0 0 44px;/)
    expect(controls).toMatch(/(?:^|;)\s*height:\s*44px;/)
    expect(controls).toMatch(/flex-wrap:\s*nowrap;/)
    expect(controls).toMatch(/overflow-x:\s*auto;/)
    expect(controls).toMatch(/overflow-y:\s*hidden;/)
    expect(controls).toMatch(/scrollbar-width:\s*none;/)
  })

  it('gives command controls, quick-file buttons and document add 44 px targets', () => {
    const controls = ' .bar-command-line .bar-controls :is(button, .chip-act, .bar-add, .bar-scope)'
    const quick = ' .cockpit-quick button'
    const add = ' footer.foot-bar .foot-doc-add'
    for (const selector of [controls, quick, add]) {
      const body = ruleBody(selector)
      expect(body, selector).toMatch(/min-height:\s*44px;/)
      expect(body, selector).toMatch(/min-width:\s*44px;/)
    }
    for (const selector of [controls, quick]) {
      expect(ruleBody(selector), selector).toMatch(/flex-shrink:\s*0;/)
    }
    for (const selector of [controls, add]) {
      expect(ruleBody(selector), selector).toMatch(/box-sizing:\s*border-box;/)
    }
  })

  for (const file of ['../localCad/LocalCadConnect.jsx', './NavRail.jsx']) {
    it(`${file} has no numeric inline font size below 12`, () => {
      const source = read(file)
      const sizes = [...source.matchAll(/\bfontSize\s*:\s*(-?(?:\d*\.)?\d+)\b/g)]
      expect(sizes.filter(([, size]) => Number(size) < 12)).toEqual([])
      expect(source).toContain("fontSize: 'var(--leaf-size-caption)'")
    })
  }
})
