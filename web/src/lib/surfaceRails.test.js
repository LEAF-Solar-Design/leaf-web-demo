// @vitest-environment node
import { describe, expect, it } from 'vitest'

import { familiesForSurface, familyMonogram } from './surfaceRails.js'

const FAMS = [
  { family_id: 'measurement', label: 'Measurement', capabilities: [1, 2] },
  { family_id: 'selection', label: 'Selection & highlighting', capabilities: [3] },
  { family_id: 'custom', label: 'Custom authored tools', capabilities: [4] },
  { family_id: 'stringing', label: 'Stringing', capabilities: [5] },
]

describe('familiesForSurface', () => {
  it('CAD (null fold) carries the whole catalog, order untouched', () => {
    expect(familiesForSurface(FAMS, 'cad')).toEqual(FAMS)
  })

  it('a listed fold filters AND orders by the list (browser: custom first)', () => {
    const folded = familiesForSurface(FAMS, 'browser')
    expect(folded.map((f) => f.family_id)).toEqual(['custom', 'measurement', 'selection'])
  })

  it('solar folds to its families and is honestly EMPTY when none are registered', () => {
    expect(familiesForSurface(FAMS, 'solar').map((f) => f.family_id)).toEqual(['stringing'])
    expect(familiesForSurface(FAMS.slice(0, 3), 'solar')).toEqual([])
  })

  it('an unknown surface fails OPEN to the whole catalog (a new tab must never boot with an empty rail)', () => {
    expect(familiesForSurface(FAMS, 'not-a-surface')).toEqual(FAMS)
  })

  it('tolerates a missing catalog', () => {
    expect(familiesForSurface(null, 'cad')).toEqual([])
  })
})

describe('familyMonogram', () => {
  it('takes the first letters of the first two WORD words (symbols skipped), upper-cased', () => {
    expect(familyMonogram('Selection & highlighting')).toBe('SH')
    expect(familyMonogram('Custom authored tools')).toBe('CA')
  })

  it('single word: first two letters; empty: placeholder dots', () => {
    expect(familyMonogram('Measurement')).toBe('ME')
    expect(familyMonogram('X')).toBe('X·')
    expect(familyMonogram('')).toBe('··')
    expect(familyMonogram(null)).toBe('··')
  })
})
