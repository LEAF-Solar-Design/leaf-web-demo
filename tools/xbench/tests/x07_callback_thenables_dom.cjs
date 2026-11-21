const assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, cleanup, dispose, resources } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor'), Confirmation = component('X07RoleConfirmation'), Picker = component('X07CopyPicker')
const throwingGetter = () => Object.defineProperty({}, 'then', { get() { throw new Error('then getter refused') } })
const settled = (value, mode) => ({ then(resolve, reject) {
  resolve(value)
  if (mode === 'double') { resolve({ invalid: true }); reject(new Error('late rejection')) }
  else throw new Error('throw after resolution')
} })

async function main() {
  const unhandled = [], record = error => unhandled.push(error)
  process.on('unhandledRejection', record)
  try {
    const context = ctx('thenables')
    const original = await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })
    let loads = 0, saves = 0
    render(React.createElement(Editor, { customerKey: context.customer_key, applicationRecord: original.applied_profile,
      loadProfile: (customer, opts) => ++loads === 1 ? throwingGetter() : rpc(context, 'editor_load', context, customer, opts.signal),
      saveProfile: (customer, roles, opts) => {
        saves++
        if (saves === 1) return throwingGetter()
        return rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version }, opts.signal)
          .then(value => settled(value, saves === 2 ? 'double' : 'throw'))
      } }))
    await screen.findByRole('alert')
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading profile' }))
    await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
    fireEvent.click(screen.getByRole('button', { name: 'Add roof role' }))
    fireEvent.change(screen.getByLabelText('Roof layer'), { target: { value: 'ROOF' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByRole('alert')
    assert.equal(screen.getByLabelText('Roof layer').value, 'ROOF')
    assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, 1)
    for (const version of [2, 3]) {
      await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
      fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
      await screen.findByText('Profile version: ' + version, {}, { timeout: 15000 })
      assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, version)
    }
    assert.equal(loads, 2); assert.equal(saves, 3)
    cleanup()
    const fresh = { ...ctx('thenable-confirm'), customer_key: 'thenable-b' }
    let confirmations = 0
    render(React.createElement(Confirmation, { proposal: { module: { candidates: [{ ...copies[0], count: 3 }] } },
      onConfirm: (value, opts) => {
        if (++confirmations === 1) return throwingGetter()
        return rpc(fresh, 'confirm', fresh, { ...value, expected_version: 0 }, opts.signal).then(value => settled(value, 'double'))
      } }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
    await screen.findByRole('alert')
    assert.equal((await rpc(fresh, 'load', fresh)).expected_version, 0)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
    await screen.findByText('Roles confirmed.', {}, { timeout: 15000 })
    assert.equal(confirmations, 2)
    cleanup()
    const next = ctx('thenable-apply')
    let picks = 0
    render(React.createElement(Picker, { copies, onPick: (value, opts) => {
      if (++picks === 1) return throwingGetter()
      return rpc(next, 'apply', next, value, opts.signal).then(value => settled(value, 'throw'))
    } }))
    fireEvent.click(screen.getByRole('button', { name: 'Use copy 1' }))
    await screen.findByRole('alert')
    assert.equal((await rpc(next, 'load', next)).application_record, null)
    fireEvent.click(screen.getByRole('button', { name: 'Use copy 1' }))
    await screen.findByText('Design copy selected.', {}, { timeout: 15000 })
    assert.equal(picks, 2)
    assert.equal((await rpc(next, 'load', next)).application_record.version, 3)
    assert.deepEqual((await rpc(context, 'load', context)).application_record, original.applied_profile)
    assert.deepEqual(unhandled, [])
    assert.equal(resources().child_count, 0)
    console.log('PASS: throwing then getters release load/save/confirm/pick controls; explicit retries, double settlement and resolve-then-throw preserve one terminal outcome and old pins')
  } finally { await dispose(); process.removeListener('unhandledRejection', record) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
