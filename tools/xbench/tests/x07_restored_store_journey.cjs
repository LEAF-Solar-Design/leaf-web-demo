const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, cleanup, dispose,
  resources } = require('./x07_adapter_jsx_journey.cjs')
const Confirmation = component('X07RoleConfirmation'), Editor = component('X07CustomerProfileEditor')

async function main() {
  try {
    const context = ctx('restored'), foreign = { customer_key: 'restore-b', project_key: 'restore-b', drawing_key: 'b:v1' }
    const root = resources().root
    const original = { receipt_database: path.join(root, 'receipts.sqlite') }
    const invoke = (scope, operation, value, fixture = original, signal) => rpc(scope, operation, scope, value, signal, null, null, fixture)
    const plan = await invoke(context, 'load')
    const payload = { ...copies[0], plan_token: plan.plan_token, expected_version: 0 }
    const confirmed = await invoke(context, 'confirm', payload)
    await invoke(foreign, 'confirm', { ...copies[1], expected_version: 0 })
    await invoke(context, 'editor_save', { customer: context.customer_key, roles: { module: { block: 'Panel', layer: 'modules' } }, expected_version: 1 })
    const restoredRoot = path.join(root, 'restored'); fs.mkdirSync(restoredRoot)
    const restored = { database: path.join(restoredRoot, 'profiles.sqlite'), receipt_database: path.join(restoredRoot, 'receipts.sqlite') }
    assert.deepEqual(await invoke(context, 'backup', null, { ...original,
      backup_paths: { profiles: restored.database, receipts: restored.receipt_database } }), { backed_up: true })
    const backupBytes = [fs.readFileSync(restored.database), fs.readFileSync(restored.receipt_database)]
    let replayCalls = 0, saves = 0
    render(React.createElement(Confirmation, { proposal: { module: { candidates: [{ ...copies[0], count: 3 }] } },
      onConfirm: async (value, opts) => { replayCalls++; const result = await invoke(context, 'confirm', payload, restored, opts.signal)
        assert.deepEqual(result, confirmed); return result }
    }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
    await screen.findByText('Roles confirmed.', {}, { timeout: 15000 })
    assert.equal(replayCalls, 1)
    assert.deepEqual([fs.readFileSync(restored.database), fs.readFileSync(restored.receipt_database)], backupBytes)
    cleanup()
    render(React.createElement(Editor, { customerKey: context.customer_key, drawingKey: context.drawing_key,
      applicationRecord: confirmed.applied_profile,
      loadProfile: (customer, opts) => invoke(context, 'editor_load', customer, restored, opts.signal),
      saveProfile: (customer, roles, opts) => { saves++; return invoke(context, 'editor_save',
        { customer, roles, expected_version: opts.expected_version }, restored, opts.signal) }
    }))
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    assert.equal(screen.getByLabelText('Module layer').value, 'MODULES')
    assert.equal(screen.getByText('Project is pinned to version 1.').textContent, 'Project is pinned to version 1.')
    assert.equal(saves, 0)
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.change(screen.getByLabelText('Module layer'), { target: { value: 'modules' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('Profile version: 3', {}, { timeout: 15000 })
    assert.equal(saves, 1)
    assert.deepEqual((await invoke(context, 'load', null, restored)).application_record, confirmed.applied_profile)
    assert.equal((await invoke(context, 'editor_load', context.customer_key)).version, 2)
    assert.equal((await invoke(foreign, 'editor_load', foreign.customer_key, restored)).version, 1)
    assert.equal((await invoke(foreign, 'load', null, restored)).application_record.customer_key, foreign.customer_key)
    await assert.rejects(invoke(context, 'editor_load', foreign.customer_key, restored), /editor customer does not match injected scope/)
    console.log('PASS: settled SQLite backups restore to fresh paths/adapters; actual JSX replays same request without writes, reloads scoped latest version and retains old/foreign pins')
  } finally { await dispose(); assert.equal(resources().child_count, 0) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
