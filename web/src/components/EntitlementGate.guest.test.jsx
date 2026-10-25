// @vitest-environment jsdom
import React from 'react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import EntitlementGate from './EntitlementGate.jsx'
import { START_BOARD_COPY } from '../site/startBoardCopy.js'

afterEach(cleanup)

describe('EntitlementGate guest', () => {
  it('a guest sees sign-in states instead of unavailable plan details', () => {
    render(<EntitlementGate tier={null} entitlements={null} loading={false} mock={false} guest studioPresentation />)
    expect(screen.getByText(START_BOARD_COPY.guestHead)).toBeTruthy()
    expect(screen.getByText(START_BOARD_COPY.guestFooter)).toBeTruthy()
    expect(screen.getAllByText(START_BOARD_COPY.guestState).length).toBeGreaterThan(0)
    expect(screen.queryByText(START_BOARD_COPY.unavailableHead)).toBeNull()
    expect(screen.queryByText(START_BOARD_COPY.unavailableFooter)).toBeNull()
    expect(screen.queryByText(START_BOARD_COPY.unavailableState)).toBeNull()
  })

  it('without the guest flag the unavailable copy is unchanged', () => {
    render(<EntitlementGate tier={null} entitlements={null} loading={false} mock={false} studioPresentation />)
    expect(screen.getByText(START_BOARD_COPY.unavailableHead)).toBeTruthy()
    expect(screen.getByText(START_BOARD_COPY.unavailableFooter)).toBeTruthy()
    expect(screen.queryByText(START_BOARD_COPY.guestHead)).toBeNull()
  })

  it('the demo copy wins over the guest flag', () => {
    render(<EntitlementGate tier={null} entitlements={null} loading={false} mock guest studioPresentation />)
    expect(screen.getByText(START_BOARD_COPY.demoHead)).toBeTruthy()
    expect(screen.queryByText(START_BOARD_COPY.guestHead)).toBeNull()
  })

  it('the frame and App pass the guest flag', () => {
    const frame = readFileSync(resolve(process.cwd(), 'src/site/SurfaceFrame.jsx'), 'utf8')
    expect(frame).toContain('guest={ent.guest === true}')
    const app = readFileSync(resolve(process.cwd(), 'src/App.jsx'), 'utf8').replace(/\r\n/g, '\n')
    expect(app).toContain('        mock,\n        guest: knownGuest,\n')
  })
})
