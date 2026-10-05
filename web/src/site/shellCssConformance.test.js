// @vitest-environment node
//
// Shell CSS conformance (uiqol S3: A3, A4, A21, B13). Source pins on the
// shell's own stylesheets and index.html, read as text the way
// siteRootOneShell.test.js reads studioShell.css. Comments are stripped first
// so a commented-out copy of a required rule can never satisfy a pin.
//   - viewport-fit=cover, and the phone dock, toast and foot bar pad with
//     env(safe-area-inset-*) so they clear the home indicator;
//   - no bare 100vh (the mobile URL bar makes it taller than the screen);
//   - the motion standard section 8 reduced-motion rule at the root, on every
//     element and both pseudo-elements, including iteration-count;
//   - no :hover outside (hover: hover) and (pointer: fine), so a tap never
//     leaves a sticky hover state on touch;
//   - touch-action, tap highlight and the .985 press rule, canvas and sliders
//     exempt;
//   - prefers-contrast, forced-colors and prefers-reduced-transparency each
//     remap onto existing tokens, never on the root token block.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const raw = (rel) => readFileSync(new URL(rel, import.meta.url), 'utf8').replace(/\r\n/g, '\n')
const stripCss = (src) => src.replace(/\/\*[\s\S]*?\*\//g, '')
const stripHtml = (src) => src.replace(/<!--[\s\S]*?-->/g, '')

const styles = stripCss(raw('../styles.css'))
const shell = stripCss(raw('./studioShell.css'))
const html = stripHtml(raw('../../index.html'))

// A small brace walker, not a full CSS parser. Grouping at-rules (@media,
// @supports, @layer, @container) recurse; every other at-rule body
// (@keyframes, @font-face) is skipped whole. Quoted strings are copied
// through so a brace inside content: '...' cannot unbalance it.
const GROUPING = /^@(media|supports|layer|container)\b/
export function parseCss(css) {
  let i = 0
  const readBody = () => {
    const start = i
    let depth = 1
    while (i < css.length && depth) {
      const c = css[i]
      if (c === '"' || c === "'") { i = css.indexOf(c, i + 1) + 1 || css.length; continue }
      if (c === '{') depth += 1
      else if (c === '}') depth -= 1
      i += 1
    }
    if (depth) throw new Error('unbalanced braces')
    return css.slice(start, i - 1)
  }
  const block = (media) => {
    const nodes = []
    let prelude = ''
    while (i < css.length) {
      const c = css[i]
      if (c === '"' || c === "'") {
        const end = css.indexOf(c, i + 1) + 1 || css.length
        prelude += css.slice(i, end)
        i = end
        continue
      }
      if (c === '}') { i += 1; return nodes }
      if (c === ';' && prelude.trim().startsWith('@')) { prelude = ''; i += 1; continue }
      if (c === '{') {
        i += 1
        const head = prelude.trim()
        prelude = ''
        if (GROUPING.test(head)) nodes.push({ kind: 'group', prelude: head, media, children: block([...media, head]) })
        else if (head.startsWith('@')) nodes.push({ kind: 'at', prelude: head, media, body: readBody() })
        else nodes.push({ kind: 'rule', selector: head, media, body: readBody() })
        continue
      }
      prelude += c
      i += 1
    }
    return nodes
  }
  const walk = (nodes) => nodes.flatMap((n) => (n.kind === 'group' ? [n, ...walk(n.children)] : [n]))
  return walk(block([]))
}

const styleNodes = parseCss(styles)
const shellNodes = parseCss(shell)
const rulesOf = (nodes) => nodes.filter((n) => n.kind === 'rule')
const decl = (body, prop) => {
  const m = body.match(new RegExp(`(?:^|;|\\s)${prop.replace(/[-]/g, '\\-')}\\s*:\\s*([^;]+);`))
  return m ? m[1].trim() : null
}
const HOVER_GATE = (prelude) => /\(\s*hover\s*:\s*hover\s*\)/.test(prelude) && /\(\s*pointer\s*:\s*fine\s*\)/.test(prelude)
const SAFE_BOTTOM = /env\(\s*safe-area-inset-bottom\b/
const PHONE = (media) => media.some((p) => /max-width\s*:\s*980px/.test(p))

describe('A3/B13 viewport and safe area', () => {
  it('the viewport meta draws under the notch with viewport-fit=cover', () => {
    const meta = html.match(/<meta\s+name="viewport"\s+content="([^"]*)"/)
    expect(meta).not.toBeNull()
    expect(meta[1].split(',').map((s) => s.trim())).toContain('viewport-fit=cover')
  })

  it('the phone command dock clears the bottom and side insets in both sheets', () => {
    const docks = rulesOf(styleNodes).filter((r) => r.selector === '.bar-dock' && PHONE(r.media))
    expect(docks.length).toBeGreaterThan(0)
    for (const r of docks) {
      expect(decl(r.body, 'bottom')).toMatch(SAFE_BOTTOM)
      expect(decl(r.body, 'left')).toMatch(/env\(\s*safe-area-inset-left\b/)
      expect(decl(r.body, 'right')).toMatch(/env\(\s*safe-area-inset-right\b/)
    }
    const studioDocks = rulesOf(shellNodes).filter((r) => r.selector.endsWith(' .bar-dock') && PHONE(r.media))
    expect(studioDocks.length).toBe(2)
    for (const r of studioDocks) expect(decl(r.body, 'bottom')).toMatch(SAFE_BOTTOM)
  })

  it('the toast clears the bottom inset', () => {
    const toast = rulesOf(styleNodes).find((r) => r.selector === '.toast' && r.media.length === 0)
    expect(toast).toBeDefined()
    expect(decl(toast.body, 'bottom')).toMatch(SAFE_BOTTOM)
  })

  it('the foot bar pads past the home indicator in every shape that sets its padding', () => {
    const base = rulesOf(styleNodes).find((r) => r.selector === 'footer.foot-bar' && r.media.length === 0)
    expect(decl(base.body, 'padding-bottom')).toMatch(SAFE_BOTTOM)
    // a later phone rule that resets the padding shorthand must carry the inset too
    for (const r of rulesOf(styleNodes).filter((n) => n.selector === 'footer.foot-bar' && n.media.length)) {
      const padding = decl(r.body, 'padding')
      if (padding) expect(padding).toMatch(SAFE_BOTTOM)
    }
    const studioFeet = rulesOf(shellNodes).filter((r) => r.selector.endsWith(' footer.foot-bar') && PHONE(r.media)
      && decl(r.body, 'position') === 'fixed')
    expect(studioFeet.length).toBe(2)
    for (const r of studioFeet) {
      expect(decl(r.body, 'padding')).toMatch(SAFE_BOTTOM)
      expect(decl(r.body, 'height')).toMatch(SAFE_BOTTOM)
    }
  })

  it('no bare 100vh: the shell sizes to the dynamic viewport', () => {
    for (const [name, text] of [['styles.css', styles], ['studioShell.css', shell], ['index.html', html]]) {
      expect(text, name).not.toMatch(/\b100vh\b/)
    }
    expect(rulesOf(styleNodes).find((r) => r.selector === '.app' && r.media.length === 0).body).toMatch(/height:\s*100dvh;/)
  })
})

describe('A4 reduced motion at the root (motion standard section 8)', () => {
  it('zeroes every duration on every element and both pseudo-elements, and runs a loop once', () => {
    const roots = rulesOf(styleNodes).filter((r) => r.media.some((p) => /prefers-reduced-motion:\s*reduce/.test(p))
      && r.selector.split(',').map((s) => s.trim()).join(', ') === '*, *::before, *::after')
    expect(roots).toHaveLength(1)
    const body = roots[0].body
    expect(decl(body, 'animation-duration')).toBe('0s !important')
    expect(decl(body, 'animation-delay')).toBe('0s !important')
    expect(decl(body, 'animation-iteration-count')).toBe('1 !important')
    expect(decl(body, 'transition-duration')).toBe('0s !important')
    expect(decl(body, 'transition-delay')).toBe('0s !important')
  })

  it('completes filled animations instead of cancelling them', () => {
    expect(styles).not.toContain('animation: none !important')
  })
})

describe('A21 hover, touch and press', () => {
  it('no :hover rule renders outside (hover: hover) and (pointer: fine)', () => {
    for (const [name, nodes] of [['styles.css', styleNodes], ['studioShell.css', shellNodes]]) {
      const hovers = rulesOf(nodes).filter((r) => r.selector.includes(':hover'))
      expect(hovers.length, name).toBeGreaterThan(0)
      const ungated = hovers.filter((r) => !r.media.some(HOVER_GATE)).map((r) => r.selector)
      expect(ungated, name).toEqual([])
    }
  })

  const EXEMPT = ':not(canvas, input[type="range"], [role="slider"])'

  it('controls drop the double-tap delay and the grey tap flash, at zero specificity, canvas and sliders exempt', () => {
    const touch = rulesOf(styleNodes).filter((r) => r.media.length === 0 && decl(r.body, 'touch-action') === 'manipulation'
      && decl(r.body, '-webkit-tap-highlight-color') === 'transparent')
    expect(touch).toHaveLength(1)
    expect(touch[0].selector.startsWith(':where(')).toBe(true)
    expect(touch[0].selector).toContain('button')
    expect(touch[0].selector).toContain(EXEMPT)
  })

  it('a press answers with the .985 scale tier at 120 ms ease, never on a disabled control, canvas or slider', () => {
    const press = rulesOf(styleNodes).filter((r) => r.selector.includes(':active:not(:disabled)'))
    expect(press).toHaveLength(1)
    const [rule] = press
    expect(rule.media).toEqual([])
    expect(rule.selector).toContain('button')
    expect(rule.selector).toContain(':not([aria-disabled="true"])')
    expect(rule.selector).toContain(EXEMPT)
    expect(decl(rule.body, 'scale')).toBe('.985')
    expect(decl(rule.body, 'transition')).toBe('scale 120ms ease')
  })
})

describe('A3 OS modes are token variants', () => {
  const rootTokens = new Set()
  for (const r of rulesOf(styleNodes).filter((n) => n.selector === ':root')) {
    for (const m of r.body.matchAll(/(--[\w-]+)\s*:/g)) rootTokens.add(m[1])
  }
  const QUERIES = ['prefers-contrast: more', 'forced-colors: active', 'prefers-reduced-transparency: reduce']

  it.each(QUERIES)('styles.css carries a (%s) block that remaps onto existing tokens', (query) => {
    const groups = styleNodes.filter((n) => n.kind === 'group' && n.prelude === `@media (${query})`)
    expect(groups).toHaveLength(1)
    const remaps = rulesOf(groups[0].children).filter((r) => r.selector === 'body')
    expect(remaps).toHaveLength(1)
    const pairs = [...remaps[0].body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)]
    expect(pairs.length).toBeGreaterThan(0)
    for (const [, name, value] of pairs) {
      expect(rootTokens.has(name), name).toBe(true)
      const target = value.trim().match(/^var\((--[\w-]+)\)$/)
      expect(target, `${name}: ${value}`).not.toBeNull()
      expect(rootTokens.has(target[1]), target[1]).toBe(true)
    }
  })

  it('no OS-mode or other media block re-declares the root token block', () => {
    expect(rulesOf(styleNodes).filter((r) => r.selector === ':root' && r.media.length)).toEqual([])
  })

  it('the cockpit skin turns its glass opaque under reduced transparency', () => {
    const group = shellNodes.find((n) => n.kind === 'group' && n.prelude === '@media (prefers-reduced-transparency: reduce)')
    expect(group).toBeDefined()
    const body = rulesOf(group.children).map((r) => r.body).join('\n')
    for (const glass of ['--ck-glass-band', '--ck-glass-panel', '--ck-glass-dock']) {
      expect(decl(body, glass)).toMatch(/^var\(--ck-chrome\b/)
    }
  })
})
