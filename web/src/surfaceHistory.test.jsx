// @vitest-environment jsdom
import React, { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { productSurfaceFromSearch, searchForProductSurface } from './site/productSurfaces.js'
import { useStudioTransition } from './site/useStudioTransition.js'

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
const retireScope = find(node => node.type === 'CallExpression' && node.callee.name === 'useLayoutEffect'
  && text(node.arguments[0]).includes('queued product-tab switch'))
function Harness({ animated = false, project = 'a' }) {
  const [surface, setActiveSurface] = useState('cad')
  const committedSurfaceRef = useRef(surface)
  committedSurfaceRef.current = surface
  const onCommit = Function('setActiveSurface', 'searchForProductSurface', 'committedSurfaceRef', `return (${text(commit)})`)(setActiveSurface, searchForProductSurface, committedSurfaceRef)
  const { request } = useStudioTransition({ committed: surface, onCommit,
    isDrafting: id => id === 'cad' || id === 'solar', reducedMotion: !animated })
  useLayoutEffect(() => {
    Function('request', 'committedSurfaceRef', `return (${text(retireScope.arguments[0])})()`)(request, committedSurfaceRef)
  }, [project, request])
  useEffect(() => {
    expect(restore).not.toBeNull()
    return Function('setActiveSurface', 'productSurfaceFromSearch', 'labStatus', 'request', 'committedSurfaceRef',
      `return (${text(restore.arguments[0])})()`)(setActiveSurface, productSurfaceFromSearch, undefined, request, committedSurfaceRef)
  }, [request])
  return <><output>{surface}</output><button onClick={() => request('solar')}>Solar</button><button onClick={() => request('browser')}>Browser</button></>
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

it('cancels a pending tab switch on Back without pushing or committing the queued tab', () => {
  vi.useFakeTimers()
  window.history.replaceState(null, '', '/app?surface=cad')
  const push = vi.spyOn(window.history, 'pushState')
  render(<Harness animated />)
  fireEvent.click(screen.getByText('Browser'))
  act(() => {
    window.history.replaceState(null, '', '/app?surface=solar&drawer=details')
    window.dispatchEvent(new PopStateEvent('popstate'))
    vi.advanceTimersByTime(1000)
  })
  expect(push).not.toHaveBeenCalled()
  expect(screen.getByText('solar')).toBeTruthy()
  expect(window.location.search).toBe('?surface=solar&drawer=details')
  expect(vi.getTimerCount()).toBe(0)
})

it('cancels a superseded surface timer and retires it on unmount', () => {
  vi.useFakeTimers()
  const push = vi.spyOn(window.history, 'pushState')
  const view = render(<Harness animated />)
  fireEvent.click(screen.getByText('Browser'))
  fireEvent.click(screen.getByText('Solar'))
  expect(push).toHaveBeenCalledTimes(1)
  act(() => vi.advanceTimersByTime(1000))
  expect(screen.getByText('solar')).toBeTruthy()
  expect(push).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByText('Browser'))
  view.unmount()
  act(() => vi.advanceTimersByTime(1000))
  expect(push).toHaveBeenCalledTimes(1)
})

it('cancels the surface timer on project change and in-app navigation', () => {
  vi.useFakeTimers()
  const push = vi.spyOn(window.history, 'pushState')
  const view = render(<Harness animated />)
  fireEvent.click(screen.getByText('Browser'))
  view.rerender(<Harness animated project="b" />)
  act(() => vi.advanceTimersByTime(1000))
  expect(push).not.toHaveBeenCalled()
  fireEvent.click(screen.getByText('Browser'))
  act(() => {
    window.dispatchEvent(new Event('leaf:navigate'))
    vi.advanceTimersByTime(1000)
  })
  expect(push).not.toHaveBeenCalled()
  expect(screen.getByText('cad')).toBeTruthy()
})

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
