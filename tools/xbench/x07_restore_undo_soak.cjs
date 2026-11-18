const assert = require('node:assert/strict'), { performance } = require('node:perf_hooks')
const h = require('./tests/x07_profile_review_dom_harness.cjs')
const copy = value => JSON.parse(JSON.stringify(value))
async function main() {
  try {
    const Editor = h.component('X07CustomerProfileEditor'), repeats = process.argv.includes('--smoke') ? 8 : 64
    let cycles = 0, writes = 0, maxNodes = 0; const started = performance.now()
    for (const unicode of ['M\u00f3D', 'Mo\u0301D']) {
      const baseline = { module: { block: 'Panel', layer: unicode }, roof: { layer: 'R\u00f3F' } }
      let model = copy(baseline), snapshot = null
      const view = h.render(h.React.createElement(Editor, { customerKey: 'a', drawingKey: unicode,
        loadProfile: async () => ({ customer_key: 'a', profile_id: 'profile-a', version: 1, roles: copy(baseline) }),
        saveProfile: async () => { writes++; throw Error('Unexpected implicit save') } }))
      await h.screen.findByRole('textbox', { name: 'Module layer' })
      const check = () => {
        const actual = {}
        for (const role of ['module', 'roof', 'obstruction']) {
          const label = role[0].toUpperCase() + role.slice(1), layer = h.screen.queryByRole('textbox', { name: label + ' layer' })
          if (layer) { actual[role] = { layer: layer.value }; const block = h.screen.queryByRole('textbox', { name: label + ' block' }); if (block) actual[role].block = block.value }
        }
        assert.deepEqual(actual, model)
        assert.equal(h.screen.queryAllByRole('button', { name: 'Undo last restore' }).length, snapshot ? 1 : 0)
        maxNodes = Math.max(maxNodes, document.body.querySelectorAll('*').length); assert.ok(maxNodes <= 200); assert.equal(writes, 0)
      }
      for (let i = 0; i < repeats; i++) {
        h.fireEvent.change(h.screen.getByRole('textbox', { name: 'Module layer' }), { target: { value: 'DIRTY-' + i } }); model.module.layer = 'DIRTY-' + i; snapshot = null; check()
        h.fireEvent.click(h.screen.getByRole('button', { name: 'Remove roof role' })); delete model.roof; check()
        h.fireEvent.click(h.screen.getByRole('button', { name: 'Add obstruction role' })); model.obstruction = { layer: '' }; check()
        h.fireEvent.change(h.screen.getByRole('textbox', { name: 'Obstruction layer' }), { target: { value: 'DRAFT-' + i } }); model.obstruction.layer = 'DRAFT-' + i; check()
        for (const role of ['roof', 'obstruction']) {
          snapshot = copy(model)
          if (baseline[role]) model[role] = copy(baseline[role]); else delete model[role]
          h.fireEvent.click(h.screen.getByRole('button', { name: `Restore loaded ${role} value` })); check()
        }
        model = snapshot; snapshot = null; h.fireEvent.click(h.screen.getByRole('button', { name: 'Undo last restore' })); check()
        snapshot = copy(model); model.module = copy(baseline.module); h.fireEvent.click(h.screen.getByRole('button', { name: 'Restore loaded module value' })); check()
        h.fireEvent.change(h.screen.getByRole('textbox', { name: 'Obstruction layer' }), { target: { value: 'AFTER' } }); model.obstruction.layer = 'AFTER'; snapshot = null; check()
        h.fireEvent.click(h.screen.getByRole('button', { name: 'Reset draft' })); model = copy(baseline); snapshot = null; check(); cycles++
      }
      view.unmount(); h.cleanup(); assert.equal(document.body.childElementCount, 0)
    }
    console.log(JSON.stringify({ schema: 'leaf.x07.restore-undo-soak.v1', cycles, writes, max_nodes: maxNodes,
      elapsed_ms: Math.round((performance.now() - started) * 100) / 100, cleanup: true }))
  } finally { await h.dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
