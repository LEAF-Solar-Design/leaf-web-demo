// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('../styles.css', import.meta.url), 'utf8')
  .replace(/\r\n/g, '\n')
  .replace(/\/\*[\s\S]*?\*\//g, '')
const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
const declarations = (selector) => {
  const matches = rules.filter(([, selectors]) =>
    selectors.split(',').some((value) => value.trim() === selector),
  )
  expect(matches.length, selector).toBeGreaterThan(0)
  return matches.map(([, , body]) => body).join('\n')
}
const user = '.converse-msg[data-converse-role="user"]'
const assistant = '.converse-msg[data-converse-role="assistant"]'
const decision = '.converse-confirm[data-converse-role="decision"]'
const question = '.converse-question[data-converse-role="question"]'
const chip = (state) => `.converse-tool-chip[data-state="${state}"]`

// Balance nested blocks so the media assertion includes the keyframes too.
function blockAt(start) {
  const open = css.indexOf('{', start)
  let depth = 1
  let end = open + 1
  while (depth && end < css.length) {
    if (css[end] === '{') depth += 1
    if (css[end] === '}') depth -= 1
    end += 1
  }
  expect(depth).toBe(0)
  return { body: css.slice(open + 1, end - 1), end }
}

describe('ConversePanel V2 attribute styles', () => {
  it('distinguishes user bubbles from full-width assistant prose with role labels', () => {
    expect(declarations(user)).toMatch(/align-self:\s*flex-end\s*;/)
    expect(declarations(user)).toMatch(/max-width:\s*85%\s*;/)
    expect(declarations(user)).toMatch(/background:\s*var\(--accent-tint\)\s*;/)
    expect(declarations(user)).toMatch(/border-radius:\s*var\(--leaf-radius-control\)\s*;/)
    expect(declarations(user)).toMatch(/padding:\s*8px 12px\s*;/)
    expect(declarations(assistant)).toMatch(/width:\s*100%\s*;/)
    expect(declarations(assistant)).toMatch(/background:\s*transparent\s*;/)
    expect(declarations(assistant)).toMatch(/font-size:\s*var\(--leaf-size-body\)\s*;/)
    for (const [selector, label] of [[user, 'You'], [assistant, 'Assistant']]) {
      const body = declarations(`${selector}::before`)
      expect(body).toContain(`content: "${label}"`)
      expect(body).toMatch(/display:\s*block\s*;/)
      expect(body).toMatch(/font-size:\s*calc\(var\(--leaf-size-caption\) - 1px\)\s*;/)
      expect(body).toMatch(/font-weight:\s*600\s*;/)
      expect(body).toMatch(/text-transform:\s*uppercase\s*;/)
      expect(body).toMatch(/letter-spacing:\s*0\.08em\s*;/)
    }
  })

  it('lays out tool pills with state glyphs and the unavailable label', () => {
    const row = declarations('.converse-tool-row[data-converse-role="tool"]')
    expect(row).toMatch(/display:\s*flex\s*;/)
    expect(row).toMatch(/gap:\s*8px\s*;/)
    expect(row).toMatch(/flex-wrap:\s*wrap\s*;/)
    expect(row).toMatch(/list-style:\s*none\s*;/)
    const pill = declarations('.converse-tool-chip')
    expect(pill).toMatch(/display:\s*inline-flex\s*;/)
    expect(pill).toMatch(/border:\s*1px solid var\(--border\)\s*;/)
    expect(pill).toMatch(/border-radius:\s*999px\s*;/)
    expect(pill).toMatch(/font:\s*500 var\(--leaf-size-dense\) var\(--font-mono\)\s*;/)
    for (const [state, glyph, color] of [
      ['running', '◌'], ['done', '✓', '--status-success'], ['failed', '✕', '--status-danger'],
    ]) {
      const body = declarations(`${chip(state)}::before`)
      expect(body).toContain(`content: "${glyph}"`)
      if (color) expect(body).toContain(`color: var(${color})`)
    }
    expect(declarations('.converse-tool-chip[data-state] > .dot')).toMatch(/display:\s*none\s*;/)
    expect(declarations(`${chip('failed')} > .converse-step-state::before`)).toContain('content: "Unavailable"')
  })

  it('pulses only the running glyph when reduced motion is not requested', () => {
    const media = /@media\s*\(prefers-reduced-motion:\s*no-preference\)\s*\{/g
    const blocks = [...css.matchAll(media)].map((match) => ({ start: match.index, ...blockAt(match.index) }))
    const pulseBlocks = blocks.filter(({ body }) => body.includes('@keyframes converse-tool-pulse'))
    expect(pulseBlocks).toHaveLength(1)
    const pulse = pulseBlocks[0]
    expect(pulse.body).toContain(`${chip('running')}::before`)
    expect(pulse.body).toMatch(/animation:\s*converse-tool-pulse 1\.2s ease-in-out infinite\s*;/)
    expect(pulse.body).toMatch(/0%,\s*100%\s*\{\s*opacity:\s*1\s*;/)
    expect(pulse.body).toMatch(/50%\s*\{\s*opacity:\s*0\.35\s*;/)
    const outside = css.slice(0, pulse.start) + css.slice(pulse.end)
    expect(outside).not.toMatch(/(?:animation[^:;{}]*:\s*[^;{}]*|@keyframes\s+)converse-tool-pulse/)
  })

  it('labels consent and question cards by ink and neutralizes resolved cards', () => {
    for (const selector of [decision, question]) {
      const body = declarations(selector)
      expect(body).toMatch(/border:\s*1px solid var\(--border\)\s*;/)
      expect(body).toMatch(/border-left-width:\s*3px\s*;/)
      expect(body).toMatch(/flex-wrap:\s*wrap\s*;/)
    }
    for (const [selector, ink, label, color] of [
      [decision, 'needs-ok', 'NEEDS YOUR OK', '--status-warning'],
      [question, 'question', 'QUESTION', '--status-warning'],
      [decision, 'approved', 'APPROVED', '--border'],
      [decision, 'denied', 'DENIED', '--border'],
      [question, 'answered', 'ANSWERED', '--border'],
    ]) {
      const state = `${selector}[data-ink="${ink}"]`
      expect(declarations(state)).toContain(`border-left-color: var(${color})`)
      expect(declarations(`${state}::before`)).toContain(`content: "${label}"`)
    }
  })

  it('keeps card buttons readable and touchable and marks errors with critical ink', () => {
    for (const selector of [decision, question]) {
      const body = declarations(`${selector} button`)
      expect(body).toMatch(/min-height:\s*44px\s*;/)
      expect(body).toMatch(/min-width:\s*120px\s*;/)
      expect(body).toMatch(/white-space:\s*normal\s*;/)
      expect(body).toMatch(/overflow-wrap:\s*break-word\s*;/)
      expect(body).toMatch(/word-break:\s*normal\s*;/)
    }
    expect(declarations(`${question} .converse-question-options`)).toMatch(/flex-wrap:\s*wrap\s*;/)
    expect(declarations('.converse-note[data-converse-role="error"]'))
      .toMatch(/border-left:\s*3px solid var\(--status-danger\)\s*;/)
    expect(css).not.toMatch(/\.converse-eyebrow\b[^{}]*\{/)
  })
})
