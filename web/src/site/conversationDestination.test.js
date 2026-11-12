// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { conversationDestination } from './conversationDestination.js'

describe('conversationDestination', () => {
  it.each([
    [true, 'primary', 'session-1', 'dock'],
    [false, 'primary', 'session-1', 'rail'],
    [true, null, 'session-1', 'rail'],
    [true, 'primary', null, 'rail'],
    [false, null, 'session-1', 'rail'],
    [false, 'primary', null, 'rail'],
    [true, null, null, 'rail'],
    [false, null, null, 'rail'],
    [true, 'race', 'session-1', 'dock'],
    [true, '', 'session-1', 'rail'],
    [true, 'primary', '', 'rail'],
  ])('host=%s mode=%s session=%s goes to %s', (hasDockHost, agentMode, sessionId, expected) => {
    expect(conversationDestination({ hasDockHost, agentMode, sessionId })).toBe(expected)
  })
})
