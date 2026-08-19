/**
 * Card C1-2: truthful create-or-upload entry flow.
 *
 * Every assertion here names the lie it prevents: an affordance that renders
 * but has no executable backing route, or an entry surface that shows up
 * while its feature flag is off.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import CadEntry from './CadEntry.jsx'

afterEach(cleanup)

describe('cad_upload fence', () => {
  it('renders nothing when the flag is off', () => {
    const { container } = render(<CadEntry flagEnabled={false} onUpload={vi.fn()} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders the entry when the flag is on', () => {
    render(<CadEntry flagEnabled onUpload={vi.fn()} />)
    expect(screen.getByTestId('cad-entry')).toBeTruthy()
  })

  it('defaults off when no flag is passed and no env override is set', () => {
    // No VITE_CAD_UPLOAD=1 in the test env, so the component must read the
    // real off state rather than assuming enabled.
    const { container } = render(<CadEntry onUpload={vi.fn()} />)
    expect(container.firstChild).toBeNull()
  })
})

describe('truthfulness: upload affordance', () => {
  it('offers upload today because POST /api/drawings/upload is a real, executable route', () => {
    render(<CadEntry flagEnabled onUpload={vi.fn()} />)
    expect(screen.getByRole('button', { name: /upload dwg or dxf/i })).toBeTruthy()
  })

  it('never renders the upload affordance if its backing route is unavailable', () => {
    render(<CadEntry flagEnabled uploadAvailable={false} onUpload={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /upload dwg or dxf/i })).toBeNull()
  })

  it('calls onUpload with the chosen file', () => {
    const onUpload = vi.fn()
    render(<CadEntry flagEnabled onUpload={onUpload} />)
    const file = new File(['x'], 'plan.dxf', { type: 'application/dxf' })
    const input = screen.getByLabelText(/drawing file/i)
    fireEvent.change(input, { target: { files: [file] } })
    expect(onUpload).toHaveBeenCalledWith(file)
  })
})

describe('truthfulness: create-from-blank affordance', () => {
  it('is absent by default, because no public backing route exists yet', () => {
    // server/da/blank_dwg.py backs only POST /broker/blank-dwg/feasibility,
    // an internal broker-auth-gated, self-documented "dormant" endpoint — the
    // browser cannot reach it, so the button must not exist by default.
    render(<CadEntry flagEnabled onUpload={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /create from blank template/i })).toBeNull()
  })

  it('renders only once a caller asserts its backing route is executable', () => {
    const onCreateBlank = vi.fn()
    render(<CadEntry flagEnabled blankCreateAvailable onCreateBlank={onCreateBlank} onUpload={vi.fn()} />)
    const button = screen.getByRole('button', { name: /create from blank template/i })
    fireEvent.click(button)
    expect(onCreateBlank).toHaveBeenCalledTimes(1)
  })
})

describe('no affordance without a backing route', () => {
  it('shows an honest empty state instead of a dead button when nothing is available', () => {
    render(<CadEntry flagEnabled uploadAvailable={false} blankCreateAvailable={false} onUpload={vi.fn()} />)
    expect(screen.getByRole('status').textContent).toContain('No CAD entry method')
    expect(screen.queryByRole('button')).toBeNull()
  })
})
