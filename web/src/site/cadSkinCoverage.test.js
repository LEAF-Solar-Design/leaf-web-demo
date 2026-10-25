// Slice 13b: the cad-skin scope (the same selector cockpit.css already uses
// to declare --ck-*) must alias every --ck-* token the file actually
// consumes via var(--ck-*), except ink and warn inherited from the v1 root,
// so a token the skin references but the map
// forgot fails this test loudly instead of silently inheriting whatever
// --leaf-* value happens to be ambient outside the cad-skin scope.
import { readFileSync } from 'node:fs'
import { describe, it, expect } from 'vitest'

const read = (p) => readFileSync(`${process.cwd()}/${p}`, 'utf8')
const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, '')
const CAD_SKIN_SELECTOR = '.studio-shell .app:is([data-surface="cad"], [data-surface="solar"])'

// Every occurrence of `selector { ... }`, brace-depth balanced so a selector
// that recurs inside an @media block is captured whole rather than cut off
// at its first nested close-brace.
function declsFor(css, selector) {
  const text = stripComments(css)
  const decls = {}
  let idx = 0
  for (;;) {
    const i = text.indexOf(selector, idx)
    if (i === -1) break
    const j = text.indexOf('{', i)
    if (j === -1) break
    if (!/^\s*$/.test(text.slice(i + selector.length, j))) { idx = i + 1; continue }
    let depth = 1
    let k = j + 1
    while (depth && k < text.length) {
      if (text[k] === '{') depth += 1
      else if (text[k] === '}') depth -= 1
      k += 1
    }
    const body = text.slice(j + 1, k - 1)
    const declRe = /(--[a-zA-Z0-9-]+)\s*:\s*([^;]+);/g
    let m
    while ((m = declRe.exec(body))) decls[m[1]] = m[2].trim()
    idx = k
  }
  return decls
}

const cockpit = read('src/site/cockpit.css')
const text = stripComments(cockpit)

describe('Start keeps the inline project summary off the board header (WL16g)', () => {
  const selector = '.studio-shell .app[data-start-open="true"] main.center-scroll > .workspace-summary'
  const summaryRules = [...text.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .filter(([, head]) => head.includes('.workspace-summary'))

  it('removes the inline summary from layout and hit testing while Start is open', () => {
    expect(summaryRules.map(([, head, body]) => [head.trim(), body.trim()])).toEqual([
      [selector, 'display: none;'],
    ])
  })

  it('spares the board summary and releases the inline summary when Start closes', () => {
    // Match the two mount locations: the ground portal is a sibling of .app.
    const shell = document.createElement('div')
    shell.className = 'studio-shell'
    shell.innerHTML = `
      <div class="studio-ground">
        <div data-ground="browser"><section class="card workspace-summary"></section></div>
      </div>
      <div class="app" data-studio-shell="cockpit" data-start-open="true">
        <div class="center-col"><main class="center-scroll">
          <section class="card workspace-summary"></section>
          <div><section class="workspace-summary"></section></div>
        </main></div>
      </div>`
    const app = shell.querySelector('.app')
    const inline = app.querySelector('main.center-scroll > .workspace-summary')
    const actualSelector = summaryRules[0]?.[1].trim()
    expect(actualSelector).toBe(selector)
    for (const surface of ['cad', 'solar']) {
      app.dataset.surface = surface
      app.dataset.startOpen = 'true'
      expect([...shell.querySelectorAll(actualSelector)]).toEqual([inline])
      app.dataset.startOpen = 'false'
      expect([...shell.querySelectorAll(actualSelector)]).toEqual([])
      delete app.dataset.startOpen
      expect([...shell.querySelectorAll(actualSelector)]).toEqual([])
    }
    app.dataset.startOpen = 'true'
    shell.className = ''
    expect([...shell.querySelectorAll(actualSelector)]).toEqual([])
  })
})

const HOST_TOKENS = {
  '--background': '--ck-chrome',
  '--card': '--ck-recess',
  '--border': '--ck-title',
  '--primary': '--ck-cmd',
  '--foreground': '--ck-ink',
  '--muted': '--ck-label',
  '--font-label': '--ck-font',
  '--font-sans': '--ck-font',
  '--cad-selection': '--ck-sel',
  '--cad-tab-band': '--ck-top',
  '--cad-icon-ribbon': '--ck-ribbon',
  '--cad-doc-tabs': '--ck-doctabs',
  '--cad-properties-pane': '--ck-pane',
  '--cad-status-bar': '--ck-status',
}

describe('the cad-skin scope declares the ratified dock host contract', () => {
  // Inspect only the original skin block, not later rules with this selector.
  const start = text.indexOf(`${CAD_SKIN_SELECTOR} {`)
  const block = text.slice(start, text.indexOf('}', start) + 1)
  const decls = declsFor(block, CAD_SKIN_SELECTOR)

  it('declares all fourteen host token names in the cad-skin block', () => {
    for (const name of Object.keys(HOST_TOKENS)) {
      expect(decls, `cad-skin scope is missing ${name}`).toHaveProperty(name)
    }
  })

  it('aliases each host token to its existing --ck-* declaration without literals', () => {
    for (const [name, source] of Object.entries(HOST_TOKENS)) {
      expect(decls[name], `${name} must reference ${source}`).toBe(`var(${source})`)
      expect(decls, `${source} must be declared in the same rule`).toHaveProperty(source)
    }
  })
})

describe('the cad-skin scope maps every --ck-* token cockpit.css consumes (slice 13b)', () => {
  it('has a --leaf-* alias or a vendored v1 role for every var(--ck-*) reference in the file', () => {
    const consumed = new Set()
    const usageRe = /var\((--ck-[a-zA-Z0-9-]+)\)/g
    let m
    while ((m = usageRe.exec(text))) consumed.add(m[1])
    expect(consumed.size).toBeGreaterThan(0)

    const decls = declsFor(cockpit, CAD_SKIN_SELECTOR)
    const root = declsFor(read('src/standard/leaf-standard.css'), ':root')
    const inherited = new Set(['--ck-ink', '--ck-warn'])
    for (const token of inherited) {
      const role = token.replace('--ck-', '--leaf-')
      expect(root, `${role} must be declared by the vendored v1 root`).toHaveProperty(role)
      expect(decls).not.toHaveProperty(role)
      expect(decls[token]).toBe(`var(${role})`)
    }
    const mapped = new Set(
      Object.values(decls)
        .map((v) => v.match(/^var\((--ck-[a-zA-Z0-9-]+)\)$/))
        .filter(Boolean)
        .map((match) => match[1])
    )
    const missing = [...consumed].filter((t) => !mapped.has(t) && !inherited.has(t))
    expect(missing, `cad-skin scope is missing a --leaf-* alias for: ${missing.join(', ')}`).toEqual([])
  })
})
