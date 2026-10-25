import React from 'react'
import { readFileSync } from 'node:fs'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import CockpitTopBand from './site/CockpitTopBand.jsx'
import { REASONS, PROFILE_REASONS, profileRibbonTabs } from './lib/ribbonClusters.js'

afterEach(cleanup)

// Exercise the App-owned adapters with the real ribbon model and rendered band.
// This follows the production-source seam in appProjectSolarDrawing.test.jsx.
const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
const constants = ['WORKSPACE_QUICK_BEFORE', 'WORKSPACE_QUICK_AFTER'].map(name =>
  source.match(new RegExp(`const ${name} = Object\\.freeze\\(\\[[\\s\\S]*?\\]\\)`))[0]).join('\n')
const branchStart = source.indexOf('    if (!drafting) {')
const branch = source.slice(branchStart, source.indexOf('    const view = viewCluster(', branchStart))
const workspace = new Function('REASONS', 'PROFILE_REASONS', 'profileTabs', `
  ${constants}
  const drafting = false, activeRibbonTab = 'project', drawerViewport = false, navExpanded = false;
  ${branch}
`)
const filesStart = source.indexOf('      files: { onUpload:')
const filesCode = source.slice(filesStart, source.indexOf('\n', filesStart)).trim().slice('files: '.length).replace(/,$/, '')
const files = new Function('mock', 'openProjectId', 'signedIn', 'standalonePolicyReady', 'setProjectPane', `return (${filesCode})`)
function mountWorkspace(ready) {
  const open = vi.fn()
  const tabs = profileRibbonTabs('project', { files: files(false, null, true, ready, open) })
  const ribbon = workspace(REASONS, PROFILE_REASONS, tabs)
  render(<CockpitTopBand tabs={tabs} before={ribbon.quickBefore} after={ribbon.quickAfter} />)
  return open
}

it('enables Open DXF when uploads are ready and opens the upload pane', () => {
  const open = mountWorkspace(true)
  const button = screen.getByRole('button', { name: 'Open DXF', exact: true })
  expect(button.disabled).toBe(false)
  fireEvent.click(button)
  expect(open).toHaveBeenCalledWith('material')
})

it('keeps unavailable uploads disabled with the ribbon reason', () => {
  const open = mountWorkspace(false)
  const button = screen.getByRole('button', { name: `Open DXF (unavailable: ${PROFILE_REASONS.uploadDrawing})` })
  expect(button.disabled).toBe(true)
  fireEvent.click(button)
  expect(open).not.toHaveBeenCalled()
})

it('explains Print and saved versions in plain words', () => {
  mountWorkspace(true)
  const print = screen.getByRole('button', { name: "Print (unavailable: Printing isn't available yet)" })
  expect(print.title).not.toContain('browser engine')
  expect(REASONS.noVersions).toBe('No saved versions yet')
  expect(screen.getByRole('button', { name: 'Undo version (unavailable: No saved versions yet)' }).disabled).toBe(true)
})
