const assert = require('node:assert/strict'), path = require('node:path'), fs = require('node:fs')
const { spawn } = require('node:child_process')
const { python, rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, dispose, resources } = require('./x07_adapter_jsx_journey.cjs')
const LIMIT = 1024 * 1024

async function inbound(request) {
  const child = spawn('python', ['-X', 'utf8', '-c', python], {
    cwd: path.resolve(__dirname, '../../..'), stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true })
  let output = '', errors = ''
  const timer = setTimeout(() => child.kill(), 15000)
  try {
    const completion = new Promise((resolve, reject) => {
      child.on('error', reject)
      child.on('close', code => code === 0 ? resolve() : reject(new Error('worker exit ' + code)))
    })
    child.stdout.on('data', chunk => { output += chunk; assert.ok(output.length < 4096) })
    child.stderr.on('data', chunk => { errors += chunk; assert.ok(errors.length < 4096) })
    child.stdin.on('error', error => { if (error.code !== 'EPIPE') throw error })
    child.stdin.end(request)
    await completion
    assert.equal(errors, '')
    return JSON.parse(output)
  } finally {
    clearTimeout(timer)
    if (child.exitCode === null && child.signalCode === null) {
      const closed = new Promise(resolve => child.once('close', resolve)); child.kill(); await closed
    }
  }
}

async function main() {
  try {
    const context = ctx('request-limits')
    const pin = (await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })).applied_profile
    const before = fs.readFileSync(resources().database)
    for (const label of ['a', '雪']) {
      const small = await rpc(context, 'identity', context, { label: label.repeat(1024) })
      assert.equal(small.database, resources().database)
      // Direct worker input bypasses the outbound guard to measure the
      // independent inbound boundary with an otherwise valid no-write command.
      const base = JSON.stringify({ protocol_fault: 'json_grammar', fixture: { reply_json: '{"ok":true,"result":true}' }, label: '' })
      const remaining = LIMIT - Buffer.byteLength(base)
      const fill = label.repeat(Math.floor(remaining / Buffer.byteLength(label))) + 'a'.repeat(remaining % Buffer.byteLength(label))
      const raw = base.replace('"label":""', '"label":' + JSON.stringify(fill))
      assert.equal(Buffer.byteLength(raw), LIMIT)
      assert.equal((await inbound(raw)).result, true)
      const refused = await inbound(raw + ' ')
      assert.equal(refused.error.code, 'BRIDGE_REQUEST_LIMIT')
      await assert.rejects(rpc(context, 'editor_save', context,
        { customer: context.customer_key, roles: { module: { block: label.repeat(LIMIT), layer: 'MODULES' } }, expected_version: 1 }),
      error => error.code === 'BRIDGE_REQUEST_LIMIT')
      assert.equal(resources().child_count, 0)
      assert.deepEqual(fs.readFileSync(resources().database), before)
    }
    const Editor = component('X07CustomerProfileEditor')
    render(React.createElement(Editor, { customerKey: context.customer_key, applicationRecord: pin,
      loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
      saveProfile: (customer, roles, opts) => rpc(context, 'editor_save', context,
        { customer, roles, expected_version: opts.expected_version, padding: '雪'.repeat(LIMIT) }, opts.signal) }))
    await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
    fireEvent.click(screen.getByRole('button', { name: 'Add roof role' }))
    fireEvent.change(screen.getByLabelText('Roof layer'), { target: { value: 'ROOF' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByRole('alert')
    assert.equal(screen.getByLabelText('Roof layer').value, 'ROOF')
    await waitFor(() => assert.equal(resources().child_count, 0))
    assert.deepEqual(fs.readFileSync(resources().database), before)
    assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
    console.log('PASS: exact inbound byte boundary, ASCII/multibyte refusals, no outbound worker writes, retained actual JSX drafts and old pins')
  } finally { await dispose(); assert.equal(resources().child_count, 0) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
