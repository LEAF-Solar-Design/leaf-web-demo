const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, cleanup, dispose,
  resources } = require('./x07_adapter_jsx_journey.cjs')
const Confirmation = component('X07RoleConfirmation'), Workflow = component('X07ProfileWorkflow')

async function main() {
  try {
    const context = ctx('readonly-journey'), other = { customer_key: 'readonly-b', project_key: 'readonly-b', drawing_key: 'b:v1' }
    const fixture = { receipt_database: path.join(resources().root, 'readonly-receipts.sqlite') }
    const invoke = (scope, op, value, readOnly = false, signal) => rpc(scope, op, scope, value, signal, null, null,
      { ...fixture, read_only: readOnly })
    const request = { ...copies[0], expected_version: 0 }
    const original = await invoke(context, 'confirm', request)
    await invoke(other, 'confirm', { ...copies[1], expected_version: 0 })
    await invoke(context, 'editor_save', { customer: context.customer_key, roles: { module: { block: 'Panel', layer: 'MODULES' } }, expected_version: 1 })
    const before = [fs.readFileSync(resources().database), fs.readFileSync(fixture.receipt_database)]
    let replays = 0, applies = 0, saves = 0, refused = 0
    render(React.createElement(Confirmation, { proposal: { module: { candidates: [{ ...copies[0], count: 3 }] } },
      onConfirm: async (value, opts) => { replays++; const result = await invoke(context, 'confirm', request, true, opts.signal)
        assert.deepEqual(result, original); return result }
    }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
    await screen.findByText('Roles confirmed.', {}, { timeout: 15000 })
    assert.equal(replays, 1); cleanup()
    const props = readOnly => ({ customerKey: context.customer_key, projectKey: context.project_key, drawingKey: context.drawing_key,
      loadWorkflow: (scope, opts) => invoke(scope, 'load', null, readOnly, opts.signal),
      confirmRoles: () => { throw new Error('no new confirmation') },
      applyProfile: (scope, value, opts) => { applies++; return invoke(scope, 'apply', value, readOnly, opts.signal) },
      loadProfile: (customer, opts) => invoke(context, 'editor_load', customer, readOnly, opts.signal),
      saveProfile: async (customer, roles, opts) => {
        saves++
        try { return await invoke(context, 'editor_save', { customer, roles, expected_version: opts.expected_version }, readOnly, opts.signal) }
        catch (error) { if (readOnly) { refused++; assert.match(error.message, /read-only/) } throw error }
      }
    })
    const view = render(React.createElement(Workflow, props(true)))
    fireEvent.click(await screen.findByRole('button', { name: 'Use copy 1' }, { timeout: 15000 }))
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    assert.equal(applies, 1); assert.ok(screen.getByText('Project is pinned to version 1.'))
    fireEvent.click(screen.getByRole('button', { name: 'Add roof role' }))
    fireEvent.change(screen.getByLabelText('Roof layer'), { target: { value: 'ROOF' } })
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByRole('alert', {}, { timeout: 15000 })
    assert.equal(saves, 1); assert.equal(refused, 1)
    assert.equal(screen.getByLabelText('Roof layer').value, 'ROOF')
    assert.deepEqual([fs.readFileSync(resources().database), fs.readFileSync(fixture.receipt_database)], before)
    await assert.rejects(invoke(context, 'editor_load', other.customer_key, true), /editor customer does not match injected scope/)
    // The host explicitly supplies writable test stores; callback identity
    // changes alone must not issue another load, apply or save.
    view.rerender(React.createElement(Workflow, props(false)))
    assert.equal(saves, 1); assert.equal(applies, 1)
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('Profile version: 3', {}, { timeout: 15000 })
    assert.equal(saves, 2); assert.equal(refused, 1)
    assert.deepEqual((await invoke(context, 'load')).application_record, original.applied_profile)
    assert.equal((await invoke(other, 'editor_load', other.customer_key, true)).version, 1)
    console.log('PASS: actual JSX replays/reloads/reuses pinned read-only stores, refuses one save retaining draft, then explicitly retries writable once without moving old/foreign pins')
  } finally { await dispose(); assert.equal(resources().child_count, 0) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
