import { describe, expect, it } from 'vitest'
import glossary from './contract/sources/leaf-glossary.v1.json'
import { VIEW_STATES, replacedWordsIn, colorLiteralsIn } from './standardsChecks.js'

describe('Lab standards checks', () => {
  it('freezes the five STD-7 view states in order', () => {
    expect(VIEW_STATES).toEqual(['loading', 'value', 'empty', 'error', 'retry'])
    expect(Object.isFrozen(VIEW_STATES)).toBe(true)
  })

  it('uses the current browser glossary including its newer terms', () => {
    expect(glossary.terms).toHaveLength(40)
    expect(replacedWordsIn('Runner release')).toEqual(['runner'])
    expect(replacedWordsIn('The bot ran a Test order')).toEqual([])
    expect(replacedWordsIn('probe settlement')).toEqual(['probe', 'settlement'])
    expect(replacedWordsIn('ROLLBACK, runner, rollback; re-arm.')).toEqual(['re-arm', 'rollback', 'runner'])
  })

  it('matches whole words and literal phrases rather than substrings or patterns', () => {
    expect(replacedWordsIn('runners probed settlementish')).toEqual([])
    expect(replacedWordsIn('A marked change.')).toContain('marked change')
    expect(replacedWordsIn('market (one Kalshi ticker)')).toContain('market (one Kalshi ticker)')
  })

  it('removes every exact identifier before checking display words', () => {
    expect(replacedWordsIn('Send it to kashbot2-runner', { identifiers: ['kashbot2-runner'] })).toEqual([])
    expect(replacedWordsIn('probe.v1 probe.v1 runner', { identifiers: ['probe.v1', 'runner'] })).toEqual([])
    expect(replacedWordsIn('Runner runner', { identifiers: ['runner'] })).toEqual(['runner'])
    expect(replacedWordsIn('probeXv1', { identifiers: ['probe.v1'] })).toEqual([])
  })

  it('finds all four hex lengths in source order, preserving duplicates and case', () => {
    expect(colorLiteralsIn('color: #fff; background: var(--leaf-surface)')).toEqual(['#fff'])
    expect(colorLiteralsIn('#abc #abcd #AABBCC #11223344 #abc')).toEqual([
      '#abc', '#abcd', '#AABBCC', '#11223344', '#abc',
    ])
  })

  it('finds RGB and HSL functions in JS, JSX and CSS', () => {
    expect(colorLiteralsIn('color: rgb(1, 2, 3)')).toEqual(['rgb('])
    expect(colorLiteralsIn('style={{ color: "rgba(1,2,3,.5)" }}; hsl(0,0%,0%); HSLA(0,0%,0%,1)')).toEqual([
      'rgba(', 'hsl(', 'HSLA(',
    ])
  })

  it('allows tokens and rejects partial hex or word-embedded fragments', () => {
    expect(colorLiteralsIn('var(--leaf-ink)')).toEqual([])
    expect(colorLiteralsIn('"abc#1" #12 #12345 #1234567 #123456789 #ffffg abc#fff myrgb(')).toEqual([])
    expect(colorLiteralsIn('')).toEqual([])
  })
})
