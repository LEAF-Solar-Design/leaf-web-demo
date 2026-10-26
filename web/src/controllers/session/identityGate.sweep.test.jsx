import React, { useEffect, useMemo, useState } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { humanizeError } from '../../errorHumanize.js'
import { getSessionIdentity } from '../../api.js'

// Execute the production effect and recovery JSX without mounting WebGL.
const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
function find(predicate, root = tree) {
  const result = []
  function walk(node) {
    if (!node || typeof node.type !== 'string') return
    if (predicate(node)) result.push(node)
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(walk)
      else if (value?.type) walk(value)
    }
  }
  walk(root)
  return result
}
const text = node => source.slice(node.start, node.end)
const effect = find(node => node.type === 'CallExpression' && node.callee.name === 'useEffect'
  && find(child => child.type === 'CallExpression' && child.callee.name === 'getSessionIdentity', node).length)[0]
const gate = find(node => node.type === 'JSXExpressionContainer'
  && node.expression.type === 'LogicalExpression' && text(node).startsWith('{!mock && identityFailure'))[0]
function evaluate(node, env) {
  if (node.type === 'JSXExpressionContainer') return evaluate(node.expression, env)
  if (node.type === 'LogicalExpression' && node.operator === '&&') return evaluate(node.left, env) && evaluate(node.right, env)
  if (node.type === 'JSXText') return node.value.trim()
  if (node.type === 'JSXElement') {
    const props = {}
    for (const attr of node.openingElement.attributes) {
      props[attr.name.name] = attr.value.type === 'StringLiteral'
        ? attr.value.value : evaluate(attr.value.expression, env)
    }
    return React.createElement(node.openingElement.name.name, props, ...node.children.map(child => evaluate(child, env)))
  }
  return Function(...Object.keys(env), `return (${text(node)})`)(...Object.values(env))
}
const noop = () => {}
function Harness({ request, login = noop, demo = noop }) {
  const [identityFailure, setIdentityFailure] = useState(null)
  const [intakeRetryKey, setIntakeRetryKey] = useState(0)
  const [status, setStatus] = useState('checking')
  const [mock, setMock] = useState(false)
  const sessionProofScope = useMemo(() => ({}), [intakeRetryKey])
  const sessionProof = useMemo(() => ({ current: () => true, activate: () => setStatus('active') }), [sessionProofScope])
  const controller = useMemo(() => ({ getSnapshot: () => ({ status: 'checking' }) }), [])
  const sessionActions = useMemo(() => ({ checking: noop, requireAuth: noop }), [])
  const env = {
    useEffect, mock, sessionProof, sessionProofScope, sessionActions, intakeRetryKey,
    session: { status, recoveries: 0, controller }, setIdentityFailure,
    setTenant: noop, setTier: noop, setOrg: noop, getSessionIdentity: request,
    is401: error => error?.status === 401, setLoadErr: noop, humanizeError,
    shouldAutoDemo: () => false, authConfigured: true, isSignedIn: () => false,
    identityFailure, setIntakeRetryKey, onLogin: login,
    setMock: value => { demo(value); setMock(value) },
  }
  Function(...Object.keys(env), text(effect))(...Object.values(env))
  return gate ? evaluate(gate, env) : null
}
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals() })

it('shows safe recovery actions after a service failure and Retry starts a fresh identity read', async () => {
  const request = vi.fn().mockRejectedValueOnce(new Error('GET /api/session/identity -> 503'))
    .mockResolvedValueOnce({ tenant: 'workspace' })
  const login = vi.fn()
  render(<Harness request={request} login={login} />)
  expect(await screen.findByRole('alert')).toHaveTextContent(humanizeError('GET /api/session/identity -> 503'))
  expect(screen.queryByText(/503|\/api\/|GET/)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
  expect(login).toHaveBeenCalledOnce()
  expect(screen.getByRole('button', { name: 'Explore the demo' })).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2))
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
})

it('offers the demo after a timed-out identity read instead of leaving the visitor waiting', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('fetch', vi.fn((url, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new DOMException('The request was aborted', 'AbortError')), { once: true })
  })))
  const request = vi.fn(() => getSessionIdentity(false))
  const demo = vi.fn()
  render(<Harness request={request} demo={demo} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(screen.getByRole('alert')).toHaveTextContent(humanizeError('The service did not respond within 5000 ms.'))
  expect(screen.getByRole('button', { name: 'Retry' })).toBeVisible()
  expect(screen.getByRole('button', { name: 'Sign in' })).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: 'Explore the demo' }))
  expect(demo).toHaveBeenCalledWith(true)
  expect(screen.queryByRole('alert')).toBeNull()
})
