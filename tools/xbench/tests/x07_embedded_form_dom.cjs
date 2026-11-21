const assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, cleanup, dispose,
  resources } = require('./x07_adapter_jsx_journey.cjs')
const Confirmation = component('X07RoleConfirmation'), Editor = component('X07CustomerProfileEditor'), Picker = component('X07CopyPicker')

async function main() {
  let outerSubmits = 0, outerResets = 0
  const embed = element => {
    const outer = document.createElement('form'), container = document.createElement('div')
    outer.appendChild(container); document.body.appendChild(outer)
    outer.addEventListener('submit', event => { outerSubmits++; event.preventDefault() })
    outer.addEventListener('reset', event => { outerResets++; event.preventDefault() })
    render(element, { container })
    return () => { cleanup(); outer.remove() }
  }
  try {
    const context = ctx('embedded-form')
    let confirmations = 0, saves = 0, picks = 0, cancels = 0
    const proposal = { module: { candidates: [{ ...copies[0], count: 3 }] } }
    let leave = embed(React.createElement(Confirmation, { proposal,
      onConfirm: (value, opts) => { confirmations++; return rpc(context, 'confirm', context, { ...value, expected_version: 0 }, opts.signal) },
      onCancel: () => { cancels++ } }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await screen.findByText('Role confirmation cancelled.')
    assert.equal(confirmations, 0); assert.equal(cancels, 1); assert.equal(outerSubmits, 0)
    leave()
    leave = embed(React.createElement(Confirmation, { proposal,
      onConfirm: (value, opts) => { confirmations++; return rpc(context, 'confirm', context, { ...value, expected_version: 0 }, opts.signal) } }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
    await screen.findByText('Roles confirmed.', {}, { timeout: 15000 })
    assert.equal(confirmations, 1); assert.equal(outerSubmits, 0)
    leave()
    const pin = (await rpc(context, 'load', context)).application_record
    leave = embed(React.createElement(Editor, { customerKey: context.customer_key, applicationRecord: pin,
      loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
      saveProfile: (customer, roles, opts) => { saves++; return rpc(context, 'editor_save', context,
        { customer, roles, expected_version: opts.expected_version }, opts.signal) } }))
    await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
    fireEvent.click(screen.getByRole('button', { name: 'Add roof role' }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove roof role' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reset draft' }))
    assert.equal(outerSubmits, 0); assert.equal(outerResets, 0); assert.equal(saves, 0)
    fireEvent.click(screen.getByRole('button', { name: 'Add roof role' }))
    fireEvent.change(screen.getByLabelText('Roof layer'), { target: { value: 'ROOF' } })
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    assert.equal(saves, 1); assert.equal(outerSubmits, 0)
    leave()
    leave = embed(React.createElement(Picker, { copies, onPick: () => { picks++ }, onCancel: () => { cancels++ } }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    assert.equal(picks, 0); assert.equal(cancels, 2); assert.equal(outerSubmits, 0)
    leave()
    const next = ctx('embedded-next')
    leave = embed(React.createElement(Picker, { copies,
      onPick: (value, opts) => { picks++; return rpc(next, 'apply', next, value, opts.signal) } }))
    fireEvent.click(screen.getByRole('button', { name: 'Use copy 1' }))
    await screen.findByText('Design copy selected.', {}, { timeout: 15000 })
    assert.equal(picks, 1); assert.equal(outerSubmits, 0); assert.equal(outerResets, 0)
    assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
    leave()
    console.log('PASS: embedded cancel/copy/optional-role/reset controls never submit/reset the outer form; confirmation and explicit save/apply each write once with old pin retained')
  } finally { await dispose(); assert.equal(resources().child_count, 0) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
