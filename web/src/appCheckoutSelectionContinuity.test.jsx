import React, { useLayoutEffect, useRef, useState } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'

afterEach(cleanup)

// Run App's actual identity effect through React rather than copying its law.
// This source-bound row does not claim a WebGL or live checkout reproduction.
const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
const nodes = []
function visit(node, parent) {
  if (!node || typeof node.type !== 'string') return
  nodes.push({ node, parent })
  for (const value of Object.values(node)) {
    if (Array.isArray(value)) value.forEach(child => visit(child, node))
    else if (value && typeof value.type === 'string') visit(value, node)
  }
}
visit(tree, null)
const text = node => source.slice(node.start, node.end)
const declaration = name => nodes.find(({ node }) =>
  node.type === 'VariableDeclarator' && node.id.name === name)?.parent
const effect = nodes.find(({ node }) => node.type === 'CallExpression'
  && node.callee.name === 'useLayoutEffect'
  && text(node.arguments[0]).includes('previousProjectDrawing.current'))?.node
if (!effect || !declaration('previousProjectDrawing')) throw new Error('App drawing identity effect missing')

const effectBody = [declaration('projectDrawingScopeKey'), declaration('previousProjectDrawing')]
  .filter(Boolean).map(text).join('\n') + '\n' + text(effect)
const runEffect = Function('useRef', 'useLayoutEffect', 'projectMode', 'projectDrawing',
  'projectSolar', 'setSelectedHandle', 'clearViewHistory', effectBody)

const binding = {
  kind: 'project', organization_id: '11111111-1111-4111-8111-111111111111',
  project_id: '22222222-2222-4222-8222-222222222222',
  drawing_id: '33333333-3333-4333-8333-333333333333',
  input_version_id: '44444444-4444-4444-8444-444444444444',
}
function Identity({ currentBinding = binding, ready = true, projectMode = true, clearViewHistory }) {
  const [selected, setSelectedHandle] = useState(null)
  runEffect(useRef, useLayoutEffect, projectMode,
    { drawingKey: ready ? 'project:' + ['organization_id', 'project_id', 'drawing_id', 'input_version_id']
      .map(field => currentBinding[field]).join(':') : null },
    { scope: { binding: currentBinding } }, setSelectedHandle, clearViewHistory)
  return <>
    <button onClick={() => setSelectedHandle('frame-a')}>Select frame</button>
    <output aria-label="Selected frame">{selected || 'none'}</output>
  </>
}

it('retains selection and view history through a same-version context refresh', () => {
  const clearViewHistory = vi.fn()
  const row = props => <Identity clearViewHistory={clearViewHistory} {...props} />
  const view = render(row({}))
  fireEvent.click(screen.getByRole('button', { name: 'Select frame' }))
  clearViewHistory.mockClear()
  view.rerender(row({ ready: false }))
  view.rerender(row({ currentBinding: { ...binding } }))
  expect(screen.getByLabelText('Selected frame')).toHaveTextContent('frame-a')
  expect(clearViewHistory).not.toHaveBeenCalled()
})

it.each(['organization_id', 'project_id', 'drawing_id', 'input_version_id'])(
  'clears selection for a real %s change even while context is pending', field => {
    const clearViewHistory = vi.fn()
    const view = render(<Identity clearViewHistory={clearViewHistory} />)
    fireEvent.click(screen.getByRole('button', { name: 'Select frame' }))
    clearViewHistory.mockClear()
    view.rerender(<Identity currentBinding={{ ...binding, [field]: '55555555-5555-4555-8555-555555555555' }}
      ready={false} clearViewHistory={clearViewHistory} />)
    expect(screen.getByLabelText('Selected frame')).toHaveTextContent('none')
    expect(clearViewHistory).toHaveBeenCalledTimes(1)
  })

it('clears selection when the canonical binding is retired', () => {
  const clearViewHistory = vi.fn()
  const view = render(<Identity clearViewHistory={clearViewHistory} />)
  fireEvent.click(screen.getByRole('button', { name: 'Select frame' }))
  view.rerender(<Identity currentBinding={null} ready={false} clearViewHistory={clearViewHistory} />)
  expect(screen.getByLabelText('Selected frame')).toHaveTextContent('none')
})
