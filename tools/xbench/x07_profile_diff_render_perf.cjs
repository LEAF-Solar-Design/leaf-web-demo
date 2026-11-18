const assert = require('node:assert/strict'), { performance } = require('node:perf_hooks')
const h = require('./tests/x07_profile_review_dom_harness.cjs')
async function main() {
  try {
    const Diff = h.component('X07ProfileRoleDiff'), timings = []
    const repeats = process.argv.includes('--smoke') ? 8 : 64
    for (const length of [0, 1, 64, 512, 4096]) {
      const before = { module: { block: 'x'.repeat(length), layer: 'MODULES' } }
      const bytes = JSON.stringify(before), start = performance.now()
      const view = h.render(h.React.createElement(Diff, { beforeRoles: before, afterRoles: before }))
      let maxNodes = 0
      for (let i = 0; i < repeats; i++) {
        const after = { module: { layer: 'EDIT-' + i, block: before.module.block } }
        view.rerender(h.React.createElement(Diff, { beforeRoles: before, afterRoles: after }))
        assert.equal(h.screen.getByRole('status').textContent, '0 added, 0 removed, 1 changed.')
        const values = [...h.screen.getByRole('region', { name: 'Changed module' }).querySelectorAll('code')].map(node => JSON.parse(node.textContent))
        assert.deepEqual(values, [before.module.block, 'MODULES', before.module.block, 'EDIT-' + i])
        maxNodes = Math.max(maxNodes, document.body.querySelectorAll('*').length)
        assert.ok(maxNodes <= 60); assert.equal(document.querySelectorAll('button,input').length, 0)
      }
      assert.equal(JSON.stringify(before), bytes)
      timings.push({ label_length: length, draft_edits: repeats, elapsed_ms: Math.round((performance.now() - start) * 100) / 100, max_nodes: maxNodes })
      view.unmount(); h.cleanup(); assert.equal(document.body.childElementCount, 0)
    }
    console.log(JSON.stringify({ schema: 'leaf.x07.role-diff-render-perf.v1', mode: repeats === 8 ? 'smoke' : 'bounded', timings, cleanup: true }))
  } finally { await h.dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
