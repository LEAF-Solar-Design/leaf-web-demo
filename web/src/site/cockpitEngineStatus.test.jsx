// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import CockpitEngineStatus from './CockpitEngineStatus.jsx'
import { useEngineSessionOptional } from '../cadedit/EngineSessionProvider.jsx'

vi.mock('../cadedit/EngineSessionProvider.jsx', () => ({ useEngineSessionOptional: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks() })

describe('CockpitEngineStatus', () => {
  it('shows and politely announces the latest session sentence', () => {
    useEngineSessionOptional.mockReturnValue({ session: { status: 'createLine applied: entity 7 drawn', errorKind: null } })
    const { rerender } = render(<CockpitEngineStatus />)
    const line = screen.getByTestId('cockpit-engine-status')
    expect(line.getAttribute('role')).toBe('status')
    expect(line.getAttribute('aria-live')).toBe('polite')
    expect(line.textContent).toBe('createLine applied: entity 7 drawn')
    useEngineSessionOptional.mockReturnValue({ session: { status: 'Trim refused: select a line.', errorKind: 'refused' } })
    rerender(<CockpitEngineStatus />)
    expect(line.textContent).toBe('Trim refused: select a line.')
    expect(line.getAttribute('data-error')).toBe('true')
  })

  it('renders nothing for an empty status or absent engine', () => {
    useEngineSessionOptional.mockReturnValue({ session: { status: '' } })
    const { rerender, container } = render(<CockpitEngineStatus />)
    expect(container.innerHTML).toBe('')
    useEngineSessionOptional.mockReturnValue(null)
    rerender(<CockpitEngineStatus />)
    expect(container.innerHTML).toBe('')
  })

  it('hands announcements to the workbench while import is open', () => {
    useEngineSessionOptional.mockReturnValue({ session: { status: 'No drawing command to repeat yet.' } })
    const { rerender } = render(<CockpitEngineStatus />)
    expect(screen.getByRole('status').textContent).toBe('No drawing command to repeat yet.')
    rerender(<CockpitEngineStatus importOpen />)
    expect(screen.queryByRole('status')).toBeNull()
    rerender(<CockpitEngineStatus />)
    expect(screen.getByRole('status').textContent).toBe('No drawing command to repeat yet.')
  })

  it('seats the consumer in the command dock slot', () => {
    useEngineSessionOptional.mockReturnValue({ session: { status: 'Copy: a line is on the clipboard' } })
    render(<><div id="status-slot" /><CockpitEngineStatus slotId="status-slot" /></>)
    expect(screen.getByRole('status').parentElement.id).toBe('status-slot')
  })
})
