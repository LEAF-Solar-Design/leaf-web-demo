const assert = require('node:assert/strict'), { performance } = require('node:perf_hooks')
const h = require('./tests/x07_profile_review_dom_harness.cjs')
async function main() {
  try {
    const Editor = h.component('X07CustomerProfileEditor'), repetitions = process.argv.includes('--smoke') ? 8 : 64
    const start = performance.now(); let writes = 0, maxNodes = 0, cycles = 0
    for (const unicode of ['M\u00f3D', 'Mo\u0301D']) {
      const roles = { module: { block: 'Panel', layer: unicode }, roof: { layer: 'R\u00f3F' } }, original = JSON.stringify(roles)
      const view = h.render(h.React.createElement(Editor, { customerKey: 'a', drawingKey: unicode,
        loadProfile: async () => ({ customer_key: 'a', profile_id: 'profile-a', version: 1, roles }),
        saveProfile: async () => { writes++; throw Error('Unexpected implicit write') } }))
      await h.screen.findByRole('textbox', { name: 'Module layer' })
      for (let i = 0; i < repetitions; i++) {
        h.fireEvent.change(h.screen.getByRole('textbox', { name: 'Module layer' }), { target: { value: 'EDIT-' + i } })
        h.fireEvent.click(h.screen.getByRole('button', { name: 'Remove roof role' }))
        h.fireEvent.click(h.screen.getByRole('button', { name: 'Add obstruction role' }))
        h.fireEvent.change(h.screen.getByRole('textbox', { name: 'Obstruction layer' }), { target: { value: 'DRAFT-' + i } })
        maxNodes = Math.max(maxNodes, document.body.querySelectorAll('*').length); assert.ok(maxNodes <= 180)
        for (const role of ['roof', 'obstruction', 'module']) h.fireEvent.click(h.screen.getByRole('button', { name: `Restore loaded ${role} value` }))
        assert.deepEqual({ module: { block: h.screen.getByRole('textbox', { name: 'Module block' }).value,
          layer: h.screen.getByRole('textbox', { name: 'Module layer' }).value }, roof: { layer: h.screen.getByRole('textbox', { name: 'Roof layer' }).value } }, roles)
        assert.equal(h.screen.queryByRole('textbox', { name: 'Obstruction layer' }), null)
        assert.equal(h.screen.queryByRole('button', { name: /Restore loaded/ }), null)
        assert.equal(writes, 0); cycles++
      }
      assert.equal(JSON.stringify(roles), original); view.unmount(); h.cleanup(); assert.equal(document.body.childElementCount, 0)
    }
    console.log(JSON.stringify({ schema: 'leaf.x07.selective-restore-soak.v1', cycles, writes, max_nodes: maxNodes,
      elapsed_ms: Math.round((performance.now() - start) * 100) / 100, cleanup: true }))
  } finally { await h.dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
