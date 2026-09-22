// @vitest-environment jsdom
import { afterEach, expect, it } from 'vitest'
import { cleanup, renderHook } from '@testing-library/react'
import { useWorkspaceFixture } from './useWorkspaceFixture.js'
import { SAMPLE_PROJECTS } from './workspaceFixture.js'

afterEach(cleanup)

it('D1 row8: the default disabled snapshot is inert and stable', () => {
  const { result, rerender } = renderHook(() => useWorkspaceFixture())
  const first = result.current
  expect(first).toEqual({ enabled: false, services: null, org: null, projects: [] })
  rerender()
  expect(result.current).toBe(first)
})

it('D1 row9: enabling constructs one service instance and a stable snapshot', async () => {
  const { result, rerender } = renderHook(({ enabled }) => useWorkspaceFixture({ enabled }), { initialProps: { enabled: true } })
  const first = result.current
  expect(first.enabled).toBe(true)
  expect(first.projects).toEqual(SAMPLE_PROJECTS)
  expect(first.projects).toHaveLength(2)
  const org = await first.services.createOrg('Hook instance')
  rerender({ enabled: true })
  expect(result.current).toBe(first)
  expect(result.current.services).toBe(first.services)
  expect(await result.current.services.listProjects(org.org_id)).toEqual([])
  rerender({ enabled: false })
  expect(result.current.services).toBeNull()
  rerender({ enabled: true })
  expect(result.current).toBe(first)
  const other = renderHook(() => useWorkspaceFixture({ enabled: true }))
  expect(other.result.current.services).not.toBe(first.services)
  await expect(other.result.current.services.listProjects(org.org_id)).rejects.toMatchObject({ name: 'SampleNotFoundError' })
})
