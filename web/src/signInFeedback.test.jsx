// @vitest-environment jsdom
import React, { useEffect, useState } from 'react'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { humanizeError } from './errorHumanize.js'
import { useAsyncScope } from './lib/urlState.js'

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
  return text(node.type === 'VariableDeclarator' ? node.init.arguments[0]
    : node.type === 'ArrowFunctionExpression' ? node : node.arguments[0])
}
const loginBody = productionCallback('./App.jsx', node => node.type === 'VariableDeclarator' && node.id.name === 'onLogin')
const returnBody = productionCallback('./site/SiteRoot.jsx', (node, text) => node.type === 'CallExpression'
  && node.callee.name === 'useEffect' && text(node.arguments[0]).includes('handleRedirectCallback('))
const appReturnBody = productionCallback('./App.jsx', (node, text) => node.type === 'CallExpression'
  && node.callee.name === 'useEffect' && text(node.arguments[0]).includes('handleRedirectCallback('))
const retryBody = productionCallback('./site/SiteRoot.jsx', (node, text) => node.type === 'ArrowFunctionExpression'
  && node.async && text(node).includes("url.searchParams.delete('code')"))
afterEach(() => { cleanup(); vi.useRealTimers() })

it('shows feedback when starting sign-in fails', async () => {
  const login = vi.fn().mockRejectedValue(new Error('Failed to fetch'))
  function Harness() {
    const [message, setMessage] = useState('')
    const beginSignIn = useAsyncScope([])
    const onLogin = Function('projectModeRef', 'checkout', 'login', 'showToast', 'humanizeError', 'beginSignIn',
      `return (${loginBody})`)({ current: false }, { actions: { getCapability: () => null } },
      login, ({ text }) => setMessage(text), humanizeError, beginSignIn)
    return <><button onClick={onLogin}>Sign in</button><p role="alert">{message}</p></>
  }
  render(<Harness />)
  await act(async () => { fireEvent.click(screen.getByRole('button')) })
  expect(screen.getByRole('alert').textContent).toBe("Sign-in couldn't start. Check your connection and try again.")
})

function CallbackHarness({ callback, onReload, scope = 'first' }) {
  const [pending, setAuthCallbackPending] = useState(true)
  const [message, setAuthError] = useState('')
  const beginSignIn = useAsyncScope([scope])
  useEffect(() => Function('authCallbackPending', 'handleRedirectCallback', 'setAuthCallbackPending', 'setAuthError', 'humanizeError', 'window', 'beginSignIn',
    `return (${returnBody})()`)(pending, callback, setAuthCallbackPending, setAuthError, humanizeError,
    { location: { reload: onReload } }, beginSignIn), [pending, callback, onReload, beginSignIn])
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

it('keeps timeout feedback and ignores sign-in success after twenty seconds', async () => {
  vi.useFakeTimers()
  let complete
  const callback = vi.fn(() => new Promise(resolve => { complete = resolve }))
  const onReload = vi.fn()
  render(<CallbackHarness callback={callback} onReload={onReload} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(20000) })
  expect(screen.getByRole('alert').textContent).toBe("Sign-in couldn't be completed.")
  expect(onReload).not.toHaveBeenCalled()
  await act(async () => { complete(true) })
  expect(screen.getByRole('alert').textContent).toBe("Sign-in couldn't be completed.")
  expect(onReload).not.toHaveBeenCalled()
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

it('ignores the site callback success and timeout after navigation changes its scope', async () => {
  vi.useFakeTimers()
  let complete
  const callback = () => new Promise(resolve => { complete = resolve })
  const onReload = vi.fn()
  render(<CallbackHarness callback={callback} onReload={onReload} />)
  await act(async () => {})
  act(() => window.dispatchEvent(new PopStateEvent('popstate')))
  await act(async () => { complete(true); await vi.advanceTimersByTimeAsync(20000) })
  expect(onReload).not.toHaveBeenCalled()
  expect(screen.queryByRole('alert')).toBeNull()
})

it.each(['success', 'failure', 'timeout'])('retires the App callback %s after its drawing scope changes', async outcome => {
  vi.useFakeTimers()
  let complete, reject
  const callback = () => new Promise((resolve, fail) => { complete = resolve; reject = fail })
  const reload = vi.fn(), showToast = vi.fn()
  function Harness({ drawing }) {
    const beginSignIn = useAsyncScope([drawing])
    useEffect(() => Function('mock', 'isAuthRedirectCallback', 'handleRedirectCallback', 'showToast', 'humanizeError', 'onLogin', 'window', 'beginSignIn',
      `return (${appReturnBody})()`)(false, () => true, callback, showToast, humanizeError, vi.fn(), { location: { reload } }, beginSignIn), [])
    return null
  }
  const view = render(<Harness drawing="first" />)
  await act(async () => {})
  view.rerender(<Harness drawing="second" />)
  await act(async () => {
    if (outcome === 'success') complete(true)
    if (outcome === 'failure') reject(new Error('late failure'))
    await vi.advanceTimersByTimeAsync(20000)
  })
  expect(showToast).not.toHaveBeenCalled()
  expect(reload).not.toHaveBeenCalled()
})

function LoginHarness({ scope = 'first', release, login, showToast, retry = false }) {
  const beginSignIn = useAsyncScope([scope])
  const onClick = retry
    ? Function('beginSignIn', 'login', 'setAuthError', 'humanizeError', `return (${retryBody})`)(beginSignIn, login, showToast, humanizeError)
    : Function('projectModeRef', 'checkout', 'login', 'showToast', 'humanizeError', 'beginSignIn', `return (${loginBody})`)(
      { current: false }, { actions: { getCapability: () => true, release } }, login, showToast, humanizeError, beginSignIn)
  return <button onClick={onClick}>Sign in</button>
}

it.each(['resolve', 'reject'])('ignores checkout release %s after changing project', async outcome => {
  let complete, reject
  const release = () => new Promise((resolve, fail) => { complete = resolve; reject = fail })
  const login = vi.fn(), showToast = vi.fn()
  const view = render(<LoginHarness release={release} login={login} showToast={showToast} />)
  fireEvent.click(screen.getByRole('button'))
  view.rerender(<LoginHarness scope="second" release={release} login={login} showToast={showToast} />)
  await act(async () => { if (outcome === 'resolve') complete(); else reject(new Error('late failure')) })
  expect(login).not.toHaveBeenCalled()
  expect(showToast).not.toHaveBeenCalled()
})

it.each([false, true])('ignores stale login feedback after a newer request, navigation, or unmount (retry=%s)', async retry => {
  for (const move of ['new request', 'navigation', 'unmount']) {
    const failures = []
    const login = vi.fn(() => new Promise((resolve, reject) => { failures.push(reject) }))
    const showToast = vi.fn()
    const view = render(<LoginHarness retry={retry} release={async () => {}} login={login} showToast={showToast} />)
    await act(async () => { fireEvent.click(screen.getByRole('button')) })
    if (move === 'new request') await act(async () => { fireEvent.click(screen.getByRole('button')) })
    if (move === 'navigation') act(() => window.dispatchEvent(new Event('leaf:navigate')))
    if (move === 'unmount') view.unmount()
    await act(async () => { failures[0](new Error('late failure')) })
    expect(showToast).not.toHaveBeenCalled()
    view.unmount()
  }
})
