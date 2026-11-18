const assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, mount, screen, fireEvent, waitFor, cleanup, dispose } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor'), Confirmation = component('X07RoleConfirmation')

async function main() {
  try {
    const context = ctx('sync-editor')
    const original = await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })
    let loads = 0, saves = 0
    render(React.createElement(Editor, { customerKey: context.customer_key, drawingKey: context.drawing_key,
      applicationRecord: original.applied_profile,
      loadProfile: (customer, opts) => { if (++loads === 1) throw new Error('synchronous load'); return rpc(context, 'editor_load', context, customer, opts.signal) },
      saveProfile: (customer, roles, opts) => { if (++saves === 1) throw new Error('synchronous save'); return rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version }, opts.signal) }
    }))
    await screen.findByRole('alert')
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading profile' }))
    await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
    assert.equal(loads, 2)
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'pAnEl' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByRole('alert')
    assert.equal(screen.getByLabelText('Module block').value, 'pAnEl')
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    assert.equal(saves, 2)
    cleanup()

    const fresh = { customer_key: 'sync-b', project_key: 'sync-confirm', drawing_key: 'sync-b:v1' }
    let confirmations = 0
    render(React.createElement(Confirmation, { proposal: { module: { candidates: [{ ...copies[0], count: 3 }] } },
      onConfirm: (value, opts) => { if (++confirmations === 1) throw new Error('synchronous confirmation'); return rpc(fresh, 'confirm', fresh, { ...value, expected_version: 0 }, opts.signal) }
    }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
    await screen.findByRole('alert')
    assert.ok(!screen.getByRole('button', { name: 'Confirm roles' }).disabled)
    assert.equal((await rpc(fresh, 'load', fresh)).expected_version, 0)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
    await screen.findByText('Roles confirmed.', {}, { timeout: 15000 })
    assert.equal(confirmations, 2)
    cleanup()

    const next = ctx('sync-apply')
    let workflowLoads = 0, applies = 0
    mount(next, {
      loadWorkflow: (scope, opts) => { if (++workflowLoads === 1) throw new Error('synchronous workflow load'); return rpc(next, 'load', scope, null, opts.signal) },
      applyProfile: (scope, value, opts) => { if (++applies === 1) throw new Error('synchronous apply'); return rpc(next, 'apply', scope, value, opts.signal) }
    })
    await screen.findByRole('alert')
    fireEvent.click(screen.getByRole('button', { name: 'Retry workflow' }))
    await screen.findByRole('button', { name: 'Use copy 1' }, { timeout: 15000 })
    fireEvent.click(screen.getByRole('button', { name: 'Use copy 1' }))
    await screen.findByRole('alert')
    assert.ok(!screen.getByRole('button', { name: 'Use copy 1' }).disabled)
    fireEvent.click(screen.getByRole('button', { name: 'Use copy 1' }))
    await screen.findByText('Drawing profile applied.', {}, { timeout: 15000 })
    assert.equal(workflowLoads, 2); assert.equal(applies, 2)
    assert.equal((await rpc(next, 'load', next)).application_record.version, 2)
    assert.deepEqual((await rpc(context, 'load', context)).application_record, original.applied_profile)
    console.log('PASS: synchronous editor/workflow load, save, confirm and apply throws release controls, retain drafts and succeed only on explicit retry')
  } finally { await dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
