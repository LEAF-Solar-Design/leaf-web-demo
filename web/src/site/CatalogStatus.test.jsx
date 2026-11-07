// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import CatalogStatus from './CatalogStatus.jsx'

afterEach(cleanup)

describe('CatalogStatus', () => {
  it('renders nothing when ready', () => {
    const { container } = render(<CatalogStatus state="ready" />)
    expect(container.innerHTML).toBe('')
  })

  it('announces loading while hiding three content-shaped skeleton rows', () => {
    render(<CatalogStatus state="loading" />)
    const status = screen.getByRole('status')
    expect(screen.getAllByRole('status')).toHaveLength(1)
    expect(status.getAttribute('aria-live')).toBe('polite')
    expect(status.classList.contains('catalog-status')).toBe(true)
    expect(status.textContent).toBe('Loading tools')
    expect(status.querySelector('.sr-only').style.position).toBe('absolute')
    expect(status.style.getPropertyValue('--skeleton-h')).toBe('46px')
    const rows = status.querySelectorAll('.skeleton-row')
    expect(rows).toHaveLength(3)
    rows.forEach((row) => expect(row.getAttribute('aria-hidden')).toBe('true'))
    expect(status.getAttribute('aria-hidden')).toBeNull()
  })

  it('explains a loaded empty catalog without offering a retry', () => {
    render(<CatalogStatus state="empty" />)
    const message = screen.getByText('No tools yet. Add a tool to get started.')
    expect(message.tagName).toBe('P')
    expect(message.className).toBe('catalog-status muted')
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByRole('status')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('names the unmatched query and clears the filter once', () => {
    const onClear = vi.fn()
    render(<CatalogStatus state="no-match" query="no-such-tool" onClear={onClear} />)
    const message = screen.getByText('No tools match "no-such-tool".')
    expect(message.className).toBe('catalog-status muted')
    const clear = screen.getByRole('button', { name: 'Clear filter' })
    expect(clear.type).toBe('button')
    expect(clear.className).toBe('chip-neutral dp-focus')
    fireEvent.click(clear)
    expect(onClear).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('omits the clear action when no handler is available', () => {
    render(<CatalogStatus state="no-match" query="no-such-tool" />)
    expect(screen.getByText('No tools match "no-such-tool".')).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('explains an unmatched surface without a query or clear action', () => {
    // Option A: design-parity-forks-20261101/receipts/operator-forks-all-a-20261101.json
    const onClear = vi.fn()
    render(<CatalogStatus state="no-match" query="" onClear={onClear} />)
    expect(screen.getByText('No tools for this surface yet.').className).toBe('catalog-status muted')
    expect(screen.queryByRole('button')).toBeNull()
    expect(onClear).not.toHaveBeenCalled()
  })

  it('alerts on failure and retries once', () => {
    const onRetry = vi.fn()
    render(<CatalogStatus state="failed" onRetry={onRetry} />)
    const alert = screen.getByRole('alert')
    expect(alert.className).toBe('catalog-status')
    expect(alert.querySelector('p').textContent).toBe('The tool list did not load. Try again.')
    const retry = screen.getByRole('button', { name: 'Retry' })
    expect(alert.contains(retry)).toBe(true)
    expect(retry.type).toBe('button')
    expect(retry.className).toBe('chip-act dp-focus')
    fireEvent.click(retry)
    expect(onRetry).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('status')).toBeNull()
  })
})
