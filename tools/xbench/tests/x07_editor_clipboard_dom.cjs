const assert = require('node:assert/strict'), path = require('node:path')
const { rpc, ctx, copies, intake, component, React, render, screen, waitFor, cleanup, dispose,
  resources } = require('./x07_adapter_jsx_journey.cjs')
const keyboardAt = process.argv.indexOf('--keyboard-deps')
const keyboardRoot = keyboardAt < 0 ? 'D:/agent-data/x07-ui-keyboard-deps/node_modules' : process.argv[keyboardAt + 1]
const userEvent = require(path.join(path.resolve(keyboardRoot), '@testing-library/user-event')).default
const Editor = component('X07CustomerProfileEditor')

async function main() {
  try {
    const user = userEvent.setup({ document: global.document })
    const context = ctx('clipboard')
    const block = 'Panneau 雪🌞', layer = 'MODULES 雪🌞'
    intake.inserts.push({ name: block, layer, x: 100, y: 0 })
    const pin = (await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })).applied_profile
    let saves = 0, loads = 0
    const props = { customerKey: context.customer_key, drawingKey: context.drawing_key, applicationRecord: pin,
      loadProfile: (customer, opts) => { loads++; return rpc(context, 'editor_load', context, customer, opts.signal) },
      saveProfile: (customer, roles, opts) => { saves++; return rpc(context, 'editor_save', context,
        { customer, roles, expected_version: opts.expected_version }, opts.signal) } }
    render(React.createElement(Editor, props))
    await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    const moduleBlock = screen.getByLabelText('Module block')
    await user.click(moduleBlock); moduleBlock.setSelectionRange(0, moduleBlock.value.length)
    const cut = await user.cut()
    assert.equal(cut.getData('text/plain'), 'Panel')
    assert.equal(moduleBlock.value, ''); assert.equal(saves, 0)
    await user.click(screen.getByRole('button', { name: 'Save profile' }))
    assert.equal(screen.getByRole('alert').textContent, 'Module block and layer are required.')
    assert.equal(saves, 0)
    await user.click(moduleBlock); await user.paste(block)
    const moduleLayer = screen.getByLabelText('Module layer')
    await user.click(moduleLayer); moduleLayer.setSelectionRange(0, moduleLayer.value.length)
    await user.cut(); await user.paste(layer)
    assert.equal(moduleBlock.value, block); assert.equal(moduleLayer.value, layer)
    assert.equal(saves, 0); assert.equal(loads, 1)
    await user.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    assert.equal(saves, 1)
    cleanup()
    render(React.createElement(Editor, props))
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    assert.equal(screen.getByLabelText('Module block').value, block)
    assert.equal(screen.getByLabelText('Module layer').value, layer)
    assert.equal(saves, 1); assert.equal(loads, 2)
    assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
    assert.equal(pin.roles.module.block, 'Panel'); assert.equal(pin.version, 1)
    console.log('PASS: actual clipboard cut/paste preserves Unicode CAD labels, empty draft validation, one explicit save/reload and old pin without implicit submits')
  } finally { await dispose(); assert.equal(resources().child_count, 0) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
