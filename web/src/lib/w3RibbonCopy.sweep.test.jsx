// @vitest-environment jsdom
import React from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { RibbonTool } from '../site/DraftingRibbon.jsx'
import { profileRibbonTabs, solarRailReason } from './ribbonClusters.js'

afterEach(cleanup)

const projectTools = (context) => profileRibbonTabs('project', context)
  .flatMap((tab) => tab.clusters.flatMap((cluster) => cluster.tools))

it('S1-132 does not claim a project loading failure requires signing in', () => {
  // App supplies these same null handlers when a signed-in project fetch fails.
  const tools = projectTools({ project: { onOpen: null, onChange: null } })
    .filter((tool) => ['project:open', 'project:change'].includes(tool.id))
  render(<>{tools.map((tool) => <RibbonTool key={tool.id} tool={tool} />)}</>)
  for (const name of ['Open project', 'Change project']) {
    const button = screen.getByRole('button', { name: new RegExp(name) })
    expect(button.disabled).toBe(true)
    expect(button.title).toMatch(/unavailable right now/)
    expect(button.getAttribute('aria-label')).not.toMatch(/sign.?in/i)
  }
})

it('S1-132 does not claim a busy conversation requires signing in', () => {
  // A run in flight or routing also removes the new-conversation handler.
  const tool = projectTools({ conversation: { onNew: null } })
    .find((row) => row.id === 'conversation:new')
  render(<RibbonTool tool={tool} />)
  const button = screen.getByRole('button', { name: /New conversation/ })
  expect(button.disabled).toBe(true)
  expect(button.title).toBe('Starting a conversation is unavailable right now')
  expect(button.getAttribute('aria-label')).not.toMatch(/sign.?in/i)
})

it('S1-135 keeps unknown server refusal details out of ribbon reasons', () => {
  for (const code of ['SIP_R4_WORKER_UNAVAILABLE', 'constructor', 'GET /api/private 503', 'Failed to fetch']) {
    const reason = solarRailReason({ input_ready: false, refusal_reasons: ['graph_seed_required', code] })
    expect(reason).toBe('Start the solar design with Solar settings first; The server refused this solar tool')
    expect(reason).not.toContain(code)
    const { unmount } = render(<RibbonTool tool={{ id: 'solar:test', label: 'Size strings', disabled: true, reason }} />)
    const button = screen.getByRole('button', { name: /Size strings/ })
    expect(button.disabled).toBe(true)
    expect(button.title).toBe(reason)
    expect(button.getAttribute('aria-label')).not.toContain(code)
    unmount()
  }
})

it('keeps available project actions working and disabled activity reasons present', () => {
  const onOpen = vi.fn()
  const tools = projectTools({ project: { onOpen } })
  render(<RibbonTool tool={tools.find((tool) => tool.id === 'project:open')} />)
  const button = screen.getByRole('button', { name: 'Open project' })
  expect(button.disabled).toBe(false)
  fireEvent.click(button)
  expect(onOpen).toHaveBeenCalledOnce()
  for (const id of ['activity:jobs', 'activity:receipts']) {
    expect(tools.find((tool) => tool.id === id)).toMatchObject({ disabled: true, reason: expect.any(String) })
    expect(tools.find((tool) => tool.id === id).reason.length).toBeGreaterThan(0)
  }
})
