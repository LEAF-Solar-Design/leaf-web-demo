const assert = require('node:assert/strict')
const path = require('node:path')
const { rpc, copies, component, React, render, screen, within, waitFor, dispose } = require('./x07_adapter_jsx_journey.cjs')
const at = process.argv.indexOf('--keyboard-deps')
const keyboardRoot = at < 0 ? 'D:/agent-data/x07-ui-keyboard-deps/node_modules' : process.argv[at + 1]
const userEvent = require(path.join(path.resolve(keyboardRoot), '@testing-library/user-event')).default
const Editor = component('X07CustomerProfileEditor')

async function main() {
  try {
    const user = userEvent.setup({ document: global.document })
    const contexts = ['a', 'b'].map(customer => ({ customer_key: 'labels-' + customer, project_key: customer, drawing_key: customer + ':v1' }))
    const pins = []
    for (const context of contexts) pins.push((await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })).applied_profile)
    const saves = [0, 0]
    render(React.createElement(React.Fragment, null, ...contexts.map((context, index) => React.createElement(Editor, {
      key: context.customer_key, customerKey: context.customer_key, drawingKey: context.drawing_key, applicationRecord: pins[index],
      loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
      saveProfile: (customer, roles, opts) => { saves[index]++; return rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version }, opts.signal) }
    }))))
    const sections = await screen.findAllByRole('region', { name: 'Customer role profile' })
    await waitFor(() => assert.equal(sections.reduce((count, section) => count + section.querySelectorAll('input').length, 0), 4), { timeout: 15000 })
    const ids = [...document.querySelectorAll('[id]')].map(node => node.id)
    assert.equal(new Set(ids).size, ids.length)
    for (const section of sections) for (const label of section.querySelectorAll('label')) {
      assert.ok(label.htmlFor)
      assert.ok(section.contains(document.getElementById(label.htmlFor)))
      assert.equal(label.control, document.getElementById(label.htmlFor))
    }
    const left = within(sections[0]), right = within(sections[1])
    await user.click(left.getByText('Module block', { selector: 'label' }))
    assert.equal(document.activeElement, left.getByLabelText('Module block'))
    await user.clear(left.getByLabelText('Module block'))
    await user.click(left.getByRole('button', { name: 'Save profile' }))
    assert.ok(left.getByRole('alert'))
    assert.equal(right.queryByRole('alert'), null)
    assert.equal(right.getByLabelText('Module block').value, 'Panel')
    for (const node of sections[0].querySelectorAll('[aria-describedby]')) for (const id of node.getAttribute('aria-describedby').split(/\s+/)) {
      assert.ok(sections[0].contains(document.getElementById(id)))
      assert.ok(!sections[1].contains(document.getElementById(id)))
    }
    await user.type(left.getByLabelText('Module block'), 'pAnEl')
    await user.click(left.getByRole('button', { name: 'Save profile' }))
    await waitFor(() => assert.ok(left.getByText('Profile version: 2')), { timeout: 15000 })
    assert.deepEqual(saves, [1, 0])
    assert.ok(right.getByText('Profile version: 1'))
    assert.equal(right.getByLabelText('Module block').value, 'Panel')
    console.log('PASS: two editor instances have unique label/control/error references and isolated keyboard edits, validation and saves')
  } finally { await dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
