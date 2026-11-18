const assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, mount, screen, waitFor, fireEvent, cleanup, dispose } = require('./x07_adapter_jsx_journey.cjs')
global.IS_REACT_ACT_ENVIRONMENT = true

function burst(form, button) {
  React.act(() => {
    for (let index = 0; index < 5; index++) {
      button.click()
      form.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
      // jsdom does not synthesize the browser's Enter default submission.
      form.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }))
    }
  })
}

async function main() {
  try {
    const Confirmation = component('X07RoleConfirmation')
    let selected = 0, release
    render(React.createElement(Confirmation, {
      proposal: { module: { candidates: [{ block: 'Panel', layer: 'MODULES', count: 3, design_window: [0, 0, 2, 0] }] } },
      onConfirm: () => { selected++; return new Promise(resolve => { release = resolve }) }
    }))
    burst(screen.getByRole('form', { name: 'Confirm drawing roles' }), screen.getByRole('button', { name: 'Confirm roles' }))
    await waitFor(() => assert.equal(selected, 1))
    await React.act(async () => release())
    await screen.findByText('Roles confirmed.')
    cleanup()

    const context = ctx('activation-burst')
    let confirmations = 0, saves = 0, fail = true
    mount(context, {
      confirmRoles: (scope, value, opts) => {
        confirmations++
        return rpc(context, 'confirm', scope, value, opts.signal)
      },
      saveProfile: (customer, roles, opts) => {
        saves++
        return rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version }, opts.signal, fail ? 'save' : null)
      }
    })
    const confirm = await screen.findByRole('button', { name: 'Confirm roles' }, { timeout: 15000 })
    burst(screen.getByRole('form', { name: 'Confirm drawing roles' }), confirm)
    await screen.findByRole('button', { name: 'Use copy 1' })
    const picks = [screen.getByRole('button', { name: 'Use copy 1' }), screen.getByRole('button', { name: 'Use copy 2' })]
    React.act(() => { for (let index = 0; index < 10; index++) picks[index % 2].click() })
    await screen.findByText('Drawing profile applied.', {}, { timeout: 15000 })
    await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
    assert.equal(confirmations, 1)
    const original = (await rpc(context, 'load', context)).application_record
    assert.equal(original.version, 1)
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'pAnEl' } })
    const save = screen.getByRole('button', { name: 'Save profile' })
    burst(save.closest('form'), save)
    await screen.findByRole('alert', {}, { timeout: 15000 })
    assert.equal(saves, 1)
    assert.equal(screen.getByLabelText('Module block').value, 'pAnEl')
    assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, 1)
    fail = false
    burst(save.closest('form'), save)
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    assert.equal(saves, 2)
    assert.equal(confirmations, 1)
    assert.deepEqual((await rpc(context, 'load', context)).application_record, original)
    console.log('PASS: batched click/Enter/submit and copy bursts call once per pending action; one confirmation pin, preserved failed draft and one explicit retry')
  } finally { await dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
