const assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, dispose,
  resources } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor')
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const status = text => screen.getAllByRole('status').find(node => node.textContent === text)
const version = number => {
  const node = screen.getByText('Profile version: ' + number)
  assert.equal(node.getAttribute('aria-live'), 'polite'); assert.equal(node.getAttribute('aria-atomic'), 'true')
}

async function main() {
  try {
    const context = ctx('announcements'), first = deferred(), third = deferred()
    const pin = (await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })).applied_profile
    let saves = 0, loads = 0; const committed = []
    render(React.createElement(Editor, { customerKey: context.customer_key, applicationRecord: pin,
      loadProfile: (customer, opts) => { loads++; return rpc(context, 'editor_load', context, customer, opts.signal) },
      saveProfile: async (customer, roles, opts) => {
        const call = ++saves
        const result = await rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version }, opts.signal)
        committed.push(result.version)
        if (call === 1) await first.promise
        if (call === 3) await third.promise
        return result
      }
    }))
    await screen.findByText('Profile version: 1', {}, { timeout: 15000 }); version(1)
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.change(screen.getByLabelText('Module layer'), { target: { value: 'mOdUlEs' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await waitFor(() => assert.equal(saves, 1))
    assert.ok(status('Reading or saving profile.'))
    fireEvent.click(screen.getByRole('button', { name: 'Add roof role' }))
    fireEvent.change(screen.getByLabelText('Roof layer'), { target: { value: 'ROOF' } })
    await React.act(async () => first.resolve())
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 }); version(2)
    assert.ok(status('Profile saved. Newer edits are unsaved.'))
    assert.equal(screen.getByLabelText('Roof layer').value, 'ROOF'); assert.equal(saves, 1)
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('Profile version: 3', {}, { timeout: 15000 }); version(3)
    assert.ok(status('Profile saved.')); assert.equal(saves, 2)
    // The third pending save has no newer draft changes. Its outcome becomes
    // unknown to the UI after cancellation, while the actual store committed4.
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await waitFor(() => assert.deepEqual(committed, [2, 3, 4]))
    assert.ok(status('Reading or saving profile.'))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel save' }))
    const unknown = 'Save cancelled. Stored changes may have completed. Reload the profile to reconcile.'
    assert.ok(status(unknown)); assert.equal(screen.getByLabelText('Roof layer').value, 'ROOF')
    version(3); assert.equal(loads, 1)
    await React.act(async () => third.resolve())
    assert.ok(status(unknown)); version(3)
    fireEvent.click(screen.getByRole('button', { name: 'Reload stored profile' }))
    await screen.findByText('Profile version: 4', {}, { timeout: 15000 }); version(4)
    assert.ok(status('Stored profile reloaded.')); assert.equal(loads, 2); assert.equal(saves, 3)
    assert.equal(screen.getByLabelText('Roof layer').value, 'ROOF')
    assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
    console.log('PASS: pending/saved/newer-unsaved/cancelled-unknown outcomes have accessible status announcements and polite atomic version updates; explicit reload reconciles without extra writes')
  } finally { await dispose(); assert.equal(resources().child_count, 0) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
