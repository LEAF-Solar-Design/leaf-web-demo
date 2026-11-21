const assert = require('node:assert/strict')
const { component, React, render, screen, fireEvent, waitFor, cleanup, dispose } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor')
global.IS_REACT_ACT_ENVIRONMENT = true
const record = () => ({ customer_key: 'a', profile_id: 'profile-a', version: 1, roles: { module: { block: 'Panel', layer: 'M' } } })

async function main() {
  try {
    for (const action of ['reset', 'cancel']) for (const succeeds of [true, false]) {
      let stored = record(), resolve, reject, signal, saves = 0
      render(React.createElement(Editor, { customerKey: 'a', drawingKey: action + succeeds,
        loadProfile: () => Promise.resolve(structuredClone(stored)),
        saveProfile: (customer, roles, opts) => {
          saves++; signal = opts.signal
          return new Promise((yes, no) => {
            resolve = () => { stored = { ...record(), version: 2, roles }; yes(structuredClone(stored)) }
            reject = () => no(new Error('injected save failure'))
          })
        }
      }))
      await screen.findByText('Profile version: 1')
      await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
      fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'Discarded draft' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
      await waitFor(() => assert.ok(resolve))
      fireEvent.click(screen.getByRole('button', { name: action === 'reset' ? 'Reset draft' : 'Cancel save' }))
      assert.equal(screen.getByLabelText('Module block').value, 'Panel')
      if (action === 'cancel') assert.ok(signal.aborted)
      await React.act(async () => { succeeds ? resolve() : reject() })
      assert.equal(screen.getByLabelText('Module block').value, 'Panel')
      assert.equal(saves, 1)
      assert.equal(stored.version, succeeds ? 2 : 1)
      if (action === 'cancel') {
        assert.ok(screen.getByText('Save cancelled. Stored changes may have completed. Reload the profile to reconcile.'))
        assert.ok(screen.getByText('Profile version: 1'))
        fireEvent.click(screen.getByRole('button', { name: 'Reload stored profile' }))
        await screen.findByText('Stored profile reloaded.')
        assert.ok(screen.getByText('Profile version: ' + stored.version))
        assert.equal(screen.getByLabelText('Module block').value, stored.roles.module.block)
      } else if (succeeds) {
        assert.ok(screen.getByText('Profile version: 2'))
        assert.ok(screen.getByText('Profile saved. Newer edits are unsaved.'))
      } else {
        assert.ok(screen.getByRole('alert'))
        assert.ok(screen.getByText('Profile version: 1'))
      }
      assert.equal(saves, 1)
      cleanup()
    }
    console.log('PASS: pending reset/cancel keeps discarded drafts retired across late success/failure, explicitly reconciles stored state and never writes twice')
  } finally { await dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
