import { describe, expect, it } from 'vitest'
import registry from './registry.json' with { type: 'json' }

describe('demo catalog copy', () => {
  it('every tool description reads as product copy', () => {
    for (const tool of registry.tools) {
      expect(tool.description).toBeTypeOf('string')
      expect(tool.description.trim()).not.toBe('')
      expect(tool.description).not.toMatch(/slice|fixture|placeholder|upstream|projection ships|\u2014|\u2013/i)
    }
  })

  it('the two rewritten descriptions are exact', () => {
    expect(registry.tools.find(tool => tool.name === 'count-by-layer').description).toBe(
      'Counts entities per layer. The safest read-only tool, and a good first run.',
    )
    expect(registry.tools.find(tool => tool.name === 'list-linked-service-items').description).toBe(
      'Lists items from a service linked to your workspace. Runs once a service is linked.',
    )
  })
})
