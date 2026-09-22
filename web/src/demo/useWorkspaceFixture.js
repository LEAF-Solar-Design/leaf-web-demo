import { useRef } from 'react'
import { createFixtureServices, SAMPLE_ORG, SAMPLE_PROJECTS } from './workspaceFixture.js'

const DISABLED = Object.freeze({ enabled: false, services: null, org: null, projects: Object.freeze([]) })

export function useWorkspaceFixture({ enabled = false } = {}) {
  const snapshot = useRef(null)
  if (enabled && snapshot.current === null) {
    snapshot.current = Object.freeze({
      enabled: true,
      services: createFixtureServices(),
      org: SAMPLE_ORG,
      projects: SAMPLE_PROJECTS,
    })
  }
  return enabled ? snapshot.current : DISABLED
}
