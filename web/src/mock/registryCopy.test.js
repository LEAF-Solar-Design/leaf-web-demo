import { describe, expect, it } from 'vitest'
import registry from './registry.json' with { type: 'json' }
import { runMock } from './mockEngine.js'

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
      'Lists items from a service linked to your workspace. Not yet available.',
    )
  })

  it('running the linked-service tool in the demo says it is not available yet', () => {
    const tool = registry.tools.find(tool => tool.name === 'list-linked-service-items')
    const envelope = runMock(tool, {}, null)
    expect(envelope.ok).toBe(false)
    expect(envelope.error).toBe('This tool reads from a linked service, which is not available yet.')
    expect(envelope.error).not.toMatch(/mock engine|engine_op|list_linked/)
  })

  it('a tool with no demo operation and no linked service keeps the engine message', () => {
    const envelope = runMock({ name: 'x', version: '1.0.0', engine_op: 'nope' }, {}, null)
    expect(envelope.ok).toBe(false)
    expect(envelope.error).toBe("mock engine has no op for 'nope'")
  })
})
