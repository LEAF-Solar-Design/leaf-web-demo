import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// Form controls take the surrounding font instead of the browser default (Arial, 13.33px).
const css = readFileSync(resolve(process.cwd(), 'src/styles.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')
const RULE = /:where\(\s*button\s*,\s*input\s*,\s*select\s*,\s*textarea\s*\)\s*\{([^}]*)\}/g

describe('form controls take the surrounding font', () => {
  it('styles.css carries exactly one zero-specificity font: inherit rule for button, input, select and textarea', () => {
    const rules = [...css.matchAll(RULE)]
    expect(rules).toHaveLength(1)
    expect(rules[0][1]).toMatch(/(^|;)\s*font:\s*inherit\s*;?\s*$/)
  })

  it('the rule sits at the top level, outside every media query', () => {
    const [match] = [...css.matchAll(RULE)]
    const before = css.slice(0, match.index)
    const depth = (before.match(/\{/g) || []).length - (before.match(/\}/g) || []).length
    expect(depth).toBe(0)
  })
})
