// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const liveDeclaration = '--ck-canvas-top-live: max(var(--ck-canvas-top), var(--cockpit-band-bottom, 0px));'
const alias = '--leaf-canvas-top: var(--ck-canvas-top);'
const liveAlias = '--leaf-canvas-top-live: var(--ck-canvas-top-live);'

for (const file of ['./cockpit.css', './studioShell.css']) {
  describe(`${file} live canvas top`, () => {
    const css = readFileSync(new URL(file, import.meta.url), 'utf8')
      .replace(/\r\n/g, '\n')
      .replace(/\/\*[\s\S]*?\*\//g, '')
    const blocks = [...css.matchAll(/\x7b([^\x7b\x7d]*)\x7d/g)].map(match => match[1])

    it('declares the live maximum wherever the base canvas top is declared', () => {
      const baseBlocks = blocks.filter(body => /--ck-canvas-top\s*:/.test(body))
      expect(baseBlocks.length).toBeGreaterThan(0)
      for (const body of baseBlocks) {
        expect(body.split(liveDeclaration).length - 1).toBe(1)
      }
    })

    it('limits base variable uses to live declarations and preserves both aliases', () => {
      const uses = css.split('var(--ck-canvas-top)').length - 1
      const liveDeclarations = css.split(liveDeclaration).length - 1
      const aliases = css.split(alias).length - 1
      expect(uses).toBe(liveDeclarations + aliases)
      const aliasBlocks = blocks.filter(body => /--leaf-canvas-top\s*:/.test(body))
      expect(aliasBlocks.length).toBeGreaterThan(0)
      for (const body of aliasBlocks) {
        expect(body).toContain(alias)
        expect(body).toContain(liveAlias)
      }
    })

    it('positions fixed overlays with the live canvas top', () => {
      expect(css).toContain('top: var(--ck-canvas-top-live)')
    })
  })
}

describe('./cadOverview.css live canvas top', () => {
  const css = readFileSync(new URL('./cadOverview.css', import.meta.url), 'utf8')
    .replace(/\r\n/g, '\n')
    .replace(/\/\*[\s\S]*?\*\//g, '')

  it('positions the overview with the live canvas top and static fallback', () => {
    expect(css).toContain('top: calc(var(--ck-canvas-top-live, var(--ck-canvas-top, 140px)) + 124px)')
  })

  it('limits static canvas top references to the live fallback', () => {
    const remaining = css.split('var(--ck-canvas-top-live, var(--ck-canvas-top, 140px))').join('')
    expect(remaining).not.toContain('--ck-canvas-top')
  })
})
