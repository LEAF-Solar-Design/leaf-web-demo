// @vitest-environment jsdom
import childProcess from 'node:child_process'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import CodeTab from './CodeTab.jsx'
import { manifests } from '../canvas/registry.js'
const css = readFileSync(resolve(process.cwd(), 'src/lab/code/codeParity.css'), 'utf8')

const manifest = manifests.find(item => item.id === 'adjust-none')
const block = { id: 'adjust-none-parity', settings: {} }
const mount = () => render(<CodeTab block={block} manifest={manifest} />)
const buttonFor = label => screen.getByRole('button', { name: `Copy ${label}`, exact: true })
const codeFor = button => button.closest('.dp-code-block').querySelector('code')
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
let writeText
let fetchSpy

beforeEach(() => {
  vi.useFakeTimers()
  writeText = vi.fn().mockResolvedValue(undefined)
  fetchSpy = vi.fn(() => { throw new Error('Network forbidden') })
  vi.stubGlobal('navigator', { clipboard: { writeText } })
  vi.stubGlobal('fetch', fetchSpy)
})

afterEach(() => {
  cleanup()
  window.getSelection?.()?.removeAllRanges()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('CodeTab code block parity', () => {
  it('shows Copied only after resolution and resets after 2000ms', async () => {
    const pending = deferred()
    writeText.mockReturnValue(pending.promise)
    mount()
    const button = buttonFor('export-contract')
    expect(button.textContent).toBe('Copy')
    expect(button).toHaveClass('dp-focus')
    expect(button).toHaveAttribute('data-dp-action')
    expect(button.closest('.dp-code-header')).toHaveTextContent('Command')
    fireEvent.click(button)
    expect(writeText).toHaveBeenCalledExactlyOnceWith(codeFor(button).textContent)
    expect(button.textContent).toBe('Copy')
    act(() => vi.advanceTimersByTime(2000))
    expect(button.textContent).toBe('Copy')
    await act(async () => { pending.resolve() })
    expect(button.textContent).toBe('Copied')
    act(() => vi.advanceTimersByTime(1999))
    expect(button.textContent).toBe('Copied')
    act(() => vi.advanceTimersByTime(1))
    expect(button.textContent).toBe('Copy')
  })

  it('reports failure and selects exactly the displayed code without claiming success', async () => {
    const pending = deferred()
    writeText.mockReturnValue(pending.promise)
    mount()
    const button = buttonFor('export-contract')
    fireEvent.click(button)
    expect(button.textContent).toBe('Copy')
    await act(async () => { pending.reject(new Error('denied')) })
    expect(button.textContent).toBe('Copy failed')
    expect(screen.queryByText('Copied')).toBeNull()
    expect(window.getSelection().toString()).toBe(codeFor(button).textContent)
    act(() => vi.advanceTimersByTime(2000))
    expect(button.textContent).toBe('Copy')
  })

  it('offers failure feedback when the clipboard is unavailable and there is no selection', async () => {
    vi.stubGlobal('navigator', {})
    vi.spyOn(window, 'getSelection').mockReturnValue(null)
    mount()
    const button = buttonFor('export-contract')
    await act(async () => { fireEvent.click(button) })
    expect(button.textContent).toBe('Copy failed')
    expect(screen.queryByText('Copied')).toBeNull()
  })

  it('ignores a pending copy after the displayed text changes', async () => {
    const pending = deferred()
    writeText.mockReturnValue(pending.promise)
    mount()
    const button = buttonFor('Ask Claude prompt')
    const copiedText = codeFor(button).textContent
    fireEvent.click(button)
    fireEvent.change(screen.getByLabelText('Ask Claude'), { target: { value: 'Text B' } })
    expect(codeFor(button).textContent).not.toBe(copiedText)
    await act(async () => { pending.resolve() })
    expect(writeText).toHaveBeenCalledExactlyOnceWith(copiedText)
    expect(button.textContent).toBe('Copy')
    expect(screen.queryByText('Copied')).toBeNull()
  })

  it('invalidates an old request even if the text changes back to its original value', async () => {
    const pending = deferred()
    writeText.mockReturnValue(pending.promise)
    mount()
    const prompt = screen.getByLabelText('Ask Claude')
    const original = prompt.value
    const button = buttonFor('Ask Claude prompt')
    fireEvent.click(button)
    fireEvent.change(prompt, { target: { value: 'Text B' } })
    fireEvent.change(prompt, { target: { value: original } })
    await act(async () => { pending.resolve() })
    expect(button.textContent).toBe('Copy')
  })

  it('ignores stale failures without selecting the newer text', async () => {
    const pending = deferred()
    writeText.mockReturnValue(pending.promise)
    mount()
    const button = buttonFor('Ask Claude prompt')
    fireEvent.click(button)
    fireEvent.change(screen.getByLabelText('Ask Claude'), { target: { value: 'Text B' } })
    await act(async () => { pending.reject(new Error('denied')) })
    expect(button.textContent).toBe('Copy')
    expect(window.getSelection().toString()).toBe('')
  })

  it('keeps feedback for the latest click when earlier requests finish out of order', async () => {
    const first = deferred()
    const second = deferred()
    writeText.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    mount()
    const button = buttonFor('export-contract')
    fireEvent.click(button)
    fireEvent.click(button)
    await act(async () => { second.resolve() })
    expect(button.textContent).toBe('Copied')
    await act(async () => { first.reject(new Error('late denial')) })
    expect(button.textContent).toBe('Copied')
    expect(window.getSelection().toString()).toBe('')
    act(() => vi.advanceTimersByTime(2000))
    expect(button.textContent).toBe('Copy')
  })

  it('cancels feedback timers and ignores pending requests after unmount', async () => {
    const pending = deferred()
    writeText.mockReturnValueOnce(pending.promise)
    const view = mount()
    fireEvent.click(buttonFor('Ask Claude prompt'))
    await act(async () => { fireEvent.click(buttonFor('export-contract')) })
    expect(vi.getTimerCount()).toBe(1)
    view.unmount()
    expect(vi.getTimerCount()).toBe(0)
    await act(async () => { pending.resolve() })
    expect(vi.getTimerCount()).toBe(0)
  })

  it('copies locally without executing a command or making a network request', async () => {
    const exec = vi.spyOn(childProcess, 'exec').mockImplementation(() => { throw new Error('Execution forbidden') })
    const spawn = vi.spyOn(childProcess, 'spawn').mockImplementation(() => { throw new Error('Execution forbidden') })
    mount()
    const buttons = screen.getAllByRole('button', { name: /^Copy / })
    await act(async () => { buttons.forEach(button => fireEvent.click(button)) })
    expect(writeText).toHaveBeenCalledTimes(buttons.length)
    expect(exec).not.toHaveBeenCalled()
    expect(spawn).not.toHaveBeenCalled()
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('wraps every code body with a header and preserves the typography and scrolling rules', () => {
    const { container } = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Settings only', exact: true }))
    for (const pre of container.querySelectorAll('pre')) {
      expect(pre.parentElement).toHaveClass('dp-code-block')
      expect(pre.previousElementSibling).toHaveClass('dp-code-header')
      expect(pre.querySelector('code')).not.toBeNull()
      expect(pre.previousElementSibling.querySelector('button').textContent).toBe('Copy')
    }
    expect(css).toContain('min-height:32px')
    expect(css).toContain('font-family:var(--leaf-font-data)')
    expect(css).toContain('font-size:var(--leaf-size-dense)')
    expect(css).toContain('line-height:var(--leaf-leading-code)')
    expect(css).toContain('font-variant-ligatures:none')
    expect(css).toContain('tab-size:2')
    expect(css).toContain('overflow-x:auto')
    expect(css).toContain('white-space:pre')
    expect(css).toContain('min-width:0')
  })
})
