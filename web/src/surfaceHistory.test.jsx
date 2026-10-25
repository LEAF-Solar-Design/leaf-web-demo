// @vitest-environment jsdom
import React, { useEffect, useState } from 'react'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { productSurfaceFromSearch, searchForProductSurface } from './site/productSurfaces.js'

// Execute the production callbacks without mounting the drawing renderer.
const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
function find(predicate, node = tree) {
  const stack = [node]
  const visited = new WeakSet()
  const skippedKeys = new Set(['loc', 'start', 'end', 'extra', 'leadingComments', 'trailingComments', 'innerComments', 'comments'])
  while (stack.length) {
    const current = stack.pop()
    if (!current || typeof current !== 'object' || typeof current.type !== 'string' || visited.has(current)) continue
    visited.add(current)
    if (predicate(current)) return current
    const children = []
    for (const [key, value] of Object.entries(current)) {
      if (skippedKeys.has(key)) continue
      for (const child of Array.isArray(value) ? value : [value]) {
        if (child && typeof child === 'object' && typeof child.type === 'string') children.push(child)
      }
    }
    for (let index = children.length - 1; index >= 0; index--) stack.push(children[index])
  }
  return null
}
const text = node => source.slice(node.start, node.end)
const commit = find(node => node.type === 'VariableDeclarator' && node.id.name === 'onCommit').init.arguments[0]
const restore = find(node => node.type === 'CallExpression' && node.callee.name === 'useEffect'
  && text(node.arguments[0]).includes("addEventListener('popstate', restoreSurface)"))
function Harness() {
  const [surface, setActiveSurface] = useState('cad')
  const onCommit = Function('setActiveSurface', 'searchForProductSurface', `return (${text(commit)})`)(setActiveSurface, searchForProductSurface)
  useEffect(() => {
    expect(restore).not.toBeNull()
    return Function('setActiveSurface', 'productSurfaceFromSearch', 'labStatus', 'settle',
      `return (${text(restore.arguments[0])})()`)(setActiveSurface, productSurfaceFromSearch, undefined, () => {})
  }, [])
  return <><output>{surface}</output><button onClick={() => onCommit('solar')}>Solar</button></>
}
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('pushes a product switch and restores an earlier surface including its drawer entry', () => {
  window.history.replaceState(null, '', '/app?surface=cad&drawer=details')
  const push = vi.spyOn(window.history, 'pushState')
  render(<Harness />)
  fireEvent.click(screen.getByText('Solar'))
  expect(push).toHaveBeenCalledTimes(1)
  expect(window.location.search).toContain('surface=solar')
  expect(screen.getByText('solar')).toBeTruthy()
  act(() => {
    window.history.replaceState(null, '', '/app?surface=cad&drawer=details')
    window.dispatchEvent(new PopStateEvent('popstate'))
  })
  expect(screen.getByText('cad')).toBeTruthy()
})

it('ignores an unknown surface on popstate', () => {
  window.history.replaceState(null, '', '/app?surface=cad')
  render(<Harness />)
  fireEvent.click(screen.getByText('Solar'))
  act(() => {
    window.history.replaceState(null, '', '/app?surface=unknown')
    window.dispatchEvent(new PopStateEvent('popstate'))
  })
  expect(screen.getByText('solar')).toBeTruthy()
})
