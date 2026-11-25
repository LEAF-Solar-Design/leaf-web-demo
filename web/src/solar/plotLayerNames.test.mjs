import assert from 'node:assert/strict'
import test from 'node:test'
import { plotLayerNames } from './plotLayerNames.js'

test('names drawing layers that affect a saved plot', () => {
  assert.deepEqual(plotLayerNames({ hidden_layers: ['Frozen PV', 'unused'],
    inserts: [{ layer: 'frozen pv' }, { layer: 'FROZEN PV' }] }), ['Frozen PV'])
  assert.deepEqual(plotLayerNames({ hidden_layers: ['Tags off'],
    texts: [{ layer: 'Tags off' }, { layer: 'Printed tags' }] }), ['Tags off'])
  assert.deepEqual(plotLayerNames({ nonplot_layers: ['Array outline'],
    polylines: [{ layer: 'Array outline' }] }), ['Array outline'])
  assert.deepEqual(plotLayerNames({ inserts: [{ layer: 'Repaired' }] }), [])
})
