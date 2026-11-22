const assert = require('node:assert/strict')
const h = require('./x07_profile_review_dom_harness.cjs')
async function main() {
  try {
    const props = h.props(); let loads = 0, confirms = 0, saves = 0
    props.onConfirm = async () => { confirms++; return h.ack() }
    props.loadProfile = async () => { if (++loads === 1) throw Error('PRIVATE_LOAD_SENTINEL'); return h.pin }
    props.saveProfile = async () => { saves++; throw Error('Unexpected save') }
    h.render(h.draw(props))
    await h.enter(await h.screen.findByRole('button', { name: 'Confirm roles' }))
    await h.screen.findByRole('alert')
    const retry = await h.screen.findByRole('button', { name: 'Retry loading profile' })
    await h.waitFor(() => assert.equal(document.activeElement, retry))
    assert.ok(retry.isConnected)
    assert.equal(h.screen.getByRole('alert').textContent, 'The profile could not be loaded for this customer.')
    assert.equal(document.body.textContent.includes('PRIVATE_LOAD_SENTINEL'), false)
    const checkReferences = () => {
      for (const node of document.querySelectorAll('[aria-describedby], [aria-labelledby]')) {
        for (const name of ['aria-describedby', 'aria-labelledby']) {
          for (const id of (node.getAttribute(name) || '').split(/\s+/).filter(Boolean)) assert.ok(document.getElementById(id), id)
        }
      }
      for (const alert of document.querySelectorAll('[role="alert"]')) assert.ok(alert.id && document.getElementById(alert.id) === alert)
    }
    checkReferences()
    await h.enter(retry)
    const layer = await h.screen.findByRole('textbox', { name: 'Module layer' })
    await h.waitFor(() => assert.equal(document.activeElement, layer))
    assert.ok(layer.isConnected); assert.equal(layer.value, 'MODULES'); checkReferences()
    assert.equal(confirms, 1); assert.equal(loads, 2); assert.equal(saves, 0)
    console.log('PASS: profile review keyboard transition, named retry/module focus and resolved references')
  } finally { await h.dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
