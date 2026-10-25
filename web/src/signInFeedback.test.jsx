// @vitest-environment jsdom
import React, { useEffect, useState } from 'react'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { humanizeError } from './errorHumanize.js'

function productionCallback(file, predicate) {
  const source = readFileSync(new URL(file, import.meta.url), 'utf8')
  const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
  const text = node => source.slice(node.start, node.end)
  function find(node) {
    if (!node || typeof node.type !== 'string') return null
    if (predicate(node, text)) return node
    for (const value of Object.values(node)) {
      for (const child of Array.isArray(value) ? value : [value]) {
        const result = find(child)
        if (result) return result
      }
    }
    return null
  }
  const node = find(tree)
  return text(node.type === 'VariableDeclarator' ? node.init.arguments[0] : node.arguments[0])
}
const loginBody = productionCallback('./App.jsx', node => node.type === 'VariableDeclarator' && node.id.name === 'onLogin')
const returnBody = productionCallback('./site/SiteRoot.jsx', (node, text) => node.type === 'CallExpression'
  && node.callee.name === 'useEffect' && text(node.arguments[0]).includes('handleRedirectCallback()'))
afterEach(() => { cleanup(); vi.useRealTimers() })

it('shows feedback when starting sign-in fails', async () => {
  const login = vi.fn().mockRejectedValue(new Error('Failed to fetch'))
  function Harness() {
    const [message, setMessage] = useState('')
    const onLogin = Function('projectModeRef', 'checkout', 'login', 'showToast', 'humanizeError',
      `return (${loginBody})`)({ current: false }, { actions: { getCapability: () => null } },
      login, ({ text }) => setMessage(text), humanizeError)
    return <><button onClick={onLogin}>Sign in</button><p role="alert">{message}</p></>
  }
  render(<Harness />)
  await act(async () => { fireEvent.click(screen.getByRole('button')) })
  expect(screen.getByRole('alert').textContent).toBe("Sign-in couldn't start. Check your connection and try again.")
})

function CallbackHarness({ callback, onReload }) {
  const [pending, setAuthCallbackPending] = useState(true)
  const [message, setAuthError] = useState('')
  useEffect(() => Function('authCallbackPending', 'handleRedirectCallback', 'setAuthCallbackPending', 'setAuthError', 'humanizeError', 'window',
    `return (${returnBody})()`)(pending, callback, setAuthCallbackPending, setAuthError, humanizeError,
    { location: { reload: onReload } }), [pending, callback, onReload])
  return message ? <p role="alert">{message}</p> : pending ? <p>Completing sign in</p> : null
}

it('clears pending feedback when the callback rejects', async () => {
  render(<CallbackHarness callback={() => Promise.reject(new Error('SDK load failed'))} />)
  expect((await screen.findByRole('alert')).textContent).toBe("Sign-in couldn't be completed.")
  expect(screen.queryByText('Completing sign in')).toBeNull()
})

it('stops waiting after twenty seconds even if the callback never settles', async () => {
  vi.useFakeTimers()
  render(<CallbackHarness callback={() => new Promise(() => {})} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(20000) })
  expect(screen.getByRole('alert').textContent).toBe("Sign-in couldn't be completed.")
  expect(screen.queryByText('Completing sign in')).toBeNull()
})

it('clears timeout feedback and reloads when sign-in succeeds after twenty seconds', async () => {
  vi.useFakeTimers()
  let complete
  const callback = vi.fn(() => new Promise(resolve => { complete = resolve }))
  const onReload = vi.fn()
  render(<CallbackHarness callback={callback} onReload={onReload} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(20000) })
  expect(screen.getByRole('alert').textContent).toBe("Sign-in couldn't be completed.")
  expect(onReload).not.toHaveBeenCalled()
  await act(async () => { complete(true) })
  expect(screen.queryByRole('alert')).toBeNull()
  expect(onReload).toHaveBeenCalledTimes(1)
  expect(callback).toHaveBeenCalledTimes(1)
})

it('ignores callback completion after unmount', async () => {
  vi.useFakeTimers()
  let complete
  const callback = vi.fn(() => new Promise(resolve => { complete = resolve }))
  const onReload = vi.fn()
  const { unmount } = render(<CallbackHarness callback={callback} onReload={onReload} />)
  await act(async () => {})
  unmount()
  await act(async () => { complete(true); await vi.advanceTimersByTimeAsync(20000) })
  expect(onReload).not.toHaveBeenCalled()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(vi.getTimerCount()).toBe(0)
})
