import { describe, expect, it } from 'vitest'
import { matchPrompt } from './mockNlPrompt.js'

const tools = [{ name: 'count-by-layer', description: 'Count entities by layer' }]

describe('demo prompt matching', () => {
  it.each(['Tell me a bedtime story', '/unknown count', '12.5, -34.7', ''])('refuses unmatched input: %s', (text) => {
    expect(matchPrompt(text, tools)).toMatchObject({
      lane: 'run', tool: null, confidence: 0, stubKind: 'demo',
      rationale: 'No matching tool in this demo. Try another description or browse available tools.',
    })
  })

  it('still resolves a supported request', () => {
    expect(matchPrompt('Count entities per layer', tools)).toMatchObject({
      lane: 'run', tool: 'count-by-layer', confidence: 0.9,
    })
  })

  it('does not manufacture a tool from the first catalog entry', () => {
    expect(matchPrompt('Do something unusual', [{ name: 'delete-marked-panel' }]).tool).toBeNull()
    expect(matchPrompt('Count entities', []).tool).toBeNull()
  })
})
