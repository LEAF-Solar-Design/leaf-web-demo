// Slice 13b: the cad-skin scope (the same selector cockpit.css already uses
// to declare --ck-*) must alias EVERY --ck-* token the file actually
// consumes via var(--ck-*), so a token the skin references but the map
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

describe('the cad-skin scope maps every --ck-* token cockpit.css consumes (slice 13b)', () => {
  it('has a --leaf-* alias for every var(--ck-*) reference in the file', () => {
    const consumed = new Set()
    const usageRe = /var\((--ck-[a-zA-Z0-9-]+)\)/g
    let m
    while ((m = usageRe.exec(text))) consumed.add(m[1])
    expect(consumed.size).toBeGreaterThan(0)

    const decls = declsFor(cockpit, CAD_SKIN_SELECTOR)
    const mapped = new Set(
      Object.values(decls)
        .map((v) => v.match(/^var\((--ck-[a-zA-Z0-9-]+)\)$/))
        .filter(Boolean)
        .map((match) => match[1])
    )
    const missing = [...consumed].filter((t) => !mapped.has(t))
    expect(missing, `cad-skin scope is missing a --leaf-* alias for: ${missing.join(', ')}`).toEqual([])
  })
})
