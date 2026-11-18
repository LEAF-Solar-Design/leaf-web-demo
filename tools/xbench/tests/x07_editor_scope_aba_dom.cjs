const assert = require('node:assert/strict')
const { component, React, render, screen, fireEvent, waitFor, cleanup, dispose } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor')
const record = (customer, version, block) => ({ customer_key: customer, profile_id: 'profile-' + customer,
  version, roles: { module: { block, layer: 'MODULES' } } })

async function ready(block) {
  await waitFor(() => assert.equal(screen.getByLabelText('Module block').value, block))
  await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
}

async function main() {
  try {
    // A pending initial load cannot also submit. Exercise pending load and save
    // as separate ABA cases through the same actual component entry point.
    for (const pending of ['load', 'save']) {
      let release, sourceSignal, aLoads = 0, saves = 0
      const props = customer => ({ customerKey: customer, drawingKey: customer + ':v1',
        loadProfile: (key, opts) => {
          if (key === 'a') {
            aLoads++
            if (aLoads === 1 && pending === 'load') {
              sourceSignal = opts.signal
              return new Promise(resolve => { release = () => resolve(record('a', 999, 'Stale initial load')) })
            }
            return Promise.resolve(record('a', aLoads, 'Fresh A ' + aLoads))
          }
          return Promise.resolve(record('b', 1, 'Fresh B'))
        },
        saveProfile: (key, roles, opts) => {
          saves++; sourceSignal = opts.signal
          return new Promise(resolve => { release = () => resolve({ ...record(key, 999, 'Stale save'), roles }) })
        }
      })
      const view = render(React.createElement(Editor, props('a')))
      if (pending === 'load') await waitFor(() => assert.ok(release))
      else {
        await ready('Fresh A 1')
        fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'Old A draft' } })
        fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
        await waitFor(() => assert.ok(release))
      }
      view.rerender(React.createElement(Editor, props('b')))
      await ready('Fresh B')
      assert.ok(sourceSignal.aborted)
      view.rerender(React.createElement(Editor, props('a')))
      await ready('Fresh A 2')
      fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'New A draft' } })
      release()
      await new Promise(resolve => setImmediate(resolve))
      assert.equal(screen.getByLabelText('Module block').value, 'New A draft')
      assert.ok(screen.getByText('Profile version: 2'))
      assert.equal(screen.queryByText('Profile version: 999'), null)
      assert.equal(aLoads, 2)
      assert.equal(saves, pending === 'save' ? 1 : 0)
      cleanup()
    }
    console.log('PASS: actual JSX A-B-A retires delayed first-A load/save, keeps fresh returned-A state and new draft')
  } finally { await dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
