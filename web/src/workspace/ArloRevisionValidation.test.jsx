import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ReferenceMaterialIntake from './ReferenceMaterialIntake.jsx'
import { readProjectArloInput, registerProjectArloInput, restoreProjectArloInput, reviseProjectArloInput } from '../projects/api.js'

vi.mock('../api.js', () => ({ config: { apiBase: 'https://revision.test' }, noteUnauthorized: vi.fn() }))
vi.mock('../controllers/workspace/createWorkspaceController.js', () => ({ WORKSPACE_ORG_KEY: 'leaf.org_id' }))
vi.mock('../auth.js', () => ({ subscribeBeforeLogout: () => () => {} }))
vi.mock('../auth/activeScope.js', () => ({
  readActiveScope: () => 'synthetic-account', subscribeActiveScope: () => () => {},
}))
vi.mock('../lib/serviceAuthSession.js', () => ({
  serviceState: () => ({ mode: false }), serviceHeaders: () => ({}), serviceRequestAuthority: () => null,
}))

const project = '11111111-2222-4333-8444-555555555555'
const orgId = '99999999-8888-4777-8666-555555555555'
const material = {
  material_id: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee', name: 'feeder.json',
  media_type: 'application/json', byte_size: 14, sha256: 'a'.repeat(64), created_at: '2026-10-01T12:00:00Z',
}
const input = {
  organization_id: orgId, project_id: project, material_id: material.material_id,
  original_sha256: material.sha256, input_version_id: '44444444-5555-4666-8777-888888888888',
  input_sha256: 'b'.repeat(64), parent_id: null, original_id: '44444444-5555-4666-8777-888888888888',
  request: { contract: 'arlo_design_request_v1', requirements: { diameter_m: 1, clearance_m: 2, max_support_span_m: 4 },
    budget: { beam_width: 2, timeout_seconds: 60 } },
}
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const envelope = message => ({ ok: false, error: { error_code: 'BAD_PARAMS', message, retryable: false } })
const fallback = 'Some of that input was not accepted.'
const diameterMessage = 'requirements.diameter_m must be greater than zero.'
const revise = patch => reviseProjectArloInput(project, input.input_version_id, patch, { orgId })

beforeEach(() => {
  vi.stubGlobal('localStorage', { getItem: () => null })
  vi.stubGlobal('fetch', vi.fn())
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('ARLO revision rejection through the real project transport', () => {
  it.each([
    ['requirements', 'diameter_m', -1, diameterMessage],
    ['requirements', 'clearance_m', -1, 'requirements.clearance_m must be nonnegative.'],
    ['budget', 'beam_width', 1.5, 'budget.beam_width must be a whole number.'],
  ])('retains the %s.%s draft and clears saving after a field rejection', async (group, name, value, message) => {
    let rejectSave
    const pending = new Promise(resolve => { rejectSave = resolve })
    fetch.mockResolvedValueOnce(json({ materials: [material] }))
      .mockResolvedValueOnce(json(input, 201)).mockResolvedValueOnce(json(input))
      .mockReturnValueOnce(pending)
    const onRunArlo = vi.fn()
    render(<ReferenceMaterialIntake projectId={project} orgId={orgId} onRunArlo={onRunArlo} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit constraints' }))
    const field = await screen.findByLabelText(`${group}.${name}`)
    fireEvent.change(field, { target: { value: String(value) } })
    const save = screen.getByRole('button', { name: 'Save revision' })
    fireEvent.click(save)
    expect(screen.getByText('Saving constraints…')).toBeTruthy()
    expect(save.disabled).toBe(true)
    await act(async () => rejectSave(json(envelope(message), 422)))
    expect(screen.getByRole('alert').textContent).toBe(message)
    expect(field.value).toBe(String(value))
    expect(field.getAttribute('aria-invalid')).toBe('true')
    expect(save.disabled).toBe(false)
    expect(screen.queryByText('Saving constraints…')).toBeNull()
    expect(screen.getByText(/Active input:/).textContent).toContain(input.input_version_id)
    expect(screen.getByText(/Active input:/).textContent).toContain(input.input_sha256)
    expect(onRunArlo).not.toHaveBeenCalled()
    expect(fetch).toHaveBeenCalledTimes(4)
    const [url, options] = fetch.mock.calls[3]
    expect(url).toBe(`https://revision.test/api/projects/${project}/arlo-inputs/${input.input_version_id}/revisions`)
    expect(options.method).toBe('POST')
    expect(JSON.parse(options.body)).toEqual({ patch: { [group]: { [name]: value } } })
  })

  it.each([
    ['noneditable field', { requirements: { new_numeric: 1 } }, 'requirements.new_numeric is not an editable numeric constraint.'],
    ['finite number', { requirements: { diameter_m: null } }, 'requirements.diameter_m must be a finite number.'],
    ['positive budget', { budget: { timeout_seconds: 0 } }, 'budget.timeout_seconds must be greater than zero.'],
  ])('preserves the validator rule for %s', async (_label, patch, message) => {
    fetch.mockResolvedValueOnce(json(envelope(message), 422))
    await expect(revise(patch)).rejects.toMatchObject({ status: 422, message })
  })

  it.each([
    null, {}, { error: null }, { error: [] }, { error: 'BAD_PARAMS' },
    { error: { error_code: 'INTERNAL', message: diameterMessage } },
    { error: { code: 'BAD_PARAMS', message: diameterMessage } },
    envelope(null), envelope({ text: diameterMessage }), envelope([diameterMessage]),
    envelope('requirements.clearance_m must be nonnegative.'),
    envelope('requirements.diameter_m must be nonnegative.'),
    envelope('requirements.diameter_m must be a whole number.'),
    envelope('requirements.diameter_m must be less than 100.'),
    envelope(`${diameterMessage} /api/private`), envelope(`<script>${diameterMessage}</script>`),
    envelope(`${diameterMessage}\nsecret`), envelope('x'.repeat(200)),
    { detail: { message: diameterMessage }, ...envelope('unrelated failure') },
  ])('uses the generic fallback for malformed, unsafe or unrelated errors: %j', async body => {
    fetch.mockResolvedValueOnce(json(body, 422))
    await expect(revise({ requirements: { diameter_m: -1 } })).rejects.toMatchObject({ status: 422, message: fallback })
  })

  it.each([
    [{ budget: { timeout_seconds: 1.5 } }, 'budget.timeout_seconds must be a whole number.'],
    [{ budget: { beam_width: -1 } }, 'budget.beam_width must be nonnegative.'],
    [{ other: { diameter_m: -1 } }, 'other.diameter_m must be greater than zero.'],
  ])('rejects rules outside the field validator', async (patch, message) => {
    fetch.mockResolvedValueOnce(json(envelope(message), 422))
    await expect(revise(patch)).rejects.toThrow(fallback)
  })

  it('keeps safe plain detail compatibility and precedence', async () => {
    const detail = 'Please reopen constraints before saving.'
    fetch.mockResolvedValueOnce(json({ detail: diameterMessage }, 422))
      .mockResolvedValueOnce(json({ ...envelope(diameterMessage), detail }, 422))
    await expect(revise({ requirements: { diameter_m: -1 } })).rejects.toThrow(diameterMessage)
    await expect(revise({ requirements: { diameter_m: -1 } })).rejects.toThrow(detail)
  })

  it.each(['/api/private -> 422', '<html>failure</html>', 'x'.repeat(161)])('rejects unsafe plain detail %s', async detail => {
    fetch.mockResolvedValueOnce(json({ detail }, 422))
    await expect(revise({ requirements: { diameter_m: -1 } })).rejects.toThrow(fallback)
  })

  it('handles non-JSON failures without exposing raw text', async () => {
    fetch.mockResolvedValueOnce(new Response('<html>failure</html>', { status: 422 }))
    await expect(revise({ requirements: { diameter_m: -1 } })).rejects.toThrow(fallback)
  })

  it.each([400, 403, 409, 500])('does not promote structured messages for status %i', async status => {
    fetch.mockResolvedValueOnce(json(envelope(diameterMessage), status))
    const error = await revise({ requirements: { diameter_m: -1 } }).catch(error => error)
    expect(error.status).toBe(status)
    expect(error.message).not.toBe(diameterMessage)
  })

  it.each(['read', 'register', 'restore'])('does not promote structured errors for %s', async operation => {
    fetch.mockResolvedValueOnce(json(envelope(diameterMessage), 422))
    const calls = {
      read: () => readProjectArloInput(project, input.input_version_id),
      register: () => registerProjectArloInput(project, material.material_id, { orgId, key: 'synthetic-key' }),
      restore: () => restoreProjectArloInput(project, input.input_version_id),
    }
    await expect(calls[operation]()).rejects.toThrow(fallback)
  })

  it('does not leave saving text after submitting unchanged constraints', async () => {
    fetch.mockResolvedValueOnce(json({ materials: [material] }))
      .mockResolvedValueOnce(json(input, 201)).mockResolvedValueOnce(json(input))
    render(<ReferenceMaterialIntake projectId={project} orgId={orgId} onRunArlo={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit constraints' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Save revision' }))
    expect(screen.queryByText('Saving constraints…')).toBeNull()
    expect(screen.queryByRole('form', { name: 'ARLO constraints' })).toBeNull()
    expect(fetch).toHaveBeenCalledTimes(3)
  })
})
