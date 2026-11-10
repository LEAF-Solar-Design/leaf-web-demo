// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('./ops-workspace.css', import.meta.url), 'utf8')
  .replace(/\r\n/g, '\n')
  .replace(/\/\*[\s\S]*?\*\//g, '')

describe('Ops workspace chrome insets', () => {
  it('bounds the scroll area below the ribbon and above the command dock', () => {
    const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
      .filter(([, selector]) => selector.trim() === '.studio-ground-ops .ops-workspace')
    expect(rules).toHaveLength(1)
    const declarations = Object.fromEntries(
      [...rules[0][2].matchAll(/(?:^|;)\s*([\w-]+)\s*:\s*([^;]+)(?=;|$)/g)]
        .map(([, property, value]) => [property, value.trim()]),
    )
    expect(declarations.top).toBeDefined()
    expect(declarations.top).not.toBe('16px')
    expect(declarations).toMatchObject({
      top: 'calc(var(--drawer-top, 95px) + var(--ck-ribbon, 95px) + 10px)',
      bottom: 'calc(var(--ck-status, 31px) + 41px)',
      left: '16px',
      right: '16px',
      height: 'auto',
    })
  })
})
