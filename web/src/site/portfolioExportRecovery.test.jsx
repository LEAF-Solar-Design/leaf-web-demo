import * as React from 'react'
import * as jsxRuntime from 'react/jsx-runtime'
import { readFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'

// Execute the actual exporter literal, not a copied implementation. Styling is
// inert here; the independently built artifact supplies browser geometry proof.
const template = readFileSync(resolve(process.cwd(), '../platform/portfolio_page_template.py'), 'utf8')
const source = template.match(/'app\/Gallery\.jsx': b'''([\s\S]*?)'''/)[1]
const module = { exports: {} }
const design = Object.fromEntries(['photoStyle', 'photoSpanStyle', 'textStyle', 'galleryStyle'].map((key) => [key, () => ({})]))
// Compile outside jsdom's typed-array realm, then render using the test's React.
const compiled = execFileSync(process.execPath, ['-e',
  "const e=require(process.argv[1]);const s=require('node:fs').readFileSync(0,'utf8');process.stdout.write(e.transformSync(s,{loader:'jsx',format:'cjs',jsx:'automatic'}).code)",
  createRequire(resolve(process.cwd(), 'package.json')).resolve('esbuild')], { input: source, encoding: 'utf8', maxBuffer: 1024 * 1024 })
new Function('require', 'module', 'exports', compiled)(
  (name) => name === 'react' ? React : name === 'react/jsx-runtime' ? jsxRuntime : design, module, module.exports)
const Gallery = module.exports.default
const photo = (id, title) => ({ material_id: id, alt: title, title })
const first = photo('11111111-1111-4111-8111-111111111111', 'Synthetic cake')
const second = photo('22222222-2222-4222-8222-222222222222', 'Synthetic bread')
const category = (image, slug = 'cakes') => ({ slug, images: [image] })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('actual exported portfolio image failure recovery', () => {
  it('detects an image that failed before hydration attached its error handler', () => {
    vi.spyOn(HTMLImageElement.prototype, 'complete', 'get').mockReturnValue(true)
    vi.spyOn(HTMLImageElement.prototype, 'naturalWidth', 'get').mockReturnValue(0)
    render(<Gallery page={{}} category={category(first)} />)
    expect(screen.getByRole('img', { name: 'Photograph unavailable: Synthetic cake' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Retry photo' })).toBeTruthy()
  })

  it('shows a stable failure and retries the same material without nested buttons', () => {
    render(<Gallery page={{}} category={category(first)} />)
    const trigger = screen.getByRole('button', { name: 'View Synthetic cake' })
    const old = screen.getByRole('img', { name: first.alt })
    fireEvent.error(old)
    expect(screen.getByRole('img', { name: 'Photograph unavailable: Synthetic cake' })).toBeTruthy()
    const retry = screen.getByRole('button', { name: 'Retry photo' })
    expect(retry.closest('button button')).toBeNull()
    fireEvent.click(retry)
    const image = screen.getByRole('img', { name: first.alt })
    expect(image.getAttribute('src')).toBe(`/images/${first.material_id}.jpg?retry=1`)
    expect(document.activeElement).toBe(trigger)
    fireEvent.error(old)
    fireEvent.load(image)
    expect(screen.queryByRole('button', { name: 'Retry photo' })).toBeNull()
    expect(screen.getByText(first.title)).toBeTruthy()
  })

  it('keeps viewer Retry reachable with Tab and preserves Escape focus return', () => {
    render(<Gallery page={{}} category={category(first)} />)
    const trigger = screen.getByRole('button', { name: 'View Synthetic cake' })
    fireEvent.click(trigger)
    const dialog = screen.getByRole('dialog')
    fireEvent.error(within(dialog).getByRole('img', { name: first.alt }))
    const close = within(dialog).getByRole('button', { name: 'Close image' })
    const retry = within(dialog).getByRole('button', { name: 'Retry photo' })
    close.focus(); fireEvent.keyDown(window, { key: 'Tab' })
    expect(document.activeElement).toBe(retry)
    fireEvent.keyDown(window, { key: 'Tab' })
    expect(document.activeElement).toBe(close)
    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(retry)
    fireEvent.click(retry)
    expect(document.activeElement).toBe(close)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('retires the previous category viewer and its failed image state', () => {
    const view = render(<Gallery page={{}} category={category(first)} />)
    fireEvent.click(screen.getByRole('button', { name: 'View Synthetic cake' }))
    const old = within(screen.getByRole('dialog')).getByRole('img', { name: first.alt })
    fireEvent.error(old)
    view.rerender(<Gallery page={{}} category={category(second, 'breads')} />)
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.error(old)
    expect(screen.queryByRole('button', { name: 'Retry photo' })).toBeNull()
    expect(screen.getByRole('img', { name: second.alt }).getAttribute('src')).toContain(second.material_id)
  })
})
