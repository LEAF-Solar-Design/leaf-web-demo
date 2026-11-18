const assert = require('node:assert/strict'), path = require('node:path')
const { rpc, ctx, copies, component, React, render, screen, waitFor, dispose,
  resources } = require('./x07_adapter_jsx_journey.cjs')
const keyboardAt = process.argv.indexOf('--keyboard-deps')
const keyboardRoot = keyboardAt < 0 ? 'D:/agent-data/x07-ui-keyboard-deps/node_modules' : process.argv[keyboardAt + 1]
const userEvent = require(path.join(path.resolve(keyboardRoot), '@testing-library/user-event')).default
const Editor = component('X07CustomerProfileEditor'), Confirmation = component('X07RoleConfirmation'), Picker = component('X07CopyPicker')

async function main() {
  try {
    const user = userEvent.setup({ document: global.document })
    const context = ctx('disabled-fieldset')
    const pin = (await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })).applied_profile
    let loads = 0, saves = 0, confirms = 0, picks = 0, cancels = 0
    const editorProps = { customerKey: context.customer_key, applicationRecord: pin,
      loadProfile: (customer, opts) => { loads++; return rpc(context, 'editor_load', context, customer, opts.signal) },
      saveProfile: (customer, roles, opts) => { saves++; return rpc(context, 'editor_save', context,
        { customer, roles, expected_version: opts.expected_version }, opts.signal) } }
    const element = disabled => React.createElement(React.Fragment, null,
      React.createElement('fieldset', { disabled },
        React.createElement(Editor, editorProps),
        React.createElement(Confirmation, { proposal: { module: { candidates: [{ ...copies[0], count: 3 }] } },
          onConfirm: () => { confirms++ }, onCancel: () => { cancels++ } }),
        React.createElement(Picker, { copies, onPick: () => { picks++ }, onCancel: () => { cancels++ } })),
      React.createElement('button', { type: 'button' }, 'Outside'))
    const view = render(element(true))
    await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
    assert.equal(loads, 1) // Initial scoped read is allowed, actions are disabled.
    const input = screen.getByLabelText('Module block')
    await user.click(input); await user.type(input, 'DISABLED')
    for (const name of ['Save profile', 'Reset draft', 'Add roof role', 'Confirm roles', 'Use copy 1'])
      await user.click(screen.getByRole('button', { name }))
    for (const button of screen.getAllByRole('button', { name: 'Cancel' })) await user.click(button)
    await user.click(document.body); await user.tab()
    assert.equal(document.activeElement, screen.getByRole('button', { name: 'Outside' }))
    await user.keyboard('{Enter} ')
    assert.equal(input.value, 'Panel')
    assert.equal(screen.queryByLabelText('Roof layer'), null)
    assert.deepEqual([loads, saves, confirms, picks, cancels], [1, 0, 0, 0, 0])
    view.rerender(element(false))
    await user.click(screen.getByRole('button', { name: 'Add roof role' }))
    await user.type(screen.getByLabelText('Roof layer'), 'ROOF')
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    await user.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    assert.deepEqual([loads, saves, confirms, picks, cancels], [1, 1, 0, 0, 0])
    assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
    console.log('PASS: inherited disabled fieldset blocks pointer/keyboard action callbacks; enabling permits one explicit adapter save and retains old pin')
  } finally { await dispose(); assert.equal(resources().child_count, 0) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
