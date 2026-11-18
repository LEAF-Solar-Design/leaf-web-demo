const assert = require('node:assert/strict'), path = require('node:path')
const { rpc, ctx, intake, copies, component, React, render, within, waitFor, dispose, resources } = require('./x07_adapter_jsx_journey.cjs')
const userEvent = require(path.join('D:/agent-data/x07-ui-keyboard-deps/node_modules', '@testing-library/user-event')).default
const Workflow = component('X07ProfileWorkflow')
// jsdom lacks CSS.escape. Hex escapes preserve every generated radio-name
// codepoint for user-event's actual radio selector and keyboard behavior.
window.CSS = { escape: value => Array.from(value, char => '\\' + char.codePointAt(0).toString(16) + ' ').join('') }

async function main() {
  try {
    const user = userEvent.setup({ document })
    const confirmations = []
    const extra = intake.inserts.map(row => ({ ...row, name: 'Panel alternate' }))
    const drawing = { ...intake, inserts: [...intake.inserts, ...extra] }
    const candidates = [...copies, ...copies.map(row => ({ ...row, block: 'Panel alternate' }))]
    const contexts = ['one', 'two'].map(project => ({ ...ctx(project), customer_key: 'synthetic-' + project }))
    const props = bound => ({ customerKey: bound.customer_key, projectKey: bound.project_key, drawingKey: bound.drawing_key,
      loadWorkflow: (scope, opts) => rpc(bound, 'load', scope, null, opts.signal, null, null, { intake: drawing, copy_candidates: candidates }),
      confirmRoles: (scope, value, opts) => {
        confirmations.push({ scope, value })
        return rpc(bound, 'confirm', scope, value, opts.signal, null, null, { intake: drawing, copy_candidates: candidates })
      } })
    const view = render(React.createElement('div', null, ...contexts.map(bound =>
      React.createElement('section', { key: bound.project_key, 'aria-label': bound.project_key }, React.createElement(Workflow, props(bound))))))
    const one = within(view.getByRole('region', { name: 'one' }))
    const two = within(view.getByRole('region', { name: 'two' }))
    await one.findByRole('button', { name: 'Confirm roles' }, { timeout: 15000 })
    await two.findByRole('button', { name: 'Confirm roles' }, { timeout: 15000 })
    const left = one.getAllByRole('radio'), right = two.getAllByRole('radio')
    assert.equal(left.length, 2); assert.equal(right.length, 2)
    assert.notEqual(left[0].name, right[0].name)
    await user.click(left[0]); await user.keyboard('{ArrowRight}')
    assert.equal(document.activeElement, left[1])
    assert.ok(left[1].checked); assert.ok(right[0].checked)
    await user.click(right[0]); await user.keyboard('{ArrowLeft}')
    assert.equal(document.activeElement, right[1])
    assert.ok(right[1].checked); assert.ok(left[1].checked)
    await user.click(right[0])
    assert.ok(right[0].checked); assert.ok(left[1].checked)
    await user.click(one.getByRole('button', { name: 'Confirm roles' }))
    await one.findByRole('button', { name: 'Use copy 2' })
    assert.equal(confirmations.length, 0)
    assert.ok(two.getByRole('button', { name: 'Confirm roles' }))
    await user.click(two.getByRole('button', { name: 'Confirm roles' }))
    await two.findByRole('button', { name: 'Use copy 1' })
    await user.click(one.getByRole('button', { name: 'Use copy 2' }))
    await one.findByText('Drawing profile applied.', {}, { timeout: 15000 })
    await user.click(two.getByRole('button', { name: 'Use copy 1' }))
    await two.findByText('Drawing profile applied.', {}, { timeout: 15000 })
    assert.deepEqual(confirmations.map(row => row.scope), contexts)
    assert.deepEqual(confirmations.map(row => row.value.block), ['Panel alternate', 'Panel'])
    assert.deepEqual(confirmations.map(row => row.value.design_window), [copies[1].design_window, copies[0].design_window])
    await waitFor(() => assert.equal(resources().child_count, 0))
    console.log('PASS: two actual workflows isolate radio clicks/arrows, competing copy choices and scoped confirmation callbacks')
  } finally { await dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
