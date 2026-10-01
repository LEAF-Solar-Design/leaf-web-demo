// Tab metadata shared by the renderer, cluster builders and plain Node walkers.
export const RIBBON_TABS = Object.freeze([
  { id: 'draw', label: 'Draw' },
  // The reason never says "browser": the product tabs include one named
  // Browser and accessible-name matching is a substring test.
  { id: 'model', label: 'Model', reason: '3D modelling is not in this engine yet' },
  { id: 'insert', label: 'Insert' },
  { id: 'annotate', label: 'Annotate' },
  { id: 'view', label: 'View' },
  { id: 'manage', label: 'Manage' },
])

export const PROFILE_RIBBON_TABS = Object.freeze({
  drafting: RIBBON_TABS,
  solar: Object.freeze([
    RIBBON_TABS[0],
    { id: 'solar', label: 'Solar' },
    ...RIBBON_TABS.slice(1),
  ]),
  project: Object.freeze([
    { id: 'project', label: 'Project' },
    { id: 'tools', label: 'Tools' },
    { id: 'activity', label: 'Activity' },
  ]),
  ship: Object.freeze([{ id: 'ship', label: 'Ship' }]),
})

export function profileRibbonTabData(profile) {
  return profile === 'solar' || profile === 'project' || profile === 'ship'
    ? PROFILE_RIBBON_TABS[profile]
    : RIBBON_TABS
}
