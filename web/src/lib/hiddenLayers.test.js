import { describe, expect, it } from 'vitest'

import { withHiddenLayerDefaults } from './hiddenLayers.js'

describe('withHiddenLayerDefaults', () => {
  it('hides the drawing\'s own hidden layers unless the user chose', () => {
    expect(withHiddenLayerDefaults({}, ['RAILS', 'TAGS'])).toEqual({ RAILS: false, TAGS: false })
    expect(withHiddenLayerDefaults({ RAILS: true }, ['RAILS', 'TAGS'])).toEqual({ RAILS: true, TAGS: false })
  })

  it('returns the same record when nothing is hidden', () => {
    const v = { A: true }
    expect(withHiddenLayerDefaults(v, undefined)).toBe(v)
    expect(withHiddenLayerDefaults(v, [])).toBe(v)
    expect(withHiddenLayerDefaults(v, ['A'])).toBe(v)
  })

  it('returns the same output for the same record and list', () => {
    const v = {}, hidden = ['R']
    expect(withHiddenLayerDefaults(v, hidden)).toBe(withHiddenLayerDefaults(v, hidden))
  })
})
