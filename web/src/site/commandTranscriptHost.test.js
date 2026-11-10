// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('./cockpit.css', import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')
const rule = selector => {
  const body = css.split('}').find(block => block.slice(0, block.indexOf('{')).trim() === selector)
  expect(body, `missing ${selector}`).toBeDefined()
  return body.slice(body.indexOf('{') + 1)
}

describe('command transcript host styles', () => {
  it('collapses the empty host', () => {
    expect(rule('.command-transcript-host:empty')).toMatch(/display:\s*none\s*;/)
  })
  it('bounds the scrollable column above the command bar', () => {
    const host = rule('.command-transcript-host')
    expect(host).toMatch(/display:\s*flex\s*;/)
    expect(host).toMatch(/flex-direction:\s*column\s*;/)
    expect(host).toMatch(/width:\s*100%\s*;/)
    expect(host).toMatch(/max-height:\s*min\(52vh,\s*560px\)\s*;/)
    expect(host).toMatch(/overflow:\s*auto\s*;/)
  })
  it('lets the host scroll the log and hides only the reply composer', () => {
    expect(rule('.command-transcript-host .converse-log')).toMatch(/max-height:\s*none\s*;/)
    expect(rule('.command-transcript-host .converse-input')).toMatch(/display:\s*none\s*;/)
    const hidden = css.split('}').filter(block => block.includes('.command-transcript-host') && /display:\s*none/.test(block))
    expect(hidden).toHaveLength(2)
  })
})
