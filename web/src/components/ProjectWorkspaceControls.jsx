import { useCallback, useLayoutEffect, useRef, useState } from 'react'

export const PROJECT_TOOLS_ID = 'project-workspace-tools'
export const PROJECT_ACTIVITY_ID = 'project-workspace-activity'

export function useProjectWorkspaceLayout({ mock, projectId, surface }) {
  const active = !mock && !!projectId && surface === 'browser'
  const appRef = useRef(null)
  const [selection, setSelection] = useState({ projectId, surface, active, tools: false, activity: false })
  const changed = selection.projectId !== projectId || selection.surface !== surface || selection.active !== active
  const current = changed ? { projectId, surface, active, tools: false, activity: false } : selection
  if (changed) setSelection(current)
  const toolsOpen = active && current.tools
  const activityOpen = active && current.activity

  // Keep the original rail as a direct shell child, including in CAD.
  useLayoutEffect(() => {
    const app = appRef.current
    if (!app) return
    const tools = app.querySelector(':scope > aside.nav')
    const activity = app.querySelector(':scope > .rail-stack')
    if (tools) tools.id = PROJECT_TOOLS_ID
    if (activity) activity.id = PROJECT_ACTIVITY_ID
    for (const [panel, open, id] of [[tools, toolsOpen, PROJECT_TOOLS_ID], [activity, activityOpen, PROJECT_ACTIVITY_ID]]) {
      if (active && !open && panel?.contains(document.activeElement)) {
        app.querySelector(`[aria-controls="${id}"]`)?.focus()
      }
    }
  }, [active, toolsOpen, activityOpen, projectId])

  const revealTools = useCallback(() => {
    if (active) setSelection(previous => ({ ...previous, tools: true }))
  }, [active])
  const toggleTools = () => setSelection(previous => ({ ...previous, tools: !previous.tools }))
  const toggleActivity = () => setSelection(previous => ({ ...previous, activity: !previous.activity }))
  return { active, appRef, toolsOpen, activityOpen, revealTools, toggleTools, toggleActivity }
}

export default function ProjectWorkspaceControls({ active, toolsOpen, activityOpen, toggleTools, toggleActivity }) {
  if (!active) return null
  return (
    <div className="project-workspace-controls" role="group" aria-label="Project workspace panels">
      <button type="button" className="chip-act" aria-expanded={toolsOpen} aria-controls={PROJECT_TOOLS_ID} onClick={toggleTools}>Tools</button>
      <button type="button" className="chip-act" aria-expanded={activityOpen} aria-controls={PROJECT_ACTIVITY_ID} onClick={toggleActivity}>Activity</button>
    </div>
  )
}
