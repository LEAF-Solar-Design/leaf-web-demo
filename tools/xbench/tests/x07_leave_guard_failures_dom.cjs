const assert = require('node:assert/strict')
const { rpc, copies, component, React, render, screen, fireEvent, waitFor, dispose } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor')
global.IS_REACT_ACT_ENVIRONMENT = true

async function main() {
  try {
    const contexts = ['a', 'b', 'c'].map(key => ({ customer_key: 'guard-' + key, project_key: key, drawing_key: key + ':v1' }))
    const pins = []
    for (const context of contexts) pins.push((await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })).applied_profile)
    let mode = 'throw', writes = 0
    const delayed = []
    const props = (index, attempt) => ({ customerKey: contexts[index].customer_key, drawingKey: contexts[index].drawing_key,
      applicationRecord: pins[index], leaveAttempt: attempt,
      leaveGuard: request => {
        if (mode === 'throw') throw new Error('injected synchronous guard error')
        if (mode === 'reject') return Promise.reject(new Error('injected asynchronous guard error'))
        if (mode === 'delay') return new Promise(resolve => delayed.push({ request, resolve }))
        return Promise.resolve(mode)
      },
      loadProfile: (customer, opts) => rpc(contexts[index], 'editor_load', contexts[index], customer, opts.signal),
      saveProfile: () => { writes++; throw new Error('unexpected save') }
    })
    const view = render(React.createElement(Editor, props(0, 0)))
    await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'pAnEl' } })
    for (const [index, fault] of ['throw', 'reject'].entries()) {
      mode = fault
      view.rerender(React.createElement(Editor, props(1, index + 1)))
      await screen.findByText('Profile edits kept. Leave decision failed.')
      assert.ok(screen.getByText('Customer: guard-a'))
      assert.equal(screen.getByLabelText('Module block').value, 'pAnEl')
    }
    mode = 'delay'
    view.rerender(React.createElement(Editor, props(1, 3)))
    await waitFor(() => assert.equal(delayed.length, 1))
    view.rerender(React.createElement(Editor, props(2, 4)))
    await waitFor(() => assert.equal(delayed.length, 2))
    assert.ok(delayed[0].request.signal.aborted)
    await React.act(async () => { delayed[1].resolve('stay') })
    await screen.findByText('Profile edits kept. Staying on the current drawing.')
    await React.act(async () => { delayed[0].resolve('discard') })
    assert.ok(screen.getByText('Customer: guard-a'))
    assert.equal(screen.getByLabelText('Module block').value, 'pAnEl')
    mode = 'discard'
    view.rerender(React.createElement(Editor, props(2, 5)))
    await screen.findByText('Customer: guard-c', {}, { timeout: 15000 })
    await waitFor(() => assert.equal(screen.getByLabelText('Module block').value, 'Panel'))
    assert.equal(writes, 0)
    for (const [index, context] of contexts.entries()) assert.deepEqual((await rpc(context, 'load', context)).application_record, pins[index])
    console.log('PASS: throwing/rejected/reordered leave guards retain drafts, show retryable failure, retire old decisions and switch only on fresh discard')
  } finally { await dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
