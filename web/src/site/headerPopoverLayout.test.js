// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
  .replace(/\r\n/g, '\n')
  .replace(/\/\*[\s\S]*?\*\//g, '')
const styles = css('../styles.css')
// studio-shell rules live in landing.css (siteRootOneShell's rollback contract)
const landing = css('./landing.css')

const declaration = (body, property) => body.match(
  new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+);`),
)?.[1].trim()

describe('header popover layout', () => {
  it('wraps card text while preserving the desktop anchor and trigger labels', () => {
    const popover = styles.match(/^\.claude-pop\s*\{([^{}]*)\}/m)?.[1]
    expect(popover).toBeDefined()
    expect(declaration(popover, 'white-space')).toBe('normal')
    expect(declaration(popover, 'overflow-wrap')).toBe('anywhere')
    expect(declaration(popover, 'position')).toBe('absolute')
    expect(declaration(popover, 'top')).toBe('calc(100% + 8px)')
    expect(declaration(popover, 'right')).toBe('0')
    expect(declaration(popover, 'width')).toBe('320px')

    const trigger = styles.match(/^\.claude-trigger \.ca-k[^{}]*\{([^{}]*)\}/m)?.[1]
    expect(trigger).toBeDefined()
    expect(declaration(trigger, 'white-space')).toBe('nowrap')
  })

  it('escapes the phone header clip with a viewport sheet above the cockpit strip', () => {
    // Match whole media bodies, including their immediate rule blocks, so a
    // matching selector outside the phone breakpoint cannot satisfy the pin.
    const phoneBlocks = [...styles.matchAll(
      /@media\s*\(\s*max-width\s*:\s*600px\s*\)\s*\{((?:[^{}]|\{[^{}]*\})*)\}/g,
    )]
    const popover = phoneBlocks.map((block) => block[1].match(
      /(?:^|\})\s*header\.top \.claude-pop\s*\{([^{}]*)\}/,
    )?.[1]).find((body) => body !== undefined)

    expect(popover).toBeDefined()
    expect(declaration(popover, 'position')).toBe('fixed')
    expect(declaration(popover, 'left')).toBe('8px')
    expect(declaration(popover, 'right')).toBe('8px')
    expect(declaration(popover, 'width')).toBe('auto')
    expect(declaration(popover, 'max-width')).toBe('none')
    expect(declaration(popover, 'top')).toBe('auto')
    expect(declaration(popover, 'bottom')).toBe('calc(64px + env(safe-area-inset-bottom, 0px))')
    expect(declaration(popover, 'max-height')).toBe('70vh')
    expect(declaration(popover, 'overflow-y')).toBe('auto')
    expect(Number(declaration(popover, 'z-index'))).toBeGreaterThan(60)
  })

  it('lifts the header above the product nav while a header popover is open', () => {
    const header = landing.match(/^\.studio-shell header\.top:has\(\.claude-pop\)\s*\{([^{}]*)\}/m)?.[1]
    expect(header).toBeDefined()
    expect(Number(declaration(header, 'z-index'))).toBeGreaterThan(6)
  })
})
