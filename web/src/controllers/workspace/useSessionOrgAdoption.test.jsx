// Regression: staging 2026-08-24 — a signed-in operator with an ACTIVE binding
// (live /api/session echoing org_id) still saw the "Create workspace org"
// affordance on /try, because nothing hydrated leaf.org_id from the
// authenticated session. The full loop under test: session echo -> adoption ->
// persisted org -> projects listed, with the auth-off (no echo) seam untouched.
import { describe, expect, it, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'

import useWorkspaceController from './useWorkspaceController.js'
import useSessionOrgAdoption from './useSessionOrgAdoption.js'
import { WORKSPACE_ORG_KEY } from './createWorkspaceController.js'

const ORG = '0a415370-1111-4222-8333-944445555666'

function memoryStorage() {
  const map = new Map()
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: (key) => map.delete(key),
  }
}

function makeServices() {
  return {
    listProjects: vi.fn(async () => [{ project_id: 'p-1', name: 'Rooftop A' }]),
    createOrg: vi.fn(async () => { throw new Error('bootstrap must not run') }),
    createProject: vi.fn(),
    openProject: vi.fn(),
  }
}

function mountSurface({ storage, services }) {
  // Mirrors the ToolCast wiring exactly: the workspace controller plus the
  // adoption hook fed by the /api/session echo.
  return renderHook(
    ({ sessionOrg }) => {
      const workspace = useWorkspaceController({ mock: false, services, storage })
      useSessionOrgAdoption(sessionOrg, workspace.adoptOrgId)
      return workspace
    },
    { initialProps: { sessionOrg: null } },
  )
}

describe('live session org adoption (leaf.org_id self-heal)', () => {
  it('persists the echoed org and lists the caller projects with no stored org', async () => {
    const storage = memoryStorage()
    const services = makeServices()
    const view = mountSurface({ storage, services })

    // Fresh browser: token present but no leaf.org_id -> no projects call yet.
    expect(view.result.current.orgId).toBeNull()
    expect(services.listProjects).not.toHaveBeenCalled()

    // The live /api/session echo lands (deps.tenant_echo org_id).
    view.rerender({ sessionOrg: ORG })

    await waitFor(() => {
      expect(view.result.current.projects).toEqual([{ project_id: 'p-1', name: 'Rooftop A' }])
    })
    expect(storage.getItem(WORKSPACE_ORG_KEY)).toBe(ORG)
    expect(view.result.current.orgId).toBe(ORG)
    expect(services.listProjects).toHaveBeenCalledWith(ORG)
    // The bootstrap affordance path (POST /api/orgs -> 409 on name mismatch)
    // must never be entered for an already-bound account.
    expect(services.createOrg).not.toHaveBeenCalled()
  })

  it('re-echoing the same org is a no-op (no state reset, no duplicate load)', async () => {
    const storage = memoryStorage()
    const services = makeServices()
    const view = mountSurface({ storage, services })

    view.rerender({ sessionOrg: ORG })
    await waitFor(() => expect(view.result.current.orgId).toBe(ORG))
    await waitFor(() => expect(services.listProjects).toHaveBeenCalledTimes(1))

    view.rerender({ sessionOrg: ORG })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(services.listProjects).toHaveBeenCalledTimes(1)
    expect(view.result.current.projects).toEqual([{ project_id: 'p-1', name: 'Rooftop A' }])
  })

  it('auth-off seam: a session with no org echo writes nothing and adopts nothing', async () => {
    const storage = memoryStorage()
    const setSpy = vi.spyOn(storage, 'setItem')
    const services = makeServices()
    const view = mountSurface({ storage, services })

    // Auth-off / mock sessions carry org: null (tenant_echo no-ops).
    view.rerender({ sessionOrg: null })
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(view.result.current.orgId).toBeNull()
    expect(storage.getItem(WORKSPACE_ORG_KEY)).toBeNull()
    expect(setSpy).not.toHaveBeenCalled()
    expect(services.listProjects).not.toHaveBeenCalled()
  })
})
