'use strict'
const assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, cleanup, dispose,
  resources } = require('./x07_adapter_jsx_journey.cjs')
const Confirmation = component('X07RoleConfirmation'), Editor = component('X07CustomerProfileEditor'), Picker = component('X07CopyPicker')
const freeze = value => { if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value) } return value }

async function main() {
  try {
    const context = ctx('frozen')
    const candidate = { ...structuredClone(copies[0]), count: 3 }
    const proposal = freeze({ module: { first_choice: candidate, candidates: [candidate] } })
    const originalProposal = JSON.stringify(proposal)
    let confirmations = 0, saves = 0, picks = 0
    render(React.createElement(Confirmation, { proposal, onConfirm: async (value, opts) => {
      confirmations++; assert.notEqual(value, candidate); assert.notEqual(value.design_window, candidate.design_window)
      const result = await rpc(context, 'confirm', context, { ...value, expected_version: 0 }, opts.signal)
      value.design_window[0] = 999
      return result
    } }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
    await screen.findByText('Roles confirmed.', {}, { timeout: 15000 })
    assert.equal(confirmations, 1); assert.equal(JSON.stringify(proposal), originalProposal)
    cleanup()
    const profile = freeze(await rpc(context, 'editor_load', context, context.customer_key))
    const pin = freeze((await rpc(context, 'load', context)).application_record)
    const profileBefore = JSON.stringify(profile), pinBefore = JSON.stringify(pin)
    render(React.createElement(Editor, { customerKey: context.customer_key, applicationRecord: pin,
      loadProfile: () => Promise.resolve(profile), saveProfile: async (customer, roles, opts) => {
        saves++; assert.notEqual(roles, profile.roles); assert.notEqual(roles.module, profile.roles.module)
        const result = await rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version }, opts.signal)
        roles.module.block = 'callback-owned mutation'
        return freeze(result)
      } }))
    await screen.findByText('Profile version: 1')
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.change(screen.getByLabelText('Module layer'), { target: { value: 'mOdUlEs' } })
    assert.equal(saves, 0)
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    assert.equal(saves, 1); assert.equal(JSON.stringify(profile), profileBefore); assert.equal(JSON.stringify(pin), pinBefore)
    cleanup()
    const rows = freeze(structuredClone(copies)), before = JSON.stringify(rows)
    const next = ctx('frozen-next')
    render(React.createElement(Picker, { copies: rows, onPick: async (value, opts) => {
      picks++; assert.notEqual(value, rows[0]); assert.notEqual(value.design_window, rows[0].design_window)
      const result = await rpc(next, 'apply', next, value, opts.signal)
      value.design_window[0] = 999
      return freeze(result)
    } }))
    fireEvent.click(screen.getByRole('button', { name: 'Use copy 1' }))
    await screen.findByText('Design copy selected.', {}, { timeout: 15000 })
    assert.equal(picks, 1); assert.equal(JSON.stringify(rows), before)
    assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
    console.log('PASS: deep-frozen proposal/profile/copy receipts render and edit; explicit callbacks receive separate mutable payloads and all originals/pins remain unchanged')
  } finally { await dispose(); assert.equal(resources().child_count, 0) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
