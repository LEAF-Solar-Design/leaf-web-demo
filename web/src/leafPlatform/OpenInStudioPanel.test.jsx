// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import golden from './__fixtures__/leaf.send-copy.v1.golden.json'
import OpenInStudioPanel from './OpenInStudioPanel.jsx'

const ORG = '0a0a0a0a-0000-4000-8000-000000000001'
const DRAWING = '0c0c0c0c-0000-4000-8000-000000000003'
const payload = JSON.parse(golden.cases.find((entry) => entry.name === 'sent').wire).payload
const LINE = 'Parking.dwg | sent copy 8e0fcf8e | BUILDING 1, SITE 1 | 2 objects in model space'

function services(overrides = {}) {
  let stored = null
  return {
    getSessionIdentity: vi.fn(async () => ({ org: ORG, workspace: { id: ORG, name: 'My workspace' } })),
    createOrg: vi.fn(), setStoredOrgId: (id) => { stored = id }, getStoredOrgId: () => stored,
    uploadDrawing: vi.fn(async () => ({ drawing_id: DRAWING, tenant_kind: 'account' })),
    getDrawingUploadStatus: vi.fn(async () => ({ status: 'ready', content_sha256: payload.sha256, extracted_version: 1, fingerprint: structuredClone(payload.fingerprint) })),
    createProject: vi.fn(async () => ({ project_id: '0b0b0b0b-0000-4000-8000-000000000002' })),
    importUploadedDrawingVersion: vi.fn(async () => ({ drawingVersion: {}, replayed: false })),
    ...overrides,
  }
}
const bridge = (result) => ({ sendCopy: vi.fn(async () => result ?? {
  status: 'sent', reason: null, fileName: payload.fileName, bytes: new Uint8Array(70), sha256: payload.sha256,
  fingerprint: structuredClone(payload.fingerprint) }) })

afterEach(cleanup)

describe('Open in Studio panel', () => {
  it('shows the same fingerprint line for AutoCAD and Studio, then opens the drawing', async () => {
    const navigate = vi.fn()
    render(<OpenInStudioPanel bridge={bridge()} services={services()} authority={() => 'token'} remember={vi.fn()} navigate={navigate} openDelayMs={0} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open in Studio' }))
    await waitFor(() => expect(screen.getByText(/Sent and checked/)).toBeTruthy())
    const lines = screen.getAllByText(LINE)
    expect(lines).toHaveLength(2)
    expect(screen.getByText('Sent and checked. Opening Studio in My workspace.')).toBeTruthy()
    await waitFor(() => expect(navigate).toHaveBeenCalledWith(`/app?drawing=${DRAWING}`))
  })

  it('says uploads are off and offers no retry', async () => {
    const error = Object.assign(new Error('off'), { status: 503 })
    render(<OpenInStudioPanel bridge={bridge()} services={services({ uploadDrawing: vi.fn(async () => { throw error }) })}
      authority={() => 'token'} remember={vi.fn()} navigate={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open in Studio' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('Studio is not accepting drawings right now. Nothing was saved. Your DWG was not changed.'))
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })

  it('tells an old plugin to update', async () => {
    render(<OpenInStudioPanel bridge={bridge({ status: 'failed', reason: 'plugin_outdated' })} services={services()}
      authority={() => 'token'} remember={vi.fn()} navigate={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open in Studio' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('Update the Leaf Automation plugin to send this drawing to Studio.'))
  })

  it('retries a failed import without asking AutoCAD again', async () => {
    const host = bridge()
    const api = services()
    api.importUploadedDrawingVersion.mockRejectedValueOnce(Object.assign(new Error('x'), { status: 500 }))
    render(<OpenInStudioPanel bridge={host} services={api} authority={() => 'token'} remember={vi.fn()} navigate={vi.fn()} openDelayMs={60_000} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open in Studio' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/not in a project/))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Try again' })) })
    await waitFor(() => expect(screen.getByText(/Sent and checked/)).toBeTruthy())
    expect(host.sendCopy).toHaveBeenCalledTimes(1)
    expect(api.uploadDrawing).toHaveBeenCalledTimes(1)
  })
})
