const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path')
const { execFileSync } = require('node:child_process')
const { rpc: rawRpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, cleanup, dispose,
  resources } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor'), Confirmation = component('X07RoleConfirmation'), Workflow = component('X07ProfileWorkflow')
const journal = path.join(resources().root, 'settled-receipts.sqlite')
const rpc = (scope, op, context, value, signal, fault, protocol) => rawRpc(scope, op, context, value, signal, fault, protocol, {
  receipt_database: journal,
  on_worker_failure: detail => console.error('X07_SYNTHETIC_WORKER_DIAGNOSTIC ' + JSON.stringify(detail))
})
function auditBoth() {
  const before = [fs.readFileSync(resources().database), fs.readFileSync(journal)]
  const code = `import sys,json
from pathlib import Path
sys.path[:0]=[str(Path.cwd()/'server'),str(Path.cwd()/'tools/xbench')]
from x07_receipt_history_audit import CustomerProfileStore,ConfirmationReceiptStore,audit_receipts
s=CustomerProfileStore(sys.argv[1],read_only=True);j=ConfirmationReceiptStore(sys.argv[2],read_only=True)
print(json.dumps([audit_receipts(s,j,customer_key=k) for k in ['synthetic-a','synthetic-b']]))`
  const reports = JSON.parse(execFileSync('python', ['-P', '-c', code, resources().database, journal], { encoding: 'utf8', timeout: 10000 }))
  for (const [index, customer] of ['synthetic-a', 'synthetic-b'].entries()) {
    assert.deepEqual(reports[index], { schema: 'leaf.x07.receipt-history-audit.v1', customer_key: customer, receipt_count: 1,
      healthy: true, issues: { invalid_receipts: 0, missing_references: 0, inconsistent_references: 0 } })
  }
  assert.deepEqual([fs.readFileSync(resources().database), fs.readFileSync(journal)], before)
}
async function confirm(scope, choice) {
  let result
  render(React.createElement(Confirmation, { proposal: { module: { candidates: [{ ...choice, count: 3 }] } },
    onConfirm: async (value, opts) => { result = await rpc(scope, 'confirm', scope, { ...choice, expected_version: 0 }, opts.signal); return result }
  }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  await screen.findByText('Roles confirmed.', {}, { timeout: 15000 })
  cleanup(); return result.applied_profile
}

async function main() {
  try {
    const context = ctx('receipt-audit-jsx')
    const other = { customer_key: 'synthetic-b', project_key: 'receipt-audit-b', drawing_key: 'b:v1' }
    const pin = await confirm(context, copies[0]), otherPin = await confirm(other, copies[1])
    auditBoth()
    let attempts = 0, lost = 0
    render(React.createElement(Editor, { customerKey: context.customer_key, drawingKey: context.drawing_key,
      applicationRecord: pin,
      loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
      saveProfile: async (customer, roles, opts) => {
        attempts++
        try { return await rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version },
          opts.signal, null, attempts === 1 ? 'lost_save_reply' : null) }
        catch (error) { if (attempts === 1) { assert.equal(error.code, 'BRIDGE_EOF'); lost++ } throw error }
      }
    }))
    await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.change(screen.getByLabelText('Module layer'), { target: { value: 'mOdUlEs' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByRole('alert', {}, { timeout: 15000 })
    assert.equal(lost, 1); assert.equal(attempts, 1)
    assert.equal(screen.getByLabelText('Module layer').value, 'mOdUlEs')
    assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, 2)
    assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
    auditBoth()
    // A changed draft must not turn an uncertain first outcome into a blind
    // second version. The stale expected version refuses until explicit reload.
    fireEvent.click(screen.getByRole('button', { name: 'Add roof role' }))
    fireEvent.change(screen.getByLabelText('Roof layer'), { target: { value: 'ROOF' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('The profile changed. Your edits are kept. Reload the version to retry.', {}, { timeout: 15000 })
    assert.equal(attempts, 2)
    assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, 2)
    fireEvent.click(screen.getByRole('button', { name: 'Reload version, keep edits' }))
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    assert.equal(screen.getByLabelText('Roof layer').value, 'ROOF')
    assert.equal(screen.getByLabelText('Module layer').value, 'mOdUlEs')
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('Profile version: 3', {}, { timeout: 15000 })
    assert.equal(attempts, 3)
    const latest = await rpc(context, 'editor_load', context, context.customer_key)
    assert.equal(latest.roles.roof.layer, 'ROOF')
    assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
    auditBoth()
    cleanup()
    render(React.createElement(Workflow, { customerKey: context.customer_key, projectKey: context.project_key, drawingKey: context.drawing_key,
      loadWorkflow: (scope, opts) => rpc(scope, 'load', scope, null, opts.signal),
      confirmRoles: () => { throw new Error('pinned reopen must not reconfirm') },
      applyProfile: (scope, value, opts) => rpc(scope, 'apply', scope, value, opts.signal),
      loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
      saveProfile: () => { throw new Error('reopen must not save') }
    }))
    fireEvent.click(await screen.findByRole('button', { name: 'Use copy 2' }, { timeout: 15000 }))
    await screen.findByText('Profile version: 3', {}, { timeout: 15000 })
    assert.ok(screen.getByText('Project is pinned to version 1.'))
    assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
    assert.deepEqual((await rpc(other, 'load', other)).application_record, otherPin)
    assert.equal((await rpc(other, 'editor_load', other, other.customer_key)).version, 1)
    auditBoth()
    console.log('PASS: actual JSX two-customer saves, copy choice, lost reply and pinned reopen retain independent exact settled audit findings and old pins')
  } finally { await dispose(); assert.equal(resources().child_count, 0) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
